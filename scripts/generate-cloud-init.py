#!/usr/bin/env python3
"""Embed reviewed thread collection in cloud-init for future publishers."""

import argparse
import base64
import copy
import gzip
import json
import re
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent


def build_cloud_config(
    group: str,
    executable: str,
    container_name: str | None = None,
    *,
    base: dict | None = None,
) -> dict:
    if not group or not Path(executable).is_absolute():
        raise ValueError("Group and absolute publisher executable path are required")
    result = copy.deepcopy(base) if base is not None else {}
    if not isinstance(result, dict):
        raise ValueError("Existing cloud-config must be a YAML mapping")
    for key in ("write_files", "packages", "runcmd"):
        if not isinstance(result.get(key, []), list):
            raise ValueError(f"Existing {key} must be a list")
        result.setdefault(key, [])
    config = {
        "executable": executable,
        "group": group,
        "instance_id": "auto",
        "region": "auto",
        "interval": 10,
    }
    if container_name:
        config["container_name"] = container_name
    files = [
        {
            "path": "/opt/npa-thread-metrics/publisher_metrics.py",
            "owner": "root:root",
            "permissions": "0644",
            "encoding": "b64",
            "content": base64.b64encode(
                (SCRIPTS / "publisher_metrics.py").read_bytes()
            ).decode(),
        },
        {
            "path": "/etc/npa-thread-metrics.json",
            "owner": "root:root",
            "permissions": "0600",
            "content": json.dumps(config, indent=2) + "\n",
        },
        {
            "path": "/etc/systemd/system/npa-thread-metrics.service",
            "owner": "root:root",
            "permissions": "0644",
            "content": (SCRIPTS / "npa-thread-metrics.service").read_text(),
        },
    ]
    paths = {f["path"] for f in files}
    for existing in result["write_files"]:
        if not isinstance(existing, dict) or not isinstance(existing.get("path"), str):
            raise ValueError("Existing write_files entries require a path")
        if existing["path"] in paths:
            raise ValueError(f"Existing collector file conflicts: {existing['path']}")
    result["write_files"].extend(files)
    for package in ("python3", "python3-venv"):
        if package not in result["packages"]:
            result["packages"].append(package)
    requirements = [
        line.strip()
        for line in (SCRIPTS.parent / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    result["runcmd"].extend(
        [
            ["python3", "-m", "venv", "/opt/npa-thread-metrics/venv"],
            ["/opt/npa-thread-metrics/venv/bin/pip", "install", *requirements],
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "npa-thread-metrics.service"],
            ["systemctl", "restart", "npa-thread-metrics.service"],
        ]
    )
    return result


def write_user_data(config: dict, output: Path, *, ami_id: str | None = None) -> Path:
    if ami_id and not re.fullmatch(r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})", ami_id):
        raise ValueError("AMI ID must be an EC2 image identifier")
    source = "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)
    compressed = gzip.compress(source.encode(), mtime=0)
    if len(compressed) > 16 * 1024:
        raise ValueError("Compressed EC2 user data exceeds the 16 KiB raw limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(compressed)
    readable = output.with_suffix(".yaml")
    readable.write_text(source)
    launch_data = output.with_suffix(".launch-template.json")
    payload = {"UserData": base64.b64encode(compressed).decode()}
    if ami_id:
        payload["ImageId"] = ami_id
    launch_data.write_text(json.dumps(payload, indent=2) + "\n")
    return launch_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True)
    parser.add_argument("--executable", default="/home/npa_publisher")
    parser.add_argument("--container-name")
    parser.add_argument("--ami-id", help="Explicit regional Marketplace AMI override")
    parser.add_argument("--base-cloud-config", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("dist/publisher-user-data.gz")
    )
    args = parser.parse_args()
    base = None
    if args.base_cloud_config:
        base = yaml.safe_load(args.base_cloud_config.read_text())
        if not isinstance(base, dict):
            parser.error("Base user data must be a cloud-config YAML mapping")
    config = build_cloud_config(
        args.group, args.executable, args.container_name, base=base
    )
    launch_data = write_user_data(config, args.output, ami_id=args.ami_id)
    print(f"Generated {args.output} ({args.output.stat().st_size} bytes)")
    print(f"Launch-template input: {launch_data}")
