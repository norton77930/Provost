import calendar
import io
import json
import os
import re
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from _support import REPO, FixtureCase

SESSION = "5d1c-ab12"
HOUR = 3600


def gate(gate_id, status="pending", kind="machine", **extra):
    return {"id": gate_id, "kind": kind, "check": "run " + gate_id, "status": status, **extra}


class AcceptanceCase(FixtureCase):
    """A session that began an hour ago, its transcript, and acceptance files under <cwd>/.claude/acceptance.
    A file belongs to the session when the transcript holds a successful main-session Write or Edit of it."""

    def setUp(self):
        super().setUp()
        self.directory = self.root / ".claude" / "acceptance"
        self.transcript = self.root / "t.jsonl"
        self.start = time.time() - HOUR  # the session began an hour ago
        self.owned_calls = 0
        self.write_transcript(self.start)

    def write_transcript(self, start):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start)) + ".000Z"
        lines = [{"type": "ai-title", "sessionId": SESSION, "aiTitle": "t"},  # metadata lines carry no time
                 {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": "go"}},
                 {"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z", "message": {"content": []}}]
        self.transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    def tool_call(self, target, *, tool="Write", error=False, sidechain=False):
        """Append a tool_use of `tool` on `target` and its tool_result to the transcript."""
        self.owned_calls += 1
        call = "toolu_own_%d" % self.owned_calls
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        at = iso(time.time() - HOUR / 2)
        use = {"type": "assistant", "timestamp": at, "isSidechain": sidechain,
               "message": {"content": [{"type": "tool_use", "id": call, "name": tool, "input": {key: target}}]}}
        result = {"type": "user", "timestamp": at, "isSidechain": sidechain,
                  "message": {"content": [{"type": "tool_result", "tool_use_id": call, "is_error": error,
                                           "content": "ok"}]}}
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(use) + "\n" + json.dumps(result) + "\n")

    def acceptance(self, name, gates, *, age=None, raw=None, mine=True):
        """Write <cwd>/.claude/acceptance/<name>; age (seconds before now) sets its mtime. With mine, this
        session wrote it: the transcript gets the matching Write call. Without, another session did."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps({"task": "feature", "gates": gates}), encoding="utf-8")
        if age is not None:
            when = time.time() - age
            os.utime(path, (when, when))
        if mine:
            self.tool_call(str(path))
        return path

    def stop(self, acceptance=True, **extra):
        from provost_runtime.hooks import handle_event
        data = {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.root),
                "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done", **extra}
        return handle_event(data, self.config_path, acceptance=acceptance)


class AcceptanceStopTests(AcceptanceCase):
    """The synchronous Stop registration (hooks/acceptance-gate) blocks the turn end while a session
    acceptance file has a non-human gate pending or failed."""

    def test_no_acceptance_directory_or_file_is_no_decision(self):
        self.assertEqual(self.stop(), {})
        self.directory.mkdir(parents=True)
        (self.directory / "notes.txt").write_text("not a gate file", encoding="utf-8")
        self.assertEqual(self.stop(), {})

    def test_pending_and_failed_gates_block_with_ids_and_path(self):
        path = self.acceptance("feature.json", [gate("lint", "pass"), gate("unit-tests"),
                                                gate("ui-dark", "fail", kind="visual"),
                                                gate("review", kind="review"), gate("cli", kind="live")])
        result = self.stop()
        self.assertEqual(result["decision"], "block")
        reason = result["reason"]
        self.assertIn(str(path), reason)
        for gate_id in ("unit-tests", "ui-dark", "review", "cli"):
            self.assertIn(gate_id, reason)
        self.assertNotIn("lint", reason)  # a passed gate is not listed

    def test_directory_without_json_files_never_reads_the_transcript(self):
        self.directory.mkdir(parents=True)
        (self.directory / "notes.txt").write_text("not a gate file", encoding="utf-8")
        with mock.patch("provost_runtime.hooks.resolve_transcript") as resolve, \
                mock.patch("provost_runtime.acceptance.open", create=True) as opened:  # errors would end the turn too
            self.assertEqual(self.stop(), {})
            self.assertEqual(self.stop(transcript_path=str(self.root / "missing.jsonl")), {})
        resolve.assert_not_called()
        opened.assert_not_called()

    def test_only_human_gates_unmet_is_no_decision(self):
        self.acceptance("release.json", [gate("tests", "pass"), gate("merge", kind="human"),
                                         gate("tag", "fail", kind="human")])
        self.assertEqual(self.stop(), {})

    def test_other_session_file_is_ignored(self):
        # Two sessions in one cwd: the other one's file is newer than this session's start, yet not ours.
        self.acceptance("manual-zh-tw-draft.json", [gate("draft")], mine=False)
        self.assertEqual(self.stop(), {})
        self.acceptance("new.json", [gate("new-gate")])
        reason = self.stop()["reason"]
        self.assertIn("new-gate", reason)
        self.assertNotIn("draft", reason)

    def test_resumed_session_keeps_its_own_files(self):
        # A resume appends to the same transcript, so a file written before the resume still counts.
        self.acceptance("feature.json", [gate("before-resume")], age=2 * HOUR)
        self.acceptance("other.json", [gate("other-session")], mine=False)
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "timestamp": iso(time.time()),
                                     "message": {"role": "user", "content": "resumed"}}) + "\n")
        reason = self.stop()["reason"]
        self.assertIn("before-resume", reason)
        self.assertNotIn("other-session", reason)

    def test_edit_only_of_a_pre_existing_file_is_owned(self):
        path = self.acceptance("feature.json", [gate("pre-existing")], age=2 * HOUR, mine=False)
        self.assertEqual(self.stop(), {})
        self.tool_call(str(path), tool="Edit")  # the file pre-existed; the session only edited it
        self.assertIn("pre-existing", self.stop()["reason"])

    def test_notebook_edit_makes_a_file_ours(self):
        path = self.acceptance("feature.json", [gate("notebook")], mine=False)
        self.assertEqual(self.stop(), {})
        self.tool_call(str(path), tool="NotebookEdit")  # notebook_path, not file_path
        self.assertIn("notebook", self.stop()["reason"])

    def test_calls_that_do_not_make_a_file_ours(self):
        path = self.acceptance("feature.json", [gate("not-ours")], mine=False)
        self.tool_call(str(path), error=True)  # the Write failed
        self.tool_call(str(path), sidechain=True)  # a subagent's Write
        self.tool_call(str(self.root / "elsewhere" / "feature.json"))  # same name, another directory
        self.tool_call(str(self.directory / "feature.json.bak"))  # another file in the directory
        self.assertEqual(self.stop(), {})

    def test_a_failed_write_does_not_hide_a_later_successful_one(self):
        path = self.acceptance("feature.json", [gate("retried")], mine=False)
        self.tool_call(str(path), error=True)
        self.assertEqual(self.stop(), {})
        self.tool_call(str(path))
        self.assertIn("retried", self.stop()["reason"])

    def test_write_early_in_a_transcript_over_8_mb_still_owns(self):
        self.acceptance("feature.json", [gate("early")])
        filler = json.dumps({"type": "assistant", "timestamp": iso(time.time()),
                             "message": {"content": [{"type": "text", "text": "x" * (1024 * 1024)}]}}) + "\n"
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(filler * 9)
        self.assertGreater(self.transcript.stat().st_size, 8 * 1024 * 1024)
        self.assertIn("early", self.stop()["reason"])

    def test_relative_and_absolute_paths_both_match(self):
        spellings = {"rel": ".claude/acceptance/rel.json", "dot": "./.claude/acceptance/dot.json",
                     "abs": (self.directory / "abs.json").as_posix()}
        if os.name == "nt":  # backslashes and case are the same path on Windows
            spellings["back"] = ".claude\\acceptance\\back.json"
            spellings["absback"] = str(self.directory / "absback.json").replace("/", "\\")
            spellings["upper"] = str(self.directory / "UPPER.JSON").upper()
        for stem, target in spellings.items():
            self.acceptance(stem + ".json", [gate(stem + "-gate")], mine=False)
            self.tool_call(target)
        reason = self.stop()["reason"]
        for stem in spellings:
            self.assertIn(stem + "-gate", reason)

    def test_every_session_file_is_checked(self):
        first = self.acceptance("a.json", [gate("a1"), gate("a2", "pass")])
        second = self.acceptance("b.json", [gate("b1", "fail")])
        reason = self.stop()["reason"]
        for text in (str(first), str(second), "a1", "b1"):
            self.assertIn(text, reason)

    def test_malformed_files_are_named_and_never_raise(self):
        broken = {
            "syntax.json": "{not json",
            "not-object.json": "[]",
            "no-gates.json": json.dumps({"task": "t"}),
            "bad-kind.json": json.dumps({"task": "t", "gates": [gate("g", kind="manual")]}),
            "bad-status.json": json.dumps({"task": "t", "gates": [gate("g", "done")]}),
            "no-id.json": json.dumps({"task": "t", "gates": [{"kind": "machine", "check": "c", "status": "pass"}]}),
            "bad-ran-at.json": json.dumps({"task": "t", "gates": [gate("g", "pass", ranAt="yesterday")]}),
        }
        for index, (name, raw) in enumerate(broken.items()):
            with self.subTest(name):
                for old in self.directory.glob("*.json") if self.directory.exists() else ():
                    old.unlink()
                path = self.acceptance(name, None, raw=raw)
                result = self.stop(session_id="case-%d" % index)  # each case its own push budget
                self.assertEqual(result.get("decision"), "block", result)
                self.assertIn(str(path), result["reason"])
                self.assertIn("malformed", result["reason"].lower())

    def test_malformed_file_is_reported_beside_unmet_gates(self):
        self.acceptance("good.json", [gate("g1")])
        bad = self.acceptance("bad.json", None, raw="{")
        reason = self.stop()["reason"]
        self.assertIn("g1", reason)
        self.assertIn(str(bad), reason)

    def test_valid_optional_fields_pass(self):
        self.acceptance("feature.json", [gate("g", "pass", ranAt="2026-10-21T03:00:00Z", evidence="12 passed",
                                              source="docs/spec.md")])
        self.assertEqual(self.stop(), {})

    def test_internal_errors_end_the_turn(self):
        self.acceptance("feature.json", [gate("g")])
        self.assertEqual(self.stop(cwd=42), {})
        self.assertEqual(self.stop(transcript_path=str(self.root / "missing.jsonl")), {})  # start unknown
        with mock.patch("provost_runtime.acceptance.json.loads", side_effect=RuntimeError("boom")):
            self.assertEqual(self.stop(), {})

    def test_async_declaration_scan_registration_never_blocks(self):
        self.acceptance("feature.json", [gate("g")])
        self.assertEqual(self.stop(acceptance=False), {})

    def test_gate_hook_answers_only_stop(self):
        from provost_runtime.hooks import handle_event
        self.acceptance("feature.json", [gate("g")])
        event = {"session_id": SESSION, "cwd": str(self.root), "hook_event_name": "SessionStart", "source": "startup"}
        self.assertEqual(handle_event(event, self.config_path, acceptance=True), {})

    def test_cli_entry_point(self):
        from provost_runtime import cli
        self.acceptance("feature.json", [gate("g")])
        event = {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.root),
                 "hook_event_name": "Stop"}
        for stdin, decision in ((json.dumps(event), "block"), ("{broken", None)):
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), \
                    mock.patch.dict(os.environ, PROVOST_CONFIG=str(self.config_path)):
                self.assertEqual(cli.main(["acceptance-hook"]), 0)
            self.assertEqual(json.loads(out.getvalue()).get("decision"), decision)


class AcceptanceExemptionTests(AcceptanceCase):
    """Legitimate pauses end the turn, and the gate pushes back at most three times per episode (#45)."""

    def setUp(self):
        super().setUp()
        self.counter = self.config_path.parent / "cache" / "acceptance" / (SESSION + ".pushes")
        self.acceptance("feature.json", [gate("unit-tests")])

    def assertBlocks(self, push, **extra):
        result = self.stop(**extra)
        self.assertEqual(result.get("decision"), "block", result)
        self.assertIn("push %d/3" % push, result["reason"])

    def prompt(self, text="next", **cfg):
        """The person's next prompt through the UserPromptSubmit registration (hooks/native-facts)."""
        if cfg:
            self.config_path.write_text(json.dumps(dict(self.cfg, **cfg)), encoding="utf-8")
        stdin = json.dumps({"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.root),
                            "hook_event_name": "UserPromptSubmit", "prompt": text})
        result = self.run_bash(REPO / "hooks" / "native-facts", stdin=stdin)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)

    def test_three_pushes_per_episode_then_no_decision(self):
        for push in (1, 2, 3):
            self.assertBlocks(push)
        self.assertEqual(self.stop(), {})  # the fourth unmet Stop ends the turn
        self.assertEqual(self.stop(), {})  # and the budget stays spent until a new episode

    def test_background_tasks_end_the_turn_without_spending_a_push(self):
        running = [{"id": "b1", "type": "bash", "status": "running"}]
        self.assertEqual(self.stop(background_tasks=running), {})
        self.assertFalse(self.counter.exists())
        self.assertBlocks(1, background_tasks=[])

    def test_blocker_line_ends_the_turn(self):
        for message in ("needs input: which locale is the default?", "Done so far.\nfailed: npm test exits 1",
                        "- needs input: pick a fixture", "* Failed: capture script", "> NEEDS INPUT: mockup?",
                        "**needs input:** the dark mockup is missing", "__failed:__ build", "  - **failed**: x"):
            with self.subTest(message):
                self.assertEqual(self.stop(last_assistant_message=message), {})
        self.assertFalse(self.counter.exists())
        self.assertBlocks(1, last_assistant_message="Nothing here needs input: the tests failed: twice, now fixed.")

    def test_subagent_stop_is_ignored(self):
        self.assertEqual(self.stop(agent_id="a1", agent_type="implementer"), {})
        self.assertFalse(self.counter.exists())

    def test_stop_with_no_unmet_gate_resets_the_episode(self):
        self.assertBlocks(1)
        self.assertBlocks(2)
        self.acceptance("feature.json", [gate("unit-tests", "pass")])
        self.assertEqual(self.stop(), {})
        self.acceptance("feature.json", [gate("unit-tests", "fail")])
        self.assertBlocks(1)

    def test_person_prompt_resets_the_episode(self):
        for push in (1, 2, 3):
            self.assertBlocks(push)
        self.assertEqual(self.prompt(), {})
        self.assertBlocks(1)

    def test_person_prompt_resets_with_handoff_off(self):
        self.assertBlocks(1)
        self.assertBlocks(2)
        self.assertEqual(self.prompt(handoff=False), {})
        self.assertFalse((self.config_path.parent / "cache" / "facts").exists())
        self.assertBlocks(1)

    def test_subagent_results_and_notifications_do_not_reset(self):
        self.assertBlocks(1)
        for text in ("<task-notification><task-id>b1</task-id><status>completed</status></task-notification>",
                     '<agent-message from="a1">[Subagent hand-back] done</agent-message>',
                     'Another Claude session sent a message: <agent-message from="a1">hi</agent-message>'):
            self.prompt(text)
        self.assertBlocks(2)

    def test_counter_errors_never_block(self):
        self.counter.parent.mkdir(parents=True)
        self.counter.write_text("garbage", encoding="utf-8")
        self.assertEqual(self.stop(), {})  # unreadable count: no decision
        self.counter.unlink()
        self.counter.mkdir()  # the counter cannot be written
        self.assertEqual(self.stop(), {})
        self.assertEqual(self.stop(session_id="../escape"), {})  # a session id that names no cache file


def iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + ".000Z"


def clock(when):
    return time.strftime("%H:%M:%S", time.localtime(when))


class AcceptanceStaleTests(AcceptanceCase):
    """A machine, live or visual gate passed before the main session's last edit is stale (#46); so is a
    review gate without files/digest (#71)."""

    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.calls = 0

    def edit(self, at, path=None, *, tool="Edit", error=False, sidechain=False):
        """Append a main-session edit tool call and its result, both stamped at `at`, to the transcript."""
        self.calls += 1
        call = "toolu_%d" % self.calls
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        target = path or "/work/repo/src/app.py"  # not self.root: temp dirs are facts.OUTSIDE
        use = {"type": "assistant", "timestamp": iso(at), "isSidechain": sidechain,
               "message": {"content": [{"type": "tool_use", "id": call, "name": tool, "input": {key: target}}]}}
        result = {"type": "user", "timestamp": iso(at), "isSidechain": sidechain,
                  "message": {"content": [{"type": "tool_result", "tool_use_id": call, "is_error": error,
                                           "content": "ok"}]}}
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(use) + "\n" + json.dumps(result) + "\n")

    def test_pass_before_the_last_edit_is_stale_with_both_times(self):
        ran, edited = self.now - 40 * 60, self.now - 30 * 60
        self.edit(self.now - 50 * 60)
        self.edit(edited, tool="Write")
        path = self.acceptance("feature.json", [
            gate("unit-tests", "pass", ranAt=iso(ran)), gate("cli", "pass", kind="live", ranAt=iso(ran)),
            gate("ui", "pass", kind="visual", ranAt=iso(ran)),
            gate("code-review", "pass", kind="review", ranAt=iso(ran)), gate("merge", "pass", kind="human")])
        result = self.stop()
        self.assertEqual(result.get("decision"), "block", result)
        reason = result["reason"]
        self.assertIn(str(path), reason)
        for gate_id in ("unit-tests", "cli", "ui"):
            self.assertIn(gate_id + " (stale", reason)
        self.assertIn(clock(ran), reason)
        self.assertIn(clock(edited), reason)
        self.assertIn("code-review (stale: review gate has no files/digest", reason)  # #71
        self.assertNotIn("merge", reason)  # human gates are never stale

    def test_pass_after_the_last_edit_is_not_stale(self):
        self.edit(self.now - 30 * 60)
        self.acceptance("feature.json", [gate("unit-tests", "pass", ranAt=iso(self.now - 20 * 60))])
        self.assertEqual(self.stop(), {})

    def test_pass_without_ran_at_is_stale_only_after_an_edit(self):
        self.acceptance("feature.json", [gate("unit-tests", "pass")])
        self.assertEqual(self.stop(), {})
        edited = self.now - 30 * 60
        self.edit(edited, tool="NotebookEdit")
        reason = self.stop()["reason"]
        self.assertIn("unit-tests (stale", reason)
        self.assertIn(clock(edited), reason)

    def test_edits_that_do_not_count(self):
        self.acceptance("feature.json", [gate("unit-tests", "pass")])
        self.edit(self.now - 30 * 60, "/work/repo/.claude/acceptance/feature.json")  # an acceptance file
        self.edit(self.now - 30 * 60, ".claude\\acceptance\\other.json", tool="Write")
        self.edit(self.now - 30 * 60, error=True)  # the edit failed
        self.edit(self.now - 30 * 60, sidechain=True)  # a subagent's edit
        self.assertEqual(self.stop(), {})

    def test_unreadable_edit_history_fails_open(self):
        self.edit(self.now - 30 * 60)
        self.acceptance("feature.json", [gate("unit-tests", "pass")])
        with mock.patch("provost_runtime.acceptance.last_edit", side_effect=OSError("boom")):
            self.assertEqual(self.stop(), {})

    def test_unreadable_edit_history_still_holds_pending_gates(self):
        self.acceptance("feature.json", [gate("unit-tests", "pass"), gate("cli", "pending")])
        with mock.patch("provost_runtime.acceptance.last_edit", side_effect=OSError("boom")):
            result = self.stop()
        self.assertEqual(result.get("decision"), "block", result)
        self.assertIn("cli (pending)", result["reason"])
        self.assertNotIn("unit-tests", result["reason"])

    def test_naive_ran_at_after_an_edit_is_stale_with_the_zone_message(self):
        self.edit(self.now - 30 * 60)
        naive = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.now - 20 * 60))  # after the edit, no zone
        self.acceptance("feature.json", [gate("unit-tests", "pass", ranAt=naive)])
        result = self.stop()
        self.assertEqual(result.get("decision"), "block", result)
        self.assertIn("unit-tests (stale", result["reason"])
        self.assertIn("ranAt has no time zone", result["reason"])

    def test_naive_ran_at_without_an_edit_is_not_stale(self):
        naive = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.now))
        self.acceptance("feature.json", [gate("unit-tests", "pass", ranAt=naive)])
        self.assertEqual(self.stop(), {})

    def test_ran_at_with_an_offset_after_the_edit_is_not_stale(self):
        self.edit(self.now - 30 * 60)
        east = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.now - 20 * 60 + 8 * HOUR)) + "+08:00"
        self.acceptance("feature.json", [gate("unit-tests", "pass", ranAt=east)])
        self.assertEqual(self.stop(), {})

    def test_block_reason_states_the_utc_now(self):
        self.acceptance("feature.json", [gate("unit-tests")])
        reason = self.stop()["reason"]
        match = re.search(r"now (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", reason)
        self.assertIsNotNone(match, reason)
        stamp = calendar.timegm(time.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ"))
        self.assertLess(abs(stamp - time.time()), 120)
        self.assertIn("ranAt", reason)

    def test_future_ran_at_is_stale_even_without_an_edit(self):
        self.acceptance("feature.json", [gate("ui", "pass", kind="visual", ranAt=iso(self.now + 8 * HOUR))])
        result = self.stop()
        self.assertEqual(result.get("decision"), "block", result)
        self.assertIn("ui (stale", result["reason"])
        self.assertIn("later than now", result["reason"])
        self.assertIn("local time", result["reason"])

    def test_ran_at_one_minute_ahead_is_not_stale(self):
        self.acceptance("feature.json", [gate("ui", "pass", kind="visual", ranAt=iso(self.now + 60))])
        self.assertEqual(self.stop(), {})

    def test_block_reason_gives_the_bash_and_powershell_clock_commands(self):
        self.acceptance("feature.json", [gate("unit-tests")])
        reason = self.stop()["reason"]
        self.assertIn("date -u +%Y-%m-%dT%H:%M:%SZ", reason)
        self.assertIn("(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')", reason)

    def test_block_reason_guide_path_is_absolute_and_exists(self):
        self.acceptance("feature.json", [gate("unit-tests")])
        self.acceptance("broken.json", None, raw="{")
        reason = self.stop()["reason"]
        match = re.search(r"Guide: (.+?docs[/\\]native-handoff\.md)", reason)
        self.assertIsNotNone(match, reason)
        guide = Path(match.group(1))
        self.assertTrue(guide.is_absolute(), guide)
        self.assertTrue(guide.exists(), guide)
        self.assertEqual(guide.resolve(), (REPO / "docs" / "native-handoff.md").resolve())


class AcceptanceReviewTests(AcceptanceCase):
    """A passed review gate with files and digest goes stale when a reviewed file's content changes, by any
    process; one without them goes stale after any main-session edit, like a machine gate without ranAt (#71)."""

    edit = AcceptanceStaleTests.edit

    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.calls = 0
        self.source = self.root / "src" / "app.py"
        self.source.parent.mkdir()
        self.source.write_bytes(b"print('reviewed')\n")

    def reviewed(self, *files, cwd=None):
        from provost_runtime.acceptance import digest
        names = list(files) or [str(self.source)]
        return gate("code-review", "pass", kind="review", files=names, digest=digest(names, cwd or str(self.root)))

    def test_digest_ignores_line_endings_and_tracks_content_names_and_order(self):
        from provost_runtime.acceptance import digest
        other = self.root / "src" / "other.py"
        other.write_bytes(b"x = 1\n")
        names = [str(self.source), str(other)]
        first = digest(names, str(self.root))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(digest(names, str(self.root)), first)
        self.source.write_bytes(b"print('reviewed')\r\n")  # a CRLF checkout of the same text
        self.assertEqual(digest(names, str(self.root)), first)
        self.assertRegex(digest(["src/app.py"], str(self.root)), r"^[0-9a-f]{64}$")  # read from cwd
        self.assertNotEqual(digest(list(reversed(names)), str(self.root)), first)
        self.source.write_bytes(b"print('changed')\n")
        self.assertNotEqual(digest(names, str(self.root)), first)
        with self.assertRaises(OSError):
            digest([str(self.root / "missing.py")], str(self.root))

    def test_unchanged_reviewed_files_pass_even_after_an_edit(self):
        self.acceptance("feature.json", [self.reviewed()])
        self.assertEqual(self.stop(), {})
        self.edit(self.now - 30 * 60)  # an edit elsewhere: the reviewed bytes are what count
        self.assertEqual(self.stop(), {})

    def test_reviewed_file_changed_by_any_process_is_stale(self):
        path = self.acceptance("feature.json", [self.reviewed(), gate("merge", "pass", kind="human")])
        self.source.write_bytes(b"print('changed by a subagent or Bash')\n")  # no transcript edit at all
        result = self.stop()
        self.assertEqual(result.get("decision"), "block", result)
        self.assertIn(str(path), result["reason"])
        self.assertIn("code-review (stale: reviewed files changed since the review", result["reason"])
        self.assertNotIn("merge", result["reason"])

    def test_deleted_reviewed_file_is_stale_and_named(self):
        self.acceptance("feature.json", [self.reviewed()])
        self.source.unlink()
        reason = self.stop()["reason"]
        self.assertIn("code-review (stale: reviewed files changed since the review", reason)
        self.assertIn(str(self.source), reason)

    def test_relative_reviewed_files_resolve_against_the_event_cwd(self):
        self.acceptance("feature.json", [self.reviewed("src/app.py")])
        self.assertEqual(self.stop(), {})
        self.source.write_bytes(b"print('changed')\n")
        self.assertIn("code-review (stale: reviewed files changed", self.stop()["reason"])

    def test_review_without_files_is_stale_only_after_an_edit(self):
        self.acceptance("feature.json", [gate("code-review", "pass", kind="review", ranAt=iso(self.now))])
        self.assertEqual(self.stop(), {})
        edited = self.now - 30 * 60  # before ranAt: a legacy review gate ignores ranAt, like no ranAt at all
        self.edit(edited)
        reason = self.stop()["reason"]
        self.assertIn("code-review (stale: review gate has no files/digest, last edit at %s" % clock(edited), reason)

    def test_pending_review_with_files_stays_pending(self):
        self.acceptance("feature.json", [dict(self.reviewed(), status="pending")])
        self.assertIn("code-review (pending)", self.stop()["reason"])

    def test_half_or_mistyped_files_digest_pair_is_malformed(self):
        whole = self.reviewed()
        broken = {
            "files-only": {key: value for key, value in whole.items() if key != "digest"},
            "digest-only": {key: value for key, value in whole.items() if key != "files"},
            "files-not-list": dict(whole, files=str(self.source)),
            "files-empty": dict(whole, files=[]),
            "files-not-strings": dict(whole, files=[1]),
            "digest-not-string": dict(whole, digest=7),
        }
        for index, (name, item) in enumerate(broken.items()):
            with self.subTest(name):
                for old in self.directory.glob("*.json") if self.directory.exists() else ():
                    old.unlink()
                path = self.acceptance(name + ".json", [item])
                result = self.stop(session_id="pair-%d" % index)  # each case its own push budget
                self.assertEqual(result.get("decision"), "block", result)
                self.assertIn(str(path), result["reason"])
                self.assertIn("malformed", result["reason"].lower())

    def test_digest_errors_never_raise_and_do_not_hold_the_turn(self):
        self.acceptance("feature.json", [self.reviewed()])
        with mock.patch("provost_runtime.acceptance.digest", side_effect=RuntimeError("boom")):
            self.assertEqual(self.stop(), {})
