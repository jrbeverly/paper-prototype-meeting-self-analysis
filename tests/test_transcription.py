from __future__ import annotations

import unittest
from datetime import datetime, timezone

from meeting_intelligence.config import TranscriptionSettings
from meeting_intelligence.models import SourceAnnotation
from meeting_intelligence.transcription import EMPTY_VTT, TranscriptionReconciler
from tests.fakes import FakeTranscribe, MemoryAnnotations, MemoryRepository


def source(recording_id: str = "file-a") -> SourceAnnotation:
    return SourceAnnotation.from_dict(
        {
            "v": 1,
            "kind": "zoom-recording",
            "occurrenceId": "occ",
            "meetingUuid": "uuid",
            "recordingFileId": recording_id,
            "expectedMp4FileIds": [recording_id],
            "recordingSetFingerprint": "sha256:set",
            "recordedAt": "2026-07-23T10:00:00Z",
            "durationSeconds": 10,
            "metadata": {"transcriptionVocabulary": "payments"},
            "sourcePrefix": "meetings/occ/",
        }
    )


class FailBeforeFinalAnnotation(MemoryAnnotations):
    def __init__(self, repository: MemoryRepository):
        super().__init__(repository)
        self.fail_once = True

    def put(self, object_ref, name, value):
        if name == "mi.transcription" and value.get("state") == "complete" and self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected final annotation failure")
        return super().put(object_ref, name, value)


class TranscriptionTests(unittest.TestCase):
    def settings(self, *, config_id: str = "v1", retry_failed: bool = False):
        return TranscriptionSettings(config_id, "en-US", 100, retry_failed)

    def reconciler(self, repo, annotations, service, settings=None):
        return TranscriptionReconciler(
            repo,
            annotations,
            service,
            settings or self.settings(),
            now=lambda: datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        )

    def test_start_complete_and_idempotent_rerun(self):
        repo = MemoryRepository()
        obj = repo.seed("meetings/occ/file-a.mp4", b"video")
        annotations = MemoryAnnotations(repo)
        service = FakeTranscribe()
        reconciler = self.reconciler(repo, annotations, service)

        submitted = reconciler.reconcile(obj, source())
        self.assertEqual("submitted", submitted["state"])
        self.assertEqual(1, len(service.started))

        again = reconciler.reconcile(obj, source())
        self.assertEqual("submitted", again["state"])
        self.assertEqual(1, len(service.started))

        job_name = service.started[0]
        service.jobs[job_name] = {
            "TranscriptionJobStatus": "COMPLETED",
            "Subtitles": {"SubtitleFileUris": ["https://signed.example/vtt"]},
        }
        complete = reconciler.reconcile(obj, source())
        self.assertEqual("complete", complete["state"])
        self.assertIn("meetings/occ/file-a.vtt", repo.values)
        vtt = repo.head("meetings/occ/file-a.vtt")
        self.assertIsNotNone(annotations.get(vtt, "mi.transcription-source"))

        writes = len(repo.writes)
        self.assertEqual(complete, reconciler.reconcile(obj, source()))
        self.assertEqual(writes, len(repo.writes))
        self.assertEqual(1, len(service.started))

    def test_recovers_start_before_annotation(self):
        repo = MemoryRepository()
        obj = repo.seed("meetings/occ/file-a.mp4", b"video")
        annotations = MemoryAnnotations(repo)
        service = FakeTranscribe()
        reconciler = self.reconciler(repo, annotations, service)
        # Learn the deterministic name, then remove the annotation as if the
        # process died after StartTranscriptionJob.
        state = reconciler.reconcile(obj, source())
        annotations.values.pop((obj.key, "mi.transcription"))
        service.started.clear()
        recovered = reconciler.reconcile(obj, source())
        self.assertEqual(state["job"], recovered["job"])
        self.assertEqual([], service.started)

    def test_recovers_vtt_write_before_final_annotation(self):
        repo = MemoryRepository()
        obj = repo.seed("meetings/occ/file-a.mp4", b"video")
        annotations = FailBeforeFinalAnnotation(repo)
        service = FakeTranscribe()
        reconciler = self.reconciler(repo, annotations, service)
        submitted = reconciler.reconcile(obj, source())
        service.jobs[submitted["job"]] = {
            "TranscriptionJobStatus": "COMPLETED",
            "Subtitles": {"SubtitleFileUris": ["https://signed.example/vtt"]},
        }
        with self.assertRaisesRegex(RuntimeError, "injected"):
            reconciler.reconcile(obj, source())
        self.assertIsNotNone(repo.head("meetings/occ/file-a.vtt"))
        repaired = reconciler.reconcile(obj, source())
        self.assertEqual("complete", repaired["state"])

    def test_no_speech_is_terminal_with_valid_vtt(self):
        repo = MemoryRepository()
        obj = repo.seed("meetings/occ/file-a.mp4", b"video")
        annotations = MemoryAnnotations(repo)
        service = FakeTranscribe()
        reconciler = self.reconciler(repo, annotations, service)
        submitted = reconciler.reconcile(obj, source())
        service.jobs[submitted["job"]] = {
            "TranscriptionJobStatus": "COMPLETED",
            "Subtitles": {"SubtitleFileUris": []},
        }
        result = reconciler.reconcile(obj, source())
        self.assertEqual("complete_no_speech", result["state"])
        self.assertEqual(EMPTY_VTT, repo.values["meetings/occ/file-a.vtt"])
        self.assertTrue(reconciler.is_publishable(obj, source(), result))

    def test_failed_job_is_terminal_until_explicit_retry_configuration(self):
        repo = MemoryRepository()
        obj = repo.seed("meetings/occ/file-a.mp4", b"video")
        annotations = MemoryAnnotations(repo)
        service = FakeTranscribe()
        reconciler = self.reconciler(repo, annotations, service)
        submitted = reconciler.reconcile(obj, source())
        service.jobs[submitted["job"]] = {
            "TranscriptionJobStatus": "FAILED",
            "FailureReason": "bad media",
        }
        failed = reconciler.reconcile(obj, source())
        self.assertEqual("failed", failed["state"])
        self.assertEqual(failed, reconciler.reconcile(obj, source()))
        self.assertEqual(1, len(service.started))

        retry = self.reconciler(repo, annotations, service, self.settings(retry_failed=True))
        retried = retry.reconcile(obj, source())
        self.assertEqual(1, retried["attempt"])
        self.assertNotEqual(submitted["job"], retried["job"])
        self.assertEqual(2, len(service.started))

    def test_changed_configuration_does_not_trust_old_completion(self):
        repo = MemoryRepository()
        obj = repo.seed("meetings/occ/file-a.mp4", b"video")
        annotations = MemoryAnnotations(repo)
        service = FakeTranscribe()
        first = self.reconciler(repo, annotations, service, self.settings(config_id="v1"))
        submitted = first.reconcile(obj, source())
        service.jobs[submitted["job"]] = {
            "TranscriptionJobStatus": "COMPLETED",
            "Subtitles": {"SubtitleFileUris": ["uri"]},
        }
        first.reconcile(obj, source())
        second = self.reconciler(repo, annotations, service, self.settings(config_id="v2"))
        state = second.reconcile(obj, source())
        self.assertEqual("submitted", state["state"])
        self.assertNotEqual(submitted["job"], state["job"])


if __name__ == "__main__":
    unittest.main()

