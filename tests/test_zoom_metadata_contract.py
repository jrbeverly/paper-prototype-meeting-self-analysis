from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ZoomMetadataWireContractTests(unittest.TestCase):
    def test_go_parser_serializes_documented_flat_routing_fields(self):
        """Exercise the actual Go parser without duplicating its rules in Python."""

        go = shutil.which("go")
        if go is None:
            self.skipTest("Go toolchain is not installed in this test environment")
        probe = r'''
package main

import (
    "encoding/json"
    "os"
)

func main() {
    metadata, err := ParseMeetingMetadata(`
scope=team
team=payments
meeting_type=daily-standup
jira_project=PAY
sharepoint_profile=payments-reference
transcription_vocabulary=payments
`)
    if err != nil {
        panic(err)
    }
    if err := json.NewEncoder(os.Stdout).Encode(metadata); err != nil {
        panic(err)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="mi-go-metadata-") as directory:
            work = Path(directory)
            shutil.copy(
                REPOSITORY_ROOT / "cmd" / "zoom-download" / "metadata.go",
                work / "metadata.go",
            )
            (work / "probe.go").write_text(probe, encoding="utf-8")
            completed = subprocess.run(
                [go, "run", "probe.go", "metadata.go"],
                cwd=work,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        wire = json.loads(completed.stdout)
        self.assertEqual(
            "aggregation",
            wire["pipeline"],
        )
        self.assertEqual("daily-brief-team-input", wire["analysisProfile"])
        self.assertEqual("daily-brief", wire["outputTemplate"])
        self.assertEqual("daily-brief", wire["aggregationProfile"])
        self.assertNotIn("routing", wire)
        self.assertEqual("payments", wire["teamId"])
        self.assertEqual("PAY", wire["jiraProject"])
        self.assertEqual("payments-reference", wire["sharePointProfile"])
        self.assertEqual("payments", wire["transcriptionVocabulary"])


if __name__ == "__main__":
    unittest.main()
