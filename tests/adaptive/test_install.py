import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _support import REPO

SPEC = importlib.util.spec_from_file_location("provost_install", REPO / "install.py")
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)

RUNTIME = "provost/runtime/native-pilot-v1"
BEGIN, END = "<!-- claudex-native:begin -->", "<!-- claudex-native:end -->"
SECRET = "ghp_SECRETVALUE0123456789"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def make_source(root):
    for name in ("_python.sh", "runtime", "native-event", "native-facts"):
        write(root / "hooks" / name, f"#!/bin/sh\necho {name}\n")
        os.chmod(root / "hooks" / name, 0o755)
    write(root / "hooks" / "native-reminder", "legacy\n")  # not managed
    for name in ("__init__.py", "store.py"):
        write(root / "scripts/provost_runtime" / name, f"# {name}\n")
    write(root / "scripts/provost_runtime/__pycache__/store.cpython-311.pyc", b"\0pyc")
    write(root / "docs/native-handoff.md", "# handoff\n")
    write(root / "docs/concepts.md", "not managed\n")
    write(root / "docs/compatibility/spec-kit.md", "# spec-kit\r\n")  # CRLF kept as bytes
    write(root / "agents/implementer.md", "implementer\n")
    write(root / "agents/code-reviewer.md", "reviewer\n")
    write(root / "skills/verification-before-completion/SKILL.md", "verify\n")
    write(root / "skills/handoff/SKILL.md", "repo handoff\n")
    write(root / "tools/dispatch-report.js", "console.log(1)\n")
    write(root / "native/CLAUDE.native.md", "# Policy\n\nline two\n")
    write(root / "install.py", "# placeholder\n")


def claude_md(body="old body\n", eol="\n"):
    text = f"# Mine\n\nkeep this\n{BEGIN}\n{body}{END}\ntrailer\n"
    return text.replace("\n", eol).encode("utf-8")


class InstallCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-install-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.source, self.config = base / "repo", base / "claude"
        make_source(self.source)
        write(self.config / "CLAUDE.md", claude_md())
        write(self.config / "agents/old.md.bak", "backup copy\n")
        write(self.config / "agents/other.md", "someone else's role\n")
        write(self.config / "skills/handoff/SKILL.md", "local handoff\n")
        write(self.config / "settings.json", json.dumps({"env": {"TOKEN": SECRET}}))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = install.main([*argv, "--source", str(self.source), "--config-dir", str(self.config)])
        return code, out.getvalue(), err.getvalue()

    def check_json(self):
        code, out, _ = self.run_cli("check", "--json")
        return code, json.loads(out)

    def statuses(self, report):
        return {item["path"]: item["status"] for item in report["targets"]}

    def snapshot(self):
        return {p.relative_to(self.config).as_posix(): p.read_bytes()
                for p in self.config.rglob("*") if p.is_file()}

    def backups(self):
        root = self.config / "provost/backups"
        return sorted(root.iterdir()) if root.is_dir() else []


