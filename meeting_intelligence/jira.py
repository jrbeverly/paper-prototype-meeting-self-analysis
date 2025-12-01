"""Jira snapshot capture through Atlassian's official `acli`."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from .ids import canonical_json, fingerprint
from .ports import ObjectRepository

LOGGER = logging.getLogger(__name__)


class JiraCommand(Protocol):
    def json(self, arguments: list[str]) -> Any: ...


class Acli:
    def __init__(self, secret: Mapping[str, Any], executable: str = "acli"):
        self.executable = executable
        self.environment = os.environ.copy()
        mapping = {
            "site": "ACLI_SITE",
            "email": "ACLI_EMAIL",
            "apiToken": "ACLI_TOKEN",
        }
        for key, environment_name in mapping.items():
            value = secret.get(key)
            if value:
                self.environment[environment_name] = str(value)

    def json(self, arguments: list[str]) -> Any:
        completed = subprocess.run(
            [self.executable, *arguments],
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"acli {' '.join(arguments)} failed: {completed.stderr.strip()}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("acli did not emit JSON") from exc


def _items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("values", "results", "issues", "workItems", "items"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, Mapping)]
    return []


def _field(issue: Mapping[str, Any], name: str, default: Any = None) -> Any:
    fields = issue.get("fields")
    if isinstance(fields, Mapping):
        return fields.get(name, default)
    return issue.get(name, default)


def _name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        candidate = value.get("name") or value.get("displayName") or value.get("value") or value.get("key")
        return str(candidate) if candidate is not None else None
    return None


def _parent_key(issue: Mapping[str, Any]) -> str | None:
    parent = _field(issue, "parent")
    if isinstance(parent, str):
        return parent
    if isinstance(parent, Mapping) and parent.get("key"):
        return str(parent["key"])
    return None


def _issue_type(issue: Mapping[str, Any]) -> str:
    return (_name(_field(issue, "issuetype")) or _name(_field(issue, "issueType")) or "").lower()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class JiraSnapshotter:
    def __init__(
        self,
        repository: ObjectRepository,
        command: JiraCommand,
        configuration: Mapping[str, Any],
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.command = command
        self.configuration = configuration
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._resolved: dict[str, Mapping[str, Any]] = {}

    def run(self, team_filter: str | None = None) -> list[str]:
        written: list[str] = []
        slot = self._slot()
        for team in self.configuration.get("teams", []):
            if team_filter and team.get("teamId") != team_filter:
                continue
            snapshot = self.capture_team(team, captured_at=slot)
            key = f"jira/{team['teamId']}/snapshots/{slot}.json"
            self.repository.write_bytes(
                key,
                (canonical_json(snapshot) + "\n").encode("utf-8"),
                "application/json",
            )
            written.append(key)
            for sprint in snapshot["sprints"]:
                baseline_key = f"jira/{team['teamId']}/sprints/{sprint['id']}/baseline.json"
                if self.repository.head(baseline_key) is None:
                    self.repository.write_bytes(
                        baseline_key,
                        (canonical_json({"capturedAt": snapshot["capturedAt"], "sprint": sprint}) + "\n").encode(
                            "utf-8"
                        ),
                        "application/json",
                    )
        return written

    def capture_team(
        self,
        team: Mapping[str, Any],
        *,
        captured_at: str | None = None,
    ) -> dict[str, Any]:
        captured_at = captured_at or self._slot()
        board_id = str(team["boardId"])
        sprint_payload = self.command.json(
            [
                "jira",
                "board",
                "list-sprints",
                "--id",
                board_id,
                "--state",
                "active",
                "--paginate",
                "--json",
            ]
        )
        active_sprints = _items(sprint_payload)
        snapshots: list[dict[str, Any]] = []
        raw_work_items: dict[str, Any] = {}
        for sprint in active_sprints:
            sprint_id = str(sprint.get("id"))
            fields = ",".join(
                (
                    "key",
                    "issuetype",
                    "summary",
                    "assignee",
                    "priority",
                    "status",
                    "parent",
                    "labels",
                    "issuelinks",
                    "flagged",
                    "updated",
                    str(team["storyPointsField"]),
                )
            )
            raw = self.command.json(
                [
                    "jira",
                    "sprint",
                    "list-workitems",
                    "--sprint",
                    sprint_id,
                    "--board",
                    board_id,
                    "--fields",
                    fields,
                    "--paginate",
                    "--json",
                ]
            )
            issues = _items(raw)
            raw_work_items[sprint_id] = raw
            normalized = self._normalize_issues(issues, str(team["storyPointsField"]))
            snapshots.append(
                {
                    "id": sprint.get("id"),
                    "name": sprint.get("name"),
                    "state": sprint.get("state", "active"),
                    "goal": sprint.get("goal"),
                    "startDate": sprint.get("startDate"),
                    "endDate": sprint.get("endDate"),
                    "issues": normalized,
                    "analytics": analytics(normalized),
                }
            )
        perspectives: dict[str, Any] = {}
        for perspective in team.get("jqlPerspectives", []):
            name = str(perspective["name"])
            jql = str(perspective["jql"]).replace("${project}", str(team["projectKey"]))
            result = self.command.json(
                ["jira", "workitem", "search", "--jql", jql, "--paginate", "--json"]
            )
            perspectives[name] = {
                "jql": jql,
                "keys": [str(item.get("key")) for item in _items(result) if item.get("key")],
                "raw": result,
            }
        payload: dict[str, Any] = {
            "schemaVersion": "1.0",
            "teamId": team["teamId"],
            "projectKey": team["projectKey"],
            "boardId": team["boardId"],
            "capturedAt": captured_at,
            "sprints": snapshots,
            "perspectives": perspectives,
            "raw": {
                "activeSprints": sprint_payload,
                "workItemsBySprint": raw_work_items,
            },
        }
        payload["contentFingerprint"] = fingerprint(payload)
        return payload

    def _normalize_issues(self, issues: list[Mapping[str, Any]], points_field: str) -> list[dict[str, Any]]:
        local = {str(issue["key"]): issue for issue in issues if issue.get("key")}

        def resolve(key: str) -> Mapping[str, Any] | None:
            if key in local:
                return local[key]
            if key in self._resolved:
                return self._resolved[key]
            payload = self.command.json(
                ["jira", "workitem", "view", key, "--fields", "key,issuetype,parent", "--json"]
            )
            candidates = _items(payload)
            value = candidates[0] if candidates else payload if isinstance(payload, Mapping) else None
            if isinstance(value, Mapping):
                self._resolved[key] = value
                return value
            return None

        normalized: list[dict[str, Any]] = []
        for issue in issues:
            key = str(issue.get("key"))
            parent_key = _parent_key(issue)
            epic_key: str | None = None
            if _issue_type(issue) == "epic":
                epic_key = key
            elif parent_key:
                parent = resolve(parent_key)
                if parent and _issue_type(parent) == "epic":
                    epic_key = parent_key
                elif parent:
                    grandparent = _parent_key(parent)
                    grandparent_issue = resolve(grandparent) if grandparent else None
                    if grandparent_issue and _issue_type(grandparent_issue) == "epic":
                        epic_key = grandparent
            labels = _field(issue, "labels", []) or []
            links = _field(issue, "issuelinks", []) or []
            blocked = any(str(label).lower() == "blocked" for label in labels)
            blocked = blocked or bool(_field(issue, "flagged", False))
            if isinstance(links, list):
                blocked = blocked or any("block" in canonical_json(link).lower() for link in links)
            status = _field(issue, "status", {})
            normalized.append(
                {
                    "key": key,
                    "summary": str(_field(issue, "summary", "")),
                    "status": _name(status) or "Unknown",
                    "statusCategory": (
                        _name(status.get("statusCategory")) if isinstance(status, Mapping) else None
                    ),
                    "issueType": _name(_field(issue, "issuetype")) or _name(_field(issue, "issueType")),
                    "assignee": _name(_field(issue, "assignee")),
                    "storyPoints": _number(_field(issue, points_field)),
                    "parentKey": parent_key,
                    "epicKey": epic_key,
                    "blocked": blocked,
                    "labels": [str(label) for label in labels],
                    "updatedAt": _field(issue, "updated"),
                }
            )
        return normalized

    def _slot(self) -> str:
        interval = int(self.configuration.get("snapshotIntervalMinutes", 30))
        current = self.now().astimezone(timezone.utc).replace(second=0, microsecond=0)
        current -= timedelta(minutes=current.minute % interval)
        return current.isoformat().replace("+00:00", "Z")


def analytics(issues: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(issues)
    by_status = Counter(str(issue.get("status") or "Unknown") for issue in items)
    by_category = Counter(str(issue.get("statusCategory") or "unknown") for issue in items)
    by_epic: dict[str, dict[str, float | int]] = defaultdict(lambda: {"count": 0, "estimate": 0.0})
    for issue in items:
        epic = str(issue.get("epicKey") or "no-epic")
        by_epic[epic]["count"] += 1
        by_epic[epic]["estimate"] += float(issue.get("storyPoints") or 0)
    return {
        "total": len(items),
        "estimates": {
            "total": sum(float(issue.get("storyPoints") or 0) for issue in items),
            "unestimated": sum(1 for issue in items if issue.get("storyPoints") is None),
        },
        "byStatus": dict(sorted(by_status.items())),
        "byStatusCategory": dict(sorted(by_category.items())),
        "byEpic": dict(sorted(by_epic.items())),
        "unassigned": [issue["key"] for issue in items if not issue.get("assignee")],
        "blocked": [issue["key"] for issue in items if issue.get("blocked")],
    }


class JiraSnapshotSelector:
    def __init__(self, repository: ObjectRepository):
        self.repository = repository

    def latest_at_or_before(self, team_id: str, instant: str) -> tuple[str, Mapping[str, Any]] | None:
        cutoff = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("meeting instant must include a timezone")
        candidates: list[tuple[datetime, str, Mapping[str, Any]]] = []
        for object_ref in self.repository.list(f"jira/{team_id}/snapshots/"):
            if not object_ref.key.endswith(".json"):
                continue
            try:
                payload = json.loads(self.repository.read_bytes(object_ref.key))
                captured = datetime.fromisoformat(str(payload["capturedAt"]).replace("Z", "+00:00"))
            except (KeyError, ValueError, json.JSONDecodeError):
                LOGGER.warning("ignoring malformed Jira snapshot", extra={"key": object_ref.key})
                continue
            if captured.tzinfo is None:
                LOGGER.warning("ignoring timezone-naive Jira snapshot", extra={"key": object_ref.key})
                continue
            if captured <= cutoff:
                candidates.append((captured, object_ref.key, payload))
        if not candidates:
            return None
        _, key, payload = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
        return key, payload
