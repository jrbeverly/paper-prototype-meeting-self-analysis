"""Protocols isolate reconciliation logic from AWS and SaaS transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .models import ObjectRef


class ObjectRepository(Protocol):
    bucket: str

    def list(self, prefix: str) -> Iterable[ObjectRef]: ...

    def head(self, key: str) -> ObjectRef | None: ...

    def read_bytes(self, key: str) -> bytes: ...

    def write_bytes(self, key: str, value: bytes, content_type: str) -> ObjectRef: ...

    def write_bytes_if_absent(self, key: str, value: bytes, content_type: str) -> tuple[ObjectRef, bool]: ...

    def download(self, key: str, destination: Path) -> None: ...


class AnnotationStore(Protocol):
    def get(self, object_ref: ObjectRef, name: str) -> Mapping[str, Any] | None: ...

    def put(self, object_ref: ObjectRef, name: str, value: Mapping[str, Any]) -> None: ...


class TranscribeService(Protocol):
    def get_job(self, name: str) -> Mapping[str, Any] | None: ...

    def start_job(self, name: str, media_uri: str, configuration: Mapping[str, Any]) -> None: ...

    def download_subtitle(self, uri: str) -> bytes: ...


class PagePublisher(Protocol):
    def ensure_page(
        self,
        *,
        title: str,
        parent_id: str,
        markdown: str,
        labels: Iterable[str],
        property_name: str,
        property_value: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ConfluenceGateway(PagePublisher, Protocol):
    def find_result_pages(self) -> Iterable[Mapping[str, Any]]: ...

    def get_page(self, page_id: str) -> Mapping[str, Any]: ...

    def get_property(self, page_id: str, name: str) -> Mapping[str, Any] | None: ...

    def put_property(self, page_id: str, name: str, value: Mapping[str, Any]) -> None: ...

    def add_labels(self, page_id: str, labels: Iterable[str]) -> None: ...


class ClipRenderer(Protocol):
    def duration(self, source: Path) -> float: ...

    def render(self, source: Path, destination: Path, start_seconds: float, end_seconds: float) -> None: ...


class SharePointGateway(Protocol):
    drive_id: str

    def ensure_file(self, remote_path: str, local_path: Path) -> Mapping[str, Any]: ...
