from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from meeting_intelligence.errors import AnnotationConflictError
from meeting_intelligence.ids import fingerprint
from meeting_intelligence.models import ObjectRef


class MemoryRepository:
    def __init__(self, bucket: str = "bucket"):
        self.bucket = bucket
        self.values: dict[str, bytes] = {}
        self.modified: dict[str, datetime] = {}
        self.writes: list[str] = []

    def _ref(self, key: str) -> ObjectRef:
        value = self.values[key]
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            etag=f'"{hashlib.md5(value).hexdigest()}"',
            size=len(value),
            last_modified=self.modified.get(key, datetime.now(timezone.utc)),
        )

    def seed(self, key: str, value: bytes, modified: datetime | None = None) -> ObjectRef:
        self.values[key] = value
        self.modified[key] = modified or datetime.now(timezone.utc)
        return self._ref(key)

    def list(self, prefix: str) -> Iterable[ObjectRef]:
        for key in sorted(self.values):
            if key.startswith(prefix):
                yield self._ref(key)

    def head(self, key: str) -> ObjectRef | None:
        return self._ref(key) if key in self.values else None

    def read_bytes(self, key: str) -> bytes:
        return self.values[key]

    def write_bytes(self, key: str, value: bytes, content_type: str) -> ObjectRef:
        self.values[key] = value
        self.modified[key] = datetime.now(timezone.utc)
        self.writes.append(key)
        return self._ref(key)

    def write_bytes_if_absent(self, key: str, value: bytes, content_type: str) -> tuple[ObjectRef, bool]:
        if key in self.values:
            return self._ref(key), False
        return self.write_bytes(key, value, content_type), True

    def download(self, key: str, destination: Path) -> None:
        destination.write_bytes(self.values[key])


class MemoryAnnotations:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository
        self.values: dict[tuple[str, str], Mapping[str, Any]] = {}
        self.puts: list[tuple[str, str]] = []
        self.fail_after_put: set[tuple[str, str]] = set()

    def get(self, object_ref: ObjectRef, name: str) -> Mapping[str, Any] | None:
        return self.values.get((object_ref.key, name))

    def put(self, object_ref: ObjectRef, name: str, value: Mapping[str, Any]) -> None:
        current = self.repository.head(object_ref.key)
        if current is None or current.identity != object_ref.identity:
            raise AnnotationConflictError("replaced")
        self.values[(object_ref.key, name)] = dict(value)
        self.puts.append((object_ref.key, name))
        if (object_ref.key, name) in self.fail_after_put:
            self.fail_after_put.remove((object_ref.key, name))
            raise RuntimeError("injected annotation failure")


class FakeTranscribe:
    def __init__(self):
        self.jobs: dict[str, Mapping[str, Any]] = {}
        self.started: list[str] = []
        self.subtitle = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n"

    def get_job(self, name: str) -> Mapping[str, Any] | None:
        return self.jobs.get(name)

    def start_job(self, name: str, media_uri: str, configuration: Mapping[str, Any]) -> None:
        self.started.append(name)
        self.jobs[name] = {"TranscriptionJobStatus": "IN_PROGRESS"}

    def download_subtitle(self, uri: str) -> bytes:
        return self.subtitle


class FakePages:
    def __init__(self):
        self.pages: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def ensure_page(
        self,
        *,
        title: str,
        parent_id: str,
        markdown: str,
        labels: Iterable[str],
        property_name: str,
        property_value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        key = (parent_id, title)
        page = self.pages.get(key)
        version = int(page["version"]) + 1 if page and page["markdown"] != markdown else int(page["version"]) if page else 1
        page = {
            "id": page["id"] if page else str(len(self.pages) + 100),
            "version": version,
            "markdown": markdown,
            "labels": set(labels) | (page["labels"] if page else set()),
            "property": dict(property_value),
            "contentFingerprint": fingerprint({"markdown": markdown, "property": property_value}),
        }
        self.pages[key] = page
        self.calls.append(page)
        return page

