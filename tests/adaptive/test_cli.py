import json
import os
import subprocess
import sys
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind


class CliTests(FixtureCase):
    def run_cli(self, *args, data=None):
        env = dict(os.environ, PYTHONPATH=str(REPO / "scripts"),
                   PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-m", "provost_runtime.cli", *args],
                              input=json.dumps(data) if data is not None else "", capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=30)

    def test_doctor_is_read_only_and_reports_selected_mode(self):
        result = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["mode"], "adaptive")
        self.assertFalse((self.root / ".provost").exists())

    def test_record_snapshot_list_and_resume_roundtrip(self):
        bind(self.root, "s1", {})
        bind(self.root, "s2", {})
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1",
                              "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.run_cli("snapshot", "--workspace", str(self.root), "--session", "s1", "--request-id", "e1")
        ident = json.loads(data.stdout)["snapshot_id"]
        listings = self.run_cli("list", "--workspace", str(self.root))
        self.assertEqual(len(json.loads(listings.stdout)), 1)
        result = self.run_cli("resume", "--workspace", str(self.root), "--session", "s2", "--from", ident)
        self.assertEqual(json.loads(result.stdout)["adopted_from"], ident)

    def test_disabled_manual_write_is_rejected(self):
        self.config_path.unlink()
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1",
                              "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / ".provost").exists())

    def test_malformed_hook_is_nonblocking_and_does_not_echo_input(self):
        result = self.run_cli("hook", data=["sensitive-marker"])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("sensitive-marker", result.stdout)
        self.assertNotIn("block", json.loads(result.stdout).get("decision", ""))
