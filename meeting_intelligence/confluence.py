"""Confluence REST reconciliation and md2conf publication."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import requests

from .config import ConfluenceSettings
from .ids import canonical_json, fingerprint, safe_path_token


class Confluence:
    def __init__(
        self,
        settings: ConfluenceSettings,
        secret: Mapping[str, Any],
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self.secret = secret
        self.session = session or requests.Session()
        token = secret.get("apiToken") or secret.get("token")
        username = secret.get("email") or secret.get("username")
        if not token:
            raise ValueError("Confluence secret requires apiToken or token")
        if username:
            self.session.auth = (str(username), str(token))
        else:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"
        self.api_root = f"{settings.base_url}/rest/api"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.api_root}{path}", timeout=(10, 60), **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    def find_page(self, title: str, parent_id: str) -> Mapping[str, Any] | None:
        matches = []
        start = 0
        limit = 100
        while True:
            payload = self._request(
                "GET",
                "/content",
                params={
                    "title": title,
                    "spaceKey": self.settings.space_key,
                    "expand": "ancestors,version",
                    "start": start,
                    "limit": limit,
                },
            )
            results = payload.get("results", [])
            for page in results:
                ancestors = page.get("ancestors") or []
                if ancestors and str(ancestors[-1].get("id")) == str(parent_id):
                    matches.append(page)
            if len(results) < limit:
                break
            start += len(results)
        if len(matches) > 1:
            raise RuntimeError(
                f"ambiguous Confluence identity: {len(matches)} pages titled {title!r} under parent {parent_id}"
            )
        return matches[0] if matches else None

    def get_page(self, page_id: str) -> Mapping[str, Any]:
        return self._request(
            "GET",
            f"/content/{page_id}",
            params={"expand": "body.storage,version,metadata.labels,ancestors"},
        )

    def find_result_pages(self) -> Iterable[Mapping[str, Any]]:
        start = 0
        limit = 100
        result_parent = str(self.settings.result_parent_id).replace('"', "")
        space_key = self.settings.space_key.replace('"', '\\"')
        cql = (
            f'type=page AND space="{space_key}" AND ancestor={result_parent} '
            'AND (label="automation-ai-result" OR title~"MI result")'
        )
        while True:
            payload = self._request(
                "GET",
                "/content/search",
                params={
                    "cql": cql,
                    "expand": "body.storage,version,metadata.labels,ancestors",
                    "start": start,
                    "limit": limit,
                },
            )
            results = payload.get("results", [])
            for page in results:
                labels = {
                    str(item.get("name"))
                    for item in ((page.get("metadata") or {}).get("labels") or {}).get("results", [])
                }
                ancestors = page.get("ancestors") or []
                under_result_parent = bool(ancestors) and str(ancestors[-1].get("id")) == result_parent
                if "automation-ai-result" in labels or (
                    under_result_parent and str(page.get("title", "")).startswith("MI result")
                ):
                    yield page
            if len(results) < limit:
                break
            start += len(results)

    def get_property(self, page_id: str, name: str) -> Mapping[str, Any] | None:
        response = self.session.get(
            f"{self.api_root}/content/{page_id}/property/{name}",
            timeout=(10, 60),
            headers={"Accept": "application/json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def put_property(self, page_id: str, name: str, value: Mapping[str, Any]) -> None:
        for attempt in range(2):
            current = self.get_property(page_id, name)
            if current is not None and current.get("value") == value:
                return
            try:
                if current is None:
                    self._request(
                        "POST",
                        f"/content/{page_id}/property",
                        json={"key": name, "value": value},
                    )
                else:
                    self._request(
                        "PUT",
                        f"/content/{page_id}/property/{name}",
                        json={
                            "key": name,
                            "value": value,
                            "version": {"number": int((current.get("version") or {}).get("number", 1)) + 1},
                        },
                    )
                return
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 409 or attempt:
                    raise
        raise AssertionError("unreachable")

    def add_labels(self, page_id: str, labels: Iterable[str]) -> None:
        values = [{"prefix": "global", "name": label} for label in sorted(set(labels))]
        if values:
            self._request("POST", f"/content/{page_id}/label", json=values)

    def ensure_page(
        self,
        *,
        title: str,
        parent_id: str,
        markdown: str,
        labels: Iterable[str],
        property_name: str,
        property_value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        content_fingerprint = fingerprint({"markdown": markdown, "property": property_value})
        desired_property = dict(property_value)
        desired_property["contentFingerprint"] = content_fingerprint
        page = self.find_page(title, parent_id)
        current_property = self.get_property(str(page["id"]), property_name) if page else None
        if page is None or (current_property or {}).get("value") != desired_property:
            self._publish_markdown(
                title=title,
                parent_id=parent_id,
                markdown=markdown,
                existing_page_id=str(page["id"]) if page else None,
            )
            page = self.find_page(title, parent_id)
            if page is None:
                raise RuntimeError(f"md2conf returned successfully but page was not found: {title}")
        page_id = str(page["id"])
        self.put_property(page_id, property_name, desired_property)
        self.add_labels(page_id, labels)
        confirmed = self.get_page(page_id)
        return {
            "id": page_id,
            "title": confirmed.get("title", title),
            "version": int((confirmed.get("version") or {}).get("number", 0)),
            "contentFingerprint": content_fingerprint,
        }

    def _publish_markdown(
        self,
        *,
        title: str,
        parent_id: str,
        markdown: str,
        existing_page_id: str | None,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="mi-md2conf-") as directory:
            source = Path(directory, f"{safe_path_token(title)}.md")
            front_matter = json.dumps({"title": title}, ensure_ascii=False)
            association = f"<!-- confluence-page-id: {existing_page_id} -->\n" if existing_page_id else ""
            source.write_text(f"---\n{front_matter}\n---\n{association}{markdown}", encoding="utf-8")
            command = [
                self.settings.md2conf_command,
                str(source),
                "--root-page",
                str(parent_id),
                "--space",
                self.settings.space_key,
                "--no-generated-by",
                "--no-notify",
            ]
            environment = self._md2conf_environment()
            completed = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
            if completed.returncode != 0:
                raise RuntimeError(f"md2conf failed: {completed.stderr.strip()}")

    def _md2conf_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        parsed = urlparse(self.settings.base_url)
        environment["CONFLUENCE_DOMAIN"] = parsed.netloc
        environment["CONFLUENCE_PATH"] = f"{parsed.path.rstrip('/')}/"
        environment["CONFLUENCE_SPACE_KEY"] = self.settings.space_key
        token = self.secret.get("apiToken") or self.secret.get("token")
        user = self.secret.get("email") or self.secret.get("username")
        environment["CONFLUENCE_API_KEY"] = str(token)
        if user:
            environment["CONFLUENCE_USER_NAME"] = str(user)
        return environment
