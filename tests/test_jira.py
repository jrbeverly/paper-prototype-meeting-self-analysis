from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from meeting_intelligence.jira import JiraSnapshotSelector, JiraSnapshotter
from tests.fakes import MemoryRepository


class FakeAcli:
    def __init__(self):
        self.calls: list[list[str]] = []

    def json(self, arguments: list[str]):
        self.calls.append(arguments)
        joined = " ".join(arguments)
        if "board list-sprints" in joined:
            return {
                "values": [
                    {"id": 10, "name": "Sprint A", "state": "active"},
                    {"id": 11, "name": "Parallel", "state": "active"},
                ]
            }
        if "sprint list-workitems --sprint 10" in joined:
            return {
                "issues": [
                    {
                        "key": "PAY-1",
                        "fields": {
                            "summary": "Epic",
                            "issuetype": {"name": "Epic"},
                            "status": {"name": "Doing", "statusCategory": {"key": "indeterminate"}},
                            "customfield_42": 8,
                            "labels": [],
                        },
                    },
                    {
                        "key": "PAY-2",
                        "fields": {
                            "summary": "Story",
                            "issuetype": {"name": "Story"},
                            "parent": {"key": "PAY-1"},
                            "status": {"name": "Blocked", "statusCategory": {"key": "indeterminate"}},
                            "customfield_42": 5,
                            "assignee": {"displayName": "Ada"},
                            "labels": ["blocked"],
                        },
                    },
                    {
                        "key": "PAY-3",
                        "fields": {
                            "summary": "Subtask",
                            "issuetype": {"name": "Sub-task"},
                            "parent": {"key": "PAY-99"},
                            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                            "customfield_42": 2,
                            "labels": [],
                        },
                    },
                ]
            }
        if "sprint list-workitems --sprint 11" in joined:
            return {"issues": []}
        if "workitem view PAY-99" in joined:
            return {
                "key": "PAY-99",
                "fields": {
                    "issuetype": {"name": "Story"},
                    "parent": {"key": "PAY-1"},
                },
            }
        if "workitem search" in joined:
            return {"results": [{"key": "PAY-2"}]}
        raise AssertionError(arguments)


class JiraTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.acli = FakeAcli()
        self.config = {
            "snapshotIntervalMinutes": 30,
            "teams": [
                {
                    "teamId": "payments",
                    "projectKey": "PAY",
                    "boardId": "7",
                    "storyPointsField": "customfield_42",
                    "jqlPerspectives": [{"name": "flagged", "jql": "project=${project} AND flagged=Impediment"}],
                }
            ],
        }
        self.now = lambda: datetime(2026, 7, 23, 12, 47, tzinfo=timezone.utc)

    def test_multiple_sprints_custom_points_nested_epic_and_idempotent_slot(self):
        snapshotter = JiraSnapshotter(self.repo, self.acli, self.config, now=self.now)
        first = snapshotter.run()
        second = snapshotter.run()
        self.assertEqual(first, second)
        self.assertEqual(["jira/payments/snapshots/2026-07-23T12:30:00Z.json"], first)
        snapshot = json.loads(self.repo.values[first[0]])
        self.assertEqual(2, len(snapshot["sprints"]))
        issues = snapshot["sprints"][0]["issues"]
        self.assertEqual("PAY-1", next(item for item in issues if item["key"] == "PAY-3")["epicKey"])
        analytics = snapshot["sprints"][0]["analytics"]
        self.assertEqual(15, analytics["estimates"]["total"])
        self.assertEqual(["PAY-2"], analytics["blocked"])
        self.assertIn("PAY-3", analytics["unassigned"])
        self.assertEqual(3, analytics["byEpic"]["PAY-1"]["count"])
        self.assertEqual(["PAY-2"], snapshot["perspectives"]["flagged"]["keys"])
        self.assertIn("jira/payments/sprints/10/baseline.json", self.repo.values)
        paginated_calls = [
            call for call in self.acli.calls if "list-" in " ".join(call) or "workitem search" in " ".join(call)
        ]
        self.assertGreaterEqual(len(paginated_calls), 4)
        self.assertTrue(all("--paginate" in call for call in paginated_calls))

    def test_selector_chooses_latest_snapshot_not_after_meeting(self):
        self.repo.seed(
            "jira/payments/snapshots/a.json",
            json.dumps({"capturedAt": "2026-07-23T10:00:00Z", "value": "old"}).encode(),
        )
        self.repo.seed(
            "jira/payments/snapshots/b.json",
            json.dumps({"capturedAt": "2026-07-23T12:00:00Z", "value": "selected"}).encode(),
        )
        self.repo.seed(
            "jira/payments/snapshots/c.json",
            json.dumps({"capturedAt": "2026-07-23T13:00:00Z", "value": "future"}).encode(),
        )
        selection = JiraSnapshotSelector(self.repo).latest_at_or_before(
            "payments", "2026-07-23T12:30:00Z"
        )
        self.assertEqual("jira/payments/snapshots/b.json", selection[0])
        self.assertEqual("selected", selection[1]["value"])


if __name__ == "__main__":
    unittest.main()
