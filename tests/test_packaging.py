"""Ensure deployment executes the same source exercised by the tests."""

import hashlib
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "package_controller", ROOT / "scripts/package-controller.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PackagingTests(unittest.TestCase):
    def test_zip_is_reproducible_and_contains_exact_controller_source(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"
            second = Path(directory) / "second.zip"
            checksum = MODULE.package(first)
            self.assertEqual(checksum, MODULE.package(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(checksum, hashlib.sha256(first.read_bytes()).hexdigest())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["scaling_controller.py"])
                self.assertEqual(
                    archive.read("scaling_controller.py"),
                    (ROOT / "scripts/controller/scaling_controller.py").read_bytes(),
                )
                compile(
                    archive.read("scaling_controller.py"),
                    "scaling_controller.py",
                    "exec",
                )


if __name__ == "__main__":
    unittest.main()
