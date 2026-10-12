"""Exercise the scanner on checkout metadata and an actual source finding."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SecretScanningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / "scripts").mkdir()
        source = Path(__file__).resolve().parents[1] / "scripts/check-secrets.py"
        self.script = self.root / "scripts/check-secrets.py"
        shutil.copyfile(source, self.script)
        (self.root / ".secrets.baseline").write_text(json.dumps({"results": {}}))

    def scan(self):
        return subprocess.run(
            [sys.executable, str(self.script)],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def test_git_fetch_metadata_is_not_a_source_secret(self):
        commit = hashlib.sha256(b"fixture commit").hexdigest()[:40]
        (self.root / ".git/FETCH_HEAD").write_text(
            commit + "\t\t'" + commit + "' of https://github.com/example/repo\n"
        )
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_new_source_secret_still_fails(self):
        (self.root / "app.py").write_text(
            "password = 'scanner-fixture'\n"  # pragma: allowlist secret
        )
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("app.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
