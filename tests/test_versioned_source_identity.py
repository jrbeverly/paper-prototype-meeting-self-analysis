from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from meeting_intelligence.finalize import AnalysisFinalizer
from meeting_intelligence.models import TERMINAL_TRANSCRIPTION_STATES
from meeting_intelligence.reconcile import MeetingReconciler
from tests.fakes import FakePages, MemoryRepository
from tests.test_finalizer_acceptance import (
    FakeConfluence,
    FakeMedia,
    FakeSharePoint,
    NOW,
    analysis,
    result_page,
    settings as finalizer_settings,
)
from tests.test_reconcile_acceptance import (
    configuration as reconciler_settings,
    recording_set_fingerprint,
)


class VersionedRepository(MemoryRepository):
    """Models ListObjectsV2 (ETag only) followed by authoritative HeadObject."""

    source_key = "meetings/occurrence-1/recording-a.mp4"
    source_version = "source-version-7"

    def list(self, prefix: str):
        for object_ref in super().list(prefix):
            # ListObjectsV2 never supplies an object's VersionId.
            yield replace(object_ref, version_id=None)

    def head(self, key: str):
        object_ref = super().head(key)
        if object_ref is not None and key == self.source_key:
            return replace(object_ref, version_id=self.source_version)
        return object_ref


class VersionAwareAnnotations:
    def __init__(self):
        self.values: dict[tuple[str, str], Mapping[str, Any]] = {}
        self.puts: list[tuple[str, str, str | None]] = []

    def get(self, object_ref, name: str) -> Mapping[str, Any] | None:
        return self.values.get((object_ref.key, name))

    def put(self, object_ref, name: str, value: Mapping[str, Any]) -> None:
        self.values[(object_ref.key, name)] = dict(value)
        self.puts.append((object_ref.key, name, object_ref.version_id))


class TerminalTranscriptions:
    def __init__(self, repository: VersionedRepository):
        self.repository = repository

    def is_publishable(self, object_ref, source, state):
        return bool(
            state
            and state.get("state") in TERMINAL_TRANSCRIPTION_STATES
            and self.repository.head(object_ref.key[:-4] + ".vtt") is not None
        )

    def is_terminal_failure(self, object_ref, source, state):
        return False

    def reconcile(self, object_ref, source):
        raise AssertionError("terminal fixture must not start transcription")


class VersionedSourceIdentityTests(unittest.TestCase):
    def test_head_version_is_frozen_and_accepted_by_finalizer_when_unchanged(self):
        repository = VersionedRepository()
        listed_source = repository.seed(repository.source_key, b"source-video")
        repository.seed(
            "meetings/occurrence-1/recording-a.vtt",
            b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n",
        )
        annotations = VersionAwareAnnotations()
        annotations.values[(repository.source_key, "mi.source")] = {
            "v": 1,
            "kind": "zoom-recording",
            "occurrenceId": "occurrence-1",
            "meetingUuid": "meeting-uuid",
            "recordingFileId": "recording-a",
            "expectedMp4FileIds": ["recording-a"],
            "recordingSetFingerprint": recording_set_fingerprint(["recording-a"]),
            "recordedAt": "2026-07-23T13:00:00Z",
            "durationSeconds": 60,
            "metadata": {
                "scope": "team",
                "teamId": "payments",
                "pipeline": "direct",
                "analysisProfile": "default",
            },
            "sourcePrefix": "meetings/occurrence-1/",
        }
        annotations.values[(repository.source_key, "mi.transcription")] = {
            "v": 1,
            "state": "complete",
        }
        pages = FakePages()
        reconciler = MeetingReconciler(
            repository,
            annotations,
            TerminalTranscriptions(repository),
            pages,
            reconciler_settings(),
            now=lambda: NOW,
        )

        reconciled = reconciler.run()

        self.assertEqual(1, reconciled["pagesPublished"])
        association = next(iter(pages.pages.values()))["property"]
        recording = association["recordings"][0]
        self.assertEqual(repository.source_version, recording["objectIdentity"])
        self.assertNotEqual(listed_source.etag, recording["objectIdentity"])
        self.assertEqual(
            # Annotation writes still target the scan's ETag-only reference;
            # the HeadObject version is durable provenance, not a write target.
            None,
            next(
                version
                for key, name, version in annotations.puts
                if key == repository.source_key and name == "mi.confluence"
            ),
        )

        demo = {
            "sourceRecordingId": "recording-a",
            "startSeconds": 1,
            "endSeconds": 2,
            "title": "Versioned source",
        }
        confluence = FakeConfluence(
            result_page(analysis(demos=[demo]), version=4),
            association,
        )
        sharepoint = FakeSharePoint()
        media = FakeMedia(duration_seconds=60)
        finalizer = AnalysisFinalizer(
            repository,
            confluence,
            sharepoint,
            media,
            finalizer_settings(),
            now=lambda: NOW,
        )

        finalized = finalizer.run()

        self.assertEqual(1, finalized["completed"])
        self.assertEqual([(1.0, 2.0)], media.render_calls)
        self.assertEqual(1, len(sharepoint.items))
        self.assertEqual(
            repository.source_version,
            repository.head(repository.source_key).identity,
        )


if __name__ == "__main__":
    unittest.main()
