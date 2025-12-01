from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from meeting_intelligence.config import Settings
from meeting_intelligence.finalize import AnalysisFinalizer
from meeting_intelligence.ids import fingerprint
from tests.fakes import MemoryRepository


NOW = datetime(2026, 7, 24, 15, 42, tzinfo=timezone.utc)


def settings(*, max_demos: int = 20) -> Settings:
    return Settings.from_dict(
        {
            "artifactBucket": "bucket",
            "activePrefix": "meetings/",
            "transcription": {
                "configurationId": "transcription-v1",
                "languageCode": "en-US",
            },
            "jira": {"required": False, "teams": []},
            "confluence": {
                "baseUrl": "https://example.atlassian.net/wiki",
                "spaceKey": "MI",
                "inputParentId": "input-parent",
                "resultParentId": "result-parent",
                "finalParentId": "final-parent",
            },
            "finalization": {
                "maxDemos": max_demos,
                "sharePoint": {
                    "siteId": "site",
                    "driveId": "clip-drive",
                    "folder": "Meeting Intelligence",
                },
            },
        }
    )


def analysis(
    *,
    summary: str = "A concise summary.",
    demos: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "summary": summary,
        "decisions": ["Ship it"],
        "actionItems": [{"owner": "Ada", "action": "Verify"}],
        "risks": [],
        "demos": list(demos or []),
    }


def result_page(value: Mapping[str, Any], *, version: int = 7) -> dict[str, Any]:
    return {
        "id": "result-456",
        "title": "MI result — 123",
        "version": {"number": version},
        "body": {
            "storage": {
                "value": f"<pre><![CDATA[{json.dumps(value)}]]></pre>",
            }
        },
    }


class FakeConfluence:
    def __init__(self, page: Mapping[str, Any], association: Mapping[str, Any]):
        self.result_page = dict(page)
        self.properties: dict[tuple[str, str], dict[str, Any]] = {
            ("123", "meeting-intelligence"): {
                "key": "meeting-intelligence",
                "value": dict(association),
                "version": {"number": 1},
            },
            ("result-456", "meeting-intelligence-source"): {
                "key": "meeting-intelligence-source",
                "value": {"inputPageId": "123"},
                "version": {"number": 1},
            },
        }
        self.final_pages: dict[tuple[str, str], dict[str, Any]] = {}
        self.labels: dict[str, set[str]] = {}
        self.operations: list[tuple[Any, ...]] = []
        self.fail_ensure_after_create_once = False
        self.fail_done_label_once = False
        self.revision_on_next_result_get: int | None = None

    def find_result_pages(self) -> Iterable[Mapping[str, Any]]:
        return [self.result_page]

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        self.operations.append(("get_page", page_id))
        if page_id == "result-456":
            if self.revision_on_next_result_get is not None:
                version = self.revision_on_next_result_get
                self.revision_on_next_result_get = None
                return {
                    **self.result_page,
                    "version": {"number": version},
                }
            return self.result_page
        for page in self.final_pages.values():
            if page["id"] == page_id:
                return page
        raise KeyError(page_id)

    def get_property(self, page_id: str, name: str) -> Mapping[str, Any] | None:
        return self.properties.get((page_id, name))

    def put_property(self, page_id: str, name: str, value: Mapping[str, Any]) -> None:
        self.operations.append(("put_property", page_id, name))
        current = self.properties.get((page_id, name))
        version = int((current or {}).get("version", {}).get("number", 0)) + 1
        self.properties[(page_id, name)] = {
            "key": name,
            "value": dict(value),
            "version": {"number": version},
        }

    def add_labels(self, page_id: str, labels: Iterable[str]) -> None:
        values = tuple(sorted(set(labels)))
        self.operations.append(("add_labels", page_id, values))
        if (
            page_id == "result-456"
            and "status-analysis-done" in values
            and self.fail_done_label_once
        ):
            self.fail_done_label_once = False
            raise RuntimeError("simulated crash before done label")
        self.labels.setdefault(page_id, set()).update(values)

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
        self.operations.append(("ensure_page", parent_id, title))
        key = (parent_id, title)
        current = self.final_pages.get(key)
        desired = {
            "markdown": markdown,
            "property": dict(property_value),
            "labels": set(labels),
        }
        changed = current is None or any(
            current[name] != desired[name] for name in ("markdown", "property", "labels")
        )
        if current and changed:
            version = int(current["version"]) + 1
        elif current:
            version = int(current["version"])
        else:
            version = 1
        page = {
            "id": current["id"] if current else "final-900",
            "title": title,
            "version": version,
            **desired,
            "contentFingerprint": fingerprint(
                {"markdown": markdown, "property": property_value}
            ),
        }
        self.final_pages[key] = page
        if self.fail_ensure_after_create_once:
            self.fail_ensure_after_create_once = False
            raise RuntimeError("simulated crash after final page creation")
        return page


