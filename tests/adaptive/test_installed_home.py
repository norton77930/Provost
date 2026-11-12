"""A runtime installed by `install.py setup --config-dir X` uses X/provost, not ~/.claude/provost (#125)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import BASH, REPO
from provost_runtime import config


def installed_file(provost, name="native-pilot-v1"):
    return Path(provost) / "runtime" / name / "scripts" / "provost_runtime" / "config.py"


class InstalledShapeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-shape-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_installed_path_names_its_provost_directory(self):
        provost = self.tmp / "claude" / "provost"
        self.assertEqual(config.installed_provost_dir(installed_file(provost)), provost)
        self.assertEqual(config.installed_provost_dir(installed_file(provost, "other-name")), provost)

    def test_checkout_path_is_not_installed(self):
        checkout = self.tmp / "Provost" / "scripts" / "provost_runtime" / "config.py"
        self.assertIsNone(config.installed_provost_dir(checkout))

    def test_runtime_directory_at_the_wrong_depth_is_not_installed(self):
        for parts in (("runtime", "scripts", "provost_runtime", "config.py"),
                      ("runtime", "a", "b", "scripts", "provost_runtime", "config.py"),
                      ("runtime", "name", "lib", "provost_runtime", "config.py"),
                      ("runtime", "name", "scripts", "other", "config.py"),
                      ("rt", "name", "scripts", "provost_runtime", "config.py")):
            with self.subTest(parts):
                self.assertIsNone(config.installed_provost_dir(self.tmp.joinpath("p", *parts)))

    def test_it_reads_this_modules_own_path_by_default(self):
        self.assertIsNone(config.installed_provost_dir())  # the tests import the checkout
        with mock.patch.object(config, "__file__", str(installed_file(self.tmp / "p"))):
            self.assertEqual(config.installed_provost_dir(), self.tmp / "p")


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-prec-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.installed = self.tmp / "inst" / "provost"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [k for k in os.environ if k.startswith("PROVOST_")]:
            del os.environ[name]

    def installed_here(self):
        patcher = mock.patch.object(config, "__file__", str(installed_file(self.installed)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_checkout_keeps_the_default_locations(self):
        self.assertEqual(config.config_path(), self.home / ".claude" / "provost" / "config.json")
        self.assertEqual(config.handoff_home(), self.home / ".claude" / "provost" / "handoffs")

    def test_config_path_precedence(self):
        self.installed_here()
        self.assertEqual(config.config_path(), self.installed / "config.json")  # the file need not exist
        with mock.patch.dict(os.environ, PROVOST_CONFIG=str(self.tmp / "x.json")):
            self.assertEqual(config.config_path(), self.tmp / "x.json")

    def test_handoff_home_precedence(self):
        self.installed_here()
        self.assertEqual(config.handoff_home(), self.installed / "handoffs")
        with mock.patch.dict(os.environ, PROVOST_HOME=str(self.tmp / "h")):
            self.assertEqual(config.handoff_home(), self.tmp / "h" / "handoffs")
            with mock.patch.dict(os.environ, PROVOST_HANDOFF_HOME=str(self.tmp / "s")):
                self.assertEqual(config.handoff_home(), self.tmp / "s")

    def test_checkout_handoff_home_still_follows_the_variables(self):
        with mock.patch.dict(os.environ, PROVOST_HOME=str(self.tmp / "h")):
            self.assertEqual(config.handoff_home(), self.tmp / "h" / "handoffs")
        with mock.patch.dict(os.environ, PROVOST_HANDOFF_HOME=str(self.tmp / "s")):
            self.assertEqual(config.handoff_home(), self.tmp / "s")


class InstalledHooksTests(unittest.TestCase):
    """`install.py setup --config-dir` into a temporary directory, then run the installed wrappers."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-installed-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.config_dir = self.tmp / "claude"
        self.provost = self.config_dir / "provost"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PROVOST_")}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        for name in ("CLAUDE_CONFIG_DIR", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(name, None)
        result = subprocess.run([sys.executable, "-B", str(REPO / "install.py"), "setup", "--config-dir",
                                 str(self.config_dir)], capture_output=True, text=True, encoding="utf-8",
                                env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.hooks = self.provost / "runtime" / "native-pilot-v1" / "hooks"

    def run_hook(self, name, event, env=None):
        result = subprocess.run([BASH, str(self.hooks / name)], input=json.dumps(event), text=True,
                                capture_output=True, encoding="utf-8", env={**self.env, **(env or {})},
                                timeout=60, cwd=self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def session_start(self, env=None):
        return self.run_hook("native-event", {"hook_event_name": "SessionStart", "source": "startup",
                                              "session_id": "s1", "cwd": str(self.workspace)}, env)

    def assertHomeUntouched(self):
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_session_start_uses_the_installed_config_and_store(self):
        result = self.session_start()
        self.assertIn("Native handoff runtime", result["hookSpecificOutput"]["additionalContext"])
        self.assertTrue((self.provost / "cache" / "facts" / "s1.enabled").exists())
        self.assertTrue(any((self.provost / "handoffs").rglob("*.json")))
        self.assertHomeUntouched()

    def test_python_cache_goes_under_the_installed_directory(self):
        self.session_start()
        self.assertTrue((self.provost / "cache" / "python").exists())
        self.assertHomeUntouched()

    def test_missing_installed_config_answers_empty_without_touching_home(self):
        (self.provost / "config.json").unlink()
        self.assertEqual(self.session_start(), {})
        self.assertHomeUntouched()

    def test_explicit_config_wins_over_the_installed_one(self):
        self.assertEqual(self.session_start({"PROVOST_CONFIG": str(self.tmp / "absent.json")}), {})
        self.assertEqual(list((self.provost / "cache").glob("facts/*")), [])
        self.assertHomeUntouched()

    def test_a_checkout_without_variables_keeps_the_home_defaults(self):
        checkout = REPO / "hooks" / "native-event"
        event = {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1", "cwd": str(self.workspace)}

        def run():
            result = subprocess.run([BASH, str(checkout)], input=json.dumps(event), text=True, capture_output=True,
                                    encoding="utf-8", env=self.env, timeout=60, cwd=self.workspace)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual(run(), {})
        user = self.home / ".claude" / "provost"
        user.mkdir(parents=True)
        (user / "config.json").write_bytes((self.provost / "config.json").read_bytes())
        self.assertIn("Native handoff runtime", run()["hookSpecificOutput"]["additionalContext"])
        self.assertTrue((user / "cache" / "facts" / "s1.enabled").exists())
        self.assertTrue(any((user / "handoffs").rglob("*.json")))

    def test_native_facts_appends_under_the_installed_cache(self):
        facts = self.provost / "cache" / "facts"
        facts.mkdir(parents=True)
        (facts / "s1.enabled").touch()
        event = {"session_id": "s1", "cwd": str(self.workspace), "hook_event_name": "PostToolUse",
                 "tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}, "tool_response": {}}
        self.assertEqual(self.run_hook("native-facts", event), {})
        rows = (facts / "s1.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(r)["p"] for r in rows], ["/repo/a.py"])
        self.assertHomeUntouched()


if __name__ == "__main__":
    unittest.main()
