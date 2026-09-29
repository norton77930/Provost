import json
import os
from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime.hooks import handle_event
from provost_runtime.store import StoreError, record, snapshot


class HookTests(FixtureCase):
    def event(self, name, source=None):
        result = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": name}
        if source:
            result["source"] = source
        return result

    def context(self, output):
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_startup_clear_fork_do_not_resume_old_task(self):
        record(self.root, "s1", self.payload("PRIVATE NEXT STEP"), None, "r")
        snapshot(self.root, "s1", "e")
        for source in ("startup", "clear", "fork"):
            output = handle_event(self.event("SessionStart", source), self.config_path)
            self.assertIn("adaptive", self.context(output))
            self.assertNotIn("PRIVATE NEXT STEP", self.context(output))
            self.assertNotIn("initialUserMessage", output.get("hookSpecificOutput", {}))

    def test_compact_and_resume_restore_same_session_as_observations(self):
        self.seed_snapshot()
        for source in ("compact", "resume"):
            output = handle_event(self.event("SessionStart", source), self.config_path)
            self.assertIn("collect result", self.context(output))
            self.assertIn("verify", self.context(output).lower())
        event = self.event("SessionStart", "compact")
        event["session_id"] = "s2"
        self.assertNotIn("collect result", self.context(handle_event(event, self.config_path)))

    def test_precompact_failure_does_not_block(self):
        with mock.patch("provost_runtime.hooks.snapshot", side_effect=StoreError("WRITE_FAILED")):
            output = handle_event(self.event("PreCompact"), self.config_path)
        self.assertNotEqual(output.get("decision"), "block")
        self.assertNotEqual(output.get("continue"), False)
        self.assertIn("terminalSequence", output)

    def test_missing_checkpoint_not_reported_as_saved(self):
        output = handle_event(self.event("PreCompact"), self.config_path)
        self.assertIn("NO_CHECKPOINT", json.dumps(output))

    def test_disabled_events_have_no_workspace_writes(self):
        self.config_path.unlink()
        before = list(self.root.iterdir())
        self.assertEqual(handle_event(self.event("PreCompact"), self.config_path), {})
        self.assertEqual(list(self.root.iterdir()), before)

    def test_legacy_resume_is_empty(self):
        self.config_path.unlink()
        self.assertEqual(handle_event(self.event("SessionStart", "resume"), self.config_path), {})
        self.assertIn("collaborator tier", self.context(handle_event(self.event("SessionStart", "startup"), self.config_path)))

    def test_bad_config_warns_and_falls_back(self):
        self.config_path.write_text("{broken")
        output = handle_event(self.event("SessionStart", "startup"), self.config_path)
        self.assertIn("systemMessage", output)
        self.assertIn("collaborator tier", self.context(output))
        self.assertFalse((self.root / ".provost").exists())

    def test_context_size_is_bounded_and_payload_is_not_silently_lost(self):
        record(self.root, "s1", self.payload("details " * 2000), None, "r")
        output = handle_event(self.event("SessionStart", "compact"), self.config_path)
        self.assertLessEqual(len(self.context(output)), 8000)
        self.assertIn("Read", self.context(output))
        self.assertIn(".json", self.context(output))

    def test_handoff_disabled_still_injects_policy_but_does_not_save(self):
        self.cfg["handoff"] = False
        self.config_path.write_text(json.dumps(self.cfg))
        self.assertEqual(handle_event(self.event("PreCompact"), self.config_path), {})
        self.assertIn("disabled", self.context(handle_event(self.event("SessionStart", "startup"), self.config_path)))

    def test_bad_event_does_not_raise_or_echo_payload(self):
        output = handle_event({"hook_event_name": "PreCompact", "secret": "do-not-echo"}, self.config_path)
        self.assertNotIn("do-not-echo", json.dumps(output))
        self.assertNotEqual(output.get("decision"), "block")


class TranscriptFallbackTests(FixtureCase):
    def transcript(self, project, session="sess.jsonl", age=0):
        path = self.root / "projects" / project / session
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        if age:
            stamp = path.stat().st_mtime - age
            os.utime(path, (stamp, stamp))
        return path

    def test_existing_path_is_used_unchanged(self):
        from provost_runtime.hooks import resolve_transcript
        bound = self.transcript("D--Repo")
        self.transcript("D--Repo--claude-worktrees-x")
        self.assertEqual(resolve_transcript(str(bound)), str(bound))

    def test_moved_transcript_is_found_in_worktree_sibling(self):
        from provost_runtime.hooks import resolve_transcript
        bound = self.root / "projects" / "D--Repo" / "sess.jsonl"
        moved = self.transcript("D--Repo--claude-worktrees-x")
        self.transcript("D--Repo--claude-worktrees-y", session="other.jsonl")
        self.transcript("D--Repository", session="sess.jsonl")
        self.assertEqual(Path(resolve_transcript(str(bound))), moved)

    def test_newest_of_several_siblings_wins(self):
        from provost_runtime.hooks import resolve_transcript
        bound = self.root / "projects" / "D--Repo" / "sess.jsonl"
        self.transcript("D--Repo--claude-worktrees-old", age=500)
        newest = self.transcript("D--Repo--claude-worktrees-new")
        self.transcript("D--Repo--claude-worktrees-older", age=900)
        self.assertEqual(Path(resolve_transcript(str(bound))), newest)

    def test_nothing_matching_returns_original_and_null_cursor(self):
        from provost_runtime.hooks import cursor, resolve_transcript
        bound = self.root / "projects" / "D--Repo" / "sess.jsonl"
        self.transcript("D--Other--claude-worktrees-x")
        self.assertEqual(resolve_transcript(str(bound)), str(bound))
        self.assertIsNone(cursor(resolve_transcript(str(bound))))
        self.assertIsNone(resolve_transcript(None))

    def test_compaction_metadata_uses_the_moved_transcript(self):
        from provost_runtime.store import session_dir
        bound = self.root / "projects" / "D--Repo" / "sess.jsonl"
        self.transcript("D--Repo--claude-worktrees-x")
        event = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "PostCompact",
                 "transcript_path": str(bound)}
        handle_event(event, self.config_path)
        data = json.loads((session_dir(self.root, "s1") / "compaction.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(data["cursor"])
