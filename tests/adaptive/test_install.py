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
    for name in ("_python.sh", "runtime", "native-event", "native-facts", "acceptance-gate"):
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


def link_directory(link, target):
    """Make link point at target: a symlink, else (Windows) a directory junction. False if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return False
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError):
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, timeout=30)
        if result.returncode != 0:
            return False
    return link.exists()


def remove_link(link):
    """Remove a symlink or junction without touching its target."""
    try:
        os.rmdir(link) if os.path.isdir(link) and not os.path.islink(link) else os.unlink(link)
    except OSError:
        pass


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

    def test_dry_run_is_rejected_for_check(self):
        for argv in (["check", "--dry-run"], ["--dry-run"]):  # bare form defaults to check
            with self.subTest(argv=argv):
                before = self.snapshot()
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                    install.main([*argv, "--source", str(self.source), "--config-dir", str(self.config)])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--dry-run", err.getvalue())
                self.assertEqual(self.snapshot(), before)

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
        self.assertEqual(sorted(manifest["changed"]),
                         ["CLAUDE.md", "agents/implementer.md", "skills/handoff/SKILL.md"])
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

    def test_interrupt_rolls_back_and_names_backup(self):
        write(self.config / "agents/implementer.md", "previous implementer\n")
        before = self.snapshot()
        real_replace, calls = os.replace, []

        def interrupted(src, dst):
            calls.append(dst)
            if len(calls) == 4:
                raise KeyboardInterrupt
            return real_replace(src, dst)

        err = io.StringIO()
        with mock.patch.object(install.os, "replace", interrupted), redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                install.main(["install", "--source", str(self.source), "--config-dir", str(self.config)])
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("rolled back", err.getvalue())
        self.assertNotIn("rollback incomplete", err.getvalue())
        self.assertIn(str(self.backups()[0]), err.getvalue())
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)
        self.assertFalse((self.config / RUNTIME).exists())
        self.assertFalse(any(p.name.startswith(".pending-") for p in self.config.rglob("*")))

    def test_interrupt_right_after_replace_restores_that_target(self):
        write(self.config / "agents/implementer.md", "previous implementer\n")
        before = self.snapshot()
        real_write = install.atomic_write

        def interrupted_after(path, data, mode=None):  # the replace landed, the bookkeeping did not
            real_write(path, data, mode)
            if Path(path).name == "implementer.md":
                raise KeyboardInterrupt

        err = io.StringIO()
        with mock.patch.object(install, "atomic_write", interrupted_after), redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                install.main(["install", "--source", str(self.source), "--config-dir", str(self.config)])
        self.assertIn("all changes rolled back", err.getvalue())
        self.assertEqual((self.config / "agents/implementer.md").read_bytes(), b"previous implementer\n")
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)
        self.assertFalse((self.config / RUNTIME).exists())

    def pending(self):
        return [p for p in self.config.rglob("*") if p.name.startswith(".pending-")]

    def test_atomic_write_falls_back_in_place_when_replace_denied(self):
        target = self.config / "agents/implementer.md"
        write(target, "previous implementer, longer than the new one\n")

        def denied(src, dst):
            raise PermissionError(5, "Access denied")

        with mock.patch.object(install.os, "replace", denied):
            install.atomic_write(target, b"new\n")
        self.assertEqual(target.read_bytes(), b"new\n")
        self.assertEqual(self.pending(), [])

    def test_atomic_write_denied_on_missing_target_propagates(self):
        target = self.config / "agents/new-role.md"

        def denied(src, dst):
            raise PermissionError(5, "Access denied")

        with mock.patch.object(install.os, "replace", denied):
            with self.assertRaises(PermissionError):
                install.atomic_write(target, b"new\n")
        self.assertFalse(target.exists())
        self.assertEqual(self.pending(), [])

    def test_atomic_write_other_replace_error_propagates_and_keeps_target(self):
        target = self.config / "agents/implementer.md"
        write(target, "previous implementer\n")

        def full(src, dst):
            raise OSError("disk full")

        with mock.patch.object(install.os, "replace", full):
            with self.assertRaises(OSError) as caught:
                install.atomic_write(target, b"new\n")
        self.assertNotIsInstance(caught.exception, PermissionError)
        self.assertEqual(target.read_bytes(), b"previous implementer\n")
        self.assertEqual(self.pending(), [])

    def test_install_writes_in_place_when_replace_denied_on_agents(self):
        write(self.config / "agents/implementer.md", "previous implementer\n")
        real_replace = os.replace

        def denied_on_agents(src, dst):
            if Path(dst).parent.name == "agents" and Path(dst).exists():
                raise PermissionError(5, "Access denied")
            return real_replace(src, dst)

        with mock.patch.object(install.os, "replace", denied_on_agents):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.config / "agents/implementer.md").read_bytes(), b"implementer\n")
        self.assertEqual(self.pending(), [])

    def test_backup_phase_failure_says_nothing_changed(self):
        write(self.config / "agents/implementer.md", "previous implementer\n")
        before = self.snapshot()

        def broken_copy(src, dst, *args, **kwargs):
            raise OSError("backup disk full")

        with mock.patch.object(install.shutil, "copy2", broken_copy):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 2)
        self.assertIn("backup disk full", err)
        self.assertIn("no managed file was changed", err)
        partial = self.backups()
        self.assertEqual(len(partial), 1)
        self.assertIn(str(partial[0]), err)
        self.assertIn("no manifest", err)
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)

    def test_manifest_write_failure_says_nothing_changed(self):
        real_replace = os.replace

        def flaky(src, dst):
            if Path(dst).name == "manifest.json":
                raise OSError("manifest refused")
            return real_replace(src, dst)

        before = self.snapshot()
        with mock.patch.object(install.os, "replace", flaky):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 2)
        self.assertIn("no managed file was changed", err)
        self.assertIn(str(self.backups()[0]), err)
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)

    def side_backups(self):
        return sorted((self.config / "skills/handoff").glob("SKILL.md.local-*.bak"))

    def test_handoff_skill_is_checked_missing_drift_and_ok(self):
        _, report = self.check_json()
        self.assertEqual(self.statuses(report)["skills/handoff/SKILL.md"], "modified")
        (self.config / "skills/handoff/SKILL.md").unlink()
        _, report = self.check_json()
        self.assertEqual(self.statuses(report)["skills/handoff/SKILL.md"], "missing")
        self.assertEqual(self.run_cli("install")[0], 0)
        _, report = self.check_json()
        self.assertEqual(self.statuses(report)["skills/handoff/SKILL.md"], "ok")
        self.assertEqual(self.side_backups(), [])  # nothing local to keep

    def test_handoff_skill_install_keeps_the_local_copy_beside_it_once(self):
        code, out, _ = self.run_cli("install", "--json")
        self.assertEqual(code, 0)
        skill = self.config / "skills/handoff/SKILL.md"
        self.assertEqual(skill.read_bytes(), b"repo handoff\n")
        [kept] = self.side_backups()
        self.assertRegex(kept.name, r"^SKILL\.md\.local-\d{8}T\d{6}Z(-\d+)?\.bak$")
        self.assertEqual(kept.read_bytes(), b"local handoff\n")
        self.assertEqual(json.loads(out)["kept_local"], [kept.relative_to(self.config).as_posix()])
        manifest = json.loads((self.backups()[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["kept_local"], [kept.relative_to(self.config).as_posix()])
        self.assertEqual((self.backups()[0] / "skills/handoff/SKILL.md").read_bytes(), b"local handoff\n")
        # Later overwrites replace the installer's own copy: the first local copy stays, no new one.
        write(self.source / "skills/handoff/SKILL.md", "repo handoff v2\n")
        write(skill, "edited again\n")
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual(skill.read_bytes(), b"repo handoff v2\n")
        self.assertEqual(self.side_backups(), [kept])
        self.assertEqual(kept.read_bytes(), b"local handoff\n")
        _, report = self.check_json()
        self.assertNotIn(kept.relative_to(self.config).as_posix(), report["extra"])

    def test_handoff_skill_side_backup_never_overwrites_an_existing_name(self):
        stamp = "20261014T000000Z"
        taken = self.config / f"skills/handoff/SKILL.md.local-{stamp}.bak"
        with mock.patch.object(install, "_stamp", return_value=stamp):
            path = install.keep_local(self.config, "skills/handoff/SKILL.md")
            self.assertEqual(path.name, f"SKILL.md.local-{stamp}.bak")
            write(self.config / "skills/handoff/SKILL.md", "second local\n")
            again = install.keep_local(self.config, "skills/handoff/SKILL.md", force=True)
        self.assertEqual(taken.read_bytes(), b"local handoff\n")
        self.assertEqual(again.name, f"SKILL.md.local-{stamp}-1.bak")
        self.assertEqual(again.read_bytes(), b"second local\n")

    def test_failed_write_drops_the_kept_skill_copy_and_restores_the_local_one(self):
        before = self.snapshot()
        real_replace = os.replace

        def flaky(src, dst):  # CLAUDE.md is written last: the skill was already overwritten by then
            if Path(dst).name == "CLAUDE.md":
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(install.os, "replace", flaky):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 2)
        self.assertIn("all changes rolled back", err)
        self.assertNotIn("rollback incomplete", err)
        self.assertEqual(self.side_backups(), [])
        self.assertEqual((self.config / "skills/handoff/SKILL.md").read_bytes(), b"local handoff\n")
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)

    def test_failed_backup_phase_drops_the_kept_skill_copy(self):
        real_replace = os.replace

        def flaky(src, dst):  # the kept copy exists by the time the manifest is written
            if Path(dst).name == "manifest.json":
                self.assertEqual(len(self.side_backups()), 1)
                raise OSError("manifest refused")
            return real_replace(src, dst)

        before = self.snapshot()
        with mock.patch.object(install.os, "replace", flaky):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 2)
        self.assertIn("no managed file was changed", err)
        self.assertEqual(self.side_backups(), [])
        self.assertEqual((self.config / "skills/handoff/SKILL.md").read_bytes(), b"local handoff\n")
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)

    def test_exhausted_kept_copy_names_fails_like_any_backup_failure(self):
        before = self.snapshot()
        # Every candidate name reports as taken, so keep_local raises InstallError (not OSError).
        with mock.patch.object(install, "open", side_effect=FileExistsError, create=True):
            code, _, err = self.run_cli("install")
        self.assertEqual(code, 2)
        self.assertIn("could not create a unique local copy", err)
        self.assertIn("backup failed", err)
        self.assertIn("no managed file was changed", err)
        [partial] = self.backups()
        self.assertIn(str(partial), err)
        self.assertIn("can be deleted", err)
        self.assertEqual(self.side_backups(), [])
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)

    def test_dry_run_names_the_local_skill_copy_it_would_keep(self):
        before = self.snapshot()
        code, out, _ = self.run_cli("install", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would keep local skills/handoff/SKILL.md", out)
        self.assertEqual(self.snapshot(), before)

    def test_verification_skill_installed(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual((self.config / "skills/verification-before-completion/SKILL.md").read_bytes(),
                         b"verify\n")

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

    def assert_rejected_with_hint(self, data, *hints):
        write(self.config / "CLAUDE.md", data)
        for command in ("check", "install"):
            code, _, err = self.run_cli(command)
            self.assertEqual(code, 2, command)
            self.assertIn("marker", err)
            for hint in hints:
                self.assertIn(hint, err)
        self.assertEqual((self.config / "CLAUDE.md").read_bytes(), data)
        self.assertFalse((self.config / RUNTIME).exists())

    def test_bom_before_begin_marker_is_rejected_with_hint(self):
        data = b"\xef\xbb\xbf" + f"{BEGIN}\nold\n{END}\ntrailer\n".encode()
        self.assert_rejected_with_hint(data, "begin marker", "BOM", "line 1")

    def test_trailing_whitespace_on_marker_is_rejected_with_hint(self):
        for suffix in (" ", "\t"):
            with self.subTest(suffix=repr(suffix)):
                data = claude_md().replace(BEGIN.encode(), (BEGIN + suffix).encode())
                self.assert_rejected_with_hint(data, "whitespace", "line 4")
        self.assert_rejected_with_hint(claude_md().replace(END.encode(), (END + " ").encode()),
                                       "whitespace", "line 6")

    def test_leading_whitespace_on_marker_is_rejected_with_hint(self):
        self.assert_rejected_with_hint(claude_md().replace(BEGIN.encode(), b"  " + BEGIN.encode()),
                                       "whitespace", "line 4")

    def test_end_marker_without_final_newline(self):
        data = f"# Mine\n{BEGIN}\nold\n{END}".encode()
        write(self.config / "CLAUDE.md", data)
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual((self.config / "CLAUDE.md").read_bytes(),
                         f"# Mine\n{BEGIN}\n# Policy\n\nline two\n{END}".encode())
        self.assertEqual(self.check_json()[0], 0)

    def test_empty_section_is_filled(self):
        write(self.config / "CLAUDE.md", f"{BEGIN}\n{END}\n".encode())
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual((self.config / "CLAUDE.md").read_bytes(),
                         f"{BEGIN}\n# Policy\n\nline two\n{END}\n".encode())

    def test_empty_policy_source_is_refused(self):
        bom = b"\xef\xbb\xbf"
        nbsp = " ".encode()
        for data in (b"", b" \r\n\t\n", bom, bom + b" \r\n\t\n", nbsp, bom + nbsp + b"\n" + " ".encode()):
            write(self.source / "native/CLAUDE.native.md", data)
            for command in ("check", "install"):
                with self.subTest(data=data, command=command):
                    before = self.snapshot()
                    code, _, err = self.run_cli(command)
                    self.assertEqual(code, 2)
                    self.assertIn("CLAUDE.native.md", err)
                    self.assertIn("empty", err)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.backups(), [])

    def test_policy_source_starting_with_a_bom_still_installs(self):
        write(self.source / "native/CLAUDE.native.md", b"\xef\xbb\xbf# Policy\n")
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual(self.run_cli("check")[0], 0)
        self.assertIn(b"# Policy", (self.config / "CLAUDE.md").read_bytes())

    def test_mixed_line_endings_outside_the_section_are_preserved(self):
        for begin_eol, body_eol in ((b"\n", b"\n"), (b"\r\n", b"\r\n")):
            with self.subTest(begin_eol=begin_eol):
                prefix = b"# Mine\r\nkeep\nold\rlone cr\r\n"
                suffix = b"\r\ntrailer\nlast\r\nno newline"
                data = prefix + BEGIN.encode() + begin_eol + b"old" + body_eol + END.encode() + suffix
                write(self.config / "CLAUDE.md", data)
                self.assertEqual(self.run_cli("install")[0], 0)
                body = b"# Policy\n\nline two\n".replace(b"\n", begin_eol)
                self.assertEqual((self.config / "CLAUDE.md").read_bytes(),
                                 prefix + BEGIN.encode() + begin_eol + body + END.encode() + suffix)
                self.assertEqual(self.check_json()[0], 0)

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

    def hooks(self, start, post, gate=None, pre=None):
        """gate: the synchronous Stop entry; by default start's command with acceptance-gate for native-event.
        pre: the PreToolUse Agent entry; by default the same as gate's default."""
        default = json.loads(json.dumps(start).replace("native-event", "acceptance-gate"))
        gate = default if gate is None else gate
        pre = default if pre is None else pre
        return {"env": {"TOKEN": SECRET}, "hooks": {
            "SessionStart": [{"hooks": [dict(type="command", **start)]}],
            "PreToolUse": [{"matcher": "Agent", "hooks": [dict(type="command", **pre)]}],
            "PostToolUse": [{"matcher": "*", "hooks": [dict(type="command", **post)]}],
            "Stop": [{"hooks": [dict(type="command", **start)]}, {"hooks": [dict(type="command", **gate)]}]}}

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
        no_stop = self.hooks({"command": f"/x/{RUNTIME}/hooks/native-event"},
                             {"command": f"/x/{RUNTIME}/hooks/native-facts"})
        del no_stop["hooks"]["Stop"]
        self.assertEqual(self.settings(no_stop), "not-registered")

    def test_synchronous_stop_gate_is_required(self):
        base = f"/x/{RUNTIME}/hooks/"
        start, post = {"command": base + "native-event"}, {"command": base + "native-facts"}
        self.assertEqual(self.settings(self.hooks(start, post)), "ok")
        no_gate = self.hooks(start, post)
        del no_gate["hooks"]["Stop"][1]
        self.assertEqual(self.settings(no_gate), "not-registered")
        # An async registration cannot block the turn end, so it does not count.
        async_gate = self.hooks(start, post, gate={"command": base + "acceptance-gate", "async": True})
        self.assertEqual(self.settings(async_gate), "not-registered")

    def test_pretooluse_acceptance_gate_is_required(self):
        base = f"/x/{RUNTIME}/hooks/"
        start, post = {"command": base + "native-event"}, {"command": base + "native-facts"}
        no_pre = self.hooks(start, post)
        del no_pre["hooks"]["PreToolUse"]
        self.assertEqual(self.settings(no_pre), "not-registered")
        # An async hook cannot add context to the dispatch, so it does not count.
        async_pre = self.hooks(start, post, pre={"command": base + "acceptance-gate", "async": True})
        self.assertEqual(self.settings(async_pre), "not-registered")
        other = self.hooks(start, post, pre={"command": base + "native-facts"})
        self.assertEqual(self.settings(other), "not-registered")

    def test_missing_or_unparsable_is_unknown(self):
        self.assertEqual(self.settings(None), "unknown")
        self.assertEqual(self.settings('{"env": {"TOKEN": "' + SECRET + '"'), "unknown")

    def test_settings_not_drift(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        code, report = self.check_json()
        self.assertEqual((code, report["settings"]), (0, "not-registered"))


class SetupTests(InstallCase):
    EVENTS = ("SessionStart", "PreCompact", "PostCompact", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")

    def run_setup(self, *argv):
        code, out, err = self.run_cli("setup", *argv)
        self.assertNotIn(SECRET, out + err)
        return code, out, err

    def settings_data(self):
        return json.loads((self.config / "settings.json").read_text(encoding="utf-8-sig"))

    def provost_commands(self, event):
        return [hook["command"] for group in self.settings_data()["hooks"].get(event, [])
                for hook in group["hooks"] if RUNTIME in hook.get("command", "")]

    def test_fresh_config_is_fully_set_up(self):
        (self.config / "CLAUDE.md").unlink()
        (self.config / "settings.json").unlink()
        code, out, err = self.run_setup()
        self.assertEqual(code, 0, err)
        code, report = self.check_json()
        self.assertEqual((code, report["settings"]), (0, "ok"), report)
        text = (self.config / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(f"{BEGIN}\n# Policy\n\nline two\n{END}\n", text)
        runtime = (self.config / RUNTIME).as_posix()
        for event in self.EVENTS:
            names = {"PostToolUse": ["native-facts"], "UserPromptSubmit": ["native-facts"],
                     "PreToolUse": ["acceptance-gate"],
                     "Stop": ["native-event", "acceptance-gate"]}.get(event, ["native-event"])
            self.assertEqual(self.provost_commands(event), [f'bash "{runtime}/hooks/{n}"' for n in names], event)
        stop, gate = (group["hooks"][0] for group in self.settings_data()["hooks"]["Stop"])
        self.assertEqual((stop.get("async"), stop.get("timeout")), (True, 20))
        self.assertEqual((gate.get("async"), gate.get("timeout")), (None, 30))
        (group,) = self.settings_data()["hooks"]["PreToolUse"]
        self.assertEqual((group["matcher"], group["hooks"][0].get("async"), group["hooks"][0].get("timeout")),
                         ("Agent", None, 30))
        config = json.loads((self.config / "provost/config.json").read_text(encoding="utf-8"))
        self.assertEqual((config["mode"], config["handoff"], config["max_writers"]), ("adaptive", True, 2))

    def test_other_settings_are_kept_and_backed_up(self):
        mine = {"type": "command", "command": "my-linter"}
        original = {"model": "opus", "env": {"TOKEN": SECRET},
                    "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [mine]}]}, "theme": "dark"}
        write(self.config / "settings.json", json.dumps(original, indent=4))
        before = (self.config / "settings.json").read_bytes()
        for argv in ((), ("--json",)):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_setup("--dry-run", *argv)[0], 0)
        self.assertEqual(self.run_setup()[0], 0)
        data = self.settings_data()
        self.assertEqual(list(data), ["model", "env", "hooks", "theme"])
        self.assertEqual(data["env"], {"TOKEN": SECRET})
        self.assertEqual(data["hooks"]["PostToolUse"][0], {"matcher": "Edit", "hooks": [mine]})
        self.assertEqual(len(data["hooks"]["PostToolUse"]), 2)
        self.assertEqual((self.backups()[0] / "settings.json").read_bytes(), before)

    def test_second_setup_changes_nothing(self):
        self.assertEqual(self.run_setup()[0], 0)
        before = self.snapshot()
        code, out, _ = self.run_setup()
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_existing_registration_is_not_duplicated(self):
        bash = "C:/Program Files/Git/bin/bash.exe"
        stop = {"type": "command", "command": bash, "args": [f"C:\\x\\{RUNTIME}\\hooks\\native-event"]}
        write(self.config / "settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [stop]}]}}))
        self.assertEqual(self.run_setup()[0], 0)
        data = self.settings_data()
        gate = {"type": "command", "command": f'bash "{(self.config / RUNTIME).as_posix()}/hooks/acceptance-gate"',
                "timeout": 30}
        self.assertEqual(data["hooks"]["Stop"], [{"hooks": [stop]}, {"hooks": [gate]}])
        self.assertEqual(set(data["hooks"]), set(self.EVENTS))

    def test_unusable_settings_are_refused_and_nothing_written(self):
        cases = {"unparsable": '{"env": {"TOKEN": "' + SECRET + '"',
                 "not an object": "[1, 2]",
                 "hooks not an object": json.dumps({"env": {"TOKEN": SECRET}, "hooks": []}),
                 "event not a list": json.dumps({"hooks": {"Stop": {"hooks": []}}})}
        for name, text in cases.items():
            with self.subTest(name):
                write(self.config / "settings.json", text)
                before = self.snapshot()
                code, _, err = self.run_setup()
                self.assertEqual(code, 2)
                self.assertIn("settings.json", err)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.backups(), [])

    def test_settings_bom_and_crlf_are_kept(self):
        write(self.config / "settings.json", b"\xef\xbb\xbf" + b'{\r\n  "env": {"TOKEN": "x"}\r\n}\r\n')
        self.assertEqual(self.run_setup()[0], 0)
        data = (self.config / "settings.json").read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf{\r\n"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_markers_are_appended_when_absent(self):
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=repr(eol)):
                mine = f"# Mine{eol}keep this".encode()
                write(self.config / "CLAUDE.md", mine)
                self.assertEqual(self.run_setup()[0], 0)
                body = "# Policy\n\nline two\n".replace("\n", eol)
                self.assertEqual((self.config / "CLAUDE.md").read_bytes(),
                                 mine + f"{eol}{eol}{BEGIN}{eol}{body}{END}{eol}".encode())

    def test_malformed_markers_are_refused(self):
        for data in (claude_md() + f"{BEGIN}\n".encode(), f"# Mine\n {BEGIN}\n{END}\n".encode(),
                     f"{END}\n{BEGIN}\n".encode(), f"# Mine\n{BEGIN}\n".encode()):
            with self.subTest(data=data):
                write(self.config / "CLAUDE.md", data)
                before = self.snapshot()
                code, _, err = self.run_setup()
                self.assertEqual(code, 2)
                self.assertIn("marker", err)
                self.assertEqual(self.snapshot(), before)

    def test_existing_runtime_config_is_kept(self):
        write(self.config / "provost/config.json", '{"mine": true}\n')
        self.assertEqual(self.run_setup()[0], 0)
        self.assertEqual((self.config / "provost/config.json").read_bytes(), b'{"mine": true}\n')

    def test_dry_run_writes_nothing_and_names_the_plan(self):
        before = self.snapshot()
        code, out, _ = self.run_setup("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backups(), [])
        for word in ("settings.json", "provost/config.json", "Stop"):
            self.assertIn(word, out)
        code, out, _ = self.run_setup("--dry-run", "--json")
        plan = json.loads(out)
        self.assertEqual(sorted(plan["register"]), sorted(self.EVENTS))
        self.assertTrue(plan["dry_run"])

    def test_failed_write_rolls_back_settings_too(self):
        before = self.snapshot()
        real_replace = os.replace

        def flaky(src, dst):
            if Path(dst).name == "CLAUDE.md":
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(install.os, "replace", flaky):
            code, _, err = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("rolled back", err)
        after = {k: v for k, v in self.snapshot().items() if not k.startswith("provost/backups/")}
        self.assertEqual(after, before)


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
        link = self.config / "experiments"
        if not link_directory(link, outside):
            self.skipTest("neither symlinks nor junctions available")
        self.addCleanup(remove_link, link)  # runs before the temp dir is removed
        for command in ("check", "install"):
            code, _, err = self.run_cli(command)
            self.assertEqual(code, 2, command)
            self.assertIn("outside", err)
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
