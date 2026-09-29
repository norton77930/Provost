import json
import shutil
from _support import FixtureCase, REPO

HOOK = REPO / "hooks" / "native-reminder"


def post_tool_use(role):
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Agent",
                       "tool_input": {"subagent_type": role, "prompt": "x"}})


def subagent_stop(role):
    return json.dumps({"hook_event_name": "SubagentStop", "agent_type": role, "session_id": "s1"})


class NativeReminderTests(FixtureCase):
    def reminder(self, stdin, **kwargs):
        result = self.run_bash(HOOK, stdin=stdin, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_missing_config_is_silent(self):
        self.config_path.unlink()
        self.assertEqual(self.reminder(post_tool_use("implementer")), {})

    def test_handoff_disabled_is_silent(self):
        self.config_path.write_text(json.dumps(dict(self.cfg, handoff=False)), encoding="utf-8")
        self.assertEqual(self.reminder(post_tool_use("implementer")), {})

    def test_pretty_printed_config_is_accepted(self):
        self.config_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        self.assertIn("hookSpecificOutput", self.reminder(post_tool_use("implementer")))

    def test_dispatch_of_writer_reminds(self):
        out = self.reminder(post_tool_use("implementer-high"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("dispatched implementer-high", out["additionalContext"])
        self.assertIn("record --input", out["additionalContext"])

    def test_writer_finish_reminds(self):
        out = self.reminder(subagent_stop("mech-executor"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SubagentStop")
        self.assertIn("mech-executor finished", out["additionalContext"])

    def test_read_only_roles_are_silent(self):
        for role in ("Explore", "code-reviewer", "test-analyst"):
            for build in (post_tool_use, subagent_stop):
                self.assertEqual(self.reminder(build(role)), {}, role)

    def test_unsafe_role_is_silent(self):
        for role in ('imple"menter', "implementer x", "implementer\\", ""):
            for build in (post_tool_use, subagent_stop):
                self.assertEqual(self.reminder(build(role)), {}, role)

    def test_malformed_or_unknown_input_is_silent(self):
        for stdin in ("", "not json", "{", '{"hook_event_name":"SessionStart","agent_type":"implementer"}',
                      '{"hook_event_name":"PostToolUse","tool_name":"Agent"}'):
            self.assertEqual(self.reminder(stdin), {}, stdin)

    def test_role_set_is_overridable(self):
        env = {"PROVOST_REMINDER_ROLES": "code-reviewer custom-writer"}
        self.assertIn("hookSpecificOutput", self.reminder(post_tool_use("code-reviewer"), env=env))
        self.assertIn("hookSpecificOutput", self.reminder(subagent_stop("custom-writer"), env=env))
        self.assertEqual(self.reminder(post_tool_use("implementer"), env=env), {})

    def test_default_writer_set(self):
        for role in ("implementer", "implementer-high", "implementer-sonnet", "mech-executor", "general-purpose"):
            self.assertIn("hookSpecificOutput", self.reminder(post_tool_use(role)), role)

    def test_runtime_hash_covers_the_reminder_hook(self):
        from provost_runtime.cli import runtime_hash
        copy = self.root / "runtime-copy"
        shutil.copytree(REPO / "hooks", copy / "hooks")
        base = runtime_hash(copy)
        (copy / "hooks" / "native-reminder").unlink()
        self.assertNotEqual(runtime_hash(copy), base)

    def test_no_python_is_launched(self):
        log = self.root / "python.log"
        bin_dir = self.root / "bin"
        for name in ("python", "python3", "py"):
            self.fake_interpreter(bin_dir, name, log)
        for stdin in (post_tool_use("implementer"), subagent_stop("implementer"), "", post_tool_use("Explore")):
            self.reminder(stdin, path_dirs=[bin_dir])
        self.assertFalse(log.exists(), "native-reminder must not launch an interpreter")
