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

    def test_paths_too_short_for_the_layout_are_not_installed(self):
        for text in ("/scripts/provost_runtime/config.py", "/provost_runtime/config.py", "/config.py",
                     "/runtime/n/scripts/provost_runtime/config.py",  # the provost directory would be the root
                     "/p/runtime/n/provost_runtime/config.py", "/p/runtime/n/scripts/config.py",
                     "/p/n/scripts/provost_runtime/config.py"):
            with self.subTest(text):
                self.assertIsNone(config.installed_provost_dir(text))

    def test_dot_and_dotdot_segments_are_collapsed(self):
        provost = self.tmp / "p"
        spelled = provost / "runtime" / "n" / "scripts" / ".." / "scripts" / "." / "provost_runtime" / "config.py"
        self.assertEqual(config.installed_provost_dir(spelled), provost)

    @unittest.skipUnless(os.name == "nt", "UNC anchors")
    def test_unc_share_root_is_not_a_provost_directory(self):
        tail = "runtime/n/scripts/provost_runtime/config.py"
        self.assertEqual(config.installed_provost_dir("//server/share/p/" + tail), Path("//server/share/p"))
        self.assertIsNone(config.installed_provost_dir("//server/share/" + tail))

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


class SpellingTests(unittest.TestCase):
    """One install for the class: the same script reached through different spellings of its path."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="provost-spelling-")
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        cls.workspace = cls.tmp / "workspace"
        cls.workspace.mkdir()
        cls.config_dir = cls.tmp / "claude"
        cls.provost = cls.config_dir / "provost"
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith("PROVOST_")}
        cls.env.update(HOME=str(cls.home), USERPROFILE=str(cls.home), PYTHONDONTWRITEBYTECODE="1")
        for name in ("CLAUDE_CONFIG_DIR", "GIT_DIR", "GIT_WORK_TREE"):
            cls.env.pop(name, None)
        result = subprocess.run([sys.executable, "-B", str(REPO / "install.py"), "setup", "--config-dir",
                                 str(cls.config_dir)], capture_output=True, text=True, encoding="utf-8",
                                env=cls.env, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        cls.runtime = cls.provost / "runtime" / "native-pilot-v1"

    def run_hook(self, script, event, cwd):
        result = subprocess.run([BASH, script], input=json.dumps(event), text=True, capture_output=True,
                                encoding="utf-8", env=self.env, timeout=60, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assertHomeUntouched(self):
        self.assertEqual(list(self.home.rglob("*")), [])

    def session_start(self, script, cwd, session):
        result = self.run_hook(script, {"hook_event_name": "SessionStart", "source": "startup",
                                        "session_id": session, "cwd": str(self.workspace)}, cwd)
        self.assertIn("Native handoff runtime", result["hookSpecificOutput"]["additionalContext"], script)
        self.assertTrue((self.provost / "cache" / "facts" / f"{session}.enabled").exists(), script)
        self.assertHomeUntouched()

    def test_native_event_spellings(self):
        hooks, scripts = self.runtime / "hooks", self.runtime / "scripts"
        for n, (script, cwd) in enumerate((("native-event", hooks), ("./native-event", hooks),
                                           ("../hooks/native-event", scripts), ("hooks/native-event", self.runtime),
                                           ("./../hooks/./native-event", scripts))):
            with self.subTest(script=script):
                self.session_start(script, cwd, f"spell-{n}")

    def test_interpreter_cache_follows_a_dotdot_spelling(self):
        cache = self.provost / "cache" / "python"
        cache.unlink(missing_ok=True)
        self.session_start("../hooks/native-event", self.runtime / "scripts", "spell-cache")
        self.assertTrue(cache.exists())

    def test_native_facts_spellings(self):
        facts = self.provost / "cache" / "facts"
        for n, (script, cwd) in enumerate((("./native-facts", self.runtime / "hooks"),
                                           ("../hooks/native-facts", self.runtime / "scripts"))):
            with self.subTest(script=script):
                session = f"facts-{n}"
                facts.mkdir(parents=True, exist_ok=True)
                (facts / f"{session}.enabled").touch()
                event = {"session_id": session, "cwd": str(self.workspace), "hook_event_name": "PostToolUse",
                         "tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}, "tool_response": {}}
                self.assertEqual(self.run_hook(script, event, cwd), {})
                self.assertEqual(len((facts / f"{session}.jsonl").read_text(encoding="utf-8").splitlines()), 1)
                self.assertHomeUntouched()

    def test_dotdot_above_the_root_stays_at_the_root(self):
        script = self.runtime / "hooks" / "native-event"
        posix = script.as_posix()
        if os.name == "nt":
            posix = f"/{posix[0].lower()}{posix[2:]}"  # the Git Bash spelling /c/...
        self.session_start("/../.." + posix, self.workspace, "above-root")

    @unittest.skipUnless(os.name == "nt", "UNC administrative share")
    def test_unc_spelling_keeps_its_double_slash(self):
        script = self.runtime / "hooks" / "native-event"
        drive, rest = os.path.splitdrive(str(script))
        if not drive.endswith(":") or len(drive) != 2:
            self.skipTest("the temporary directory has no drive letter")
        admin = f"//localhost/{drive[0]}$"
        if not os.path.isdir(admin + "/"):
            self.skipTest(f"{admin} is not readable for this user")
        unc = admin + rest.replace("\\", "/")
        self.session_start(unc, self.workspace, "unc")
        self.session_start(unc.replace("/hooks/", "/scripts/../hooks/"), self.workspace, "unc-dotdot")

    @unittest.skipUnless(os.name == "nt", "Windows path spellings")
    def test_windows_spellings_of_the_absolute_path(self):
        script = self.runtime / "hooks" / "native-event"
        drive = str(script)[0].lower()
        spellings = (str(script), script.as_posix(), f"/{drive}/" + script.as_posix()[3:])
        for n, text in enumerate(spellings):
            with self.subTest(text):
                self.session_start(text, self.workspace, f"win-{n}")


if __name__ == "__main__":
    unittest.main()
