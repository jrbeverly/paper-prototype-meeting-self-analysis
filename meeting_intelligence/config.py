"""Configuration loading and validation.

Non-secret configuration is one JSON document. Credentials remain separate
Secrets Manager JSON values injected by CodeBuild.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigurationError
from .ids import fingerprint


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{path} must be an object")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{path} must be a non-empty string")
    return value.strip()


def _integer(value: Any, path: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(f"{path} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class TranscriptionSettings:
    configuration_id: str
    language_code: str
    max_items_per_run: int
    retry_failed: bool

    @property
    def fingerprint(self) -> str:
        return fingerprint({"id": self.configuration_id, "languageCode": self.language_code})


@dataclass(frozen=True)
class ConfluenceSettings:
    base_url: str
    space_key: str
    input_parent_id: str
    result_parent_id: str
    final_parent_id: str
    md2conf_command: str


@dataclass(frozen=True)
class FinalizationSettings:
    sharepoint_site_id: str
    sharepoint_drive_id: str
    sharepoint_folder: str
    max_demos: int
    simple_upload_limit_bytes: int
    upload_chunk_bytes: int
    ffmpeg_command: str
    ffprobe_command: str


@dataclass(frozen=True)
class Settings:
    raw: Mapping[str, Any]
    artifact_bucket: str
    active_prefix: str
    business_timezone: str
    transcription: TranscriptionSettings
    confluence: ConfluenceSettings
    finalization: FinalizationSettings

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Settings":
        artifact_bucket = _text(os.getenv("MI_BUCKET") or value.get("artifactBucket"), "artifactBucket")
        active_prefix = _text(value.get("activePrefix", "meetings/"), "activePrefix")
        if not active_prefix.endswith("/"):
            active_prefix += "/"

        transcribe = _mapping(value.get("transcription"), "transcription")
        confluence = _mapping(value.get("confluence"), "confluence")
        finalization = _mapping(value.get("finalization"), "finalization")
        sharepoint = _mapping(finalization.get("sharePoint"), "finalization.sharePoint")

        chunk_bytes = _integer(
            finalization.get("uploadChunkBytes", 10 * 1024 * 1024),
            "finalization.uploadChunkBytes",
            320 * 1024,
        )
        if chunk_bytes % (320 * 1024) != 0:
            raise ConfigurationError("finalization.uploadChunkBytes must be a multiple of 320 KiB")
        if chunk_bytes >= 60 * 1024 * 1024:
            raise ConfigurationError("finalization.uploadChunkBytes must be smaller than 60 MiB")
        simple_upload_limit = _integer(
            finalization.get("simpleUploadLimitBytes", 250_000_000),
            "finalization.simpleUploadLimitBytes",
            1,
        )
        if simple_upload_limit > 250_000_000:
            raise ConfigurationError("finalization.simpleUploadLimitBytes cannot exceed 250,000,000")

        jira = value.get("jira", {"teams": []})
        jira_mapping = _mapping(jira, "jira")
        teams = jira_mapping.get("teams", [])
        if not isinstance(teams, list):
            raise ConfigurationError("jira.teams must be an array")
        for index, team in enumerate(teams):
            candidate = _mapping(team, f"jira.teams[{index}]")
            for key in ("teamId", "projectKey", "boardId", "storyPointsField"):
                _text(candidate.get(key), f"jira.teams[{index}].{key}")

        inbound = value.get("inboundSharePointProfiles", {})
        for name, profile in _mapping(inbound, "inboundSharePointProfiles").items():
            _text(name, "inboundSharePointProfiles key")
            profile_value = _mapping(profile, f"inboundSharePointProfiles.{name}")
            prefixes = profile_value.get("prefixes")
            if not isinstance(prefixes, list) or not prefixes or not all(
                isinstance(prefix, str)
                and prefix
                and not prefix.startswith("/")
                and prefix.endswith("/")
                for prefix in prefixes
            ):
                raise ConfigurationError(
                    f"inboundSharePointProfiles.{name}.prefixes must be non-empty relative S3 prefixes ending in '/'"
                )
            _integer(profile_value.get("maxDocuments", 20), f"inboundSharePointProfiles.{name}.maxDocuments", 1)
            _integer(
                profile_value.get("maxBytesPerDocument", 200_000),
                f"inboundSharePointProfiles.{name}.maxBytesPerDocument",
                1,
            )
            max_age = profile_value.get("maxAgeHours", 168)
            if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or max_age < 0:
                raise ConfigurationError(f"inboundSharePointProfiles.{name}.maxAgeHours must be a number >= 0")
            if not isinstance(profile_value.get("required", False), bool):
                raise ConfigurationError(f"inboundSharePointProfiles.{name}.required must be boolean")

        return cls(
            raw=value,
            artifact_bucket=artifact_bucket,
            active_prefix=active_prefix,
            business_timezone=_text(value.get("businessTimeZone", "America/Toronto"), "businessTimeZone"),
            transcription=TranscriptionSettings(
                configuration_id=_text(transcribe.get("configurationId"), "transcription.configurationId"),
                language_code=_text(transcribe.get("languageCode", "en-US"), "transcription.languageCode"),
                max_items_per_run=_integer(
                    transcribe.get("maxItemsPerRun", 100), "transcription.maxItemsPerRun", 1
                ),
                retry_failed=bool(transcribe.get("retryFailed", False)),
            ),
            confluence=ConfluenceSettings(
                base_url=_text(confluence.get("baseUrl"), "confluence.baseUrl").rstrip("/"),
                space_key=_text(confluence.get("spaceKey"), "confluence.spaceKey"),
                input_parent_id=_text(confluence.get("inputParentId"), "confluence.inputParentId"),
                result_parent_id=_text(confluence.get("resultParentId"), "confluence.resultParentId"),
                final_parent_id=_text(confluence.get("finalParentId"), "confluence.finalParentId"),
                md2conf_command=_text(confluence.get("md2confCommand", "md2conf"), "confluence.md2confCommand"),
            ),
            finalization=FinalizationSettings(
                sharepoint_site_id=_text(sharepoint.get("siteId"), "finalization.sharePoint.siteId"),
                sharepoint_drive_id=_text(sharepoint.get("driveId"), "finalization.sharePoint.driveId"),
                sharepoint_folder=_text(
                    sharepoint.get("folder", "Meeting Intelligence"), "finalization.sharePoint.folder"
                ).strip("/"),
                max_demos=_integer(finalization.get("maxDemos", 20), "finalization.maxDemos", 0),
                simple_upload_limit_bytes=simple_upload_limit,
                upload_chunk_bytes=chunk_bytes,
                ffmpeg_command=_text(finalization.get("ffmpegCommand", "ffmpeg"), "finalization.ffmpegCommand"),
                ffprobe_command=_text(finalization.get("ffprobeCommand", "ffprobe"), "finalization.ffprobeCommand"),
            ),
        )

    @property
    def jira(self) -> Mapping[str, Any]:
        return _mapping(self.raw.get("jira", {}), "jira")

    @property
    def inbound_sharepoint_profiles(self) -> Mapping[str, Any]:
        return _mapping(self.raw.get("inboundSharePointProfiles", {}), "inboundSharePointProfiles")

    @property
    def analysis_profiles(self) -> Mapping[str, Any]:
        return _mapping(self.raw.get("analysisProfiles", {}), "analysisProfiles")

    @property
    def aggregation_profiles(self) -> list[Mapping[str, Any]]:
        profiles = self.raw.get("aggregationProfiles", [])
        if not isinstance(profiles, list):
            raise ConfigurationError("aggregationProfiles must be an array")
        return [_mapping(item, "aggregationProfiles[]") for item in profiles]


def _read_ssm_json(parameter_name: str) -> Mapping[str, Any]:
    try:
        import boto3

        response = boto3.client("ssm").get_parameter(Name=parameter_name)
        raw = response["Parameter"]["Value"]
    except Exception as exc:  # pragma: no cover - exercised against AWS
        raise ConfigurationError(f"could not load SSM parameter {parameter_name}: {exc}") from exc
    return _parse_json(raw, f"SSM parameter {parameter_name}")


def _parse_json(raw: str, source: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{source} is not valid JSON: {exc}") from exc
    return _mapping(value, source)


def load_settings(explicit_path: str | None = None) -> Settings:
    path = explicit_path or os.getenv("MI_CONFIG_FILE")
    if path:
        try:
            value = _parse_json(Path(path).read_text(encoding="utf-8"), path)
        except OSError as exc:
            raise ConfigurationError(f"could not read configuration {path}: {exc}") from exc
        return Settings.from_dict(value)
    if raw := os.getenv("MI_CONFIG_JSON"):
        return Settings.from_dict(_parse_json(raw, "MI_CONFIG_JSON"))
    if parameter := os.getenv("MI_CONFIG_PARAMETER"):
        return Settings.from_dict(_read_ssm_json(parameter))
    raise ConfigurationError("set --config, MI_CONFIG_FILE, MI_CONFIG_JSON, or MI_CONFIG_PARAMETER")


def load_secret(environment_name: str) -> Mapping[str, Any]:
    raw = os.getenv(environment_name)
    if not raw:
        raise ConfigurationError(f"{environment_name} is required")
    return _parse_json(raw, environment_name)


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="JSON config path; overrides MI_CONFIG_*")
