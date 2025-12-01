from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from meeting_intelligence.config import Settings
from meeting_intelligence.finalize import AnalysisFinalizer, extract_result_json
from tests.fakes import MemoryRepository
from tests.test_ids_models_config import valid_config


class FakeConfluence:
    def __init__(self, result: Mapping[str, Any]):
        self.result = dict(result)
        self.properties: dict[tuple[str, str], dict[str, Any]] = {}
        self.final_pages: dict[tuple[str, str], dict[str, Any]] = {}
        self.labels: dict[str, set[str]] = {}
        self.ensure_calls = 0
        self.fail_completion_once = False
        self.fail_done_label_once = False

    def find_result_pages(self):
        return [self.result]

    def get_page(self, page_id: str):
        if page_id == str(self.result["id"]):
            return self.result
        for page in self.final_pages.values():
            if page["id"] == page_id:
                return page
        raise KeyError(page_id)

    def get_property(self, page_id: str, name: str):
        value = self.properties.get((page_id, name))
        return {"key": name, "value": value, "version": {"number": 1}} if value is not None else None

    def put_property(self, page_id: str, name: str, value: Mapping[str, Any]):
        if name == "meeting-intelligence-completion" and self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError("injected completion failure")
        self.properties[(page_id, name)] = dict(value)

    def add_labels(self, page_id: str, labels: Iterable[str]):
        labels = set(labels)
        if "status-analysis-done" in labels and self.fail_done_label_once:
            self.fail_done_label_once = False
            raise RuntimeError("injected label failure")
        self.labels.setdefault(page_id, set()).update(labels)

    def ensure_page(
        self,
        *,
        title: str,
        parent_id: str,
        markdown: str,
        labels: Iterable[str],
        property_name: str,
        property_value: Mapping[str, Any],
    ):
        self.ensure_calls += 1
        key = (parent_id, title)
        page = self.final_pages.get(key)
        if page is None:
            page = {"id": "final-1", "version": {"number": 1}, "markdown": markdown}
        elif page["markdown"] != markdown:
            page["version"]["number"] += 1
            page["markdown"] = markdown
        self.final_pages[key] = page
        return {
            "id": page["id"],
            "version": page["version"]["number"],
            "contentFingerprint": "fp",
        }


class FakeMedia:
    def __init__(self, duration: float = 60):
        self.source_duration = duration
        self.rendered: list[str] = []

    def duration(self, source: Path) -> float:
        return self.source_duration

    def render(self, source: Path, destination: Path, start_seconds: float, end_seconds: float):
        destination.write_bytes(f"clip:{start_seconds}:{end_seconds}".encode())
        self.rendered.append(destination.name)


class FakeSharePoint:
    drive_id = "drive"

    def __init__(self):
        self.files: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_on_new_number: int | None = None

    def ensure_file(self, remote_path: str, local_path: Path):
        self.calls.append(remote_path)
        if remote_path in self.files:
            return self.files[remote_path]
        if self.fail_on_new_number is not None and len(self.files) + 1 == self.fail_on_new_number:
            self.fail_on_new_number = None
            raise RuntimeError("injected Graph failure")
        reference = {
            "driveId": "drive",
            "itemId": f"item-{len(self.files) + 1}",
            "webUrl": f"https://sharepoint.example/{len(self.files) + 1}",
            "remotePath": remote_path,
        }
        self.files[remote_path] = reference
        return reference


def result_page(demos: list[dict[str, Any]], version: int = 1):
    result = {
        "schemaVersion": "1.0",
        "summary": "A factual summary",
        "decisions": ["Ship"],
        "actionItems": [],
        "risks": [],
        "demos": demos,
    }
    return {
        "id": "900",
        "title": "MI result — 100",
        "version": {"number": version},
        "body": {
            "storage": {
                "value": f'<ac:structured-macro><ac:plain-text-body><![CDATA[{json.dumps(result)}]]></ac:plain-text-body></ac:structured-macro>'
            }
        },
    }


