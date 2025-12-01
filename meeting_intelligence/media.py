"""FFmpeg wrapper for exact, re-encoded clips."""

from __future__ import annotations

import subprocess
from pathlib import Path


class Ffmpeg:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def duration(self, source: Path) -> float:
        completed = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"ffprobe failed for {source}: {completed.stderr.strip()}")
        try:
            duration = float(completed.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(f"ffprobe returned an invalid duration for {source}") from exc
        if duration <= 0:
            raise RuntimeError(f"recording has no positive duration: {source}")
        return duration

    def render(self, source: Path, destination: Path, start_seconds: float, end_seconds: float) -> None:
        completed = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-ss",
                f"{start_seconds:.3f}",
                "-t",
                f"{end_seconds - start_seconds:.3f}",
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-y",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {source}: {completed.stderr.strip()}")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg did not create a non-empty clip: {destination}")

