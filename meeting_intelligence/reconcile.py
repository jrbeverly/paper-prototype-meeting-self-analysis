"""Meeting transcription and Confluence input-page reconciliation."""

from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, time, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .config import Settings
from .errors import ContextUnavailableError, ContractError
from .ids import business_date, canonical_json, fingerprint, label_token, sha256
from .jira import JiraSnapshotSelector
from .models import Occurrence, Recording, SourceAnnotation, TERMINAL_TRANSCRIPTION_STATES
from .ports import AnnotationStore, ObjectRepository, PagePublisher
from .rendering import render_rovo_input, vtt_to_text
from .transcription import TranscriptionReconciler

LOGGER = logging.getLogger(__name__)
TEXT_EXTENSIONS = (".txt", ".md", ".json", ".csv", ".html", ".htm", ".vtt")


class MeetingReconciler:
    def __init__(
        self,
        repository: ObjectRepository,
        annotations: AnnotationStore,
        transcriptions: TranscriptionReconciler,
        pages: PagePublisher,
        settings: Settings,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.annotations = annotations
        self.transcriptions = transcriptions
        self.pages = pages
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.jira = JiraSnapshotSelector(repository)

    def run(self) -> dict[str, int]:
        objects = tuple(
            object_ref
            for object_ref in self.repository.list(self.settings.active_prefix)
            if object_ref.key.lower().endswith(".mp4")
        )
        sources: dict[str, SourceAnnotation] = {}
        advanced = 0
        for object_ref in objects:
            raw_source = self.annotations.get(object_ref, "mi.source")
            if raw_source is None:
                LOGGER.warning("MP4 has no mi.source annotation", extra={"key": object_ref.key})
                continue
            try:
                source = SourceAnnotation.from_dict(raw_source)
            except ContractError as exc:
                LOGGER.error("invalid mi.source annotation", extra={"key": object_ref.key, "error": str(exc)})
                continue
            sources[object_ref.key] = source
            current = self.annotations.get(object_ref, "mi.transcription")
            if not self.transcriptions.is_publishable(object_ref, source, current):
                if self.transcriptions.is_terminal_failure(object_ref, source, current):
                    continue
                if advanced >= self.settings.transcription.max_items_per_run:
                    continue
                self.transcriptions.reconcile(object_ref, source)
                advanced += 1

        ready = self._ready_occurrences(objects, sources)
        direct, aggregate_candidates = self._route(ready)
        published = 0
        for occurrence in direct:
            try:
                self._publish((occurrence,), title=f"Meeting — {occurrence.occurrence_id}")
                published += 1
            except ContextUnavailableError as exc:
                LOGGER.info("meeting waits for synchronized context", extra={"error": str(exc)})
        for aggregation in self._ready_aggregations(aggregate_candidates):
            try:
                self._publish(
                    aggregation["occurrences"],
                    title=f"Daily brief — {aggregation['profileId']} — {aggregation['businessDate']}",
                    aggregate=aggregation,
                )
                published += 1
            except ContextUnavailableError as exc:
                LOGGER.info("aggregate waits for synchronized context", extra={"error": str(exc)})
        return {
            "objectsScanned": len(objects),
            "transcriptionsAdvanced": advanced,
            "occurrencesReady": len(ready),
            "pagesPublished": published,
        }

    def _ready_occurrences(
        self,
        objects: Iterable[Any],
        sources: Mapping[str, SourceAnnotation],
    ) -> tuple[Occurrence, ...]:
        grouped: dict[str, list[Recording]] = defaultdict(list)
        for object_ref in objects:
            source = sources.get(object_ref.key)
            if source is None:
                continue
            state = self.annotations.get(object_ref, "mi.transcription")
            grouped[source.occurrence_id].append(
                Recording(object=object_ref, source=source, transcription=state)
            )
        ready: list[Occurrence] = []
        for occurrence_id, recordings in grouped.items():
            expected_sets = {tuple(sorted(recording.source.expected_mp4_file_ids)) for recording in recordings}
            fingerprints = {recording.source.recording_set_fingerprint for recording in recordings}
            present_ids = [recording.source.recording_file_id for recording in recordings]
            if len(expected_sets) != 1 or len(fingerprints) != 1 or len(present_ids) != len(set(present_ids)):
                LOGGER.error("inconsistent split recording annotations", extra={"occurrenceId": occurrence_id})
                continue
            expected = set(next(iter(expected_sets)))
            if set(present_ids) != expected:
                continue
            expected_fingerprint = f"sha256:{sha256(chr(10).join(sorted(expected)))}"
            if fingerprints != {expected_fingerprint}:
                LOGGER.error(
                    "recording-set fingerprint does not match expected file IDs",
                    extra={"occurrenceId": occurrence_id},
                )
                continue
            if not all(
                self.transcriptions.is_publishable(
                    recording.object,
                    recording.source,
                    recording.transcription,
                )
                for recording in recordings
            ):
                continue
            first = min(
                recordings,
                key=lambda item: datetime.fromisoformat(
                    item.source.recorded_at.replace("Z", "+00:00")
                ),
            )
            ready.append(
                Occurrence(
                    occurrence_id=occurrence_id,
                    source_prefix=first.source.source_prefix,
                    recorded_at=first.source.recorded_at,
                    metadata=first.source.metadata,
                    recordings=tuple(sorted(recordings, key=lambda item: item.source.recording_file_id)),
                    recording_set_fingerprint=first.source.recording_set_fingerprint,
                )
            )
        return tuple(sorted(ready, key=lambda occurrence: occurrence.occurrence_id))

    def _route(self, occurrences: Iterable[Occurrence]) -> tuple[list[Occurrence], list[Occurrence]]:
        direct: list[Occurrence] = []
        aggregate: list[Occurrence] = []
        for occurrence in occurrences:
            routing = occurrence.metadata.get("routing")
            pipeline = (
                routing.get("pipeline") if isinstance(routing, Mapping) else occurrence.metadata.get("pipeline")
            ) or "direct"
            if pipeline == "aggregation":
                aggregate.append(occurrence)
            else:
                direct.append(occurrence)
        return direct, aggregate

    def _ready_aggregations(self, occurrences: list[Occurrence]) -> list[dict[str, Any]]:
        profiles = {str(profile["id"]): profile for profile in self.settings.aggregation_profiles}
        grouped: dict[tuple[str, str], list[Occurrence]] = defaultdict(list)
        for occurrence in occurrences:
            routing = occurrence.metadata.get("routing")
            profile_id = str(
                (routing.get("aggregationProfile") if isinstance(routing, Mapping) else None)
                or occurrence.metadata.get("aggregationProfile")
                or occurrence.metadata.get("aggregation_profile")
                or ""
            )
            if profile_id not in profiles:
                LOGGER.error(
                    "aggregation occurrence has no configured profile",
                    extra={"occurrenceId": occurrence.occurrence_id, "profile": profile_id},
                )
                continue
            grouped[(profile_id, business_date(occurrence.recorded_at, self.settings.business_timezone))].append(
                occurrence
            )
        result: list[dict[str, Any]] = []
        for (profile_id, date), candidates in sorted(grouped.items()):
            profile = profiles[profile_id]
            key = f"aggregates/{profile_id}/{date}.json"
            existing = self.repository.head(key)
            if existing:
                receipt = json.loads(self.repository.read_bytes(key))
                member_ids = set(receipt.get("occurrenceIds", []))
                selected = [item for item in candidates if item.occurrence_id in member_ids]
                if len(selected) != len(member_ids):
                    continue
            else:
                by_team: dict[str, Occurrence] = {}
                for occurrence in sorted(candidates, key=lambda item: (item.recorded_at, item.occurrence_id)):
                    team = str(occurrence.metadata.get("teamId") or occurrence.metadata.get("team") or "")
                    if team:
                        by_team[team] = occurrence
                expected_teams = {str(team) for team in profile.get("expectedTeams", [])}
                complete = expected_teams and expected_teams.issubset(by_team)
                if not complete and not self._deadline_passed(date, str(profile["deadlineLocal"])):
                    continue
                membership_teams = expected_teams if expected_teams else set(by_team)
                selected = [by_team[team] for team in sorted(membership_teams) if team in by_team]
                if not selected:
                    continue
                receipt = {
                    "v": 1,
                    "profileId": profile_id,
                    "businessDate": date,
                    "expectedTeams": sorted(expected_teams),
                    "occurrenceIds": [item.occurrence_id for item in selected],
                    "complete": bool(complete),
                    "frozenAt": self._timestamp(),
                }
                payload = (canonical_json(receipt) + "\n").encode("utf-8")
                _, created = self.repository.write_bytes_if_absent(key, payload, "application/json")
                if not created:
                    receipt = json.loads(self.repository.read_bytes(key))
                    member_ids = set(receipt.get("occurrenceIds", []))
                    selected = [item for item in candidates if item.occurrence_id in member_ids]
            result.append(
                {
                    **receipt,
                    "receiptKey": key,
                    "occurrences": tuple(sorted(selected, key=lambda item: item.occurrence_id)),
                }
            )
        return result

    def _deadline_passed(self, business_date_value: str, local_time: str) -> bool:
        parsed_time = time.fromisoformat(local_time)
        zone = ZoneInfo(self.settings.business_timezone)
        deadline = datetime.combine(
            datetime.fromisoformat(business_date_value).date(),
            parsed_time,
            tzinfo=zone,
        )
        return self.now().astimezone(zone) >= deadline

    def _publish(
        self,
        occurrences: tuple[Occurrence, ...],
        *,
        title: str,
        aggregate: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        transcripts: dict[str, str] = {}
        jira_context: dict[str, Mapping[str, Any] | None] = {}
        sharepoint_context: dict[str, Iterable[Mapping[str, Any]]] = {}
        recording_associations: list[dict[str, Any]] = []
        for occurrence in occurrences:
            for recording in occurrence.recordings:
                transcripts[recording.source.recording_file_id] = vtt_to_text(
                    self.repository.read_bytes(recording.vtt_key)
                )
                current_object = self.repository.head(recording.object.key)
                if current_object is None:
                    raise ContextUnavailableError(
                        f"{recording.object.key} disappeared before its source association was frozen"
                    )
                if current_object.etag != recording.object.etag:
                    raise ContextUnavailableError(
                        f"{recording.object.key} changed before its source association was frozen"
                    )
                recording_associations.append(
                    {
                        "occurrenceId": occurrence.occurrence_id,
                        "recordingId": recording.source.recording_file_id,
                        "key": recording.object.key,
                        "objectIdentity": current_object.identity,
                        "durationSeconds": recording.source.duration_seconds,
                        "sourceFingerprint": recording.source.recording_set_fingerprint,
                    }
                )
            team_id = occurrence.metadata.get("teamId") or occurrence.metadata.get("team")
            jira_project = occurrence.metadata.get("jiraProject") or occurrence.metadata.get("jira_project")
            jira_team_id = str(team_id) if team_id else None
            if not jira_team_id and jira_project:
                matching_teams = [
                    str(team["teamId"])
                    for team in self.settings.jira.get("teams", [])
                    if str(team.get("projectKey")) == str(jira_project)
                ]
                if len(matching_teams) > 1:
                    raise ContextUnavailableError(
                        f"{occurrence.occurrence_id} Jira project {jira_project} maps to multiple configured teams"
                    )
                jira_team_id = matching_teams[0] if matching_teams else None
            selected = (
                self.jira.latest_at_or_before(jira_team_id, occurrence.recorded_at) if jira_team_id else None
            )
            if selected:
                key, payload = selected
                jira_context[occurrence.occurrence_id] = {"s3Key": key, **payload}
            else:
                jira_expected = bool(
                    jira_project
                    or any(str(team.get("teamId")) == str(team_id) for team in self.settings.jira.get("teams", []))
                )
                if jira_expected and self.settings.jira.get("required", True):
                    raise ContextUnavailableError(
                        f"{occurrence.occurrence_id} has no Jira snapshot at or before {occurrence.recorded_at}"
                    )
                jira_context[occurrence.occurrence_id] = {
                    "status": "missing" if jira_expected else "not-configured"
                }
            sharepoint_context[occurrence.occurrence_id] = self._sharepoint_context(occurrence)

        profile_ids = {
            str(
                (
                    occurrence.metadata.get("routing", {}).get("analysisProfile")
                    if isinstance(occurrence.metadata.get("routing"), Mapping)
                    else None
                )
                or occurrence.metadata.get("analysisProfile")
                or occurrence.metadata.get("analysis_profile")
                or "default"
            )
            for occurrence in occurrences
        }
        instructions = "\n\n".join(
            str(self.settings.analysis_profiles.get(profile, {}).get("instructions", ""))
            for profile in sorted(profile_ids)
        ).strip()
        markdown = render_rovo_input(
            occurrences,
            transcripts,
            jira_context,
            sharepoint_context,
            instructions,
        )
        association: dict[str, Any] = {
            "v": 1,
            "kind": "aggregate" if aggregate else "meeting",
            "bucket": self.repository.bucket,
            "sourcePrefix": occurrences[0].source_prefix if len(occurrences) == 1 else None,
            "occurrenceIds": [occurrence.occurrence_id for occurrence in occurrences],
            "recordings": recording_associations,
            "jiraSnapshotKeys": [
                context["s3Key"] for context in jira_context.values() if context and context.get("s3Key")
            ],
            "sharePointArtifacts": [
                {
                    "occurrenceId": occurrence_id,
                    **{
                        key: value
                        for key, value in artifact.items()
                        if key != "text"
                    },
                }
                for occurrence_id, artifacts in sharepoint_context.items()
                for artifact in artifacts
            ],
            "resultTitle": "MI result — {{inputPageId}}",
        }
        if aggregate:
            association["aggregateReceiptKey"] = aggregate["receiptKey"]
            association["aggregationProfile"] = aggregate["profileId"]
        request_revision = fingerprint({"markdown": markdown, "association": association}).split(":", 1)[1][:12]
        association["requestRevision"] = request_revision
        revision_title = f"{title} — r{request_revision}"
        labels = {"automation-ai-request", "status-ready-for-ai"}
        labels.update(label_token(item["recordingId"]) for item in recording_associations)
        page = self.pages.ensure_page(
            title=revision_title,
            parent_id=self.settings.confluence.input_parent_id,
            markdown=markdown,
            labels=labels,
            property_name="meeting-intelligence",
            property_value=association,
        )
        annotation = {
            "v": 1,
            "state": "published",
            "pageId": str(page["id"]),
            "pageVersion": int(page["version"]),
            "contentFingerprint": page["contentFingerprint"],
            "updated": self._timestamp(),
        }
        for occurrence in occurrences:
            for recording in occurrence.recordings:
                self.annotations.put(recording.object, "mi.confluence", annotation)
        return page

    def _sharepoint_context(self, occurrence: Occurrence) -> list[Mapping[str, Any]]:
        profile_name = occurrence.metadata.get("sharePointProfile") or occurrence.metadata.get(
            "sharepoint_profile"
        )
        if not profile_name:
            return []
        profile = self.settings.inbound_sharepoint_profiles.get(str(profile_name))
        if not isinstance(profile, Mapping):
            raise ContextUnavailableError(
                f"{occurrence.occurrence_id} names undefined SharePoint profile {profile_name}"
            )
        max_documents = int(profile.get("maxDocuments", 20))
        max_bytes = int(profile.get("maxBytesPerDocument", 200_000))
        max_age_hours = float(profile.get("maxAgeHours", 168))
        objects = []
        for prefix in profile["prefixes"]:
            objects.extend(
                item
                for item in self.repository.list(str(prefix))
                if item.key.lower().endswith("/metadata.json") or item.key.lower().endswith(".metadata.json")
            )
        objects.sort(key=lambda item: ((item.last_modified or datetime.min.replace(tzinfo=timezone.utc)), item.key), reverse=True)
        result: list[Mapping[str, Any]] = []
        for object_ref in objects:
            try:
                metadata = json.loads(self.repository.read_bytes(object_ref.key))
                required = ("siteId", "driveId", "itemId", "version", "name", "webUrl", "syncedAt", "contentKey")
                if (
                    not isinstance(metadata, Mapping)
                    or metadata.get("status") != "synchronized"
                    or any(not metadata.get(field) for field in required)
                ):
                    raise ValueError("missing required synchronized-item provenance")
                content_key = str(metadata["contentKey"])
                if not any(content_key.startswith(str(prefix)) for prefix in profile["prefixes"]):
                    raise ValueError("contentKey is outside the configured profile prefixes")
                if not content_key.lower().endswith(TEXT_EXTENSIONS):
                    raise ValueError("contentKey is not a supported text format")
                content_object = self.repository.head(content_key)
                if content_object is None or content_object.size > max_bytes:
                    raise ValueError("content object is missing or exceeds maxBytesPerDocument")
                synced_at = datetime.fromisoformat(str(metadata["syncedAt"]).replace("Z", "+00:00"))
                if synced_at.tzinfo is None:
                    raise ValueError("syncedAt must include a timezone")
                now_utc = self.now().astimezone(timezone.utc)
                synced_utc = synced_at.astimezone(timezone.utc)
                age_hours = (now_utc - synced_utc).total_seconds() / 3600
                if age_hours < -(5 / 60):
                    raise ValueError("syncedAt is in the future")
                if age_hours > max_age_hours:
                    raise ValueError("synchronized item is stale")
                raw = self.repository.read_bytes(content_key).decode("utf-8", errors="replace")
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                LOGGER.warning(
                    "ignoring invalid inbound SharePoint item",
                    extra={"key": object_ref.key, "error": str(exc)},
                )
                continue
            if content_key.lower().endswith((".html", ".htm")):
                raw = html.unescape(re.sub(r"<[^>]*>", " ", raw))
                raw = re.sub(r"\s+", " ", raw)
            result.append(
                {
                    "key": content_key,
                    "metadataKey": object_ref.key,
                    "name": metadata["name"],
                    "objectIdentity": content_object.identity,
                    "siteId": metadata["siteId"],
                    "driveId": metadata["driveId"],
                    "itemId": metadata["itemId"],
                    "version": metadata["version"],
                    "webUrl": metadata["webUrl"],
                    "syncedAt": metadata["syncedAt"],
                    "text": raw,
                }
            )
            if len(result) >= max_documents:
                break
        if profile.get("required", False) and not result:
            raise ContextUnavailableError(
                f"{occurrence.occurrence_id} requires SharePoint profile {profile_name}, but no current item is available"
            )
        return result

    def _timestamp(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
