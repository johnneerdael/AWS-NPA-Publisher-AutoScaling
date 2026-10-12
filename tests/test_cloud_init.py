"""Provision the reviewed collector without changing publisher binaries."""

import base64
import gzip
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class CloudInitTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "scripts/generate-cloud-init.py"
        self.assertTrue(
            path.exists(), "Cloud-init generator is required for provisioning"
        )
        spec = importlib.util.spec_from_file_location("cloud_init", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def build(self, base=None):
        return self.module.build_cloud_config(
            "publishers", "/home/npa_publisher", "publisher", base=base
        )

    def test_cloud_init_embeds_exact_reviewed_collector_and_starts_it(self):
        config = self.build()
        files = {item["path"]: item for item in config["write_files"]}
        collector = files["/opt/npa-thread-metrics/publisher_metrics.py"]
        self.assertEqual(
            base64.b64decode(collector["content"]),
            (ROOT / "scripts/publisher_metrics.py").read_bytes(),
        )
        settings = json.loads(files["/etc/npa-thread-metrics.json"]["content"])
        self.assertEqual(settings["executable"], "/home/npa_publisher")
        self.assertEqual(settings["group"], "publishers")
        self.assertEqual(settings["instance_id"], "auto")
        self.assertNotIn("ready_file", settings)
        self.assertIn(
            ["systemctl", "restart", "npa-thread-metrics.service"], config["runcmd"]
        )
        self.assertIn("python3-venv", config["packages"])
        self.assertFalse(any("npa_publisher" in str(c) for c in config["runcmd"]))

    def test_existing_registration_cloud_config_is_preserved(self):
        base = {
            "packages": ["curl"],
            "runcmd": [["/opt/existing-register-publisher"]],
            "write_files": [{"path": "/etc/existing-bootstrap", "content": "retain"}],
            "bootcmd": [["echo", "existing"]],
        }
        config = self.build(base)
        self.assertEqual(config["runcmd"][0], ["/opt/existing-register-publisher"])
        self.assertEqual(config["bootcmd"], base["bootcmd"])
        self.assertIn("curl", config["packages"])
        self.assertEqual(config["write_files"][0]["path"], "/etc/existing-bootstrap")
        self.assertEqual(len(base["runcmd"]), 1)

    def test_conflicting_collector_file_and_malformed_base_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(
                {
                    "write_files": [
                        {"path": "/etc/npa-thread-metrics.json", "content": "old"}
                    ]
                }
            )
        with self.assertRaises(ValueError):
            self.build({"runcmd": "not-a-list"})

    def test_explicit_regional_ami_is_preserved_in_launch_input(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "publisher.gz"
            result = self.module.write_user_data(
                self.build(), output, ami_id="ami-059e32c80fcfbab6e"
            )
            self.assertEqual(
                json.loads(result.read_text())["ImageId"], "ami-059e32c80fcfbab6e"
            )
            with self.assertRaises(ValueError):
                self.module.write_user_data(self.build(), output, ami_id="invalid")

    def test_user_data_is_reproducible_compressed_and_under_ec2_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one.gz"
            second = Path(directory) / "two.gz"
            result = self.module.write_user_data(self.build(), first)
            self.module.write_user_data(self.build(), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertLessEqual(len(first.read_bytes()), 16 * 1024)
            decoded = gzip.decompress(first.read_bytes()).decode()
            self.assertTrue(decoded.startswith("#cloud-config\n"))
            config = yaml.safe_load(decoded)
            self.assertEqual(
                json.loads(
                    next(
                        f["content"]
                        for f in config["write_files"]
                        if f["path"] == "/etc/npa-thread-metrics.json"
                    )
                )["group"],
                "publishers",
            )
            payload = json.loads(result.read_text())
            self.assertEqual(base64.b64decode(payload["UserData"]), first.read_bytes())


if __name__ == "__main__":
    unittest.main()
