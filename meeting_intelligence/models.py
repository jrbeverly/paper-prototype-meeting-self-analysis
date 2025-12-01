"""Small, explicit data shapes at external boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Any, Mapping

from .errors import ContractError, ResultValidationError


TERMINAL_TRANSCRIPTION_STATES = frozenset({"complete", "complete_no_speech"})


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{path} must be finite")
    return result


def _instant(value: Any, path: str) -> str:
    result = _string(value, path)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{path} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{path} must include a timezone")
    return result


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    etag: str
    size: int = 0
    version_id: str | None = None
    last_modified: datetime | None = None

    @property
    def identity(self) -> str:
        return self.version_id or self.etag


@dataclass(frozen=True)
class SourceAnnotation:
    occurrence_id: str
    meeting_uuid: str
    recording_file_id: str
    expected_mp4_file_ids: tuple[str, ...]
    recording_set_fingerprint: str
    recorded_at: str
    duration_seconds: float
    metadata: Mapping[str, Any]
    source_prefix: str
    v: int = 1

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceAnnotation":
        if value.get("v") != 1 or value.get("kind") != "zoom-recording":
            raise ContractError("mi.source must be v=1 kind=zoom-recording")
        expected = value.get("expectedMp4FileIds")
        if not isinstance(expected, list) or not expected:
            raise ContractError("mi.source.expectedMp4FileIds must be a non-empty list")
        if len(set(expected)) != len(expected) or not all(isinstance(item, str) and item for item in expected):
            raise ContractError("mi.source.expectedMp4FileIds must contain unique non-empty strings")
        metadata = value.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ContractError("mi.source.metadata must be an object")
        duration = _number(value.get("durationSeconds"), "mi.source.durationSeconds")
        if duration < 0:
            raise ContractError("mi.source.durationSeconds cannot be negative")
        return cls(
            occurrence_id=_string(value.get("occurrenceId"), "mi.source.occurrenceId"),
            meeting_uuid=_string(value.get("meetingUuid"), "mi.source.meetingUuid"),
            recording_file_id=_string(value.get("recordingFileId"), "mi.source.recordingFileId"),
            expected_mp4_file_ids=tuple(expected),
            recording_set_fingerprint=_string(
                value.get("recordingSetFingerprint"), "mi.source.recordingSetFingerprint"
            ),
            recorded_at=_instant(value.get("recordedAt"), "mi.source.recordedAt"),
            duration_seconds=duration,
            metadata=metadata,
            source_prefix=_string(value.get("sourcePrefix"), "mi.source.sourcePrefix"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "kind": "zoom-recording",
            "occurrenceId": self.occurrence_id,
            "meetingUuid": self.meeting_uuid,
            "recordingFileId": self.recording_file_id,
            "expectedMp4FileIds": list(self.expected_mp4_file_ids),
            "recordingSetFingerprint": self.recording_set_fingerprint,
            "recordedAt": self.recorded_at,
            "durationSeconds": self.duration_seconds,
            "metadata": dict(self.metadata),
            "sourcePrefix": self.source_prefix,
        }


@dataclass(frozen=True)
class Recording:
    object: ObjectRef
    source: SourceAnnotation
    transcription: Mapping[str, Any] | None = None

    @property
    def vtt_key(self) -> str:
        from .ids import canonical_vtt_key

        return canonical_vtt_key(self.object.key)


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    source_prefix: str
    recorded_at: str
    metadata: Mapping[str, Any]
    recordings: tuple[Recording, ...]
    recording_set_fingerprint: str

    def recording_by_id(self, recording_id: str) -> Recording | None:
        return next((recording for recording in self.recordings if recording.source.recording_file_id == recording_id), None)


@dataclass(frozen=True)
class Demo:
    source_recording_id: str
    start_seconds: float
    end_seconds: float
    title: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], index: int) -> "Demo":
        prefix = f"demos[{index}]"
        try:
            start = _number(value.get("startSeconds"), f"{prefix}.startSeconds")
            end = _number(value.get("endSeconds"), f"{prefix}.endSeconds")
            source_recording_id = _string(value.get("sourceRecordingId"), f"{prefix}.sourceRecordingId")
            title = _string(value.get("title"), f"{prefix}.title")
        except ContractError as exc:
            raise ResultValidationError(str(exc)) from exc
        if start < 0:
            raise ResultValidationError(f"{prefix}.startSeconds must be non-negative")
        if end <= start:
            raise ResultValidationError(f"{prefix}.endSeconds must be greater than startSeconds")
        return cls(
            source_recording_id=source_recording_id,
            start_seconds=start,
            end_seconds=end,
            title=title,
        )


@dataclass(frozen=True)
class AnalysisResult:
    schema_version: str
    summary: str
    demos: tuple[Demo, ...]
    title: str | None = None
    decisions: tuple[Any, ...] = field(default_factory=tuple)
    action_items: tuple[Any, ...] = field(default_factory=tuple)
    risks: tuple[Any, ...] = field(default_factory=tuple)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Any, max_demos: int = 20) -> "AnalysisResult":
        if not isinstance(value, Mapping):
            raise ResultValidationError("Rovo result must be a JSON object")
        schema_version = value.get("schemaVersion")
        if schema_version != "1.0":
            raise ResultValidationError("schemaVersion must equal 1.0")
        try:
            summary = _string(value.get("summary"), "summary")
        except ContractError as exc:
            raise ResultValidationError(str(exc)) from exc
        demos_value = value.get("demos")
        if not isinstance(demos_value, list):
            raise ResultValidationError("demos must be an array")
        if len(demos_value) > max_demos:
            raise ResultValidationError(f"demos exceeds configured maximum of {max_demos}")
        demos: list[Demo] = []
        for index, demo in enumerate(demos_value):
            if not isinstance(demo, Mapping):
                raise ResultValidationError(f"demos[{index}] must be an object")
            demos.append(Demo.from_dict(demo, index))
        list_fields: dict[str, tuple[Any, ...]] = {}
        for name in ("decisions", "actionItems", "risks"):
            candidate = value.get(name, [])
            if not isinstance(candidate, list):
                raise ResultValidationError(f"{name} must be an array when present")
            list_fields[name] = tuple(candidate)
        title = value.get("title")
        if title is not None and not isinstance(title, str):
            raise ResultValidationError("title must be a string when present")
        return cls(
            schema_version=schema_version,
            summary=summary,
            demos=tuple(demos),
            title=title,
            decisions=list_fields["decisions"],
            action_items=list_fields["actionItems"],
            risks=list_fields["risks"],
            raw=value,
        )
