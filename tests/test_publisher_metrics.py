import os
import tempfile
import unittest
from pathlib import Path

from scripts import publisher_metrics as metrics


def stat_text(pid, ticks, start=100, name="publisher ) worker", processor=0):
    fields = ["0"] * 50
    fields[0] = "S"
    fields[11] = str(ticks)
    fields[12] = "0"
    fields[19] = str(start)
    fields[36] = str(processor)
    return f"{pid} ({name}) " + " ".join(fields)


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.exe = "/home/npa_publisher"
        self.process(123)

    def process(self, pid, start=100, container=""):
        p = self.root / str(pid)
        p.mkdir(exist_ok=True)
        (p / "exe").symlink_to(self.exe)
        (p / "stat").write_text(stat_text(pid, 0, start))
        (p / "cgroup").write_text(f"0::/system.slice/docker-{container}.scope\n")
        (p / "task").mkdir(exist_ok=True)
        self.thread(pid, pid, 0, start)
        return p

    def thread(self, pid, tid, ticks, start=100, processor=0):
        d = self.root / str(pid) / "task" / str(tid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(stat_text(tid, ticks, start, processor=processor))

    def selector(self, **kwargs):
        return metrics.ProcessSelector(self.exe, proc_root=self.root, **kwargs)

    def test_configuration_loads_selector_and_paths(self):
        import json

        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "executable": self.exe,
                    "group": "group",
                    "container_name": "publisher",
                }
            )
        )
        values = metrics.read_configuration(config)
        self.assertEqual(values["executable"], self.exe)
        self.assertEqual(values["container_name"], "publisher")
        config.write_text(json.dumps({"shell_command": "unsafe"}))
        with self.assertRaises(ValueError):
            metrics.read_configuration(config)

    def test_configuration_rejects_removed_readiness_fields(self):
        import json

        config = self.root / "config.json"
        for field, value in [("ready_file", "ready.json"), ("ready_ttl", 180)]:
            config.write_text(json.dumps({"executable": self.exe, field: value}))
            with self.subTest(field=field), self.assertRaises(ValueError):
                metrics.read_configuration(config)

    def test_stat_parser_handles_parentheses_in_thread_name(self):
        self.assertEqual(metrics.parse_stat(stat_text(123, 70)), (70, 100))

    def test_single_cpu_units_do_not_depend_on_machine_cpu_count(self):
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        self.assertIsNone(sampler.sample(now=10).cpu)
        self.thread(123, 123, 70)
        result = sampler.sample(now=11)
        self.assertAlmostEqual(result.cpu, 70)
        self.assertEqual(result.hottest_tid, 123)

    def test_hottest_thread_not_sum_or_average(self):
        self.thread(123, 124, 0)
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.thread(123, 123, 80)
        self.thread(123, 124, 15)
        self.assertEqual(sampler.sample(now=11).cpu, 80)

    def test_migration_between_cores_does_not_reset_sampling(self):
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.thread(123, 123, 75, processor=4)
        self.assertEqual(sampler.sample(now=11).cpu, 75)

    def test_process_restart_requires_new_baseline(self):
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        (self.root / "123/stat").write_text(stat_text(123, 1, 200))
        self.thread(123, 123, 1, 200)
        self.assertIsNone(sampler.sample(now=11).cpu)
        self.thread(123, 123, 71, 200)
        self.assertEqual(sampler.sample(now=12).cpu, 70)

    def test_thread_reuse_does_not_create_cpu_spike(self):
        self.thread(123, 124, 100)
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.thread(123, 123, 10)
        self.thread(123, 124, 9999, 200)
        self.assertIsNone(sampler.sample(now=11).cpu)
        self.thread(123, 124, 10074, 200)
        self.assertEqual(sampler.sample(now=12).cpu, 75)

    def test_counter_regression_is_an_error_not_zero(self):
        self.thread(123, 123, 100)
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.thread(123, 123, 1)
        with self.assertRaises(metrics.CollectionError):
            sampler.sample(now=11)

    def test_no_process_and_ambiguous_process_fail_closed(self):
        (self.root / "123/exe").unlink()
        with self.assertRaises(metrics.CollectionError):
            self.selector().select()
        (self.root / "123/exe").symlink_to(self.exe)
        self.process(124)
        with self.assertRaises(metrics.CollectionError):
            self.selector().select()

    def test_pid_file_still_requires_matching_executable(self):
        pid_file = self.root / "publisher.pid"
        pid_file.write_text("123")
        selector = self.selector(pid_file=pid_file)
        self.assertEqual(selector.select().pid, 123)
        (self.root / "123/exe").unlink()
        (self.root / "123/exe").symlink_to("/bin/sleep")
        with self.assertRaises(metrics.CollectionError):
            selector.select()

    def test_container_selection_distinguishes_identical_executables(self):
        container = "a" * 64
        (self.root / "123/cgroup").write_text(f"0::/docker/{container}\n")
        self.process(124, container="b" * 64)
        selector = self.selector(container_id=container)
        self.assertEqual(selector.select().pid, 123)
        with self.assertRaises(ValueError):
            self.selector(container_id="../../etc/passwd")

    def test_bad_thread_stat_fails_without_fabricated_cpu(self):
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        (self.root / "123/task/123/stat").write_text("broken")
        with self.assertRaises(metrics.CollectionError):
            sampler.sample(now=11)

    def test_nonpositive_interval_is_rejected(self):
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        with self.assertRaises(metrics.CollectionError):
            sampler.sample(now=10)

    def test_metric_dimensions_and_health_failure_does_not_emit_cpu(self):
        identity = self.selector().select()
        result = metrics.ThreadSample(identity, 70, 123)
        payload = metrics.metric_data("group", "i-example", result)
        values = {item["MetricName"]: item["Value"] for item in payload}
        self.assertEqual(values, {"HottestThreadCPU": 70, "CollectorHealthy": 1})
        for item in payload:
            self.assertEqual(
                item["Dimensions"],
                [
                    {"Name": "AutoScalingGroupName", "Value": "group"},
                    {"Name": "InstanceId", "Value": "i-example"},
                ],
            )
        failed = metrics.metric_data("group", "i-example", None)
        self.assertEqual(
            {i["MetricName"]: i["Value"] for i in failed},
            {"CollectorHealthy": 0},
        )

    def core_stat(self, rows):
        # The aggregate row must never affect any per-core reading.
        lines = ["cpu deliberately invalid aggregate row", "intr 123"]
        lines += [
            cpu + " " + " ".join(map(str, values)) for cpu, values in rows.items()
        ]
        (self.root / "stat").write_text("\n".join(lines) + "\n")

    def test_hot_core_is_not_averaged_with_idle_core_or_guest_time(self):
        self.core_stat({"cpu0": [0] * 10, "cpu1": [0] * 10})
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        self.assertEqual(sampler.sample(now=10).core_cpu, {})
        self.core_stat(
            {
                "cpu0": [80, 0, 0, 20, 0, 0, 0, 0, 80, 0],
                "cpu1": [0, 0, 0, 100, 0, 0, 0, 0, 0, 0],
            }
        )
        self.thread(123, 123, 80, processor=1)
        sample = sampler.sample(now=11)
        self.assertEqual(sample.cpu, 80)
        self.assertEqual(sample.core_cpu, {"cpu0": 80, "cpu1": 0})

    def test_core_iowait_is_idle_and_other_first_eight_counters_are_busy(self):
        self.core_stat({"cpu0": [0] * 10})
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.core_stat({"cpu0": [60, 5, 5, 10, 10, 5, 0, 5, 60, 5]})
        self.assertEqual(sampler.sample(now=11).core_cpu, {"cpu0": 80})

    def test_missing_or_malformed_core_file_keeps_thread_cpu_usable(self):
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        for now, ticks, text in [(11, 75, None), (12, 150, "cpu0 broken")]:
            if text is not None:
                (self.root / "stat").write_text(text)
            self.thread(123, 123, ticks)
            sample = sampler.sample(now=now)
            with self.subTest(core_file=text):
                self.assertEqual(sample.cpu, 75)
                self.assertEqual(sample.core_cpu, {})
                self.assertEqual(metrics.metric_data("g", "i", sample)[0]["Value"], 1)

    def test_core_read_failure_discards_the_previous_baseline(self):
        self.core_stat({"cpu0": [0] * 8})
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.core_stat({"cpu0": [80, 0, 0, 20, 0, 0, 0, 0]})
        self.assertEqual(sampler.sample(now=11).core_cpu, {"cpu0": 80})
        (self.root / "stat").unlink()
        self.assertEqual(sampler.sample(now=12).core_cpu, {})
        self.core_stat({"cpu0": [900, 0, 0, 100, 0, 0, 0, 0]})
        self.assertEqual(sampler.sample(now=13).core_cpu, {})
        self.core_stat({"cpu0": [980, 0, 0, 120, 0, 0, 0, 0]})
        self.assertEqual(sampler.sample(now=14).core_cpu, {"cpu0": 80})

    def test_core_counter_regression_requires_a_new_baseline(self):
        self.core_stat({"cpu0": [100, 0, 0, 100, 0, 0, 0, 0]})
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.core_stat({"cpu0": [99, 0, 0, 200, 0, 0, 0, 0]})
        self.assertEqual(sampler.sample(now=11).core_cpu, {})
        self.core_stat({"cpu0": [179, 0, 0, 220, 0, 0, 0, 0]})
        self.assertEqual(sampler.sample(now=12).core_cpu, {"cpu0": 80})

    def test_unchanged_core_counters_do_not_fabricate_zero_utilization(self):
        self.core_stat({"cpu0": [0] * 8})
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.assertEqual(sampler.sample(now=11).core_cpu, {})

    def test_hotplugged_core_requires_baseline_without_resetting_other_cores(self):
        self.core_stat({"cpu0": [0] * 8, "cpu1": [0] * 8})
        sampler = metrics.ThreadSampler(self.selector(), ticks_per_second=100)
        sampler.sample(now=10)
        self.core_stat({"cpu0": [40, 0, 0, 60, 0, 0, 0, 0]})
        self.assertEqual(sampler.sample(now=11).core_cpu, {"cpu0": 40})
        self.core_stat(
            {"cpu0": [80, 0, 0, 120, 0, 0, 0, 0], "cpu1": [900, 0, 0, 100, 0, 0, 0, 0]}
        )
        self.assertEqual(sampler.sample(now=12).core_cpu, {"cpu0": 40})
        self.core_stat(
            {"cpu0": [120, 0, 0, 180, 0, 0, 0, 0], "cpu1": [980, 0, 0, 120, 0, 0, 0, 0]}
        )
        self.assertEqual(sampler.sample(now=13).core_cpu, {"cpu0": 40, "cpu1": 80})

    def test_core_metrics_preserve_individual_dimensions_and_collection_time(self):
        from datetime import datetime, timezone

        stamp = datetime(2026, 10, 12, tzinfo=timezone.utc)
        sample = metrics.ThreadSample(
            self.selector().select(), 80, 123, core_cpu={"cpu0": 80, "cpu1": 0}
        )
        payload = metrics.metric_data("group", "i-example", sample, timestamp=stamp)
        cores = [
            datum for datum in payload if datum["MetricName"] == "CPUCoreUtilization"
        ]
        self.assertEqual(len(cores), 2)
        self.assertEqual(
            [(datum["Dimensions"][-1], datum["Value"]) for datum in cores],
            [
                ({"Name": "Cpu", "Value": "cpu0"}, 80),
                ({"Name": "Cpu", "Value": "cpu1"}, 0),
            ],
        )
        for datum in payload:
            self.assertEqual(
                datum["Dimensions"][:2],
                [
                    {"Name": "AutoScalingGroupName", "Value": "group"},
                    {"Name": "InstanceId", "Value": "i-example"},
                ],
            )
            self.assertEqual(datum["Timestamp"], stamp)
            self.assertEqual(datum["StorageResolution"], 1)
        self.assertNotIn("PublisherReady", [datum["MetricName"] for datum in payload])

    def test_inspection_logs_per_core_map_without_readiness_or_aws_access(self):
        import json
        import subprocess
        import sys

        script = Path(metrics.__file__)
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--executable",
                self.exe,
                "--proc-root",
                str(self.root),
                "--inspect",
                "--samples",
                "2",
                "--interval",
                "0.001",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        samples = [
            json.loads(line[line.index("{") :]) for line in result.stderr.splitlines()
        ]
        self.assertEqual(len(samples), 2)
        self.assertIsNone(samples[0]["cpu_percent_one_vcpu"])
        self.assertEqual(samples[1]["cpu_percent_one_vcpu"], 0)
        for sample in samples:
            self.assertEqual(sample["cpu_core_utilization"], {})
            self.assertNotIn("registered_ready", sample)


@unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux procfs smoke test")
class LinuxSmokeTest(unittest.TestCase):
    def test_live_process_and_threads_are_measurable(self):
        import sys
        import time

        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            pid_file.write_text(str(os.getpid()))
            selector = metrics.ProcessSelector(
                os.path.realpath(sys.executable), pid_file=pid_file
            )
            sampler = metrics.ThreadSampler(selector)
            self.assertIsNone(sampler.sample().cpu)
            stop = time.monotonic() + 0.2
            while time.monotonic() < stop:
                pass
            result = sampler.sample()
            self.assertGreater(result.cpu, 50)
            self.assertLessEqual(result.cpu, 105)
            self.assertGreater(result.hottest_tid, 0)


if __name__ == "__main__":
    unittest.main()
