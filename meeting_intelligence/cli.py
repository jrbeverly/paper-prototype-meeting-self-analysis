"""Command-line entry points for the three Python CodeBuild jobs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from .config import add_config_argument, load_secret, load_settings
from .confluence import Confluence
from .finalize import AnalysisFinalizer
from .graph import GraphSharePoint
from .jira import Acli, JiraSnapshotter
from .media import Ffmpeg
from .reconcile import MeetingReconciler
from .storage import BotoTranscribeService, S3AnnotationStore, S3Repository
from .transcription import TranscriptionReconciler


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("key", "pageId", "occurrenceId", "profile", "error"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _logging(verbose: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="meeting-intelligence")
    root.add_argument("--verbose", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)

    jira = commands.add_parser("jira-snapshot", help="capture active-sprint snapshots")
    add_config_argument(jira)
    jira.add_argument("--team", help="capture one configured team")

    reconcile = commands.add_parser(
        "meeting-reconcile",
        help="advance transcription and publish ready Confluence inputs",
    )
    add_config_argument(reconcile)

    finalize = commands.add_parser(
        "analysis-finalize",
        help="finalize unfinished Rovo result pages",
    )
    add_config_argument(finalize)

    validate = commands.add_parser("validate-config", help="validate configuration without external calls")
    add_config_argument(validate)
    return root


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    _logging(args.verbose)
    try:
        settings = load_settings(args.config)
        if args.command == "validate-config":
            result: Any = {"valid": True, "artifactBucket": settings.artifact_bucket}
        elif args.command == "jira-snapshot":
            repository = S3Repository(settings.artifact_bucket)
            runner = Acli(
                load_secret("MI_JIRA_SECRET_JSON"),
                executable=str(settings.jira.get("acliCommand") or os.getenv("MI_ACLI_PATH", "acli")),
            )
            result = {
                "written": JiraSnapshotter(repository, runner, settings.jira).run(args.team),
            }
        elif args.command == "meeting-reconcile":
            repository = S3Repository(settings.artifact_bucket)
            annotations = S3AnnotationStore(repository.client)
            confluence = Confluence(settings.confluence, load_secret("MI_CONFLUENCE_SECRET_JSON"))
            transcriptions = TranscriptionReconciler(
                repository,
                annotations,
                BotoTranscribeService(),
                settings.transcription,
            )
            result = MeetingReconciler(
                repository,
                annotations,
                transcriptions,
                confluence,
                settings,
            ).run()
        elif args.command == "analysis-finalize":
            repository = S3Repository(settings.artifact_bucket)
            confluence = Confluence(settings.confluence, load_secret("MI_CONFLUENCE_SECRET_JSON"))
            graph = GraphSharePoint(
                load_secret("MI_GRAPH_SECRET_JSON"),
                site_id=settings.finalization.sharepoint_site_id,
                drive_id=settings.finalization.sharepoint_drive_id,
                simple_upload_limit_bytes=settings.finalization.simple_upload_limit_bytes,
                upload_chunk_bytes=settings.finalization.upload_chunk_bytes,
            )
            media = Ffmpeg(
                settings.finalization.ffmpeg_command,
                settings.finalization.ffprobe_command,
            )
            result = AnalysisFinalizer(repository, confluence, graph, media, settings).run()
        else:  # pragma: no cover - argparse enforces the command set
            raise AssertionError(args.command)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        logging.getLogger(__name__).exception("command failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
