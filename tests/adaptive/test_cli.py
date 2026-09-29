import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind


class CliTests(FixtureCase):
    def run_cli(self, *args, data=None, scripts=None):
        env = dict(os.environ, PYTHONPATH=str(scripts or REPO / "scripts"),
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

    def test_record_input_file_replaces_stdin_and_keeps_limits(self):
        bind(self.root, "s1", {})
        args = ("record", "--workspace", str(self.root), "--session", "s1", "--expected", "null")
        path = self.root / "payload.json"
        path.write_text(json.dumps(self.payload("from file")), encoding="utf-8")
        result = self.run_cli(*args, "--request-id", "r1", "--input", str(path), data={"ignored": "stdin"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["payload"]["next"], "from file")
        big = self.root / "big.json"
        big.write_text(json.dumps(self.payload("é" * 20000), ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(*args, "--request-id", "r2", "--input", str(big))
        self.assertEqual(json.loads(result.stdout), {"error": "PAYLOAD_TOO_LARGE"})
        for name, content in (("missing.json", None), ("bad.json", "{broken")):
            target = self.root / name
            if content is not None:
                target.write_text(content, encoding="utf-8")
            result = self.run_cli(*args, "--request-id", "r3", "--input", str(target))
            self.assertEqual(json.loads(result.stdout), {"error": "INPUT_OR_IO_ERROR"}, name)

    def copy_runtime(self, name, newline=None):
        root = self.root / name
        (root / "scripts").mkdir(parents=True)
        (root / "hooks").mkdir()
        shutil.copytree(REPO / "scripts" / "provost_runtime", root / "scripts" / "provost_runtime",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for script in ("runtime", "native-event", "_python.sh"):
            shutil.copyfile(REPO / "hooks" / script, root / "hooks" / script)
        if newline:
            for path in [*(root / "scripts" / "provost_runtime").glob("*.py"), *(root / "hooks").iterdir()]:
                text = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline)
                path.write_bytes(text)
        return root

    def doctor(self, runtime):
        result = self.run_cli("doctor", "--workspace", str(self.root), scripts=runtime / "scripts")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_doctor_reports_runtime_root_and_hash(self):
        data = json.loads(self.run_cli("doctor", "--workspace", str(self.root)).stdout)
        self.assertEqual(Path(data["runtime_root"]).resolve(), REPO.resolve())
        self.assertRegex(data["runtime_hash"], r"^[0-9a-f]{64}$")
        for key in ("mode", "python_supported", "handoff", "storage_ready", "storage_error", "host_verified"):
            self.assertIn(key, data)

    def test_runtime_hash_ignores_crlf_but_tracks_content_and_absence(self):
        lf, crlf = self.copy_runtime("lf", b"\n"), self.copy_runtime("crlf", b"\r\n")
        self.assertEqual(Path(self.doctor(crlf)["runtime_root"]).resolve(), crlf.resolve())
        base = self.doctor(lf)["runtime_hash"]
        self.assertEqual(self.doctor(crlf)["runtime_hash"], base)
        with (crlf / "hooks" / "runtime").open("ab") as handle:
            handle.write(b"# changed\r\n")
        self.assertNotEqual(self.doctor(crlf)["runtime_hash"], base)
        (lf / "hooks" / "native-event").unlink()
        self.assertNotEqual(self.doctor(lf)["runtime_hash"], base)

    def test_runtime_hash_covers_the_shared_python_resolver(self):
        runtime = self.copy_runtime("resolver")
        base = self.doctor(runtime)["runtime_hash"]
        (runtime / "hooks" / "_python.sh").unlink()
        self.assertNotEqual(self.doctor(runtime)["runtime_hash"], base)
