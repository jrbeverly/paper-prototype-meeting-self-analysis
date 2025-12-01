from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from unittest.mock import patch
from urllib.parse import unquote

from meeting_intelligence.config import ConfluenceSettings
from meeting_intelligence.confluence import Confluence
from meeting_intelligence.errors import AnnotationConflictError
from meeting_intelligence.graph import GraphSharePoint
from meeting_intelligence.models import ObjectRef
from meeting_intelligence.storage import S3AnnotationStore, S3Repository


class InMemoryConfluence(Confluence):
    def __init__(self):
        self.settings = ConfluenceSettings(
            base_url="https://example.atlassian.net/wiki",
            space_key="MI",
            input_parent_id="input-parent",
            result_parent_id="result-parent",
            final_parent_id="final-parent",
            md2conf_command="md2conf",
        )
        self.pages: dict[tuple[str, str], dict[str, Any]] = {}
        self.properties: dict[tuple[str, str], dict[str, Any]] = {}
        self.labels: dict[str, set[str]] = {}
        self.publications: list[tuple[str, str | None]] = []
        self.fail_property_once = False

    def find_page(self, title: str, parent_id: str) -> Mapping[str, Any] | None:
        return self.pages.get((parent_id, title))

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        for page in self.pages.values():
            if page["id"] == page_id:
                return {
                    **page,
                    "version": {"number": page["versionNumber"]},
                }
        raise KeyError(page_id)

    def get_property(self, page_id: str, name: str) -> Mapping[str, Any] | None:
        return self.properties.get((page_id, name))

    def put_property(self, page_id: str, name: str, value: Mapping[str, Any]) -> None:
        if self.fail_property_once:
            self.fail_property_once = False
            raise RuntimeError("simulated property outage")
        current = self.properties.get((page_id, name))
        version = int((current or {}).get("version", {}).get("number", 0)) + 1
        self.properties[(page_id, name)] = {
            "key": name,
            "value": dict(value),
            "version": {"number": version},
        }

    def add_labels(self, page_id: str, labels: Iterable[str]) -> None:
        self.labels.setdefault(page_id, set()).update(labels)

    def _publish_markdown(
        self,
        *,
        title: str,
        parent_id: str,
        markdown: str,
        existing_page_id: str | None,
    ) -> None:
        self.publications.append((title, existing_page_id))
        key = (parent_id, title)
        current = self.pages.get(key)
        self.pages[key] = {
            "id": current["id"] if current else "page-100",
            "title": title,
            "parentId": parent_id,
            "markdown": markdown,
            "versionNumber": int(current["versionNumber"]) + 1 if current else 1,
        }


