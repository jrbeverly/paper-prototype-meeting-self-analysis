from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from meeting_intelligence.config import Settings
from meeting_intelligence.ids import sha256
from meeting_intelligence.reconcile import MeetingReconciler
from meeting_intelligence.transcription import TranscriptionReconciler
from tests.fakes import FakePages, FakeTranscribe, MemoryAnnotations, MemoryRepository
from tests.test_ids_models_config import valid_config


def source_value(
    occurrence: str,
    recording_id: str,
    expected: list[str],
    *,
    team: str = "payments",
    fingerprint: str | None = None,
    pipeline: str = "direct",
    sharepoint_profile: str | None = None,
):
    metadata = {
        "scope": "team",
        "teamId": team,
        "meetingType": "daily-standup",
        "pipeline": pipeline,
        "analysisProfile": "daily",
    }
    if pipeline == "aggregation":
        metadata["aggregationProfile"] = "daily-brief"
    if sharepoint_profile:
        metadata["sharePointProfile"] = sharepoint_profile
    return {
        "v": 1,
        "kind": "zoom-recording",
        "occurrenceId": occurrence,
        "meetingUuid": f"uuid-{occurrence}",
        "recordingFileId": recording_id,
        "expectedMp4FileIds": expected,
        "recordingSetFingerprint": fingerprint or f"sha256:{sha256(chr(10).join(sorted(expected)))}",
        "recordedAt": "2026-07-23T12:00:00Z",
        "durationSeconds": 60,
        "metadata": metadata,
        "sourcePrefix": f"meetings/{occurrence}/",
    }


