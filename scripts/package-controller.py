#!/usr/bin/env python3
"""Build a deterministic Lambda ZIP from the reviewed controller source."""

import argparse
import hashlib
import zipfile
from pathlib import Path


def package(output: Path) -> str:
    source = Path(__file__).parent / "controller" / "scaling_controller.py"
    output.parent.mkdir(parents=True, exist_ok=True)
    entry = zipfile.ZipInfo("scaling_controller.py", date_time=(1980, 1, 1, 0, 0, 0))
    entry.external_attr = 0o644 << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(entry, source.read_bytes())
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output", type=Path, nargs="?", default=Path("dist/controller.zip")
    )
    args = parser.parse_args()
    print(f"{package(args.output)}  {args.output}")
