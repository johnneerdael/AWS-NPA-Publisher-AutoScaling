#!/usr/bin/env python3
"""Publish CPU of the hottest publisher thread relative to a single vCPU."""

import argparse
import http.client
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)


class CollectionError(RuntimeError):
    """The selected publisher cannot be measured reliably."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class ThreadSample:
    identity: ProcessIdentity
    cpu: float | None
    hottest_tid: int | None
    core_cpu: dict[str, float] = field(default_factory=dict)


def parse_stat(text: str) -> tuple[int, int]:
    """Return user+system CPU ticks and start time from Linux procfs stat."""
    try:
        fields = text.rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])
        start = int(fields[19])
        if ticks < 0 or start < 0:
            raise ValueError("negative procfs counter")
        return ticks, start
    except (IndexError, ValueError) as exc:
        raise CollectionError("Invalid publisher thread stat") from exc


def resolve_container_id(name: str) -> str:
    """Resolve a Docker name on the host without executing a shell."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        raise ValueError("Invalid Docker container name")
    docker = shutil.which("docker")
    if docker is None:
        raise CollectionError("Docker is unavailable on this host")
    try:
        result = subprocess.run(
            [docker, "inspect", "--format", "{{.Id}}", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        container_id = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise CollectionError("Docker did not return one full container ID")
        return container_id
    except (OSError, subprocess.SubprocessError) as exc:
        raise CollectionError("Cannot inspect selected Docker container") from exc


class ProcessSelector:
    def __init__(
        self,
        executable: str,
        *,
        proc_root: Path = Path("/proc"),
        pid_file: Path | None = None,
        container_id: str | None = None,
        container_name: str | None = None,
    ):
        if not Path(executable).is_absolute():
            raise ValueError("Publisher executable must be an absolute path")
        if container_id and not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValueError("Container ID must contain 64 lowercase hex characters")
        if container_id and container_name:
            raise ValueError("Choose container ID or container name, not both")
        self.executable = executable
        self.proc_root = proc_root
        self.pid_file = pid_file
        self.container_id = container_id
        self.container_name = container_name

    def select(self) -> ProcessIdentity:
        try:
            container_id = self.container_id
            if self.container_name:
                container_id = resolve_container_id(self.container_name)
            if self.pid_file:
                pid = int(self.pid_file.read_text().strip())
                if pid <= 0:
                    raise ValueError("Invalid publisher PID")
                candidates = [self.proc_root / str(pid)]
            else:
                candidates = [p for p in self.proc_root.iterdir() if p.name.isdigit()]
            matches = []
            for p in candidates:
                try:
                    if os.readlink(p / "exe") != self.executable:
                        continue
                    if container_id:
                        cgroup = (p / "cgroup").read_text()
                        pattern = rf"[/\-]{container_id}(?:[./\n]|$)"
                        if not re.search(pattern, cgroup):
                            continue
                    _, start = parse_stat((p / "stat").read_text())
                    matches.append(ProcessIdentity(int(p.name), start))
                except FileNotFoundError:
                    continue  # Processes can exit during discovery.
                except PermissionError:
                    if self.pid_file:
                        raise
                    continue  # Normal for unrelated processes; run as root.
            if len(matches) != 1:
                raise CollectionError(
                    f"Expected exactly one publisher process; found {len(matches)}"
                )
            return matches[0]
        except (OSError, ValueError) as exc:
            raise CollectionError("Cannot select publisher process") from exc


class CoreSampler:
    """Measure each OS CPU separately; never use the aggregate procfs CPU row."""

    def __init__(self, proc_root: Path):
        self.stat_path = proc_root / "stat"
        self.previous = {}

    def sample(self) -> dict[str, float]:
        try:
            counters = {}
            for line in self.stat_path.read_text().splitlines():
                fields = line.split()
                if not fields or not re.fullmatch(r"cpu[0-9]+", fields[0]):
                    continue
                # Guest counters are already included in user/nice; ignore them.
                values = tuple(int(value) for value in fields[1:9])
                if len(values) != 8 or min(values) < 0 or fields[0] in counters:
                    raise ValueError("Invalid per-core CPU counters")
                counters[fields[0]] = values
            previous = self.previous
            self.previous = counters
            measured = {}
            for cpu, values in counters.items():
                if cpu not in previous:
                    continue  # New/hotplugged cores need their own baseline.
                deltas = tuple(value - old for value, old in zip(values, previous[cpu]))
                total = sum(deltas)
                if min(deltas) < 0 or total <= 0:
                    continue  # Regressed/unchanged counters cannot establish CPU.
                idle = deltas[3] + deltas[4]
                measured[cpu] = 100 * (total - idle) / total
            return measured
        except (OSError, ValueError):
            self.previous = {}
            return {}  # Optional core telemetry must not invalidate thread CPU.


class ThreadSampler:
    def __init__(self, selector: ProcessSelector, ticks_per_second: int | None = None):
        self.selector = selector
        self.hz = ticks_per_second or os.sysconf("SC_CLK_TCK")
        if self.hz <= 0:
            raise ValueError("Clock ticks per second must be positive")
        self.previous = None
        self.core_sampler = CoreSampler(selector.proc_root)

    def sample(self, now: float | None = None) -> ThreadSample:
        core_cpu = self.core_sampler.sample()
        try:
            identity = self.selector.select()
            process = self.selector.proc_root / str(identity.pid)
            ticks = {}
            for thread in (process / "task").iterdir():
                try:
                    cpu, start = parse_stat((thread / "stat").read_text())
                    ticks[(int(thread.name), start)] = cpu
                except FileNotFoundError:
                    continue  # A thread exited during sampling.
            _, start_after = parse_stat((process / "stat").read_text())
            if start_after != identity.start_ticks or not ticks:
                raise CollectionError("Publisher changed while reading thread counters")
            sampled_at = time.monotonic() if now is None else now
            previous = self.previous
            self.previous = (identity, sampled_at, ticks)
            if previous is None or previous[0] != identity:
                return ThreadSample(identity, None, None, core_cpu)
            elapsed = sampled_at - previous[1]
            if elapsed <= 0:
                raise CollectionError("Sampling interval must be positive")
            if any(key not in previous[2] for key in ticks):
                # An unseen live thread could be the busiest; baseline every thread.
                return ThreadSample(identity, None, None, core_cpu)
            measured = []
            for key, current in ticks.items():
                delta = current - previous[2][key]
                if delta < 0:
                    raise CollectionError("Publisher CPU counter regressed")
                # A thread can use at most one vCPU; jiffy rounding may overshoot.
                measured.append((min(100.0, 100 * delta / self.hz / elapsed), key[0]))
            if not measured:
                return ThreadSample(identity, None, None, core_cpu)
            cpu, tid = max(measured)
            return ThreadSample(identity, cpu, tid, core_cpu)
        except (OSError, ValueError, CollectionError) as exc:
            self.previous = None
            if isinstance(exc, CollectionError):
                raise
            raise CollectionError("Cannot read publisher thread counters") from exc


def read_configuration(path: Path) -> dict:
    values = json.loads(path.read_text())
    allowed = {
        "executable",
        "pid_file",
        "container_name",
        "container_id",
        "proc_root",
        "group",
        "instance_id",
        "region",
        "interval",
    }
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("Collector configuration contains unsupported fields")
    return values


def instance_metadata() -> dict:
    """Read instance identity with IMDSv2 only; never fall back to IMDSv1."""
    connection = http.client.HTTPConnection("169.254.169.254", timeout=2)
    try:
        connection.request(
            "PUT",
            "/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        response = connection.getresponse()
        token = response.read().decode()
        if response.status != 200:
            raise CollectionError("IMDSv2 token request failed")
        connection.request(
            "GET",
            "/latest/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token},
        )
        response = connection.getresponse()
        document = response.read()
        if response.status != 200:
            raise CollectionError("IMDSv2 identity request failed")
        identity = json.loads(document)
        return {"instance_id": identity["instanceId"], "region": identity["region"]}
    finally:
        connection.close()


def metric_data(
    group: str,
    instance: str,
    sample: ThreadSample | None,
    *,
    timestamp: datetime | None = None,
) -> list[dict]:
    healthy = sample is not None and sample.cpu is not None
    dimensions = [
        {"Name": "AutoScalingGroupName", "Value": group},
        {"Name": "InstanceId", "Value": instance},
    ]
    data = [{"MetricName": "CollectorHealthy", "Value": int(healthy)}]
    if healthy:
        data.append(
            {"MetricName": "HottestThreadCPU", "Value": sample.cpu, "Unit": "Percent"}
        )
    if sample is not None:
        data.extend(
            {
                "MetricName": "CPUCoreUtilization",
                "Value": value,
                "Unit": "Percent",
                "Dimensions": dimensions + [{"Name": "Cpu", "Value": cpu}],
            }
            for cpu, value in sample.core_cpu.items()
        )
    for metric in data:
        metric.setdefault("Unit", "Count")
        metric.setdefault("Dimensions", dimensions)
        metric["StorageResolution"] = 1
        if timestamp is not None:
            metric["Timestamp"] = timestamp
    return data


def main() -> None:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path)
    config_args, _ = config_parser.parse_known_args()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--executable")
    parser.add_argument("--pid-file", type=Path)
    containers = parser.add_mutually_exclusive_group()
    containers.add_argument("--container-name")
    containers.add_argument("--container-id")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--group")
    parser.add_argument("--instance-id", default="auto")
    parser.add_argument("--region", default="auto")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--samples", type=int, default=0)
    parser.add_argument(
        "--inspect", action="store_true", help="Read-only; no AWS writes"
    )
    if config_args.config:
        parser.set_defaults(**read_configuration(config_args.config))
    args = parser.parse_args()
    if not args.executable:
        parser.error("Specify --executable or executable in --config")
    if not math.isfinite(args.interval) or args.interval <= 0 or args.samples < 0:
        parser.error("Interval must be positive and sample count nonnegative")
    if not args.inspect:
        if args.interval > 10:
            parser.error("Publishing interval must be at most 10 seconds")
        if not args.group:
            parser.error("Publishing requires --group or group in --config")
        if args.instance_id == "auto" or args.region == "auto":
            identity = instance_metadata()
            if args.instance_id == "auto":
                args.instance_id = identity["instance_id"]
            if args.region == "auto":
                args.region = identity["region"]
    logging.basicConfig(level=logging.INFO)
    selector = ProcessSelector(
        args.executable,
        proc_root=args.proc_root,
        pid_file=args.pid_file,
        container_id=args.container_id,
        container_name=args.container_name,
    )
    cloudwatch = None
    if not args.inspect:
        import boto3
        from botocore.config import Config

        cloudwatch = boto3.client(
            "cloudwatch",
            region_name=args.region,
            config=Config(
                connect_timeout=2,
                read_timeout=3,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
    sampler = ThreadSampler(selector)
    remaining = args.samples
    next_sample = time.monotonic()
    while True:
        sample = None
        try:
            sample = sampler.sample()
            LOG.info(
                json.dumps(
                    {
                        "pid": sample.identity.pid,
                        "hottest_tid": sample.hottest_tid,
                        "cpu_percent_one_vcpu": sample.cpu,
                        "cpu_core_utilization": sample.core_cpu,
                    }
                )
            )
        except CollectionError as exc:
            LOG.error("Publisher telemetry unavailable: %s", exc)
        timestamp = datetime.now(timezone.utc)
        if cloudwatch:
            try:
                cloudwatch.put_metric_data(
                    Namespace="NPA/Publisher",
                    MetricData=metric_data(
                        args.group,
                        args.instance_id,
                        sample,
                        timestamp=timestamp,
                    ),
                )
            except Exception:
                LOG.exception("CloudWatch publication failed")
        if args.samples:
            remaining -= 1
            if remaining == 0:
                return
        next_sample += args.interval
        delay = next_sample - time.monotonic()
        if delay < 0:
            # Skip a missed interval; do not inflate sample counts with catch-up.
            next_sample = time.monotonic() + args.interval
            delay = args.interval
        time.sleep(delay)


if __name__ == "__main__":
    main()
