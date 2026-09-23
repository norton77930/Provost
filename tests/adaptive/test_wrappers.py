import json
import os
import shutil
import subprocess
from pathlib import Path
from _support import FixtureCase, REPO

BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")


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
        self.assertFalse((self.root / ".provost").exists())

    def test_legacy_resume_does_not_add_policy(self):
        self.assertEqual(self.run_script("session-start", self.event(source="resume"), config=False), {})

    def test_disabled_compaction_hook_is_noop(self):
        self.assertEqual(self.run_script("handoff-event", self.event("PreCompact"), config=False), {})
        self.assertFalse((self.root / ".provost").exists())

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
