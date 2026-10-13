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
# PROVOST_TEST_BASH picks another bash (e.g. a non-default Git for Windows install).
BASH = (os.environ.get("PROVOST_TEST_BASH")
        or ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")))
# `git rev-parse --local-env-vars`: inherited from a git hook or `git rebase -x`, they
# point every fixture git call at that repository instead of the fixture's own.
GIT_REPO_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
                "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
                "GIT_SHALLOW_FILE", "GIT_COMMON_DIR")


class FixtureCase(unittest.TestCase):
    def setUp(self):
        git_env = mock.patch.dict(os.environ)
        git_env.start()
        self.addCleanup(git_env.stop)
        for name in GIT_REPO_ENV:
            os.environ.pop(name, None)
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
        # The handoff store lives in its own temp dir: tests turn self.root into
        # a Git repo, and a store inside a work tree is refused. Never rely on
        # HOME for it (Path.home() reads USERPROFILE on Windows).
        store = tempfile.TemporaryDirectory(prefix="provost-store-")
        self.addCleanup(store.cleanup)
        self.store_home = Path(store.name)
        patcher = mock.patch.dict(os.environ, PROVOST_HOME=str(self.provost_home), HOME=str(self.provost_home),
                                  PROVOST_HANDOFF_HOME=str(self.store_home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertNoStore(self, *workspaces):
        """Nothing was written to the handoff store or under any workspace."""
        self.assertEqual(list(self.store_home.iterdir()), [])
        for workspace in (self.root, *workspaces):
            self.assertFalse((Path(workspace) / ".provost").exists())

    def isolate_user_git_config(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-gitcfg-")
        self.addCleanup(tmp.cleanup)
        empty_config = Path(tmp.name) / "empty-gitconfig"
        empty_config.write_text("", encoding="utf-8")
        xdg = Path(tmp.name) / "xdg"
        xdg.mkdir()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty_config), "XDG_CONFIG_HOME": str(xdg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()

    def git_repo_with_worktree(self):
        """self.root as a main checkout plus a real linked worktree under .claude/worktrees."""
        self.isolate_user_git_config()
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")
        worktree = self.root / ".claude" / "worktrees" / "wt"
        self.git("worktree", "add", "-q", "--detach", str(worktree))
        return self.root.resolve(), worktree.resolve()

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

    def seed_facts(self, files=3, repos=1, agents=2, name_chars=20, session="s1"):
        """A sealed facts.json for session: files f0000.. (newest last), repos with 2 of 7 dirty and 1 commit."""
        from provost_runtime.config import handoff_home
        from provost_runtime.store import _seal, as_workspace, atomic_write, session_dir
        ws = as_workspace(self.root)
        body = {"schema_version": 1, "kind": "facts", "workspace_id": ws.repo_key, "session_id": session,
                "updated_at": "2026-10-06T00:00:00+00:00", "last_change_at": "2026-10-06T00:00:00+00:00",
                "files": [{"path": "C:/repo/f%04d-%s.py" % (i, "n" * name_chars), "count": 1, "last_ts": 1000 + i,
                           "agents": [""]} for i in range(files)],
                "files_omitted": 0, "outside_repo_count": 4,
                "agents": [{"role": "implementer" if i % 2 else "code-reviewer", "desc": "d", "bg": False, "ts": i}
                           for i in range(agents)], "agents_omitted": 0, "worktrees": [],
                "repos": [{"root": "C:/repo%d-%s" % (i, "r" * name_chars), "branch": "main", "first_touched_at": "x",
                           "last_touched_at": "x", "dirty": [{"status": " M", "path": "a"}] * 2, "dirty_total": 7,
                           "commits": [{"hash": "abc", "subject": "s"}], "error": None} for i in range(repos)],
                "repos_omitted": 0, "spool_errors": 0, "events_captured": files + agents + repos,
                "spool_consumed": {}}
        path = session_dir(ws, session) / "facts.json"
        atomic_write(handoff_home(), path, _seal(body))
        return path

    def enable_facts(self, session="s1"):
        """The marker a native SessionStart leaves when handoff is enabled for the workspace."""
        marker = self.config_path.parent / "cache" / "facts" / (session + ".enabled")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return marker

    def seed_snapshot(self):
        from provost_runtime.store import record, snapshot
        record(self.root, "s1", self.payload(), None, "r1")
        return snapshot(self.root, "s1", "e1")