class FakeSharePoint:
    drive_id = "clip-drive"

    def __init__(self):
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, bytes]] = []

    def ensure_file(self, remote_path: str, local_path: Path) -> Mapping[str, Any]:
        payload = local_path.read_bytes()
        self.calls.append((remote_path, payload))
        item = self.items.get(remote_path)
        if item is None:
            item = {
                "driveId": self.drive_id,
                "itemId": f"item-{len(self.items) + 1}",
                "webUrl": f"https://sharepoint.example/{len(self.items) + 1}",
                "name": Path(remote_path).name,
                "size": len(payload),
                "remotePath": remote_path,
            }
            self.items[remote_path] = item
        return item


class FakeMedia:
    def __init__(self, duration_seconds: float = 120.0):
        self.duration_seconds = duration_seconds
        self.duration_calls: list[Path] = []
        self.render_calls: list[tuple[float, float]] = []

    def duration(self, source: Path) -> float:
        self.duration_calls.append(source)
        return self.duration_seconds

    def render(
        self,
        source: Path,
        destination: Path,
        start_seconds: float,
        end_seconds: float,
    ) -> None:
        self.render_calls.append((start_seconds, end_seconds))
        destination.write_bytes(
            f"clip:{source.read_bytes().decode()}:{start_seconds}:{end_seconds}".encode()
        )


