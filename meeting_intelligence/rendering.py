"""Deterministic Markdown rendering for Rovo input and final reports."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable, Mapping

from .ids import canonical_json
from .models import AnalysisResult, Occurrence

TIMING = re.compile(
    r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[.,]\d{3}(?:\s+.*)?$"
)
TAG = re.compile(r"<[^>]+>")

ROVO_RESULT_CONTRACT: dict[str, Any] = {
    "schemaVersion": "1.0",
    "title": "Short report title (optional)",
    "summary": "Required concise factual summary",
    "decisions": ["Decision strings or structured decision objects"],
    "actionItems": ["Action strings or structured action objects"],
    "risks": ["Risk strings or structured risk objects"],
    "demos": [
        {
            "sourceRecordingId": "one exact recording ID listed on this page",
            "startSeconds": 12.3,
            "endSeconds": 47.8,
            "title": "Short clip title",
        }
    ],
}


def vtt_to_text(payload: bytes | str) -> str:
    source = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    lines: list[str] = []
    for raw in source.replace("\ufeff", "").splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or TIMING.match(line) or line.isdigit():
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        text = html.unescape(TAG.sub("", line)).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def _quoted_block(value: str) -> str:
    if not value:
        return "> _(No speech was transcribed.)_"
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def _json_block(value: Any) -> str:
    return f"```json\n{json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)}\n```"


def render_rovo_input(
    occurrences: Iterable[Occurrence],
    transcript_by_recording: Mapping[str, str],
    jira_by_occurrence: Mapping[str, Mapping[str, Any] | None],
    sharepoint_by_occurrence: Mapping[str, Iterable[Mapping[str, Any]]],
    instructions: str,
) -> str:
    items = tuple(occurrences)
    title = items[0].occurrence_id if len(items) == 1 else f"{items[0].recorded_at[:10]} aggregate"
    sections = [
        f"# Meeting intelligence input — {title}",
        "",
        "Analyze only the source material rendered on this page. Do not infer facts from inaccessible S3 links.",
        "",
        "## Analysis instructions",
        "",
        instructions.strip() or "Summarize the meeting and identify decisions, actions, risks, and useful demonstrations.",
    ]
    for occurrence in items:
        sections.extend(
            [
                "",
                f"## Occurrence `{occurrence.occurrence_id}`",
                "",
                f"- Recorded at: `{occurrence.recorded_at}`",
                f"- Source fingerprint: `{occurrence.recording_set_fingerprint}`",
                "",
                "### Normalized meeting metadata",
                "",
                _json_block(occurrence.metadata),
                "",
                "### Transcripts",
            ]
        )
        for recording in occurrence.recordings:
            recording_id = recording.source.recording_file_id
            sections.extend(
                [
                    "",
                    f"#### Recording `{recording_id}`",
                    "",
                    f"- Duration: `{recording.source.duration_seconds:.3f}` seconds",
                    f"- Source object: `s3://{recording.object.bucket}/{recording.object.key}`",
                    "",
                    _quoted_block(transcript_by_recording.get(recording_id, "")),
                ]
            )
        jira = jira_by_occurrence.get(occurrence.occurrence_id)
        sections.extend(["", "### Jira state at meeting start", "", _json_block(jira or {"status": "missing"})])
        source_documents = list(sharepoint_by_occurrence.get(occurrence.occurrence_id, ()))
        sections.extend(["", "### Synchronized SharePoint context", ""])
        if not source_documents:
            sections.append("_No SharePoint profile was configured for this occurrence._")
        for document in source_documents:
            sections.extend(
                [
                    f"#### {document.get('name') or document.get('key') or 'Supporting document'}",
                    "",
                    f"Source: `{document.get('key', 'unknown')}`",
                    "",
                    _quoted_block(str(document.get("text", ""))),
                    "",
                ]
            )
    sections.extend(
        [
            "",
            "## Required response",
            "",
            "Return exactly one JSON object and no prose or Markdown fences. Use this contract:",
            "",
            _json_block(ROVO_RESULT_CONTRACT),
            "",
            "Every demo timestamp must refer to one exact recording ID shown above.",
        ]
    )
    return "\n".join(sections).rstrip() + "\n"


def render_final_report(
    result: AnalysisResult,
    occurrence_ids: Iterable[str],
    clip_references: Iterable[Mapping[str, Any]],
) -> str:
    title = result.title or f"Meeting analysis — {', '.join(occurrence_ids)}"
    lines = [f"# {title}", "", result.summary, "", "## Source occurrences", ""]
    lines.extend(f"- `{occurrence_id}`" for occurrence_id in occurrence_ids)
    for heading, values in (
        ("Decisions", result.decisions),
        ("Action items", result.action_items),
        ("Risks", result.risks),
    ):
        lines.extend(["", f"## {heading}", ""])
        if not values:
            lines.append("_None reported._")
        else:
            for value in values:
                rendered = value if isinstance(value, str) else canonical_json(value)
                lines.append(f"- {rendered}")
    references = list(clip_references)
    lines.extend(["", "## Demonstration clips", ""])
    if not references:
        lines.append("_No clips were requested._")
    for reference in references:
        title_value = str(reference.get("title") or reference.get("itemId") or "Demo clip").replace("]", "\\]")
        url = str(reference.get("webUrl") or "")
        lines.append(f"- [{title_value}]({url})")
    lines.extend(
        [
            "",
            "## Analysis provenance",
            "",
            f"- Result schema: `{result.schema_version}`",
            "- Analysis source: the associated Confluence Rovo result page and version",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
