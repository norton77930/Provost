import json
import os
import shutil
import subprocess
from pathlib import Path
from _support import BASH, FixtureCase, REPO


class WrapperTests(FixtureCase):
    def run_script(self, script, event, *, config=True):
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path if config else self.root / "absent.json"),
                   PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([BASH, str(REPO / "hooks" / script)], input=json.dumps(event),
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout or "{}")

    def event(self, name="SessionStart", source="startup"):
        return {"session_id": "s1", "cwd": str(self.root), "hook_event_name": name, "source": source}

    def test_opt_in_shell_routes_to_adaptive(self):
        result = self.run_script("session-start", self.event())
        self.assertIn("adaptive", result["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("Run the main/orchestrator agent on Opus", result["hookSpecificOutput"]["additionalContext"])

    def test_legacy_startup_preserves_context(self):
        result = self.run_script("session-start", self.event(), config=False)
        self.assertIn("collaborator tier", result["hookSpecificOutput"]["additionalContext"])
        self.assertNoStore()

    def test_legacy_resume_does_not_add_policy(self):
        self.assertEqual(self.run_script("session-start", self.event(source="resume"), config=False), {})

    def test_disabled_compaction_hook_is_noop(self):
        self.assertEqual(self.run_script("handoff-event", self.event("PreCompact"), config=False), {})
        self.assertNoStore()

    def test_enabled_compaction_missing_record_does_not_block(self):
        result = self.run_script("handoff-event", self.event("PreCompact"))
        self.assertNotEqual(result.get("decision"), "block")
        self.assertIn("NO_CHECKPOINT", json.dumps(result))

    def test_plugin_path_with_spaces_and_missing_python(self):
        plugin = self.root / "plugin with spaces"
        plugin.mkdir()
        for name in ("hooks", "scripts", "policies"):
            shutil.copytree(REPO / name, plugin / name)
        shutil.copy2(REPO / "orchestration.md", plugin / "orchestration.md")
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        event = json.dumps(self.event())
        command = [BASH, str(plugin / "hooks/session-start")]
        result = subprocess.run(command, input=event, capture_output=True, text=True,
                                encoding="utf-8", env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("adaptive", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
        # The run above cached a working interpreter; forget it so the fakes are what gets probed.
        self.cache_file.unlink(missing_ok=True)
        missing = self.root / "missing-python"
        missing.mkdir()
        for name in ("python3", "python", "py"):
            candidate = missing / name
            candidate.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
            candidate.chmod(0o755)
        # Git Bash uses a colon-separated path internally; MSYS converts the
        # inherited native Windows PATH before this command.
        setup = 'probe="$1"; if command -v cygpath >/dev/null; then probe="$(cygpath -u "$probe")"; fi; export PATH="$probe:$PATH"; exec bash "$2"'
        for script, field in (("session-start", "systemMessage"), ("handoff-event", "terminalSequence")):
            result = subprocess.run([BASH, "-c", setup, "probe", missing.as_posix(),
                                     str(plugin / "hooks" / script)], input=event,
                                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("unavailable", json.loads(result.stdout)[field])


class InterpreterCacheTests(FixtureCase):
    """hooks/runtime resolves the interpreter once, then reuses the cached path."""

    def setUp(self):
        super().setUp()
        self.log = self.root / "calls.log"
        self.bin = self.root / "bin"

    def calls(self, log=None):
        log = Path(log or self.log)
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def probes(self, log=None):
        return [c for c in self.calls(log) if " -c " in c]

    def runtime(self, *args, **kwargs):
        return self.run_bash(REPO / "hooks" / "runtime", *args, path_dirs=[self.bin], **kwargs)

    def test_cache_written_after_first_probe(self):
        self.fake_interpreter(self.bin, "python3", self.log)
        result = self.runtime("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.cache_file.is_file())
        self.assertTrue(self.cache_file.read_text(encoding="utf-8").strip().endswith("/bin/python3"))
        self.assertEqual(len(self.probes()), 1)

    def test_cached_interpreter_runs_without_probe(self):
        cached_log = self.root / "cached.log"
        cached = self.fake_interpreter(self.root / "cached", "python", cached_log)
        self.fake_interpreter(self.bin, "python3", self.log)
        self.cache_file.parent.mkdir(parents=True)
        self.cache_file.write_text(cached.as_posix() + "\n", encoding="utf-8")
        result = self.runtime("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls(cached_log)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("-m provost_runtime.cli status", calls[0])
        self.assertEqual(self.calls(), [])

    def test_unusable_cache_falls_back_and_is_rewritten(self):
        self.fake_interpreter(self.bin, "python3", self.log)
        plain = self.root / "plain-file"
        plain.write_text("not executable", encoding="utf-8")
        plain.chmod(0o644)
        for label, content in (("missing", (self.root / "gone" / "python").as_posix()), ("empty", ""),
                               ("not-executable", plain.as_posix())):
            with self.subTest(label):
                self.log.unlink(missing_ok=True)
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                self.cache_file.write_text(content, encoding="utf-8")
                result = self.runtime("status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.cache_file.read_text(encoding="utf-8").strip().endswith("/bin/python3"))
                self.assertEqual(len(self.probes()), 1)

    def test_provost_python_overrides_cache_and_probe(self):
        override_log = self.root / "override.log"
        override = self.fake_interpreter(self.root / "override", "python", override_log)
        self.fake_interpreter(self.bin, "python3", self.log)
        result = self.runtime("status", env={"PROVOST_PYTHON": override.as_posix()})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls(override_log)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("-m provost_runtime.cli status", calls[0])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.cache_file.exists())

    def test_unusable_provost_python_falls_back_to_probe(self):
        self.fake_interpreter(self.bin, "python3", self.log)
        result = self.runtime("status", env={"PROVOST_PYTHON": (self.root / "nope").as_posix()})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.probes()), 1)

    def test_no_qualifying_interpreter_exits_127(self):
        self.bin.mkdir()
        for name in ("python3", "python", "py"):
            candidate = self.bin / name
            candidate.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8", newline="\n")
            candidate.chmod(0o755)
        result = self.runtime("status")
        self.assertEqual(result.returncode, 127)
        self.assertIn("requires Python 3.11+", result.stderr)
        self.assertFalse(self.cache_file.exists())

    def test_module_root_from_every_invocation_form(self):
        self.fake_interpreter(self.bin, "python3", self.log)
        forms = {"absolute": (str(REPO / "hooks" / "runtime"), None),
                 "relative": ("hooks/runtime", REPO),
                 "same-directory": ("runtime", REPO / "hooks"),
                 "parent-relative": ("../hooks/runtime", REPO / "scripts")}
        if os.name == "nt":
            forms["msys-posix"] = ("/" + REPO.drive[0].lower() + REPO.as_posix()[2:] + "/hooks/runtime", None)
        for label, (script, cwd) in forms.items():
            with self.subTest(label):
                self.log.unlink(missing_ok=True)
                result = self.run_bash(script, "status", path_dirs=[self.bin], cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                line = self.calls()[-1]
                pythonpath = line.split("PYTHONPATH=", 1)[1].split(" -m ", 1)[0]
                self.assertEqual(Path(pythonpath).resolve(), (REPO / "scripts").resolve(), line)
