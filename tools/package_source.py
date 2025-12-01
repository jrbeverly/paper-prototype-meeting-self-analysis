#!/usr/bin/env python3
"""Create the deterministic source ZIP consumed by all CodeBuild projects."""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path

INCLUDED_ROOTS = ("cmd", "meeting_intelligence", "schemas", "buildspecs")
INCLUDED_FILES = ("go.mod", "go.sum", "pyproject.toml")


def files(root: Path):
    for relative in INCLUDED_FILES:
        candidate = root / relative
        if candidate.is_file():
            yield candidate
    for relative in INCLUDED_ROOTS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.rglob("*")):
            if candidate.is_file() and "__pycache__" not in candidate.parts:
                yield candidate


def package(root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in files(root):
            info = zipfile.ZipInfo(source.relative_to(root).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if os.access(source, os.X_OK) else 0o644) << 16
            archive.writestr(info, source.read_bytes())


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--output", default="dist/meeting-intelligence-source.zip")
    args = argument_parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    package(root, (root / args.output).resolve())


if __name__ == "__main__":
    main()

