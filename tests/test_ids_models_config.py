from __future__ import annotations

import math
import os
import unittest
from unittest.mock import patch

from meeting_intelligence.config import Settings
from meeting_intelligence.errors import ConfigurationError, ResultValidationError
from meeting_intelligence.ids import (
    business_date,
    clip_filename,
    fingerprint,
    transcribe_job_name,
)
from meeting_intelligence.models import AnalysisResult, SourceAnnotation


def valid_config() -> dict:
    return {
        "artifactBucket": "meeting-bucket",
        "activePrefix": "meetings/",
        "businessTimeZone": "America/Toronto",
        "transcription": {
            "configurationId": "en-us-default-v1",
            "languageCode": "en-US",
            "maxItemsPerRun": 10,
        },
        "jira": {
            "teams": [
                {
                    "teamId": "payments",
                    "projectKey": "PAY",
                    "boardId": "12",
                    "storyPointsField": "customfield_10016",
                }
            ]
        },
        "confluence": {
            "baseUrl": "https://example.atlassian.net/wiki",
            "spaceKey": "MI",
            "inputParentId": "1",
            "resultParentId": "2",
            "finalParentId": "3",
        },
        "inboundSharePointProfiles": {},
        "analysisProfiles": {},
        "aggregationProfiles": [],
        "finalization": {
            "sharePoint": {"siteId": "site", "driveId": "drive", "folder": "Meeting Intelligence"},
        },
    }


class IdsModelsConfigTests(unittest.TestCase):
    def test_deterministic_ids_and_paths(self):
        one = transcribe_job_name("b", "k", "etag", "source", "cfg", 0)
        self.assertEqual(one, transcribe_job_name("b", "k", "etag", "source", "cfg", 0))
        self.assertNotEqual(one, transcribe_job_name("b", "k", "etag2", "source", "cfg", 0))
        self.assertEqual(67, len(one))
        self.assertEqual("rec-1200-3457-r7.mp4", clip_filename("rec", 1.2, 3.4567, 7))
        self.assertTrue(clip_filename("../../bad/id", 0, 1, 1).endswith("-0-1000-r1.mp4"))
        self.assertEqual("2026-07-23", business_date("2026-07-24T01:00:00Z", "America/Toronto"))
        self.assertEqual(fingerprint({"b": 2, "a": 1}), fingerprint({"a": 1, "b": 2}))

    def test_settings_validate_chunk_alignment_and_env_bucket_override(self):
        config = valid_config()
        config["finalization"]["uploadChunkBytes"] = 123
        with self.assertRaises(ConfigurationError):
            Settings.from_dict(config)
        config["finalization"]["uploadChunkBytes"] = 320 * 1024
        with patch.dict(os.environ, {"MI_BUCKET": "actual-bucket"}):
            self.assertEqual("actual-bucket", Settings.from_dict(config).artifact_bucket)

    def test_source_annotation_requires_complete_split_set(self):
        value = {
            "v": 1,
            "kind": "zoom-recording",
            "occurrenceId": "occ",
            "meetingUuid": "uuid",
            "recordingFileId": "file-a",
            "expectedMp4FileIds": ["file-a", "file-b"],
            "recordingSetFingerprint": "sha256:x",
            "recordedAt": "2026-07-23T10:00:00Z",
            "durationSeconds": 10,
            "metadata": {"scope": "team", "teamId": "payments"},
            "sourcePrefix": "meetings/occ/",
        }
        source = SourceAnnotation.from_dict(value)
        self.assertEqual(("file-a", "file-b"), source.expected_mp4_file_ids)
        value["expectedMp4FileIds"] = ["file-a", "file-a"]
        with self.assertRaises(ValueError):
            SourceAnnotation.from_dict(value)

    def test_result_rejects_unsafe_demo_values(self):
        base = {"schemaVersion": "1.0", "summary": "summary", "demos": []}
        self.assertEqual((), AnalysisResult.from_dict(base).demos)
        for demo in (
            {"sourceRecordingId": "a", "startSeconds": -1, "endSeconds": 2, "title": "x"},
            {"sourceRecordingId": "a", "startSeconds": 2, "endSeconds": 2, "title": "x"},
            {"sourceRecordingId": "a", "startSeconds": math.nan, "endSeconds": 2, "title": "x"},
            {"sourceRecordingId": "a", "startSeconds": 0, "endSeconds": math.inf, "title": "x"},
        ):
            with self.subTest(demo=demo), self.assertRaises(ResultValidationError):
                AnalysisResult.from_dict({**base, "demos": [demo]})


if __name__ == "__main__":
    unittest.main()

