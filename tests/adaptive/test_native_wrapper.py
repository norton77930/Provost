import json
import os
import shutil
import subprocess
from _support import FixtureCase, REPO

BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")

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
