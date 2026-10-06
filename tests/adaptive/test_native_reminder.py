import json
import shutil
from _support import FixtureCase, REPO

HOOK = REPO / "hooks" / "native-reminder"


def post_tool_use(role):
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Agent",
                       "tool_input": {"subagent_type": role, "prompt": "x"}})


class NativeReminderTests(FixtureCase):
    """Deprecated: native-facts reminds at turn start; this stub stays one release
    so stale registrations keep working, and answers {} to everything."""

    def reminder(self, stdin, **kwargs):
        result = self.run_bash(HOOK, stdin=stdin, **kwargs)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)

    def test_every_input_is_silent(self):
        stdins = [post_tool_use(role) for role in ("implementer", "implementer-high", "general-purpose", "Explore")]
        stdins += ["", "not json", json.dumps({"hook_event_name": "SubagentStop", "agent_type": "implementer"})]
        for stdin in stdins:
            self.assertEqual(self.reminder(stdin), {}, stdin)
        env = {"PROVOST_REMINDER_ROLES": "custom-writer"}
        self.assertEqual(self.reminder(post_tool_use("custom-writer"), env=env), {})

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
        self.reminder(post_tool_use("implementer"), path_dirs=[bin_dir])
        self.assertFalse(log.exists(), "native-reminder must not launch an interpreter")