class Response:
    def __init__(
        self,
        status_code: int,
        payload: Mapping[str, Any] | None = None,
        *,
        content: bytes = b"{}",
    ):
        self.status_code = status_code
        self._payload = dict(payload or {})
        self.content = content

    def json(self) -> Mapping[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def graph_path(url: str) -> str:
    if "/root:/" not in url:
        return ""
    encoded = url.split("/root:/", 1)[1].split(":", 1)[0]
    return unquote(encoded)


class GraphSession:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.simple_uploads: list[tuple[str, bytes, Mapping[str, str]]] = []
        self.upload_session_requests: list[tuple[str, Mapping[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        path = graph_path(url)
        item = self.items.get(path)
        return Response(200, item) if item else Response(404)

    def put(
        self,
        url: str,
        *,
        data: Any,
        headers: Mapping[str, str],
        **kwargs: Any,
    ) -> Response:
        path = graph_path(url)
        payload = data.read()
        self.simple_uploads.append((path, payload, dict(headers)))
        item = {
            "id": f"item-{len(self.items) + 1}",
            "webUrl": f"https://sharepoint.example/{path}",
            "name": Path(path).name,
            "size": len(payload),
            "file": {"hashes": {"sha1Hash": hashlib.sha1(payload).hexdigest()}},
        }
        self.items[path] = item
        return Response(201, item)

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        **kwargs: Any,
    ) -> Response:
        if url.endswith(":/createUploadSession"):
            self.upload_session_requests.append((graph_path(url), dict(json)))
            return Response(200, {"uploadUrl": "https://upload.example/session"})
        raise AssertionError(f"unexpected POST {url}")


class Paginator:
    def __init__(self, pages: list[Mapping[str, Any]]):
        self.pages = pages
        self.calls: list[Mapping[str, Any]] = []

    def paginate(self, **kwargs: Any):
        self.calls.append(kwargs)
        return iter(self.pages)


class ListingS3Client:
    def __init__(self, pages: list[Mapping[str, Any]]):
        self.paginator = Paginator(pages)

    def get_paginator(self, operation: str) -> Paginator:
        if operation != "list_objects_v2":
            raise AssertionError(operation)
        return self.paginator


class ServiceError(Exception):
    def __init__(self, *, code: str, status: int):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class AnnotationClient:
    def __init__(self):
        self.values: dict[tuple[str, str, str, str | None], bytes] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.conflict = False

    def get_object_annotation(self, **request: Any) -> Mapping[str, Any]:
        key = (
            request["Bucket"],
            request["Key"],
            request["AnnotationName"],
            request.get("VersionId"),
        )
        if key not in self.values:
            raise ServiceError(code="NoSuchAnnotation", status=404)
        return {"AnnotationPayload": io.BytesIO(self.values[key])}

    def put_object_annotation(self, **request: Any) -> None:
        self.put_calls.append(request)
        if self.conflict:
            raise ServiceError(code="PreconditionFailed", status=412)
        key = (
            request["Bucket"],
            request["Key"],
            request["AnnotationName"],
            request.get("VersionId"),
        )
        self.values[key] = request["AnnotationPayload"]


class AdapterAcceptanceTests(unittest.TestCase):
    def test_confluence_publication_is_idempotent_and_updates_only_on_content_change(self):
        confluence = InMemoryConfluence()
        arguments = {
            "title": "Meeting — occurrence-1",
            "parent_id": "input-parent",
            "markdown": "# Input\n",
            "labels": {"automation-ai-request", "status-ready-for-ai"},
            "property_name": "meeting-intelligence",
            "property_value": {"v": 1, "occurrenceIds": ["occurrence-1"]},
        }

        first = confluence.ensure_page(**arguments)
        second = confluence.ensure_page(**arguments)

        self.assertEqual("page-100", first["id"])
        self.assertEqual(first, second)
        self.assertEqual(
            [("Meeting — occurrence-1", None)],
            confluence.publications,
        )
        self.assertEqual(1, len(confluence.pages))
        self.assertEqual(
            {"automation-ai-request", "status-ready-for-ai"},
            confluence.labels["page-100"],
        )
        stored = confluence.properties[
            ("page-100", "meeting-intelligence")
        ]["value"]
        self.assertEqual(first["contentFingerprint"], stored["contentFingerprint"])

        revised = confluence.ensure_page(
            **{
                **arguments,
                "markdown": "# Revised input\n",
            }
        )

        self.assertEqual("page-100", revised["id"])
        self.assertEqual(2, revised["version"])
        self.assertEqual(
            [
                ("Meeting — occurrence-1", None),
                ("Meeting — occurrence-1", "page-100"),
            ],
            confluence.publications,
        )
        self.assertEqual(1, len(confluence.pages))

    def test_confluence_retry_rediscovers_page_after_publish_before_property(self):
        confluence = InMemoryConfluence()
        confluence.fail_property_once = True
        arguments = {
            "title": "Meeting — occurrence-1",
            "parent_id": "input-parent",
            "markdown": "# Input\n",
            "labels": {"automation-ai-request"},
            "property_name": "meeting-intelligence",
            "property_value": {"v": 1, "occurrenceIds": ["occurrence-1"]},
        }

        with self.assertRaisesRegex(RuntimeError, "property outage"):
            confluence.ensure_page(**arguments)

        self.assertEqual(1, len(confluence.pages))
        page_id = next(iter(confluence.pages.values()))["id"]

        repaired = confluence.ensure_page(**arguments)

        self.assertEqual(page_id, repaired["id"])
        self.assertEqual(1, len(confluence.pages))
        self.assertEqual(
            [
                ("Meeting — occurrence-1", None),
                ("Meeting — occurrence-1", "page-100"),
            ],
            confluence.publications,
        )
        self.assertIsNotNone(
            confluence.get_property(page_id, "meeting-intelligence")
        )

    def test_graph_uses_simple_upload_at_limit_and_reuses_matching_item(self):
        session = GraphSession()
        with patch(
            "meeting_intelligence.graph.requests.post",
            return_value=Response(200, {"access_token": "token"}),
        ):
            graph = GraphSharePoint(
                {
                    "tenantId": "tenant",
                    "clientId": "client",
                    "clientSecret": "secret",
                },
                site_id="site",
                drive_id="drive",
                simple_upload_limit_bytes=5,
                upload_chunk_bytes=4,
                session=session,
            )
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory, "clip.mp4")
            clip.write_bytes(b"12345")

            first = graph.ensure_file("clip.mp4", clip)
            second = graph.ensure_file("clip.mp4", clip)

        self.assertEqual("Bearer token", session.headers["Authorization"])
        self.assertEqual(1, len(session.simple_uploads))
        self.assertEqual(("clip.mp4", b"12345"), session.simple_uploads[0][:2])
        self.assertEqual("video/mp4", session.simple_uploads[0][2]["Content-Type"])
        self.assertEqual(first, second)
        self.assertEqual("drive", first["driveId"])
        self.assertEqual("item-1", first["itemId"])
        self.assertEqual("https://sharepoint.example/clip.mp4", first["webUrl"])

    def test_graph_resumable_upload_sends_contiguous_content_ranges(self):
        session = GraphSession()
        chunks: list[tuple[bytes, Mapping[str, str]]] = []

        def upload_part(
            url: str,
            *,
            data: bytes,
            headers: Mapping[str, str],
            **kwargs: Any,
        ) -> Response:
            self.assertEqual("https://upload.example/session", url)
            chunks.append((data, dict(headers)))
            uploaded = sum(len(payload) for payload, _ in chunks)
            if uploaded < 10:
                return Response(202, {"nextExpectedRanges": [f"{uploaded}-"]})
            return Response(
                201,
                {
                    "id": "large-item",
                    "webUrl": "https://sharepoint.example/large.mp4",
                    "name": "large.mp4",
                    "size": 10,
                },
            )

        with patch(
            "meeting_intelligence.graph.requests.post",
            return_value=Response(200, {"access_token": "token"}),
        ):
            graph = GraphSharePoint(
                {
                    "tenantId": "tenant",
                    "clientId": "client",
                    "clientSecret": "secret",
                },
                site_id="site",
                drive_id="drive",
                simple_upload_limit_bytes=5,
                upload_chunk_bytes=4,
                session=session,
            )
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory, "large.mp4")
            clip.write_bytes(b"0123456789")
            with patch(
                "meeting_intelligence.graph.requests.put",
                side_effect=upload_part,
            ):
                reference = graph.ensure_file("large.mp4", clip)

        self.assertEqual(
            [
                (b"0123", "bytes 0-3/10", "4"),
                (b"4567", "bytes 4-7/10", "4"),
                (b"89", "bytes 8-9/10", "2"),
            ],
            [
                (
                    payload,
                    headers["Content-Range"],
                    headers["Content-Length"],
                )
                for payload, headers in chunks
            ],
        )
        self.assertEqual(
            [
                (
                    "large.mp4",
                    {"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                )
            ],
            session.upload_session_requests,
        )
        self.assertEqual("large-item", reference["itemId"])
        self.assertEqual("https://sharepoint.example/large.mp4", reference["webUrl"])

    def test_s3_listing_consumes_every_paginator_page(self):
        client = ListingS3Client(
            [
                {
                    "Contents": [
                        {
                            "Key": "meetings/a.mp4",
                            "ETag": '"a"',
                            "Size": 1,
                            "LastModified": datetime(
                                2026, 7, 23, tzinfo=timezone.utc
                            ),
                        }
                    ]
                },
                {},
                {
                    "Contents": [
                        {
                            "Key": "meetings/b.mp4",
                            "ETag": '"b"',
                            "Size": 2,
                        }
                    ]
                },
            ]
        )

        objects = list(S3Repository("bucket", client).list("meetings/"))

        self.assertEqual(["meetings/a.mp4", "meetings/b.mp4"], [item.key for item in objects])
        self.assertEqual([1, 2], [item.size for item in objects])
        self.assertEqual(
            [{"Bucket": "bucket", "Prefix": "meetings/"}],
            client.paginator.calls,
        )

    def test_annotation_writes_are_etag_and_version_conditioned_and_conflicts_are_explicit(self):
        client = AnnotationClient()
        store = S3AnnotationStore(client)
        object_ref = ObjectRef(
            bucket="bucket",
            key="meetings/a.mp4",
            etag='"etag-value"',
            size=10,
            version_id="version-7",
        )

        self.assertIsNone(store.get(object_ref, "mi.transcription"))
        store.put(object_ref, "mi.transcription", {"v": 1, "state": "submitted"})

        request = client.put_calls[-1]
        self.assertEqual('"etag-value"', request["ObjectIfMatch"])
        self.assertEqual("version-7", request["VersionId"])
        self.assertEqual("mi.transcription", request["AnnotationName"])
        self.assertEqual(
            {"v": 1, "state": "submitted"},
            json.loads(request["AnnotationPayload"]),
        )
        self.assertEqual(
            {"v": 1, "state": "submitted"},
            store.get(object_ref, "mi.transcription"),
        )

        client.conflict = True
        with self.assertRaisesRegex(AnnotationConflictError, "object changed"):
            store.put(object_ref, "mi.transcription", {"v": 1, "state": "complete"})


if __name__ == "__main__":
    unittest.main()
