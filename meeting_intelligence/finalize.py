"""Single-pass Rovo result finalization."""

from __future__ import annotations

import html
import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .config import Settings
from .errors import ResultValidationError
from .ids import canonical_json, clip_filename, label_token, safe_path_token
from .models import AnalysisResult
from .ports import ClipRenderer, ConfluenceGateway, ObjectRepository, SharePointGateway
from .rendering import render_final_report

LOGGER = logging.getLogger(__name__)
INPUT_TITLE = re.compile(r"^MI result\s+[—-]\s+(\d+)$")
TAG = re.compile(r"<[^>]+>")
CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def extract_result_json(storage_value: str) -> Mapping[str, Any]:
    candidates = CDATA.findall(storage_value)
    candidates.append(html.unescape(TAG.sub("", storage_value)))
    for candidate in candidates:
        text = html.unescape(candidate).strip()
        try:
            value = json.loads(text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, Mapping):
            return value
    raise ResultValidationError("result page does not contain one parseable analysis JSON object")


class AnalysisFinalizer:
    def __init__(
        self,
        repository: ObjectRepository,
        confluence: ConfluenceGateway,
        sharepoint: SharePointGateway,
        media: ClipRenderer,
        settings: Settings,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.confluence = confluence
        self.sharepoint = sharepoint
        self.media = media
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))

    def run(self) -> dict[str, int]:
        counts = {"examined": 0, "completed": 0, "alreadyComplete": 0, "invalid": 0}
        for page in self.confluence.find_result_pages():
            counts["examined"] += 1
            outcome = self._process(page)
            counts[outcome] += 1
        return counts

    def _process(self, page: Mapping[str, Any]) -> str:
        page_id = str(page["id"])
        version = int((page.get("version") or {}).get("number", 0))
        receipt = self.confluence.get_property(page_id, "meeting-intelligence-completion")
        if (
            receipt
            and (receipt.get("value") or {}).get("state") == "done"
            and int((receipt.get("value") or {}).get("resultVersion", -1)) == version
        ):
            self.confluence.add_labels(page_id, ["status-analysis-done"])
            return "alreadyComplete"
        failure = self.confluence.get_property(page_id, "meeting-intelligence-finalization")
        if (
            failure
            and (failure.get("value") or {}).get("state") == "failed"
            and int((failure.get("value") or {}).get("resultVersion", -1)) == version
        ):
            return "invalid"
        try:
            return self._finalize(page, page_id, version)
        except ResultValidationError as exc:
            self.confluence.put_property(
                page_id,
                "meeting-intelligence-finalization",
                {
                    "state": "failed",
                    "resultVersion": version,
                    "reason": str(exc),
                    "updatedAt": self._timestamp(),
                },
            )
            LOGGER.error("Rovo result failed validation", extra={"pageId": page_id, "error": str(exc)})
            return "invalid"

    def _finalize(self, page: Mapping[str, Any], page_id: str, version: int) -> str:
        input_page_id = self._input_page_id(page)
        source_property = self.confluence.get_property(input_page_id, "meeting-intelligence")
        if not source_property or not isinstance(source_property.get("value"), Mapping):
            raise ResultValidationError(f"input page {input_page_id} has no meeting-intelligence property")
        association = source_property["value"]
        if association.get("bucket") != self.repository.bucket:
            raise ResultValidationError("input association bucket does not match the configured artifact bucket")
        recordings_value = association.get("recordings")
        if not isinstance(recordings_value, list) or not recordings_value:
            raise ResultValidationError("input association contains no source recordings")
        recordings = {
            str(item.get("recordingId")): item
            for item in recordings_value
            if isinstance(item, Mapping) and item.get("recordingId")
        }
        if len(recordings) != len(recordings_value):
            raise ResultValidationError("input association recordings must have unique recordingId values")
        body = ((page.get("body") or {}).get("storage") or {}).get("value")
        if not isinstance(body, str):
            latest = self.confluence.get_page(page_id)
            body = ((latest.get("body") or {}).get("storage") or {}).get("value")
        if not isinstance(body, str):
            raise ResultValidationError("result page has no Confluence storage body")
        result = AnalysisResult.from_dict(
            extract_result_json(body),
            max_demos=self.settings.finalization.max_demos,
        )

        occurrence_ids = association.get("occurrenceIds")
        if not isinstance(occurrence_ids, list) or not all(isinstance(item, str) for item in occurrence_ids):
            raise ResultValidationError("input association occurrenceIds is invalid")
        occurrence_id_set = set(occurrence_ids)
        for recording_id, recording in recordings.items():
            if recording.get("occurrenceId") not in occurrence_id_set:
                raise ResultValidationError(
                    f"recording {recording_id} belongs to an occurrence outside the input association"
                )
        clip_references: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="mi-finalize-") as directory_name:
            directory = Path(directory_name)
            Path(directory, "analysis.json").write_text(
                canonical_json(result.raw) + "\n",
                encoding="utf-8",
            )
            downloaded: dict[str, tuple[Path, float]] = {}
            for demo in result.demos:
                source = recordings.get(demo.source_recording_id)
                if not source:
                    raise ResultValidationError(
                        f"demo references recording outside the associated occurrence: {demo.source_recording_id}"
                    )
                source_key = source.get("key")
                if not isinstance(source_key, str) or not source_key.lower().endswith(".mp4"):
                    raise ResultValidationError(f"recording {demo.source_recording_id} has an invalid S3 key")
                if demo.source_recording_id not in downloaded:
                    current_object = self.repository.head(source_key)
                    if current_object is None:
                        raise ResultValidationError(f"source recording no longer exists: {source_key}")
                    if current_object.identity != source.get("objectIdentity"):
                        raise ResultValidationError(
                            f"source recording revision changed after Rovo input: {demo.source_recording_id}"
                        )
                    local_source = Path(directory, f"source-{len(downloaded)}.mp4")
                    self.repository.download(source_key, local_source)
                    verified_object = self.repository.head(source_key)
                    if verified_object is None or verified_object.identity != source.get("objectIdentity"):
                        raise ResultValidationError(
                            f"source recording changed while it was downloaded: {demo.source_recording_id}"
                        )
                    downloaded[demo.source_recording_id] = (local_source, self.media.duration(local_source))
                local_source, actual_duration = downloaded[demo.source_recording_id]
                if demo.end_seconds > actual_duration + 0.001:
                    raise ResultValidationError(
                        f"demo {demo.title!r} ends at {demo.end_seconds}, beyond recording duration {actual_duration:.3f}"
                    )
                filename = clip_filename(
                    demo.source_recording_id,
                    demo.start_seconds,
                    demo.end_seconds,
                    version,
                )
                local_clip = Path(directory, filename)
                self.media.render(
                    local_source,
                    local_clip,
                    demo.start_seconds,
                    demo.end_seconds,
                )
                occurrence_id = str(source.get("occurrenceId") or occurrence_ids[0])
                remote_path = str(
                    PurePosixPath(
                        self.settings.finalization.sharepoint_folder,
                        safe_path_token(occurrence_id),
                        filename,
                    )
                )
                reference = dict(self.sharepoint.ensure_file(remote_path, local_clip))
                reference["title"] = demo.title
                reference["sourceRecordingId"] = demo.source_recording_id
                reference["startSeconds"] = demo.start_seconds
                reference["endSeconds"] = demo.end_seconds
                clip_references.append(reference)

            final_markdown = render_final_report(result, occurrence_ids, clip_references)
            final_page = self.confluence.ensure_page(
                title=f"Meeting analysis — {input_page_id}",
                parent_id=self.settings.confluence.final_parent_id,
                markdown=final_markdown,
                labels={
                    "meeting-intelligence-analysis",
                    "status-analysis-final",
                    *(label_token(recording_id) for recording_id in recordings),
                },
                property_name="meeting-intelligence",
                property_value={
                    "v": 1,
                    "kind": "final-analysis",
                    "inputPageId": input_page_id,
                    "resultPageId": page_id,
                    "resultVersion": version,
                    "occurrenceIds": occurrence_ids,
                },
            )

        latest = self.confluence.get_page(page_id)
        latest_version = int((latest.get("version") or {}).get("number", -1))
        if latest_version != version:
            raise RuntimeError(
                f"Rovo result page {page_id} changed from version {version} to {latest_version} during finalization"
            )
        completion = {
            "state": "done",
            "resultVersion": version,
            "finalPageId": str(final_page["id"]),
            "sharePointClips": [
                {
                    "driveId": reference["driveId"],
                    "itemId": reference["itemId"],
                    "webUrl": reference["webUrl"],
                }
                for reference in clip_references
            ],
            "completedAt": self._timestamp(),
        }
        self.confluence.put_property(page_id, "meeting-intelligence-completion", completion)
        confirmed = self.confluence.get_property(page_id, "meeting-intelligence-completion")
        if not confirmed or confirmed.get("value") != completion:
            raise RuntimeError(f"completion receipt was not confirmed for result page {page_id}")
        self.confluence.add_labels(page_id, ["status-analysis-done"])
        return "completed"

    def _input_page_id(self, page: Mapping[str, Any]) -> str:
        page_id = str(page["id"])
        source = self.confluence.get_property(page_id, "meeting-intelligence-source")
        if source and isinstance(source.get("value"), Mapping):
            value = source["value"].get("inputPageId")
            if value:
                return str(value)
        match = INPUT_TITLE.match(str(page.get("title", "")).strip())
        if not match:
            raise ResultValidationError(
                "result must have meeting-intelligence-source.inputPageId or title 'MI result — <input-page-id>'"
            )
        return match.group(1)

    def _timestamp(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
