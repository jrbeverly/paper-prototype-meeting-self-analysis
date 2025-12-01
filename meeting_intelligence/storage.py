"""S3 object and Object Annotation adapters."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import AnnotationConflictError
from .models import ObjectRef


def _etag(value: str | None) -> str:
    return value or ""


class S3Repository:
    def __init__(self, bucket: str, client: Any | None = None):
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket = bucket
        self.client = client

    def list(self, prefix: str) -> Iterable[ObjectRef]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield ObjectRef(
                    bucket=self.bucket,
                    key=item["Key"],
                    etag=_etag(item.get("ETag")),
                    size=int(item.get("Size", 0)),
                    last_modified=item.get("LastModified"),
                )

    def head(self, key: str) -> ObjectRef | None:
        try:
            item = self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            response = getattr(exc, "response", {})
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            etag=_etag(item.get("ETag")),
            size=int(item.get("ContentLength", 0)),
            version_id=item.get("VersionId"),
            last_modified=item.get("LastModified"),
        )

    def read_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def write_bytes(self, key: str, value: bytes, content_type: str) -> ObjectRef:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=value, ContentType=content_type)
        result = self.head(key)
        if result is None:  # pragma: no cover - S3 consistency invariant
            raise RuntimeError(f"S3 did not return freshly written object {key}")
        return result

    def write_bytes_if_absent(self, key: str, value: bytes, content_type: str) -> tuple[ObjectRef, bool]:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=value,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            created = True
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if status != 412 and code not in {"PreconditionFailed", "412"}:
                raise
            created = False
        result = self.head(key)
        if result is None:  # pragma: no cover - S3 consistency invariant
            raise RuntimeError(f"S3 conditional write did not yield an object: {key}")
        return result, created

    def download(self, key: str, destination: Path) -> None:
        self.client.download_file(self.bucket, key, str(destination))


class S3AnnotationStore:
    """Uses a current boto3 client, falling back to the documented AWS CLI.

    Managed images can lag the S3 model that introduced annotations. The
    buildspec installs a current CLI; this adapter remains functional during
    that rollout window.
    """

    def __init__(self, client: Any | None = None, aws_command: str = "aws"):
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client
        self.aws_command = aws_command
        self._sdk_supported = hasattr(client, "get_object_annotation") and hasattr(client, "put_object_annotation")

    def get(self, object_ref: ObjectRef, name: str) -> Mapping[str, Any] | None:
        if self._sdk_supported:
            request: dict[str, Any] = {
                "Bucket": object_ref.bucket,
                "Key": object_ref.key,
                "AnnotationName": name,
            }
            if object_ref.version_id:
                request["VersionId"] = object_ref.version_id
            try:
                result = self.client.get_object_annotation(**request)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code in {"NoSuchAnnotation", "NoSuchKey", "404"}:
                    return None
                raise
            payload = result["AnnotationPayload"].read()
            return _annotation_json(payload, object_ref, name)
        return self._cli_get(object_ref, name)

    def put(self, object_ref: ObjectRef, name: str, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if not payload or len(payload) > 1024 * 1024:
            raise ValueError("annotation payload must be between 1 byte and 1 MiB")
        if self._sdk_supported:
            request: dict[str, Any] = {
                "Bucket": object_ref.bucket,
                "Key": object_ref.key,
                "AnnotationName": name,
                "AnnotationPayload": payload,
                "ObjectIfMatch": object_ref.etag,
            }
            if object_ref.version_id:
                request["VersionId"] = object_ref.version_id
            try:
                self.client.put_object_annotation(**request)
            except Exception as exc:
                status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if status == 412 or code in {"PreconditionFailed", "412"}:
                    raise AnnotationConflictError(f"S3 object changed before {name} was written") from exc
                raise
            return
        self._cli_put(object_ref, name, payload)

    def _cli_get(self, object_ref: ObjectRef, name: str) -> Mapping[str, Any] | None:
        with tempfile.TemporaryDirectory(prefix="mi-annotation-") as directory:
            output = Path(directory, "annotation.json")
            command = [
                self.aws_command,
                "s3api",
                "get-object-annotation",
                "--bucket",
                object_ref.bucket,
                "--key",
                object_ref.key,
                "--annotation-name",
                name,
            ]
            if object_ref.version_id:
                command.extend(("--version-id", object_ref.version_id))
            command.append(str(output))
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                if "NoSuchAnnotation" in completed.stderr or "NoSuchKey" in completed.stderr:
                    return None
                raise RuntimeError(f"get-object-annotation failed: {completed.stderr.strip()}")
            return _annotation_json(output.read_bytes(), object_ref, name)

    def _cli_put(self, object_ref: ObjectRef, name: str, payload: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="mi-annotation-") as directory:
            source = Path(directory, "annotation.json")
            source.write_bytes(payload)
            command = [
                self.aws_command,
                "s3api",
                "put-object-annotation",
                "--bucket",
                object_ref.bucket,
                "--key",
                object_ref.key,
                "--annotation-name",
                name,
                "--annotation-payload",
                str(source),
                "--object-if-match",
                object_ref.etag,
            ]
            if object_ref.version_id:
                command.extend(("--version-id", object_ref.version_id))
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                if "PreconditionFailed" in completed.stderr or "412" in completed.stderr:
                    raise AnnotationConflictError(f"S3 object changed before {name} was written")
                raise RuntimeError(f"put-object-annotation failed: {completed.stderr.strip()}")


def _annotation_json(payload: bytes, object_ref: ObjectRef, name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{object_ref.key} annotation {name} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{object_ref.key} annotation {name} must contain a JSON object")
    return value


class BotoTranscribeService:
    def __init__(self, client: Any | None = None):
        if client is None:
            import boto3

            client = boto3.client("transcribe")
        self.client = client

    def get_job(self, name: str) -> Mapping[str, Any] | None:
        try:
            return self.client.get_transcription_job(TranscriptionJobName=name)["TranscriptionJob"]
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"NotFoundException", "404"}:
                return None
            raise

    def start_job(self, name: str, media_uri: str, configuration: Mapping[str, Any]) -> None:
        request: dict[str, Any] = {
            "TranscriptionJobName": name,
            "Media": {"MediaFileUri": media_uri},
            "MediaFormat": "mp4",
            "LanguageCode": configuration["languageCode"],
            "Subtitles": {"Formats": ["vtt"], "OutputStartIndex": 1},
        }
        if vocabulary := configuration.get("vocabularyName"):
            request["Settings"] = {"VocabularyName": vocabulary}
        try:
            self.client.start_transcription_job(**request)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code != "ConflictException":
                raise

    def download_subtitle(self, uri: str) -> bytes:
        import requests

        response = requests.get(uri, timeout=(10, 120))
        response.raise_for_status()
        return response.content
