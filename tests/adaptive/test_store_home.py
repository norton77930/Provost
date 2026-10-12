import os
import subprocess
import sys
from unittest import mock

from _support import FixtureCase
from provost_runtime import store


class StoreHomeCheckTests(FixtureCase):
    def test_undecodable_git_stderr_is_git_unreadable_not_a_decode_error(self):
        """Git's stderr follows its own locale; bytes invalid in UTF-8 and in common
        legacy code pages must not raise UnicodeDecodeError out of the home check."""
        real_run = subprocess.run
        script = "import sys; sys.stderr.buffer.write(b'fatal: \\x81\\xff\\n'); sys.exit(128)"

        def git_writing_raw_stderr(args, **kwargs):
            return real_run([sys.executable, "-c", script], **kwargs)

        home = self.store_home / "undecodable"
        self.addCleanup(store._HOME_CHECKED.pop, os.path.normcase(str(home)), None)
        with mock.patch.object(store.subprocess, "run", git_writing_raw_stderr):
            self.assertEqual(store._home_outside_repo(home), "GIT_UNREADABLE")