class CheckTests(InstallCase):
    def test_fresh_config_reports_missing_and_drift(self):
        code, report = self.check_json()
        self.assertEqual(code, 1)
        status = self.statuses(report)
        self.assertEqual(status[f"{RUNTIME}/hooks/native-event"], "missing")
        self.assertEqual(status[f"{RUNTIME}/scripts/provost_runtime/store.py"], "missing")
        self.assertEqual(status["experiments/dispatch-report.js"], "missing")
        self.assertEqual(status["CLAUDE.md"], "modified")
        self.assertTrue(report["drift"])
        self.assertFalse(any("__pycache__" in path for path in status))

    def test_check_is_default_command_and_read_only(self):
        before = self.snapshot()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("missing", out)
        self.assertEqual(self.snapshot(), before)

    def test_modified_and_extra_after_install(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        write(self.config / RUNTIME / "hooks/runtime", "tampered\n")
        write(self.config / RUNTIME / "hooks/native-reminder", "old\n")
        write(self.config / RUNTIME / "scripts/provost_runtime/__pycache__/x.pyc", b"x")
        code, report = self.check_json()
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(report)[f"{RUNTIME}/hooks/runtime"], "modified")
        self.assertEqual(report["extra"], [f"{RUNTIME}/hooks/native-reminder"])

    def test_extra_alone_is_not_drift_and_agents_extras_not_reported(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        write(self.config / RUNTIME / "hooks/native-reminder", "old\n")
        code, report = self.check_json()
        self.assertEqual(code, 0)
        self.assertFalse(report["drift"])
        self.assertEqual(report["extra"], [f"{RUNTIME}/hooks/native-reminder"])
        self.assertFalse(any(path.startswith("agents/") for path in report["extra"]))

    def test_missing_source_is_error(self):
        (self.source / "tools/dispatch-report.js").unlink()
        for command in ("check", "install"):
            code, _, err = self.run_cli(command)
            self.assertEqual(code, 2, command)
            self.assertIn("dispatch-report.js", err)


class InstallTests(InstallCase):
    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        code, out, _ = self.run_cli("install", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("experiments/dispatch-report.js", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])

    def test_install_then_check_is_clean_and_idempotent(self):
        code, out, _ = self.run_cli("install")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn(str(self.backups()[0]), out)
        code, report = self.check_json()
        self.assertEqual(code, 0, report)
        self.assertEqual(set(self.statuses(report).values()), {"ok"})
        self.assertEqual((self.config / RUNTIME / "docs/compatibility/spec-kit.md").read_bytes(),
                         b"# spec-kit\r\n")
        self.assertEqual((self.config / "agents/implementer.md").read_bytes(), b"implementer\n")
        before = self.snapshot()
        code, out, _ = self.run_cli("install")
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backups()), 1)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_hook_mode_is_copied(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        mode = (self.config / RUNTIME / "hooks/native-event").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_backup_contents_and_manifest(self):
        write(self.config / "agents/implementer.md", "previous implementer\n")
        self.assertEqual(self.run_cli("install")[0], 0)
        backup = self.backups()[0]
        self.assertRegex(backup.name, r"^install-\d{8}T\d{6}Z")
        self.assertEqual((backup / "agents/implementer.md").read_bytes(), b"previous implementer\n")
        self.assertEqual((backup / "CLAUDE.md").read_bytes(), claude_md())
        self.assertFalse((backup / "agents/code-reviewer.md").exists())
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["changed"]), ["CLAUDE.md", "agents/implementer.md"])
        self.assertIn("agents/code-reviewer.md", manifest["created"])
        self.assertIn(f"{RUNTIME}/hooks/native-event", manifest["created"])

    def test_rollback_on_failed_write(self):
        write(self.config / "agents/implementer.md", "previous implementer\n")
        before = self.snapshot()
        real_replace = os.replace

        def flaky(src, dst):  # the policy section is the last write: earlier ones must be undone
            if Path(dst).name == "CLAUDE.md":
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(install.os, "replace", flaky):
            code, out, err = self.run_cli("install")
        self.assertEqual(code, 2)
        self.assertIn("rolled back", err)
        self.assertNotIn("rollback incomplete", err)
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)
        self.assertFalse((self.config / RUNTIME).exists())
        self.assertFalse(any(p.name.startswith(".pending-") for p in self.config.rglob("*")))

    def test_handoff_skill_never_installed(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual((self.config / "skills/handoff/SKILL.md").read_bytes(), b"local handoff\n")
        self.assertEqual((self.config / "skills/verification-before-completion/SKILL.md").read_bytes(),
                         b"verify\n")
        _, report = self.check_json()
        self.assertFalse(any("skills/handoff" in path for path in self.statuses(report)))

    def test_agents_dir_other_files_untouched(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual((self.config / "agents/old.md.bak").read_bytes(), b"backup copy\n")
        self.assertEqual((self.config / "agents/other.md").read_bytes(), b"someone else's role\n")

    def test_settings_json_never_written(self):
        before = (self.config / "settings.json").read_bytes()
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual((self.config / "settings.json").read_bytes(), before)


class PolicySectionTests(InstallCase):
    def assert_policy(self, eol):
        write(self.config / "CLAUDE.md", claude_md(eol=eol))
        self.assertEqual(self.run_cli("install")[0], 0)
        expected = claude_md(body="# Policy\n\nline two\n", eol=eol)
        self.assertEqual((self.config / "CLAUDE.md").read_bytes(), expected)
        self.assertEqual(self.check_json()[0], 0)

    def test_replace_between_markers_lf(self):
        self.assert_policy("\n")

    def test_replace_between_markers_crlf(self):
        self.assert_policy("\r\n")

    def test_marker_errors_never_rewrite(self):
        cases = {
            "missing": b"# Mine\nno markers\n",
            "duplicate": claude_md() + f"{BEGIN}\n".encode(),
            "misordered": f"{END}\nbody\n{BEGIN}\n".encode(),
        }
        for name, data in cases.items():
            with self.subTest(name):
                write(self.config / "CLAUDE.md", data)
                for command in ("check", "install"):
                    code, _, err = self.run_cli(command)
                    self.assertEqual(code, 2, command)
                    self.assertIn("marker", err)
                self.assertEqual((self.config / "CLAUDE.md").read_bytes(), data)
                self.assertFalse((self.config / RUNTIME).exists())

    def test_missing_claude_md_is_error(self):
        (self.config / "CLAUDE.md").unlink()
        self.assertEqual(self.run_cli("install")[0], 2)
        self.assertFalse((self.config / "CLAUDE.md").exists())


class SettingsTests(InstallCase):
    def settings(self, value):
        if value is None:
            (self.config / "settings.json").unlink()
        else:
            write(self.config / "settings.json", value if isinstance(value, str) else json.dumps(value))
        code, out, err = self.run_cli("check", "--json")
        self.assertNotIn(SECRET, out + err)
        text_out = self.run_cli("check")
        self.assertNotIn(SECRET, text_out[1] + text_out[2])
        return json.loads(out)["settings"]

    def hooks(self, start, post):
        return {"env": {"TOKEN": SECRET}, "hooks": {
            "SessionStart": [{"hooks": [dict(type="command", **start)]}],
            "PostToolUse": [{"matcher": "*", "hooks": [dict(type="command", **post)]}]}}

    def test_plain_command_strings(self):
        base = "/home/u/.claude/" + RUNTIME + "/hooks/"
        self.assertEqual(self.settings(self.hooks({"command": base + "native-event"},
                                                  {"command": f'bash "{base}native-facts"'})), "ok")

    def test_command_with_args_and_backslashes(self):
        bash = "C:/Program Files/Git/bin/bash.exe"
        base = "C:\\Users\\u\\.claude\\provost\\runtime\\native-pilot-v1\\hooks\\"
        self.assertEqual(self.settings(self.hooks({"command": bash, "args": [base + "native-event"]},
                                                  {"command": bash, "args": [base + "native-facts"]})), "ok")

    def test_not_registered(self):
        self.assertEqual(self.settings({"env": {"TOKEN": SECRET}}), "not-registered")
        partial = self.hooks({"command": f"/x/{RUNTIME}/hooks/native-event"}, {"command": "/x/other"})
        self.assertEqual(self.settings(partial), "not-registered")

    def test_missing_or_unparsable_is_unknown(self):
        self.assertEqual(self.settings(None), "unknown")
        self.assertEqual(self.settings('{"env": {"TOKEN": "' + SECRET + '"'), "unknown")

    def test_settings_not_drift(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        code, report = self.check_json()
        self.assertEqual((code, report["settings"]), (0, "not-registered"))


class SafetyTests(InstallCase):
    def test_config_inside_source_refused(self):
        inner = self.source / "claude"
        write(inner / "CLAUDE.md", claude_md())
        self.config = inner
        before = self.snapshot()
        self.assertEqual(self.run_cli("install")[0], 2)
        self.assertEqual(self.snapshot(), before)

    def test_source_inside_config_refused(self):
        self.source = self.config / "repo-copy"
        make_source(self.source)
        self.assertEqual(self.run_cli("install")[0], 2)
        self.assertFalse((self.config / RUNTIME).exists())

    def test_symlinked_target_escape_refused(self):
        outside = self.config.parent / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.config / "experiments", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.run_cli("install")[0], 2)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.config / RUNTIME).exists())

    def test_cli_entrypoint_exit_code(self):
        result = subprocess.run(
            [sys.executable, "-I", str(REPO / "install.py"), "check",
             "--source", str(self.source), "--config-dir", str(self.config)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
