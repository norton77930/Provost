import json
import os
import shutil
import subprocess
from _support import BASH, FixtureCase, REPO

class NativeWrapperTests(FixtureCase):
    def invoke(self, env=None, command=None):
        settings=dict(os.environ,PROVOST_CONFIG=str(self.config_path),PYTHONDONTWRITEBYTECODE="1")
        settings.update(env or {})
        result=subprocess.run(command or [BASH,str(REPO/"hooks/native-event")],
                              input=json.dumps(dict(hook_event_name="SessionStart",source="startup",
                                                    session_id="s1",cwd=str(self.root))),
                              text=True,capture_output=True,encoding="utf-8",env=settings,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)
    def test_enabled_native_wrapper(self):
        result=self.invoke()
        self.assertIn("Native handoff runtime",result["hookSpecificOutput"]["additionalContext"])
    def test_missing_config_does_not_inject_legacy(self):
        self.config_path.unlink()
        self.assertEqual(self.invoke(),{})
    def test_missing_python_is_nonblocking_and_does_not_inject_policy(self):
        missing=self.root/"missing";missing.mkdir()
        for name in ("python3","python","py"):
            p=missing/name;p.write_text("#!/usr/bin/env bash\nexit 127\n");p.chmod(0o755)
        setup='probe="$1"; if command -v cygpath >/dev/null; then probe="$(cygpath -u "$probe")"; fi; export PATH="$probe:$PATH"; exec bash "$2"'
        result=self.invoke(command=[BASH,"-c",setup,"probe",missing.as_posix(),str(REPO/"hooks/native-event")])
        self.assertIn("unavailable",result["systemMessage"])
        self.assertNotIn("hookSpecificOutput",result)

    def native_event(self, *, fail_module=False):
        """Run a copy of native-event whose sibling `runtime` only records that it was launched."""
        log = self.root / "python.log"
        bash_log = self.root / "runtime.log"
        bin_dir = self.root / "bin"
        hooks = self.root / "plugin" / "hooks"
        self.fake_interpreter(bin_dir, "python3", log, fail_module=fail_module)
        if not hooks.exists():
            shutil.copytree(REPO / "hooks", hooks)
            (hooks / "runtime").write_text(f'#!/usr/bin/env bash\necho "$@" >> "{bash_log.as_posix()}"\nexit 1\n',
                                           encoding="utf-8", newline="\n")
        result = self.run_bash(hooks / "native-event", path_dirs=[bin_dir], stdin='{"hook_event_name":"SessionStart"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, log, bash_log

    def test_native_event_runs_interpreter_without_extra_bash(self):
        result, log, bash_log = self.native_event()
        self.assertEqual(json.loads(result.stdout), {})
        self.assertIn("-m provost_runtime.cli native-hook", log.read_text(encoding="utf-8"))
        self.assertFalse(bash_log.exists(), "native-event must not launch the sibling `runtime` script")

    def test_native_event_fallback_json_when_interpreter_fails(self):
        result, log, bash_log = self.native_event(fail_module=True)
        self.assertIn("unavailable", json.loads(result.stdout)["systemMessage"])
        self.assertIn("-m provost_runtime.cli native-hook", log.read_text(encoding="utf-8"))
        self.assertFalse(bash_log.exists())

    def test_native_event_reuses_cached_interpreter(self):
        self.native_event()
        self.assertTrue(self.cache_file.read_text(encoding="utf-8").strip().endswith("/bin/python3"))
        result, log, _ = self.native_event()
        self.assertEqual(len([c for c in log.read_text(encoding="utf-8").splitlines() if " -c " in c]), 1)

    def test_acceptance_gate_starts_no_interpreter_without_acceptance_dir(self):
        log = self.root / "python.log"
        bin_dir = self.root / "bin"
        self.fake_interpreter(bin_dir, "python3", log)
        hook = REPO / "hooks" / "acceptance-gate"
        stop = json.dumps({"session_id": "s1", "transcript_path": str(self.root / "t.jsonl"),
                           "cwd": str(self.root), "hook_event_name": "Stop", "last_assistant_message": "done"})
        result = self.run_bash(hook, path_dirs=[bin_dir], stdin=stop)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertFalse(log.exists(), "no acceptance directory: no interpreter may start")
        (self.root / ".claude" / "acceptance").mkdir(parents=True)
        result = self.run_bash(hook, path_dirs=[bin_dir], stdin=stop)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}), result.stderr)
        self.assertIn("-m provost_runtime.cli acceptance-hook", log.read_text(encoding="utf-8"))

    def test_acceptance_gate_blocks_end_to_end(self):
        directory = self.root / ".claude" / "acceptance"
        directory.mkdir(parents=True)
        transcript = self.root / "t.jsonl"  # the session wrote the acceptance file with the Write tool
        write = {"type": "assistant", "timestamp": "2020-01-01T00:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Write",
             "input": {"file_path": str(directory / "task.json")}}]}}
        transcript.write_text(json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z"}) + "\n"
                              + json.dumps(write) + "\n", encoding="utf-8")
        (directory / "task.json").write_text(json.dumps({"task": "t", "gates": [
            {"id": "unit-tests", "kind": "machine", "check": "pytest", "status": "pending"}]}), encoding="utf-8")
        stop = json.dumps({"session_id": "s1", "transcript_path": str(transcript), "cwd": str(self.root),
                           "hook_event_name": "Stop"})
        result = self.run_bash(REPO / "hooks" / "acceptance-gate", stdin=stop)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("unit-tests", output["reason"])
        dispatch = json.dumps({"session_id": "s1", "transcript_path": str(transcript), "cwd": str(self.root),
                               "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {
                                   "subagent_type": "implementer", "prompt": "Build it."}})
        result = self.run_bash(REPO / "hooks" / "acceptance-gate", stdin=dispatch)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(list(output), ["hookSpecificOutput"])
        self.assertIn("task.json", output["hookSpecificOutput"]["additionalContext"])
