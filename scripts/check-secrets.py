#!/usr/bin/env python3
"""Reject secret findings beyond the reviewed baseline without rewriting it."""

import json
import subprocess
import sys
from pathlib import Path


def signatures(results):
    return {
        (filename, entry["type"], entry["hashed_secret"])
        for filename, entries in results.items()
        for entry in entries
    }


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / ".secrets.baseline").read_text())
    scan = subprocess.run(
        [
            sys.executable,
            "-m",
            "detect_secrets",
            "scan",
            "--all-files",
            "--exclude-files",
            r"(^|/)(?:\.git/|dist/|build/|\.secrets\.baseline$)",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    current = json.loads(scan.stdout)
    introduced = signatures(current["results"]) - signatures(baseline["results"])
    for filename, secret_type, _ in sorted(introduced):
        print(f"New secret finding: {filename} ({secret_type})", file=sys.stderr)
    if introduced:
        raise SystemExit(1)
    print("No new secret findings beyond the reviewed baseline.")