class FinalizeTests(unittest.TestCase):
    def make(self, demos, *, duration=60, version=1):
        self.repo = MemoryRepository()
        self.source = self.repo.seed("meetings/occ/rec.mp4", b"video")
        self.confluence = FakeConfluence(result_page(demos, version))
        self.confluence.properties[("100", "meeting-intelligence")] = {
            "v": 1,
            "bucket": "bucket",
            "occurrenceIds": ["occ"],
            "recordings": [
                {
                    "occurrenceId": "occ",
                    "recordingId": "rec",
                    "key": self.source.key,
                    "objectIdentity": self.source.identity,
                    "durationSeconds": 60,
                }
            ],
        }
        self.sharepoint = FakeSharePoint()
        self.media = FakeMedia(duration)
        self.settings = Settings.from_dict(valid_config())
        return AnalysisFinalizer(
            self.repo,
            self.confluence,
            self.sharepoint,
            self.media,
            self.settings,
            now=lambda: datetime(2026, 7, 23, 18, tzinfo=timezone.utc),
        )

    def test_extracts_wrapped_json(self):
        value = '<p>response</p><ac:plain-text-body><![CDATA[{"schemaVersion":"1.0","summary":"x","demos":[]}]]>'
        self.assertEqual("x", extract_result_json(value)["summary"])

    def test_no_clips_still_publishes_and_marks_done_last(self):
        finalizer = self.make([])
        result = finalizer.run()
        self.assertEqual(1, result["completed"])
        receipt = self.confluence.properties[("900", "meeting-intelligence-completion")]
        self.assertEqual([], receipt["sharePointClips"])
        self.assertEqual("done", receipt["state"])
        self.assertIn("status-analysis-done", self.confluence.labels["900"])
        self.assertEqual(1, len(self.confluence.final_pages))

        rerun = finalizer.run()
        self.assertEqual(1, rerun["alreadyComplete"])
        self.assertEqual(1, self.confluence.ensure_calls)

    def test_clips_are_exact_versioned_paths_and_graph_failure_recovers_without_duplicates(self):
        demos = [
            {"sourceRecordingId": "rec", "startSeconds": 1, "endSeconds": 2, "title": "One"},
            {"sourceRecordingId": "rec", "startSeconds": 3, "endSeconds": 4, "title": "Two"},
        ]
        finalizer = self.make(demos)
        self.sharepoint.fail_on_new_number = 2
        with self.assertRaisesRegex(RuntimeError, "Graph"):
            finalizer.run()
        self.assertEqual(1, len(self.sharepoint.files))
        self.assertNotIn(("900", "meeting-intelligence-completion"), self.confluence.properties)

        result = finalizer.run()
        self.assertEqual(1, result["completed"])
        self.assertEqual(2, len(self.sharepoint.files))
        self.assertTrue(all(path.endswith("-r1.mp4") for path in self.sharepoint.files))
        self.assertEqual(2, len(self.confluence.properties[("900", "meeting-intelligence-completion")]["sharePointClips"]))

    def test_final_page_then_receipt_failure_is_repaired(self):
        finalizer = self.make([])
        self.confluence.fail_completion_once = True
        with self.assertRaisesRegex(RuntimeError, "completion"):
            finalizer.run()
        self.assertEqual(1, len(self.confluence.final_pages))
        result = finalizer.run()
        self.assertEqual(1, result["completed"])
        self.assertEqual(1, len(self.confluence.final_pages))

    def test_receipt_before_done_label_is_repaired_without_refinalizing(self):
        finalizer = self.make([])
        self.confluence.fail_done_label_once = True
        with self.assertRaisesRegex(RuntimeError, "label"):
            finalizer.run()
        self.assertEqual("done", self.confluence.properties[("900", "meeting-intelligence-completion")]["state"])
        result = finalizer.run()
        self.assertEqual(1, result["alreadyComplete"])
        self.assertEqual(1, self.confluence.ensure_calls)

    def test_out_of_bounds_and_replaced_source_are_explicit_failed_state(self):
        finalizer = self.make(
            [{"sourceRecordingId": "rec", "startSeconds": 0, "endSeconds": 61, "title": "Bad"}],
            duration=60,
        )
        result = finalizer.run()
        self.assertEqual(1, result["invalid"])
        failure = self.confluence.properties[("900", "meeting-intelligence-finalization")]
        self.assertIn("beyond recording duration", failure["reason"])
        self.assertEqual([], self.media.rendered)

        finalizer = self.make(
            [{"sourceRecordingId": "rec", "startSeconds": 0, "endSeconds": 1, "title": "Bad"}]
        )
        self.repo.seed(self.source.key, b"replacement")
        result = finalizer.run()
        self.assertEqual(1, result["invalid"])
        failure = self.confluence.properties[("900", "meeting-intelligence-finalization")]
        self.assertIn("revision changed", failure["reason"])

    def test_new_result_page_version_is_not_suppressed_by_old_receipt(self):
        demo = [{"sourceRecordingId": "rec", "startSeconds": 1, "endSeconds": 2, "title": "Demo"}]
        finalizer = self.make(demo, version=1)
        finalizer.run()
        self.assertTrue(any(path.endswith("-r1.mp4") for path in self.sharepoint.files))
        self.confluence.result = result_page(demo, version=2)
        result = finalizer.run()
        self.assertEqual(1, result["completed"])
        self.assertTrue(any(path.endswith("-r2.mp4") for path in self.sharepoint.files))
        self.assertEqual(
            2,
            self.confluence.properties[("900", "meeting-intelligence-completion")]["resultVersion"],
        )


if __name__ == "__main__":
    unittest.main()
