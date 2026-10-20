import io
import json
import os
import time
from contextlib import redirect_stdout
from unittest import mock

from _support import FixtureCase

SESSION = "5d1c-ab12"
HOUR = 3600


def gate(gate_id, status="pending", kind="machine", **extra):
    return {"id": gate_id, "kind": kind, "check": "run " + gate_id, "status": status, **extra}


class AcceptanceStopTests(FixtureCase):
    """The synchronous Stop registration (hooks/acceptance-gate) blocks the turn end while a session
    acceptance file has a non-human gate pending or failed."""

    def setUp(self):
        super().setUp()
        self.directory = self.root / ".claude" / "acceptance"
        self.transcript = self.root / "t.jsonl"
        self.start = time.time() - HOUR  # the session began an hour ago
        self.write_transcript(self.start)

    def write_transcript(self, start):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start)) + ".000Z"
        lines = [{"type": "ai-title", "sessionId": SESSION, "aiTitle": "t"},  # metadata lines carry no time
                 {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": "go"}},
                 {"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z", "message": {"content": []}}]
        self.transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    def acceptance(self, name, gates, *, age=None, raw=None):
        """Write <cwd>/.claude/acceptance/<name>; age (seconds before now) sets its mtime."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps({"task": "feature", "gates": gates}), encoding="utf-8")
        if age is not None:
            when = time.time() - age
            os.utime(path, (when, when))
        return path

    def stop(self, acceptance=True, **extra):
        from provost_runtime.hooks import handle_event
        data = {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.root),
                "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done", **extra}
        return handle_event(data, self.config_path, acceptance=acceptance)

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

    def test_only_human_gates_unmet_is_no_decision(self):
        self.acceptance("release.json", [gate("tests", "pass"), gate("merge", kind="human"),
                                         gate("tag", "fail", kind="human")])
        self.assertEqual(self.stop(), {})

    def test_files_from_before_the_session_start_are_ignored(self):
        self.acceptance("old.json", [gate("old-gate")], age=2 * HOUR)
        self.assertEqual(self.stop(), {})
        self.acceptance("new.json", [gate("new-gate")], age=HOUR / 2)  # written during this session
        reason = self.stop()["reason"]
        self.assertIn("new-gate", reason)
        self.assertNotIn("old-gate", reason)

    def test_resumed_session_keeps_its_own_files(self):
        # A resume appends to the same transcript, so a file from before the resume still counts.
        self.write_transcript(time.time() - 3 * HOUR)
        self.acceptance("feature.json", [gate("before-resume")], age=2 * HOUR)
        self.assertIn("before-resume", self.stop()["reason"])

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
        for name, raw in broken.items():
            with self.subTest(name):
                for old in self.directory.glob("*.json") if self.directory.exists() else ():
                    old.unlink()
                path = self.acceptance(name, None, raw=raw)
                result = self.stop()
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
