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
                    "ready_file": "/run/npa-metrics/ready.json",
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

    def test_ready_proof_expires_instead_of_remaining_ready_forever(self):
        marker = self.root / "ready.json"
        identity = self.selector().select()
        metrics.mark_ready(marker, identity, now=1000)
        self.assertTrue(metrics.is_ready(marker, identity, now=1100))
        self.assertFalse(metrics.is_ready(marker, identity, now=1200))
        self.assertFalse(metrics.is_ready(marker, identity, now=900))

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

    def test_readiness_marker_is_bound_to_process_start(self):
        marker = self.root / "ready.json"
        identity = self.selector().select()
        self.assertFalse(metrics.is_ready(marker, identity))
        metrics.mark_ready(marker, identity)
        self.assertTrue(metrics.is_ready(marker, identity))
        (self.root / "123/stat").write_text(stat_text(123, 0, 200))
        self.assertFalse(metrics.is_ready(marker, self.selector().select()))
        marker.write_text("broken")
        self.assertFalse(metrics.is_ready(marker, identity))

    def test_metric_dimensions_and_health_failure_does_not_emit_cpu(self):
        identity = self.selector().select()
        result = metrics.ThreadSample(identity, 70, 123)
        payload = metrics.metric_data("group", "i-example", result, ready=True)
        values = {item["MetricName"]: item["Value"] for item in payload}
        self.assertEqual(
            values, {"HottestThreadCPU": 70, "PublisherReady": 1, "CollectorHealthy": 1}
        )
        for item in payload:
            self.assertEqual(
                item["Dimensions"],
                [
                    {"Name": "AutoScalingGroupName", "Value": "group"},
                    {"Name": "InstanceId", "Value": "i-example"},
                ],
            )
        failed = metrics.metric_data("group", "i-example", None, ready=False)
        self.assertEqual(
            {i["MetricName"]: i["Value"] for i in failed},
            {"PublisherReady": 0, "CollectorHealthy": 0},
        )


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
