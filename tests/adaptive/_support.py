import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")


class FixtureCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_path = self.root / "user-config.json"
        self.cfg = {"schema_version": 1, "mode": "adaptive",
                    "workspace_roots": [str(self.root)], "all_workspaces": False,
                    "max_writers": 2, "burst_writers": 3, "handoff": True}
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        # Wrappers cache the resolved interpreter under PROVOST_HOME; never
        # let a test touch the real ~/.claude/provost.
        self.provost_home = self.root / "provost-home"
        self.cache_file = self.provost_home / "cache" / "python"
        patcher = mock.patch.dict(os.environ, PROVOST_HOME=str(self.provost_home), HOME=str(self.provost_home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_interpreter(self, directory, name, log, *, fail_module=False):
        """A stand-in interpreter that logs each call: passes the version probe,
        prints '{}' for the module call (or exits 1 when fail_module)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("#!/usr/bin/env bash\n"
                        f'echo "$0 PYTHONPATH=${{PYTHONPATH:-}} $*" >> "{Path(log).as_posix()}"\n'
                        'if [[ "$1" == -c ]]; then exit 0; fi\n'
                        + ("exit 1\n" if fail_module else "echo '{}'\n"), encoding="utf-8", newline="\n")
        path.chmod(0o755)
        return path

    def run_bash(self, script, *args, path_dirs=(), env=None, stdin="", cwd=None):
        """Run a hook script under bash with path_dirs prepended to PATH; the
        real bash is used for the final exec so fake `bash` shims cannot recurse."""
        settings = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        settings.pop("PROVOST_PYTHON", None)
        settings.update(env or {})
        setup = ('for d in "${@:4}"; do if command -v cygpath >/dev/null; then d="$(cygpath -u "$d")"; fi; '
                 'export PATH="$d:$PATH"; done; exec "$1" "$2" $3')
        command = [BASH, "-c", setup, "probe", BASH, str(script), " ".join(args),
                   *[Path(d).as_posix() for d in path_dirs]]
        return subprocess.run(command, input=stdin, text=True, capture_output=True,
                              encoding="utf-8", env=settings, timeout=60, cwd=cwd)

    def payload(self, next_step="collect result"):
        return {"work_id": "fixture", "phase": "implementation", "sources": [],
                "assignments": [], "decisions": [], "blockers": [], "evidence": [],
                "next": next_step, "suggested_skills": []}

    def seed_snapshot(self):
        from provost_runtime.store import record, snapshot
        record(self.root, "s1", self.payload(), None, "r1")
        return snapshot(self.root, "s1", "e1")
