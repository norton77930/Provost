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
