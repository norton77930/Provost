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
        output = json.dumps(handle_event(self.event("PreCompact"), self.config_path))
        self.assertIn("decision record missing", output)
        self.assertNotIn("handoff saving recovered", output)

    def test_compact_after_partial_save_asks_for_a_record_and_points_to_facts(self):
        from provost_runtime.store import session_dir
        handle_event(self.event("PreCompact"), self.config_path)
        output = handle_event(self.event("SessionStart", "compact"), self.config_path)
        self.assertEqual(output["systemMessage"],
                         "Provost: last compaction saved facts only; write a decision record from the compaction "
                         "summary before continuing (run status, then record).")
        self.assertIn(str(session_dir(self.root, "s1") / "facts.json"), self.context(output))

    def test_compact_after_failed_save_keeps_failure_warning(self):
        from provost_runtime import facts
        with mock.patch.object(facts, "fold", side_effect=StoreError("WRITE_FAILED")):
            handle_event(self.event("PreCompact"), self.config_path)
        output = handle_event(self.event("SessionStart", "compact"), self.config_path)
        self.assertEqual(output["systemMessage"],
                         "Provost: previous handoff save failed; verify any older checkpoint before continuing.")
        self.assertNotIn("facts.json", self.context(output))

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
        self.assertNoStore()

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

    def test_hook_path_spawns_once_for_identity_plus_one_cached_home_probe(self):
        from provost_runtime import store
        store._HOME_CHECKED.clear()
        record(self.root, "s1", self.payload(), None, "r")
        store._HOME_CHECKED.clear()
        with mock.patch.object(store.subprocess, "run", wraps=store.subprocess.run) as run:
            handle_event(self.event("SessionStart", "compact"), self.config_path, native=True)
            self.assertEqual(run.call_count, 2)
            handle_event(self.event("PreCompact"), self.config_path, native=True)
            self.assertEqual(run.call_count, 3)

    def test_bad_event_does_not_raise_or_echo_payload(self):
        output = handle_event({"hook_event_name": "PreCompact", "secret": "do-not-echo"}, self.config_path)
        self.assertNotIn("do-not-echo", json.dumps(output))
        self.assertNotEqual(output.get("decision"), "block")


class SessionStartBudgetTests(FixtureCase):
    """SessionStart context: priority order, whole-item degradation, never over 8000, never an error."""

    def event(self, source, session="s1"):
        return {"session_id": session, "cwd": str(self.root), "hook_event_name": "SessionStart", "source": source}

    def start(self, source="compact", native=True):
        output = handle_event(self.event(source), self.config_path, native=native)
        text = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("unavailable", output.get("systemMessage", ""))
        self.assertLessEqual(len(text), 8000)
        return text

    def test_startup_clear_fork_carry_no_facts(self):
        record(self.root, "s1", self.payload(), None, "r")
        self.seed_facts()
        for source in ("startup", "clear", "fork"):
            text = self.start(source)
            self.assertNotIn("Session facts", text)
            self.assertNotIn("f0002", text)
            self.assertIn("New work context", text)

    def test_compact_lists_facts_record_and_files_in_priority_order(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        path = self.seed_facts()
        text = self.start()
        order = [text.index(marker) for marker in
                 ("Native handoff runtime active", "Session facts", "small next", "f0002", "f0000")]
        self.assertEqual(order, sorted(order))
        self.assertIn(str(path), text)
        self.assertIn("dirty 2/7, session commits 1", text)
        self.assertIn("code-reviewer x1, implementer x1", text)
        self.assertIn("Edits outside repos: 4", text)

    def test_resume_without_facts_omits_the_section(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        text = self.start("resume")
        self.assertIn("small next", text)
        self.assertNotIn("Session facts", text)

    def test_file_list_is_dropped_first(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        path = self.seed_facts(files=300, name_chars=40)
        text = self.start()
        self.assertNotIn("f0299", text)
        self.assertIn("full list: " + str(path), text)
        self.assertIn("Observed payload: ", text)
        self.assertIn("small next", text)

    def test_record_shrinks_to_digest_then_to_its_path(self):
        payload = self.payload("keep next")
        payload["decisions"] = ["decision %d %s" % (i, "d" * 330) for i in range(12)]  # full JSON still <= 4500
        written = record(self.root, "s1", payload, None, "r")
        self.seed_facts(files=300, repos=5, agents=40, name_chars=300)
        text = self.start()
        self.assertIn("Observed payload digest", text)
        self.assertIn("decision 11", text)
        self.assertNotIn("decision 8 ", text)
        self.assertIn("Session facts", text)
        payload["decisions"] = ["decision %d %s" % (i, "d" * 1400) for i in range(12)]
        record(self.root, "s1", payload, written["revision"], "r2")
        self.seed_facts(files=300, repos=5, agents=40, name_chars=400)
        text = self.start()
        self.assertNotIn("Observed payload", text)
        self.assertIn("full checkpoint: ", text)
        self.assertIn("records", text.split("full checkpoint: ")[1])
        self.assertIn("Session facts", text)
        self.assertIn("dirty 2/7", text)

    def test_partial_save_status_is_kept_with_the_facts(self):
        handle_event({"session_id": "s1", "cwd": str(self.root), "hook_event_name": "PreCompact"}, self.config_path)
        self.seed_facts(files=300, repos=5, agents=40, name_chars=300)
        text = self.start()
        self.assertIn("write a decision record from the compaction summary", text)
        self.assertIn("template.sources", text)
        self.assertIn("Session facts", text)
        self.assertIn("full list: ", text)

    def test_oversized_identity_falls_back_to_its_shortest_form(self):
        from provost_runtime import hooks
        record(self.root, "s1", self.payload("small next"), None, "r")
        self.seed_facts(files=300, repos=5, agents=40, name_chars=300)
        for chars, native in ((2600, True), (2600, False), (9000, True)):
            with self.subTest(chars=chars, native=native), \
                    mock.patch.object(hooks, "PLUGIN", Path("C:/" + "p" * chars)), \
                    mock.patch.object(hooks, "_policy", return_value="POLICY " * 3000):
                text = self.start(native=native)
                if chars < 8000:
                    self.assertIn("status --workspace", text)
                    self.assertNotIn("Writer limits", text)
                    self.assertIn("Session: s1", text)

    def test_every_oversized_combination_stays_within_budget(self):
        expected = None
        for files, repos, agents, chars, decisions in ((0, 0, 0, 10, 0), (400, 5, 300, 400, 30),
                                                       (400, 0, 0, 900, 0), (0, 5, 300, 900, 30)):
            with self.subTest(files=files, repos=repos, chars=chars):
                payload = self.payload("n" * 3000)
                payload["decisions"] = ["d" * 900] * decisions
                expected = record(self.root, "s1", payload, expected, "r-%d-%d" % (files, chars))["revision"]
                self.seed_facts(files, repos, agents, chars)
                self.start("compact")
                self.start("resume", native=False)

    def test_session_start_creates_the_facts_cache_dir(self):
        facts_dir = self.config_path.parent / "cache" / "facts"
        self.start("startup")
        self.assertTrue(facts_dir.is_dir())
        self.cfg["handoff"] = False
        self.config_path.write_text(json.dumps(self.cfg))
        facts_dir.rmdir()
        self.start("startup")
        self.assertFalse(facts_dir.exists())


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
