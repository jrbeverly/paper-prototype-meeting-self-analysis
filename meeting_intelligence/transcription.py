"""Bounded Amazon Transcribe reconciliation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .config import TranscriptionSettings
from .ids import fingerprint, transcribe_job_name
from .models import ObjectRef, Recording, SourceAnnotation, TERMINAL_TRANSCRIPTION_STATES
from .ports import AnnotationStore, ObjectRepository, TranscribeService

LOGGER = logging.getLogger(__name__)
EMPTY_VTT = b"WEBVTT\n\nNOTE Amazon Transcribe completed without speech\n"


class TranscriptionReconciler:
    def __init__(
        self,
        repository: ObjectRepository,
        annotations: AnnotationStore,
        service: TranscribeService,
        settings: TranscriptionSettings,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.annotations = annotations
        self.service = service
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))

    def reconcile(self, object_ref: ObjectRef, source: SourceAnnotation) -> Mapping[str, Any]:
        vtt_key = f"{object_ref.key[:-4]}.vtt"
        current = self.annotations.get(object_ref, "mi.transcription")
        vocabulary = source.metadata.get("transcriptionVocabulary") or source.metadata.get(
            "transcription_vocabulary"
        )
        configuration = {
            "id": self.settings.configuration_id,
            "languageCode": self.settings.language_code,
            "vocabularyName": vocabulary,
        }
        configuration_fingerprint = fingerprint(configuration)
        source_fingerprint = fingerprint(
            {
                "recordingSetFingerprint": source.recording_set_fingerprint,
                "recordingFileId": source.recording_file_id,
                "objectIdentity": object_ref.identity,
            }
        )

        if (
            current
            and current.get("state") == "failed"
            and current.get("sourceFingerprint") == source_fingerprint
            and current.get("cfg") == configuration_fingerprint
            and not self.settings.retry_failed
        ):
            return current

        if self._is_verified_complete(
            current,
            source_fingerprint=source_fingerprint,
            configuration_fingerprint=configuration_fingerprint,
            vtt_key=vtt_key,
        ):
            return current or {}

        attempt = self._attempt(current, source_fingerprint, configuration_fingerprint)
        job_name = transcribe_job_name(
            object_ref.bucket,
            object_ref.key,
            object_ref.identity,
            source_fingerprint,
            configuration_fingerprint,
            attempt,
        )
        job = self.service.get_job(job_name)
        if job is None:
            self.service.start_job(
                job_name,
                f"s3://{object_ref.bucket}/{object_ref.key}",
                configuration,
            )
            state = self._state(
                source_fingerprint,
                configuration_fingerprint,
                job_name,
                attempt,
                "submitted",
                vtt_key,
            )
            self.annotations.put(object_ref, "mi.transcription", state)
            return state

        status = str(job.get("TranscriptionJobStatus", "")).upper()
        if status in {"QUEUED", "IN_PROGRESS"}:
            state = self._state(
                source_fingerprint,
                configuration_fingerprint,
                job_name,
                attempt,
                "submitted",
                vtt_key,
            )
            self.annotations.put(object_ref, "mi.transcription", state)
            return state
        if status == "FAILED":
            state = self._state(
                source_fingerprint,
                configuration_fingerprint,
                job_name,
                attempt,
                "failed",
                vtt_key,
                failure=str(job.get("FailureReason") or "Amazon Transcribe reported failure"),
            )
            self.annotations.put(object_ref, "mi.transcription", state)
            return state
        if status != "COMPLETED":
            raise RuntimeError(f"unexpected Transcribe status for {job_name}: {status!r}")

        subtitle_uris = (job.get("Subtitles") or {}).get("SubtitleFileUris") or []
        if subtitle_uris:
            payload = self.service.download_subtitle(str(subtitle_uris[0]))
            terminal_state = "complete"
        else:
            payload = EMPTY_VTT
            terminal_state = "complete_no_speech"

        vtt_object = self.repository.write_bytes(vtt_key, payload, "text/vtt; charset=utf-8")
        self.annotations.put(
            vtt_object,
            "mi.transcription-source",
            {
                "v": 1,
                "sourceBucket": object_ref.bucket,
                "sourceKey": object_ref.key,
                "sourceObjectIdentity": object_ref.identity,
                "sourceFingerprint": source_fingerprint,
                "configurationFingerprint": configuration_fingerprint,
                "job": job_name,
                "updated": self._timestamp(),
            },
        )
        verified = self.repository.head(vtt_key)
        if verified is None or verified.size != len(payload):
            raise RuntimeError(f"canonical subtitle output was not verified: {vtt_key}")
        state = self._state(
            source_fingerprint,
            configuration_fingerprint,
            job_name,
            attempt,
            terminal_state,
            vtt_key,
        )
        self.annotations.put(object_ref, "mi.transcription", state)
        return state

    def is_publishable(
        self,
        object_ref: ObjectRef,
        source: SourceAnnotation,
        state: Mapping[str, Any] | None,
    ) -> bool:
        vocabulary = source.metadata.get("transcriptionVocabulary") or source.metadata.get(
            "transcription_vocabulary"
        )
        configuration_fingerprint = fingerprint(
            {
                "id": self.settings.configuration_id,
                "languageCode": self.settings.language_code,
                "vocabularyName": vocabulary,
            }
        )
        source_fingerprint = fingerprint(
            {
                "recordingSetFingerprint": source.recording_set_fingerprint,
                "recordingFileId": source.recording_file_id,
                "objectIdentity": object_ref.identity,
            }
        )
        return self._is_verified_complete(
            state,
            source_fingerprint=source_fingerprint,
            configuration_fingerprint=configuration_fingerprint,
            vtt_key=f"{object_ref.key[:-4]}.vtt",
        )

    def is_terminal_failure(
        self,
        object_ref: ObjectRef,
        source: SourceAnnotation,
        state: Mapping[str, Any] | None,
    ) -> bool:
        """Return whether this exact source/config failure is intentionally parked."""

        vocabulary = source.metadata.get("transcriptionVocabulary") or source.metadata.get(
            "transcription_vocabulary"
        )
        configuration_fingerprint = fingerprint(
            {
                "id": self.settings.configuration_id,
                "languageCode": self.settings.language_code,
                "vocabularyName": vocabulary,
            }
        )
        source_fingerprint = fingerprint(
            {
                "recordingSetFingerprint": source.recording_set_fingerprint,
                "recordingFileId": source.recording_file_id,
                "objectIdentity": object_ref.identity,
            }
        )
        return bool(
            state
            and state.get("state") == "failed"
            and state.get("sourceFingerprint") == source_fingerprint
            and state.get("cfg") == configuration_fingerprint
            and not self.settings.retry_failed
        )

    def _is_verified_complete(
        self,
        state: Mapping[str, Any] | None,
        *,
        source_fingerprint: str,
        configuration_fingerprint: str,
        vtt_key: str,
    ) -> bool:
        return bool(
            state
            and state.get("state") in TERMINAL_TRANSCRIPTION_STATES
            and state.get("sourceFingerprint") == source_fingerprint
            and state.get("cfg") == configuration_fingerprint
            and state.get("vtt") == vtt_key
            and self.repository.head(vtt_key) is not None
        )

    def _attempt(
        self,
        state: Mapping[str, Any] | None,
        source_fingerprint: str,
        configuration_fingerprint: str,
    ) -> int:
        if (
            state
            and state.get("sourceFingerprint") == source_fingerprint
            and state.get("cfg") == configuration_fingerprint
        ):
            current_attempt = int(state.get("attempt", 0))
            if state.get("state") == "failed" and self.settings.retry_failed:
                return current_attempt + 1
            return current_attempt
        return 0

    def _state(
        self,
        source_fingerprint: str,
        configuration_fingerprint: str,
        job: str,
        attempt: int,
        state: str,
        vtt: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "v": 1,
            "cfg": configuration_fingerprint,
            "sourceFingerprint": source_fingerprint,
            "job": job,
            "attempt": attempt,
            "state": state,
            "vtt": vtt,
            "updated": self._timestamp(),
            **extra,
        }

    def _timestamp(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def recording_with_state(
    object_ref: ObjectRef,
    source: SourceAnnotation,
    annotations: AnnotationStore,
) -> Recording:
    return Recording(
        object=object_ref,
        source=source,
        transcription=annotations.get(object_ref, "mi.transcription"),
    )