class FinalizerAcceptanceTests(unittest.TestCase):
    def make_system(
        self,
        result: Mapping[str, Any],
        *,
        version: int = 7,
        duration_seconds: float = 120.0,
        source_occurrence_id: str = "occurrence-1",
        associated_occurrence_ids: list[str] | None = None,
        max_demos: int = 20,
    ):
        repository = MemoryRepository()
        source = repository.seed("meetings/occurrence-1/recording-a.mp4", b"source-video")
        association = {
            "v": 1,
            "kind": "meeting",
            "bucket": "bucket",
            "occurrenceIds": list(associated_occurrence_ids or ["occurrence-1"]),
            "recordings": [
                {
                    "occurrenceId": source_occurrence_id,
                    "recordingId": "recording-a",
                    "key": source.key,
                    "objectIdentity": source.identity,
                    "durationSeconds": duration_seconds,
                }
            ],
        }
        confluence = FakeConfluence(result_page(result, version=version), association)
        sharepoint = FakeSharePoint()
        media = FakeMedia(duration_seconds)
        finalizer = AnalysisFinalizer(
            repository,
            confluence,
            sharepoint,
            media,
            settings(max_demos=max_demos),
            now=lambda: NOW,
        )
        return repository, confluence, sharepoint, media, finalizer

    def test_no_clip_result_publishes_and_marks_done_last_idempotently(self):
        _, confluence, sharepoint, media, finalizer = self.make_system(analysis())

        first = finalizer.run()

        self.assertEqual(
            {"examined": 1, "completed": 1, "alreadyComplete": 0, "invalid": 0},
            first,
        )
        self.assertEqual([], sharepoint.calls)
        self.assertEqual([], media.render_calls)
        self.assertEqual(1, len(confluence.final_pages))
        final_page = next(iter(confluence.final_pages.values()))
        self.assertIn("_No clips were requested._", final_page["markdown"])
        receipt = confluence.properties[
            ("result-456", "meeting-intelligence-completion")
        ]["value"]
        self.assertEqual(7, receipt["resultVersion"])
        self.assertEqual([], receipt["sharePointClips"])
        self.assertEqual("final-900", receipt["finalPageId"])
        completion_index = confluence.operations.index(
            (
                "put_property",
                "result-456",
                "meeting-intelligence-completion",
            )
        )
        done_index = confluence.operations.index(
            (
                "add_labels",
                "result-456",
                ("status-analysis-done",),
            )
        )
        self.assertLess(completion_index, done_index)

        second = finalizer.run()

        self.assertEqual(
            {"examined": 1, "completed": 0, "alreadyComplete": 1, "invalid": 0},
            second,
        )
        self.assertEqual(
            1,
            sum(1 for operation in confluence.operations if operation[0] == "ensure_page"),
        )

    def test_multiple_clips_share_one_download_and_publish_stable_references(self):
        demos = [
            {
                "sourceRecordingId": "recording-a",
                "startSeconds": 1,
                "endSeconds": 2.5,
                "title": "First flow",
            },
            {
                "sourceRecordingId": "recording-a",
                "startSeconds": 10.25,
                "endSeconds": 12,
                "title": "Second flow",
            },
        ]
        _, confluence, sharepoint, media, finalizer = self.make_system(
            analysis(demos=demos)
        )

        outcome = finalizer.run()

        self.assertEqual(1, outcome["completed"])
        self.assertEqual(1, len(media.duration_calls))
        self.assertEqual([(1.0, 2.5), (10.25, 12.0)], media.render_calls)
        self.assertEqual(
            {
                "Meeting Intelligence/occurrence-1/recording-a-1000-2500-r7.mp4",
                "Meeting Intelligence/occurrence-1/recording-a-10250-12000-r7.mp4",
            },
            set(sharepoint.items),
        )
        receipt = confluence.properties[
            ("result-456", "meeting-intelligence-completion")
        ]["value"]
        self.assertEqual(2, len(receipt["sharePointClips"]))
        self.assertTrue(
            all(
                set(reference) == {"driveId", "itemId", "webUrl"}
                for reference in receipt["sharePointClips"]
            )
        )
        markdown = next(iter(confluence.final_pages.values()))["markdown"]
        self.assertIn("[First flow](https://sharepoint.example/1)", markdown)
        self.assertIn("[Second flow](https://sharepoint.example/2)", markdown)

    def test_crash_after_final_page_creation_reuses_page_and_clip_on_retry(self):
        demo = {
            "sourceRecordingId": "recording-a",
            "startSeconds": 3,
            "endSeconds": 4,
            "title": "Crash window",
        }
        _, confluence, sharepoint, _, finalizer = self.make_system(
            analysis(demos=[demo])
        )
        confluence.fail_ensure_after_create_once = True

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            finalizer.run()

        self.assertEqual(1, len(confluence.final_pages))
        self.assertEqual(1, len(sharepoint.items))
        self.assertNotIn(
            ("result-456", "meeting-intelligence-completion"),
            confluence.properties,
        )

        recovered = finalizer.run()

        self.assertEqual(1, recovered["completed"])
        self.assertEqual(1, len(confluence.final_pages))
        self.assertEqual(1, len(sharepoint.items))
        self.assertEqual(2, len(sharepoint.calls))
        self.assertEqual(sharepoint.calls[0][0], sharepoint.calls[1][0])
        self.assertIn(
            ("result-456", "meeting-intelligence-completion"),
            confluence.properties,
        )

    def test_crash_after_completion_property_repairs_done_label_without_rework(self):
        _, confluence, sharepoint, _, finalizer = self.make_system(analysis())
        confluence.fail_done_label_once = True

        with self.assertRaisesRegex(RuntimeError, "done label"):
            finalizer.run()

        self.assertIn(
            ("result-456", "meeting-intelligence-completion"),
            confluence.properties,
        )
        ensure_calls = sum(
            1 for operation in confluence.operations if operation[0] == "ensure_page"
        )

        recovered = finalizer.run()

        self.assertEqual(1, recovered["alreadyComplete"])
        self.assertIn("status-analysis-done", confluence.labels["result-456"])
        self.assertEqual(
            ensure_calls,
            sum(1 for operation in confluence.operations if operation[0] == "ensure_page"),
        )
        self.assertEqual([], sharepoint.calls)

    def test_new_result_page_version_creates_revisioned_clip_and_updates_same_report(self):
        demo = {
            "sourceRecordingId": "recording-a",
            "startSeconds": 1,
            "endSeconds": 2,
            "title": "Versioned flow",
        }
        _, confluence, sharepoint, _, finalizer = self.make_system(
            analysis(summary="Version seven", demos=[demo]),
            version=7,
        )
        self.assertEqual(1, finalizer.run()["completed"])
        first_final_id = next(iter(confluence.final_pages.values()))["id"]

        confluence.result_page = result_page(
            analysis(summary="Version eight", demos=[demo]),
            version=8,
        )
        revised = finalizer.run()

        self.assertEqual(1, revised["completed"])
        self.assertEqual(
            {
                "Meeting Intelligence/occurrence-1/recording-a-1000-2000-r7.mp4",
                "Meeting Intelligence/occurrence-1/recording-a-1000-2000-r8.mp4",
            },
            set(sharepoint.items),
        )
        final_page = next(iter(confluence.final_pages.values()))
        self.assertEqual(first_final_id, final_page["id"])
        self.assertIn("Version eight", final_page["markdown"])
        self.assertEqual(
            8,
            confluence.properties[
                ("result-456", "meeting-intelligence-completion")
            ]["value"]["resultVersion"],
        )

    def test_result_revision_during_work_is_not_marked_complete(self):
        demo = {
            "sourceRecordingId": "recording-a",
            "startSeconds": 1,
            "endSeconds": 2,
            "title": "Moving target",
        }
        _, confluence, _, _, finalizer = self.make_system(
            analysis(demos=[demo]),
            version=7,
        )
        confluence.revision_on_next_result_get = 8

        with self.assertRaisesRegex(RuntimeError, "changed from version 7 to 8"):
            finalizer.run()

        self.assertNotIn(
            ("result-456", "meeting-intelligence-completion"),
            confluence.properties,
        )
        self.assertNotIn("status-analysis-done", confluence.labels.get("result-456", set()))

    def test_replaced_source_is_terminally_invalid_and_never_drives_ffmpeg(self):
        demo = {
            "sourceRecordingId": "recording-a",
            "startSeconds": 1,
            "endSeconds": 2,
            "title": "Stale source",
        }
        repository, confluence, sharepoint, media, finalizer = self.make_system(
            analysis(demos=[demo])
        )
        repository.seed("meetings/occurrence-1/recording-a.mp4", b"replacement")

        outcome = finalizer.run()

        self.assertEqual(1, outcome["invalid"])
        self.assertEqual([], media.render_calls)
        self.assertEqual([], sharepoint.calls)
        failure = confluence.properties[
            ("result-456", "meeting-intelligence-finalization")
        ]["value"]
        self.assertEqual("failed", failure["state"])
        self.assertIn("revision changed", failure["reason"])
        self.assertNotIn(
            ("result-456", "meeting-intelligence-completion"),
            confluence.properties,
        )

    def test_recording_from_another_occurrence_is_rejected_even_without_clips(self):
        _, confluence, sharepoint, media, finalizer = self.make_system(
            analysis(),
            source_occurrence_id="other-occurrence",
            associated_occurrence_ids=["occurrence-1"],
        )

        outcome = finalizer.run()

        self.assertEqual(1, outcome["invalid"])
        self.assertEqual([], media.render_calls)
        self.assertEqual([], sharepoint.calls)
        self.assertIn(
            "outside the input association",
            confluence.properties[
                ("result-456", "meeting-intelligence-finalization")
            ]["value"]["reason"],
        )

    def test_demo_contract_and_recording_duration_bounds_fail_closed(self):
        cases = [
            (
                {
                    "sourceRecordingId": "recording-a",
                    "startSeconds": -1,
                    "endSeconds": 2,
                    "title": "Negative",
                },
                120,
                "non-negative",
            ),
            (
                {
                    "sourceRecordingId": "recording-a",
                    "startSeconds": 4,
                    "endSeconds": 4,
                    "title": "Empty",
                },
                120,
                "greater than",
            ),
            (
                {
                    "sourceRecordingId": "recording-a",
                    "startSeconds": 8,
                    "endSeconds": 10.01,
                    "title": "Past end",
                },
                10,
                "beyond recording duration",
            ),
            (
                {
                    "sourceRecordingId": "not-associated",
                    "startSeconds": 1,
                    "endSeconds": 2,
                    "title": "Foreign",
                },
                120,
                "outside the associated occurrence",
            ),
        ]
        for demo, duration, reason in cases:
            with self.subTest(reason=reason):
                _, confluence, sharepoint, media, finalizer = self.make_system(
                    analysis(demos=[demo]),
                    duration_seconds=duration,
                )

                outcome = finalizer.run()

                self.assertEqual(1, outcome["invalid"])
                self.assertEqual([], sharepoint.calls)
                self.assertIn(
                    reason,
                    confluence.properties[
                        ("result-456", "meeting-intelligence-finalization")
                    ]["value"]["reason"],
                )

    def test_maximum_demo_count_is_enforced_before_source_or_clip_work(self):
        demos = [
            {
                "sourceRecordingId": "recording-a",
                "startSeconds": index,
                "endSeconds": index + 0.5,
                "title": f"Demo {index}",
            }
            for index in range(3)
        ]
        _, confluence, sharepoint, media, finalizer = self.make_system(
            analysis(demos=demos),
            max_demos=2,
        )

        outcome = finalizer.run()

        self.assertEqual(1, outcome["invalid"])
        self.assertEqual([], media.duration_calls)
        self.assertEqual([], sharepoint.calls)
        self.assertIn(
            "maximum of 2",
            confluence.properties[
                ("result-456", "meeting-intelligence-finalization")
            ]["value"]["reason"],
        )


if __name__ == "__main__":
    unittest.main()
