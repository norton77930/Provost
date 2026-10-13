import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from _support import FixtureCase


class FixtureGitEnvTests(unittest.TestCase):
    def test_fixture_git_ignores_inherited_repo_env(self):
        """A run started under GIT_DIR/GIT_WORK_TREE (a git hook, `git rebase -x`) must
        not send fixture commits to that repository."""
        class _Probe(FixtureCase):
            def runTest(self):
                pass

        outer = tempfile.TemporaryDirectory(prefix="provost-outer-")
        self.addCleanup(outer.cleanup)
        decoy = Path(outer.name)
        subprocess.run(["git", "init", "-q", str(decoy)], check=True, capture_output=True)
        with mock.patch.dict(os.environ, GIT_DIR=str(decoy / ".git"), GIT_WORK_TREE=str(decoy)):
            probe = _Probe()
            probe.setUp()
            try:
                probe.isolate_user_git_config()
                probe.git("init", "-q")
                probe.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                          "commit", "-q", "--allow-empty", "-m", "init")
                top = probe.git("rev-parse", "--show-toplevel")
                self.assertEqual(Path(top).resolve(), probe.root.resolve())
            finally:
                probe.doCleanups()
            self.assertEqual(os.environ["GIT_DIR"], str(decoy / ".git"))
        log = subprocess.run(["git", "-C", str(decoy), "log", "--oneline"], capture_output=True, text=True)
        self.assertNotEqual(log.returncode, 0, "decoy repository received a commit: " + log.stdout)


if __name__ == "__main__":
    unittest.main()
