"""Deterministic identifiers and path-safe names."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: object) -> str:
    return f"sha256:{sha256(canonical_json(value))}"


def business_date(instant: str, timezone: str = "America/Toronto") -> str:
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("instant must include a timezone")
    return parsed.astimezone(ZoneInfo(timezone)).date().isoformat()


def transcribe_job_name(
    bucket: str,
    key: str,
    object_identity: str,
    source_fingerprint: str,
    configuration_fingerprint: str,
    attempt: int,
) -> str:
    material = "\0".join(
        (
            bucket,
            key,
            object_identity,
            source_fingerprint,
            configuration_fingerprint,
            str(attempt),
        )
    )
    return f"mi-{sha256(material)}"


def canonical_vtt_key(mp4_key: str) -> str:
    if not mp4_key.lower().endswith(".mp4"):
        raise ValueError(f"not an MP4 key: {mp4_key}")
    return f"{mp4_key[:-4]}.vtt"


def label_token(value: str, prefix: str = "recording") -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        normalized = sha256(value)[:16]
    candidate = f"{prefix}-{normalized}"
    if len(candidate) <= 80:
        return candidate
    return f"{prefix}-{normalized[:54]}-{sha256(value)[:16]}"


def safe_path_token(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if normalized == value and normalized:
        return normalized
    stem = normalized[:80] or "recording"
    return f"{stem}-{sha256(value)[:12]}"


def clip_filename(source_recording_id: str, start_seconds: float, end_seconds: float, result_version: int) -> str:
    start_ms = round(start_seconds * 1000)
    end_ms = round(end_seconds * 1000)
    return f"{safe_path_token(source_recording_id)}-{start_ms}-{end_ms}-r{result_version}.mp4"

