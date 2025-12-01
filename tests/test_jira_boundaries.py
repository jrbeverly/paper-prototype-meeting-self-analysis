from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from meeting_intelligence.jira import JiraSnapshotSelector, JiraSnapshotter
from tests.fakes import MemoryRepository


class EmptyAcli:
    def json(self, arguments: list[str]):
        if "list-sprints" in arguments:
            return {"values": []}
        if "search" in arguments:
            return {"results": []}
        raise AssertionError(arguments)


class AdvancingClock:
    def __init__(self, *values: datetime):
        self.values = list(values)

    def __call__(self) -> datetime:
        if not self.values:
            raise AssertionError("clock read more times than expected")
        return self.values.pop(0)


class ShapeAcli:
    def __init__(self):
        self.calls: list[list[str]] = []

    def json(self, arguments: list[str]):
        self.calls.append(arguments)
        joined = " ".join(arguments)
        if "board list-sprints" in joined:
            return {
                "values": [
                    {"id": 1, "name": "One", "state": "active"},
                    {"id": 2, "name": "Two", "state": "active"},
                ]
            }
        if "sprint list-workitems" in joined and (
            "--id 1" in joined or "--sprint 1" in joined
        ):
            return {
                "workItems": [
                    {
                        "key": "PAY-1",
                        "fields": {
                            "summary": "First",
                            "issuetype": {"name": "Story"},
                            "status": {
                                "name": "Doing",
                                "statusCategory": {"key": "indeterminate"},
                            },
                            "customfield_points": 3,
                        },
                    }
                ]
            }
        if "sprint list-workitems" in joined and (
            "--id 2" in joined or "--sprint 2" in joined
        ):
            return {
                "items": [
                    {
                        "key": "PAY-2",
                        "summary": "Second",
                        "issueType": {"name": "Task"},
                        "status": {
                            "name": "Done",
                            "statusCategory": {"name": "Done"},
                        },
                        "customfield_points": 5,
                    }
                ]
            }
        if "workitem search" in joined:
            return {"issues": [{"key": "PAY-1"}, {"key": "PAY-2"}]}
        raise AssertionError(arguments)


def team_configuration():
    return {
        "snapshotIntervalMinutes": 30,
        "teams": [
            {
                "teamId": "payments",
                "projectKey": "PAY",
                "boardId": "7",
                "storyPointsField": "customfield_points",
                "jqlPerspectives": [
                    {
                        "name": "all",
                        "jql": "project=${project}",
                    }
                ],
            }
        ],
    }


class JiraBoundaryTests(unittest.TestCase):
    def test_snapshot_key_uses_the_same_interval_slot_as_captured_at(self):
        repository = MemoryRepository()
        clock = AdvancingClock(
            datetime(2026, 7, 23, 12, 29, 59, tzinfo=timezone.utc),
            datetime(2026, 7, 23, 12, 30, 1, tzinfo=timezone.utc),
        )
        snapshotter = JiraSnapshotter(
            repository,
            EmptyAcli(),
            team_configuration(),
            now=clock,
        )

        written = snapshotter.run()

        self.assertEqual(
            ["jira/payments/snapshots/2026-07-23T12:00:00Z.json"],
            written,
        )
        payload = json.loads(repository.read_bytes(written[0]))
        self.assertEqual("2026-07-23T12:00:00Z", payload["capturedAt"])

    def test_selector_ignores_snapshot_without_timezone_instead_of_crashing(self):
        repository = MemoryRepository()
        repository.seed(
            "jira/payments/snapshots/naive.json",
            json.dumps(
                {
                    "capturedAt": "2026-07-23T12:00:00",
                    "selection": "invalid",
                }
            ).encode(),
        )
        repository.seed(
            "jira/payments/snapshots/valid.json",
            json.dumps(
                {
                    "capturedAt": "2026-07-23T11:30:00Z",
                    "selection": "valid",
                }
            ).encode(),
        )

        selected = JiraSnapshotSelector(repository).latest_at_or_before(
            "payments",
            "2026-07-23T12:30:00Z",
        )

        self.assertIsNotNone(selected)
        self.assertEqual("valid", selected[1]["selection"])

    def test_collection_shapes_normalize_across_parallel_sprints_and_all_commands_paginate(self):
        repository = MemoryRepository()
        command = ShapeAcli()
        snapshotter = JiraSnapshotter(
            repository,
            command,
            team_configuration(),
            now=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        )

        snapshot = snapshotter.capture_team(team_configuration()["teams"][0])

        self.assertEqual([1, 2], [sprint["id"] for sprint in snapshot["sprints"]])
        self.assertEqual(
            [["PAY-1"], ["PAY-2"]],
            [[issue["key"] for issue in sprint["issues"]] for sprint in snapshot["sprints"]],
        )
        self.assertEqual(["PAY-1", "PAY-2"], snapshot["perspectives"]["all"]["keys"])
        paginated_calls = [
            call
            for call in command.calls
            if "list-sprints" in call or "list-workitems" in call or "search" in call
        ]
        self.assertEqual(4, len(paginated_calls))
        self.assertTrue(all("--paginate" in call for call in paginated_calls))


if __name__ == "__main__":
    unittest.main()
