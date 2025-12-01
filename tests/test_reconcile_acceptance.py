from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from meeting_intelligence.config import Settings
from meeting_intelligence.models import SourceAnnotation, TERMINAL_TRANSCRIPTION_STATES
from meeting_intelligence.reconcile import MeetingReconciler
from meeting_intelligence.transcription import TranscriptionReconciler
from tests.fakes import (
    FakePages,
    FakeTranscribe,
    MemoryAnnotations,
    MemoryRepository,
)


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def configuration(
    *,
    jira: Mapping[str, Any] | None = None,
    inbound: Mapping[str, Any] | None = None,
    analysis_profiles: Mapping[str, Any] | None = None,
    aggregation_profiles: list[Mapping[str, Any]] | None = None,
    max_items_per_run: int = 100,
) -> Settings:
    return Settings.from_dict(
        {
            "artifactBucket": "bucket",
            "activePrefix": "meetings/",
            "businessTimeZone": "America/Toronto",
            "transcription": {
                "configurationId": "transcription-v1",
                "languageCode": "en-US",
                "maxItemsPerRun": max_items_per_run,
            },
            "jira": dict(jira or {"required": False, "teams": []}),
            "inboundSharePointProfiles": dict(inbound or {}),
            "analysisProfiles": dict(analysis_profiles or {}),
            "aggregationProfiles": list(aggregation_profiles or []),
            "confluence": {
                "baseUrl": "https://example.atlassian.net/wiki",
                "spaceKey": "MI",
                "inputParentId": "input-parent",
                "resultParentId": "result-parent",
                "finalParentId": "final-parent",
            },
            "finalization": {
                "sharePoint": {
                    "siteId": "site",
                    "driveId": "drive",
                    "folder": "Meeting Intelligence",
                }
            },
        }
    )


def recording_set_fingerprint(recording_ids: list[str]) -> str:
    payload = "\n".join(sorted(recording_ids)).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class TerminalTranscriptions:
    """A narrow readiness seam; transcription behavior has its own test suite."""

    def __init__(self, repository: MemoryRepository):
        self.repository = repository
        self.reconciled: list[str] = []

    def is_publishable(self, object_ref, source, state):
        return bool(
            state
            and state.get("state") in TERMINAL_TRANSCRIPTION_STATES
            and self.repository.head(object_ref.key[:-4] + ".vtt") is not None
        )

    def reconcile(self, object_ref, source):
        self.reconciled.append(object_ref.key)
        return {}


class ReconcileAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.annotations = MemoryAnnotations(self.repository)
        self.pages = FakePages()

    def reconciler(self, settings: Settings) -> MeetingReconciler:
        return MeetingReconciler(
            self.repository,
            self.annotations,
            TerminalTranscriptions(self.repository),
            self.pages,
            settings,
            now=lambda: NOW,
        )

    def published_page(self, title_prefix: str) -> Mapping[str, Any]:
        matches = [
            page
            for (parent_id, title), page in self.pages.pages.items()
            if parent_id == "input-parent" and title.startswith(title_prefix)
        ]
        self.assertEqual(1, len(matches), self.pages.pages)
        return matches[0]

    def seed_segment(
        self,
        recording_id: str,
        *,
        expected: list[str],
        fingerprint: str | None = None,
        occurrence_id: str = "occurrence-1",
        recorded_at: str = "2026-07-23T13:00:00Z",
        metadata: Mapping[str, Any] | None = None,
    ):
        key = f"meetings/{occurrence_id}/{recording_id}.mp4"
        object_ref = self.repository.seed(key, f"video-{recording_id}".encode())
        self.repository.seed(
            key[:-4] + ".vtt",
            (
                "WEBVTT\n\n"
                "00:00:00.000 --> 00:00:01.000\n"
                f"transcript for {recording_id}\n"
            ).encode(),
        )
        self.annotations.values[(key, "mi.source")] = {
            "v": 1,
            "kind": "zoom-recording",
            "occurrenceId": occurrence_id,
            "meetingUuid": "meeting-uuid",
            "recordingFileId": recording_id,
            "expectedMp4FileIds": list(expected),
            "recordingSetFingerprint": fingerprint or recording_set_fingerprint(expected),
            "recordedAt": recorded_at,
            "durationSeconds": 60,
            "metadata": dict(
                metadata
                or {
                    "scope": "team",
                    "teamId": "payments",
                    "routing": {
                        "pipeline": "direct",
                        "analysisProfile": "default",
                    },
                }
            ),
            "sourcePrefix": f"meetings/{occurrence_id}/",
        }
        self.annotations.values[(key, "mi.transcription")] = {
            "v": 1,
            "state": "complete",
        }
        return object_ref

    def test_split_occurrence_waits_for_every_segment_then_publishes_once(self):
        expected = ["segment-a", "segment-b"]
        self.seed_segment("segment-a", expected=expected)
        reconciler = self.reconciler(configuration())

        first = reconciler.run()

        self.assertEqual(0, first["occurrencesReady"])
        self.assertEqual(0, first["pagesPublished"])
        self.assertEqual({}, self.pages.pages)

        self.seed_segment("segment-b", expected=expected)
        second = reconciler.run()

        self.assertEqual(1, second["occurrencesReady"])
        self.assertEqual(1, second["pagesPublished"])
        page = self.published_page("Meeting — occurrence-1")
        self.assertIn("Recording `segment-a`", page["markdown"])
        self.assertIn("Recording `segment-b`", page["markdown"])
        self.assertEqual(
            {"segment-a", "segment-b"},
            {item["recordingId"] for item in page["property"]["recordings"]},
        )

    def test_split_occurrence_rejects_disagreeing_fingerprints(self):
        expected = ["segment-a", "segment-b"]
        self.seed_segment("segment-a", expected=expected, fingerprint="sha256:first")
        self.seed_segment("segment-b", expected=expected, fingerprint="sha256:second")

        result = self.reconciler(configuration()).run()

        self.assertEqual(0, result["occurrencesReady"])
        self.assertEqual({}, self.pages.pages)

    def test_split_occurrence_rejects_fingerprint_that_does_not_describe_expected_set(self):
        expected = ["segment-a", "segment-b"]
        self.seed_segment("segment-a", expected=expected, fingerprint="sha256:forged")
        self.seed_segment("segment-b", expected=expected, fingerprint="sha256:forged")

        result = self.reconciler(configuration()).run()

        self.assertEqual(0, result["occurrencesReady"])
        self.assertEqual({}, self.pages.pages)

    def test_nested_routing_drives_aggregation_and_analysis_profile(self):
        metadata = {
            "scope": "team",
            "teamId": "payments",
            "meetingType": "daily-standup",
            "routing": {
                "pipeline": "aggregation",
                "aggregationProfile": "daily-brief",
                "analysisProfile": "daily-brief-team-input",
                "outputTemplate": "daily-brief",
            },
        }
        self.seed_segment("segment-a", expected=["segment-a"], metadata=metadata)
        settings = configuration(
            analysis_profiles={
                "daily-brief-team-input": {
                    "instructions": "NESTED ROUTING INSTRUCTION",
                }
            },
            aggregation_profiles=[
                {
                    "id": "daily-brief",
                    "expectedTeams": ["payments"],
                    "deadlineLocal": "17:00",
                }
            ],
        )

        result = self.reconciler(settings).run()

        self.assertEqual(1, result["pagesPublished"])
        page = self.published_page("Daily brief — daily-brief — 2026-07-23")
        self.assertIn("NESTED ROUTING INSTRUCTION", page["markdown"])
        self.assertEqual("aggregate", page["property"]["kind"])
        self.assertEqual("daily-brief", page["property"]["aggregationProfile"])

    def test_required_jira_snapshot_defers_until_eligible_context_exists(self):
        metadata = {
            "scope": "team",
            "teamId": "payments",
            "jiraProject": "PAY",
            "routing": {
                "pipeline": "direct",
                "analysisProfile": "sprint-review",
            },
        }
        self.seed_segment("segment-a", expected=["segment-a"], metadata=metadata)
        settings = configuration(
            jira={
                "required": True,
                "teams": [
                    {
                        "teamId": "payments",
                        "projectKey": "PAY",
                        "boardId": "7",
                        "storyPointsField": "customfield_42",
                    }
                ],
            }
        )
        reconciler = self.reconciler(settings)

        waiting = reconciler.run()

        self.assertEqual(0, waiting["pagesPublished"])
        self.repository.seed(
            "jira/payments/snapshots/2026-07-23T12:30:00Z.json",
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "capturedAt": "2026-07-23T12:30:00Z",
                    "sprints": [{"id": 7, "name": "Sprint", "issues": []}],
                }
            ).encode(),
        )

        published = reconciler.run()

        self.assertEqual(1, published["pagesPublished"])
        page = self.published_page("Meeting — occurrence-1")
        self.assertIn("jira/payments/snapshots/2026-07-23T12:30:00Z.json", page["markdown"])
        self.assertNotIn('"status": "missing"', page["markdown"])

    def test_split_occurrence_uses_earliest_segment_time_for_jira_cutoff(self):
        metadata = {
            "scope": "team",
            "teamId": "payments",
            "jiraProject": "PAY",
            "routing": {
                "pipeline": "direct",
                "analysisProfile": "sprint-review",
            },
        }
        expected = ["a-later-segment", "z-earlier-segment"]
        self.seed_segment(
            "a-later-segment",
            expected=expected,
            recorded_at="2026-07-23T13:30:00Z",
            metadata=metadata,
        )
        self.seed_segment(
            "z-earlier-segment",
            expected=expected,
            recorded_at="2026-07-23T13:00:00Z",
            metadata=metadata,
        )
        self.repository.seed(
            "jira/payments/snapshots/2026-07-23T12:45:00Z.json",
            json.dumps(
                {
                    "capturedAt": "2026-07-23T12:45:00Z",
                    "selection": "before-meeting",
                }
            ).encode(),
        )
        self.repository.seed(
            "jira/payments/snapshots/2026-07-23T13:15:00Z.json",
            json.dumps(
                {
                    "capturedAt": "2026-07-23T13:15:00Z",
                    "selection": "after-meeting",
                }
            ).encode(),
        )
        settings = configuration(
            jira={
                "required": True,
                "teams": [
                    {
                        "teamId": "payments",
                        "projectKey": "PAY",
                        "boardId": "7",
                        "storyPointsField": "customfield_42",
                    }
                ],
            }
        )

        outcome = self.reconciler(settings).run()

        self.assertEqual(1, outcome["pagesPublished"])
        page = self.published_page("Meeting — occurrence-1")
        self.assertIn('"selection": "before-meeting"', page["markdown"])
        self.assertNotIn('"selection": "after-meeting"', page["markdown"])

    def test_optional_jira_context_is_explicitly_missing_not_not_configured(self):
        metadata = {
            "scope": "team",
            "teamId": "payments",
            "jiraProject": "PAY",
            "routing": {"pipeline": "direct", "analysisProfile": "sprint-review"},
        }
        self.seed_segment("segment-a", expected=["segment-a"], metadata=metadata)
        settings = configuration(
            jira={
                "required": False,
                "teams": [
                    {
                        "teamId": "payments",
                        "projectKey": "PAY",
                        "boardId": "7",
                        "storyPointsField": "customfield_42",
                    }
                ],
            }
        )

        result = self.reconciler(settings).run()

        self.assertEqual(1, result["pagesPublished"])
        page = self.published_page("Meeting — occurrence-1")
        self.assertIn('"status": "missing"', page["markdown"])
        self.assertNotIn('"status": "not-configured"', page["markdown"])

    def test_required_sharepoint_profile_defers_until_current_provenance_and_text_exist(self):
        metadata = {
            "scope": "team",
            "teamId": "payments",
            "sharePointProfile": "payments-reference",
            "routing": {"pipeline": "direct", "analysisProfile": "default"},
        }
        self.seed_segment("segment-a", expected=["segment-a"], metadata=metadata)
        settings = configuration(
            inbound={
                "payments-reference": {
                    "prefixes": ["sharepoint/payments/"],
                    "maxDocuments": 5,
                    "maxBytesPerDocument": 10_000,
                    "maxAgeHours": 24,
                    "required": True,
                }
            }
        )
        reconciler = self.reconciler(settings)

        waiting = reconciler.run()

        self.assertEqual(0, waiting["pagesPublished"])
        content_key = "sharepoint/payments/architecture.md"
        self.repository.seed(
            content_key,
            b"# Architecture\n\nUse the reconciler design.",
            modified=NOW - timedelta(minutes=15),
        )
        self.repository.seed(
            "sharepoint/payments/architecture.metadata.json",
            json.dumps(
                {
                    "schemaVersion": "1.0",
                    "status": "synchronized",
                    "siteId": "site",
                    "driveId": "source-drive",
                    "itemId": "item-7",
                    "version": "3",
                    "name": "architecture.docx",
                    "webUrl": "https://sharepoint.example/architecture",
                    "syncedAt": "2026-07-24T11:45:00Z",
                    "contentKey": content_key,
                }
            ).encode(),
            modified=NOW - timedelta(minutes=10),
        )

        published = reconciler.run()

        self.assertEqual(1, published["pagesPublished"])
        page = self.published_page("Meeting — occurrence-1")
        self.assertIn("Use the reconciler design.", page["markdown"])
        self.assertEqual(
            [
                {
                    "occurrenceId": "occurrence-1",
                    "key": content_key,
                    "metadataKey": "sharepoint/payments/architecture.metadata.json",
                    "name": "architecture.docx",
                    "objectIdentity": self.repository.head(content_key).identity,
                    "siteId": "site",
                    "driveId": "source-drive",
                    "itemId": "item-7",
                    "version": "3",
                    "webUrl": "https://sharepoint.example/architecture",
                    "syncedAt": "2026-07-24T11:45:00Z",
                }
            ],
            page["property"]["sharePointArtifacts"],
        )

    def test_terminal_failed_transcription_does_not_starve_later_new_recording(self):
        failed_object = self.seed_segment(
            "a-failed",
            expected=["a-failed"],
            occurrence_id="failed-occurrence",
        )
        pending_object = self.seed_segment(
            "b-pending",
            expected=["b-pending"],
            occurrence_id="pending-occurrence",
        )
        for object_ref in (failed_object, pending_object):
            self.repository.values.pop(object_ref.key[:-4] + ".vtt")
            self.repository.modified.pop(object_ref.key[:-4] + ".vtt")
            self.annotations.values.pop((object_ref.key, "mi.transcription"))

        settings = configuration(max_items_per_run=1)
        service = FakeTranscribe()
        transcriptions = TranscriptionReconciler(
            self.repository,
            self.annotations,
            service,
            settings.transcription,
            now=lambda: NOW,
        )
        failed_source = SourceAnnotation.from_dict(
            self.annotations.values[(failed_object.key, "mi.source")]
        )
        submitted = transcriptions.reconcile(failed_object, failed_source)
        service.jobs[submitted["job"]] = {
            "TranscriptionJobStatus": "FAILED",
            "FailureReason": "unsupported media",
        }
        self.assertEqual(
            "failed",
            transcriptions.reconcile(failed_object, failed_source)["state"],
        )
        self.assertEqual(1, len(service.started))
        reconciler = MeetingReconciler(
            self.repository,
            self.annotations,
            transcriptions,
            self.pages,
            settings,
            now=lambda: NOW,
        )

        outcome = reconciler.run()

        self.assertEqual(1, outcome["transcriptionsAdvanced"])
        self.assertEqual(2, len(service.started))
        self.assertEqual(
            "submitted",
            self.annotations.values[
                (pending_object.key, "mi.transcription")
            ]["state"],
        )


if __name__ == "__main__":
    unittest.main()
