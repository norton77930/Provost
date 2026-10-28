from unittest import mock

from test_acceptance import AcceptanceCase, SESSION, gate

ROLES = ("implementer", "implementer-sonnet", "mech-executor")
UNSET = object()


class DispatchReminderTests(AcceptanceCase):
    """The PreToolUse Agent registration of hooks/acceptance-gate: an advisory reminder when an
    implementation brief does not cite the session's acceptance file; never a decision."""

    def dispatch(self, prompt="Build the panel.", role="implementer", acceptance=True, tool_input=UNSET, **extra):
        from provost_runtime.hooks import handle_event
        if tool_input is UNSET:
            tool_input = {"subagent_type": role, "description": "build", "prompt": prompt}
        data = {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.root),
                "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input, **extra}
        return handle_event(data, self.config_path, acceptance=acceptance)

    def assert_reminder(self, result, *needles):
        self.assertEqual(list(result), ["hookSpecificOutput"])
        output = result["hookSpecificOutput"]
        self.assertEqual(set(output), {"hookEventName", "additionalContext"})
        self.assertEqual(output["hookEventName"], "PreToolUse")
        for needle in needles:
            self.assertIn(needle, output["additionalContext"])

    def test_each_implementation_role_gets_a_reminder_naming_the_file(self):
        self.acceptance("feature.json", [gate("unit-tests")])
        for role in ROLES:
            with self.subTest(role=role):
                self.assert_reminder(self.dispatch(role=role), "feature.json", ".claude/acceptance")

    def test_visual_gates_add_fixture_and_screenshot_directory(self):
        self.acceptance("ui.json", [gate("home-dark", kind="visual")])
        self.assert_reminder(self.dispatch(), "fixture", "screenshot")
        self.acceptance("ui.json", [gate("unit-tests")])
        self.assertNotIn("fixture", self.dispatch()["hookSpecificOutput"]["additionalContext"])

    def test_every_session_file_is_listed(self):
        self.acceptance("a.json", [gate("one")])
        self.acceptance("b.json", [gate("two")])
        self.assert_reminder(self.dispatch(), "a.json", "b.json")

    def test_citing_any_session_file_is_silent(self):
        self.acceptance("a.json", [gate("one")])
        self.acceptance("b.json", [gate("two")])
        for prompt in ("Follow .claude/acceptance/b.json.", "gates: b.json", "see " + str(self.directory / "b.json"),
                       "see .claude\\acceptance\\B.JSON", "`a.json`"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.dispatch(prompt), {})

    def test_a_longer_name_is_not_a_citation(self):
        self.acceptance("feature.json", [gate("one")])
        self.assert_reminder(self.dispatch("see myfeature.json and feature.json.bak"), "feature.json")

    def test_other_roles_are_silent(self):
        self.acceptance("feature.json", [gate("one")])
        for role in ("code-reviewer", "Explore", "general-purpose", "acceptance-verifier", "test-analyst", None, 7):
            with self.subTest(role=role):
                self.assertEqual(self.dispatch(role=role), {})

    def test_no_session_acceptance_file_is_silent(self):
        self.assertEqual(self.dispatch(), {})
        self.acceptance("old.json", [gate("one")], age=3 * 3600, mine=False)  # written before this session began
        self.assertEqual(self.dispatch(), {})
        self.acceptance("other.json", [gate("two")], mine=False)  # another session's, newer than our start
        self.assertEqual(self.dispatch(), {})
        (self.directory / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.dispatch(), {})

    def test_other_session_file_is_not_listed_or_counted(self):
        self.acceptance("mine.json", [gate("one")])
        self.acceptance("other.json", [gate("two")], mine=False)
        self.assert_reminder(self.dispatch(), "mine.json")
        self.assertNotIn("other.json", self.dispatch()["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.dispatch("Follow mine.json."), {})
        self.assert_reminder(self.dispatch("Follow other.json."), "mine.json")  # citing theirs is no citation

    def test_directory_without_json_files_never_reads_the_transcript(self):
        self.directory.mkdir(parents=True)
        (self.directory / "notes.txt").write_text("x", encoding="utf-8")
        with mock.patch("provost_runtime.hooks.resolve_transcript") as resolve,                 mock.patch("provost_runtime.acceptance.open", create=True) as opened:  # errors would be swallowed
            self.assertEqual(self.dispatch(), {})
        resolve.assert_not_called()
        opened.assert_not_called()

    def test_unreadable_transcript_is_silent(self):
        self.acceptance("feature.json", [gate("one")])
        self.assertEqual(self.dispatch(transcript_path=str(self.root / "missing.jsonl")), {})

    def test_subagent_dispatch_is_silent(self):
        self.acceptance("feature.json", [gate("one")])
        self.assertEqual(self.dispatch(agent_id="agent-1"), {})

    def test_malformed_file_still_counts_as_an_acceptance_file(self):
        self.acceptance("broken.json", None, raw="{not json")
        self.assert_reminder(self.dispatch(), "broken.json")

    def test_never_a_decision_and_native_event_does_not_answer(self):
        self.acceptance("feature.json", [gate("one", "fail")])
        result = self.dispatch()
        for key in ("decision", "permissionDecision", "continue"):
            self.assertNotIn(key, result)
            self.assertNotIn(key, result["hookSpecificOutput"])
        self.assertEqual(self.dispatch(acceptance=False), {})

    def test_malformed_input_and_internal_errors_return_nothing(self):
        self.acceptance("feature.json", [gate("one")])
        for tool_input in (None, "text", [], {}, {"subagent_type": "implementer"},
                           {"subagent_type": "implementer", "prompt": 5}):
            with self.subTest(tool_input=tool_input):
                self.assertEqual(self.dispatch(tool_input=tool_input), {})
        with mock.patch("provost_runtime.dispatch_reminder.session_files", side_effect=RuntimeError("boom")):
            self.assertEqual(self.dispatch(), {})