class ReconcileTests(unittest.TestCase):
    def make(
        self,
        config: dict | None = None,
        *,
        now=lambda: datetime(2026, 7, 23, 18, tzinfo=timezone.utc),
    ):
        self.repo = MemoryRepository()
        self.annotations = MemoryAnnotations(self.repo)
        self.service = FakeTranscribe()
        self.pages = FakePages()
        self.settings = Settings.from_dict(config or valid_config())
        transcribe = TranscriptionReconciler(
            self.repo,
            self.annotations,
            self.service,
            self.settings.transcription,
            now=now,
        )
        return MeetingReconciler(
            self.repo,
            self.annotations,
            transcribe,
            self.pages,
            self.settings,
            now=now,
        )

    def seed_recording(self, occurrence, recording_id, expected, **kwargs):
        obj = self.repo.seed(f"meetings/{occurrence}/{recording_id}.mp4", f"video-{recording_id}".encode())
        self.annotations.values[(obj.key, "mi.source")] = source_value(
            occurrence, recording_id, expected, **kwargs
        )
        return obj

    def complete_started_jobs(self):
        for name in self.service.started:
            self.service.jobs[name] = {
                "TranscriptionJobStatus": "COMPLETED",
                "Subtitles": {"SubtitleFileUris": ["uri"]},
            }

    def seed_jira(self):
        self.repo.seed(
            "jira/payments/snapshots/2026-07-23T11:30:00Z.json",
            json.dumps(
                {
                    "capturedAt": "2026-07-23T11:30:00Z",
                    "teamId": "payments",
                    "sprints": [],
                    "analytics": {},
                }
            ).encode(),
        )

    def test_split_occurrence_waits_then_publishes_once_with_full_body_and_association(self):
        reconciler = self.make()
        self.seed_jira()
        a = self.seed_recording("occ", "a", ["a", "b"])
        b = self.seed_recording("occ", "b", ["a", "b"])

        first = reconciler.run()
        self.assertEqual(2, first["transcriptionsAdvanced"])
        self.assertEqual(0, first["pagesPublished"])
        self.complete_started_jobs()
        second = reconciler.run()
        self.assertEqual(1, second["pagesPublished"])
        self.assertEqual(1, len(self.pages.pages))
        page = next(iter(self.pages.pages.values()))
        self.assertIn("Recording `a`", page["markdown"])
        self.assertIn("Duration: `60.000` seconds", page["markdown"])
        self.assertIn("hello", page["markdown"])
        self.assertEqual({"a", "b"}, {item["recordingId"] for item in page["property"]["recordings"]})
        self.assertEqual(
            "published", self.annotations.get(a, "mi.confluence")["state"]
        )
        self.assertEqual(
            "published", self.annotations.get(b, "mi.confluence")["state"]
        )

        third = reconciler.run()
        self.assertEqual(1, third["pagesPublished"])
        self.assertEqual(1, len(self.pages.pages))
        self.assertEqual(1, next(iter(self.pages.pages.values()))["version"])
        self.assertEqual(2, len(self.service.started))

    def test_incomplete_or_inconsistent_split_set_never_publishes(self):
        reconciler = self.make()
        self.seed_jira()
        self.seed_recording("missing", "a", ["a", "b"])
        self.seed_recording("bad", "a", ["a", "b"], fingerprint="one")
        self.seed_recording("bad", "b", ["a", "b"], fingerprint="two")
        reconciler.run()
        self.complete_started_jobs()
        result = reconciler.run()
        self.assertEqual(0, result["pagesPublished"])
        self.assertEqual({}, self.pages.pages)

    def test_required_sharepoint_context_defers_then_preserves_provenance(self):
        config = valid_config()
        config["inboundSharePointProfiles"] = {
            "payments-docs": {
                "prefixes": ["sharepoint/payments/"],
                "required": True,
                "maxAgeHours": 24,
            }
        }
        reconciler = self.make(config)
        self.seed_jira()
        self.seed_recording(
            "occ",
            "a",
            ["a"],
            sharepoint_profile="payments-docs",
        )
        reconciler.run()
        self.complete_started_jobs()
        self.assertEqual(0, reconciler.run()["pagesPublished"])

        content_key = "sharepoint/payments/site/drive/item/v1/content.md"
        self.repo.seed(content_key, b"# Release plan\nship it")
        self.repo.seed(
            "sharepoint/payments/site/drive/item/v1/metadata.json",
            json.dumps(
                {
                    "status": "synchronized",
                    "siteId": "site",
                    "driveId": "drive",
                    "itemId": "item",
                    "version": "v1",
                    "name": "Release plan",
                    "webUrl": "https://sharepoint.example/item",
                    "syncedAt": "2026-07-23T17:30:00Z",
                    "contentKey": content_key,
                }
            ).encode(),
        )
        self.assertEqual(1, reconciler.run()["pagesPublished"])
        page = next(iter(self.pages.pages.values()))
        self.assertIn("ship it", page["markdown"])
        artifact = page["property"]["sharePointArtifacts"][0]
        self.assertEqual("item", artifact["itemId"])
        self.assertNotIn("text", artifact)

    def test_aggregation_freezes_exact_configured_membership(self):
        config = valid_config()
        config["jira"]["teams"] = []
        config["aggregationProfiles"] = [
            {
                "id": "daily-brief",
                "expectedTeams": ["payments", "orders"],
                "deadlineLocal": "23:00",
            }
        ]
        reconciler = self.make(config)
        self.seed_recording("pay-occ", "pay", ["pay"], team="payments", pipeline="aggregation")
        self.seed_recording("order-occ", "order", ["order"], team="orders", pipeline="aggregation")
        self.seed_recording("extra-occ", "extra", ["extra"], team="extra", pipeline="aggregation")
        reconciler.run()
        self.complete_started_jobs()
        result = reconciler.run()
        self.assertEqual(1, result["pagesPublished"])
        receipt_key = "aggregates/daily-brief/2026-07-23.json"
        receipt = json.loads(self.repo.values[receipt_key])
        self.assertEqual(["order-occ", "pay-occ"], receipt["occurrenceIds"])
        self.assertTrue(receipt["complete"])
        self.assertEqual(1, len(self.pages.pages))
        page = next(iter(self.pages.pages.values()))
        self.assertEqual(["order-occ", "pay-occ"], page["property"]["occurrenceIds"])


if __name__ == "__main__":
    unittest.main()
