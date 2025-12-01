"""Microsoft Graph client for deterministic SharePoint clip uploads."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote

import requests


class GraphSharePoint:
    def __init__(
        self,
        secret: Mapping[str, Any],
        *,
        site_id: str,
        drive_id: str,
        simple_upload_limit_bytes: int,
        upload_chunk_bytes: int,
        session: requests.Session | None = None,
    ):
        for field in ("tenantId", "clientId", "clientSecret"):
            if not secret.get(field):
                raise ValueError(f"Graph secret requires {field}")
        self.secret = secret
        self.site_id = site_id
        self.drive_id = drive_id
        self.simple_upload_limit_bytes = simple_upload_limit_bytes
        self.upload_chunk_bytes = upload_chunk_bytes
        self.session = session or requests.Session()
        token = self._token()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.root = (
            "https://graph.microsoft.com/v1.0/sites/"
            f"{quote(site_id, safe=',')}/drives/{quote(drive_id, safe='')}"
        )

    def _token(self) -> str:
        response = requests.post(
            f"https://login.microsoftonline.com/{quote(str(self.secret['tenantId']), safe='')}/oauth2/v2.0/token",
            data={
                "client_id": self.secret["clientId"],
                "client_secret": self.secret["clientSecret"],
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=(10, 60),
        )
        response.raise_for_status()
        return str(response.json()["access_token"])

    def ensure_file(self, remote_path: str, local_path: Path) -> Mapping[str, Any]:
        normalized = str(PurePosixPath(remote_path.strip("/")))
        self._ensure_folders(str(PurePosixPath(normalized).parent))
        existing = self._get_path(normalized)
        size = local_path.stat().st_size
        hashes = ((existing or {}).get("file") or {}).get("hashes") or {}
        remote_sha1 = hashes.get("sha1Hash")
        if (
            existing
            and int(existing.get("size", -1)) == size
            and remote_sha1
            and str(remote_sha1).lower() == _sha1(local_path)
        ):
            return self._reference(existing, normalized)
        if size <= self.simple_upload_limit_bytes:
            item = self._simple_upload(normalized, local_path)
        else:
            item = self._resumable_upload(normalized, local_path)
        return self._reference(item, normalized)

    def _path_url(self, remote_path: str, suffix: str = "") -> str:
        encoded = quote(remote_path, safe="/")
        return f"{self.root}/root:/{encoded}:{suffix}"

    def _get_path(self, remote_path: str) -> Mapping[str, Any] | None:
        response = self.session.get(self._path_url(remote_path), timeout=(10, 60))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _ensure_folders(self, folder_path: str) -> None:
        if folder_path in {"", "."}:
            return
        current = ""
        for segment in PurePosixPath(folder_path).parts:
            current = f"{current}/{segment}".strip("/")
            if self._get_path(current):
                continue
            parent = str(PurePosixPath(current).parent)
            if parent == ".":
                endpoint = f"{self.root}/root/children"
            else:
                endpoint = self._path_url(parent, "/children")
            response = self.session.post(
                endpoint,
                json={
                    "name": segment,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
                timeout=(10, 60),
            )
            if response.status_code == 409 and self._get_path(current):
                continue
            response.raise_for_status()

    def _simple_upload(self, remote_path: str, local_path: Path) -> Mapping[str, Any]:
        with local_path.open("rb") as stream:
            response = self.session.put(
                self._path_url(remote_path, "/content"),
                data=stream,
                headers={"Content-Type": "video/mp4"},
                timeout=(10, 600),
            )
        response.raise_for_status()
        return response.json()

    def _resumable_upload(self, remote_path: str, local_path: Path) -> Mapping[str, Any]:
        response = self.session.post(
            self._path_url(remote_path, "/createUploadSession"),
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=(10, 60),
        )
        response.raise_for_status()
        upload_url = str(response.json()["uploadUrl"])
        total = local_path.stat().st_size
        offset = 0
        result: Mapping[str, Any] | None = None
        with local_path.open("rb") as stream:
            while offset < total:
                chunk = stream.read(self.upload_chunk_bytes)
                if not chunk:
                    raise RuntimeError("local clip ended before advertised size")
                end = offset + len(chunk) - 1
                part = requests.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total}",
                    },
                    timeout=(10, 600),
                )
                part.raise_for_status()
                if part.status_code in {200, 201}:
                    result = part.json()
                elif part.status_code != 202:
                    raise RuntimeError(f"unexpected upload-session response: {part.status_code}")
                offset = end + 1
        if result is None:
            result = self._get_path(remote_path)
        if result is None:
            raise RuntimeError("Graph upload session completed without a driveItem")
        return result

    def _reference(self, item: Mapping[str, Any], remote_path: str) -> dict[str, Any]:
        if not item.get("id") or not item.get("webUrl"):
            raise RuntimeError("Graph driveItem lacks stable id or webUrl")
        return {
            "driveId": self.drive_id,
            "itemId": str(item["id"]),
            "webUrl": str(item["webUrl"]),
            "name": str(item.get("name") or PurePosixPath(remote_path).name),
            "size": int(item.get("size", 0)),
            "remotePath": remote_path,
        }


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
