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
            self.assertIn("Native handoff runtime active", self.context(output))
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
        self.enable_facts()
        output = json.dumps(handle_event(self.event("PreCompact"), self.config_path))
        self.assertIn("decision record missing", output)
        self.assertNotIn("handoff saving recovered", output)

    def test_precompact_without_marker_keeps_the_pre_facts_behaviour(self):
        from provost_runtime.store import session_dir
        output = json.dumps(handle_event(self.event("PreCompact"), self.config_path))
        self.assertIn("NO_CHECKPOINT", output)
        self.assertFalse((session_dir(self.root, "s1") / "facts.json").exists())
        record(self.root, "s1", self.payload(), None, "r")
        self.assertIn("handoff saving recovered", json.dumps(handle_event(self.event("PreCompact"), self.config_path)))
        self.assertFalse((session_dir(self.root, "s1") / "facts.json").exists())

    def test_compact_after_partial_save_asks_for_a_record_and_points_to_facts(self):
        from provost_runtime.store import session_dir
        self.enable_facts()
        handle_event(self.event("PreCompact"), self.config_path)
        output = handle_event(self.event("SessionStart", "compact"), self.config_path)
        self.assertEqual(output["systemMessage"],
                         "Provost: last compaction saved facts only; write a decision record from the compaction "
                         "summary before continuing (run status, then record).")
        self.assertIn(str(session_dir(self.root, "s1") / "facts.json"), self.context(output))

    def test_compact_after_failed_save_keeps_failure_warning(self):
        from provost_runtime import facts
        self.enable_facts()
        with mock.patch.object(facts, "fold", side_effect=StoreError("WRITE_FAILED")) as fold:
            handle_event(self.event("PreCompact"), self.config_path)
            fold.assert_called_once()
            # SessionStart(compact) refolds and fails again: still no facts.json to point at.
            output = handle_event(self.event("SessionStart", "compact"), self.config_path)
        self.assertEqual(fold.call_count, 2)
        self.assertEqual(output["systemMessage"],
                         "Provost: previous handoff save failed; verify any older checkpoint before continuing.")
        self.assertNotIn("facts.json", self.context(output))

    def test_compact_of_a_never_recorded_session_is_not_called_a_failed_save(self):
        handle_event(self.event("PreCompact"), self.config_path)  # NO_CHECKPOINT: nothing ever recorded
        never = ("No handoff checkpoint: this session has not recorded yet; continue from the compact "
                 "summary and record a milestone now.")
        for source in ("compact", "resume"):
            output = handle_event(self.event("SessionStart", source), self.config_path)
            self.assertEqual(output["systemMessage"], "Provost: " + never)
            self.assertIn("Save status: " + never, self.context(output))
            self.assertNotIn("save failed", json.dumps(output))

    def test_unreadable_never_recorded_check_keeps_the_failed_save_wording(self):
        from provost_runtime import hooks
        handle_event(self.event("PreCompact"), self.config_path)  # NO_CHECKPOINT
        # _recovery reads the store first (None); the never-recorded check then cannot read it.
        with mock.patch.object(hooks, "current", side_effect=[None, StoreError("CORRUPT_RECORD")]) as read:
            output = handle_event(self.event("SessionStart", "compact"), self.config_path)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(output["systemMessage"],
                         "Provost: previous handoff save failed; verify any older checkpoint before continuing.")
        self.assertNotIn("has not recorded yet", json.dumps(output))

    def test_disabled_events_have_no_workspace_writes(self):
        self.config_path.unlink()
        before = list(self.root.iterdir())
        self.assertEqual(handle_event(self.event("PreCompact"), self.config_path), {})
        self.assertEqual(list(self.root.iterdir()), before)

    def test_missing_config_session_start_is_empty(self):
        self.config_path.unlink()
        for source in ("startup", "resume"):
            self.assertEqual(handle_event(self.event("SessionStart", source), self.config_path), {})
        self.assertNoStore()

    def test_bad_config_warns_without_context(self):
        self.config_path.write_text("{broken")
        output = handle_event(self.event("SessionStart", "startup"), self.config_path)
        self.assertIn("Personal policy unchanged; handoff unavailable.", output["systemMessage"])
        self.assertEqual(self.context(output), "")
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
            handle_event(self.event("SessionStart", "compact"), self.config_path)
            self.assertEqual(run.call_count, 2)
            handle_event(self.event("PreCompact"), self.config_path)
            self.assertEqual(run.call_count, 3)

    def test_bad_event_does_not_raise_or_echo_payload(self):
        output = handle_event({"hook_event_name": "PreCompact", "secret": "do-not-echo"}, self.config_path)
        self.assertNotIn("do-not-echo", json.dumps(output))
        self.assertNotEqual(output.get("decision"), "block")


class SessionStartBudgetTests(FixtureCase):
    """SessionStart context: priority order, whole-item degradation, never over 8000, never an error."""

    def event(self, source, session="s1"):
        return {"session_id": session, "cwd": str(self.root), "hook_event_name": "SessionStart", "source": source}

    def start(self, source="compact"):
        output = handle_event(self.event(source), self.config_path)
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
        path = self.seed_facts(files=300, name_chars=700)  # even ten such names overflow the budget
        text = self.start()
        self.assertNotIn("f0299", text)
        self.assertIn("full list: " + str(path), text)
        self.assertIn("Observed payload: ", text)
        self.assertIn("small next", text)

    def test_edited_file_list_is_capped_with_the_omitted_count(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        path = self.seed_facts(files=42)
        text = self.start()
        listed = [line for line in text.splitlines() if line.startswith("- C:/repo/f")]
        self.assertEqual(len(listed), 10)
        self.assertIn("f0041", listed[0])
        self.assertIn("f0032", listed[-1])
        self.assertNotIn("f0031", text)
        self.assertIn("- 32 more not listed; full list: " + str(path), text)

    def test_omitted_count_includes_files_the_facts_did_not_keep(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        for files, kept_out, line in (
                (42, 8, "- 40 more not listed: 32 in %s, 8 older not kept"),
                (3, 5, "- 5 more not listed: older, not kept"),
                (42, 0, "- 32 more not listed; full list: %s"),
                (0, 6, "Edited repo files: 6 older not kept")):
            with self.subTest(files=files, omitted=kept_out):
                path = self.seed_facts(files=files)
                self.reseal(path, files_omitted=kept_out)
                self.assertIn(line.replace("%s", str(path)), self.start())
        path = self.seed_facts(files=300, name_chars=700)
        self.reseal(path, files_omitted=8)
        self.assertIn("Edited repo files omitted for size; 300 in %s, 8 older not kept" % path, self.start())

    def reseal(self, path, **changes):
        from provost_runtime.config import handoff_home
        from provost_runtime.store import _seal, atomic_write
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("checksum")
        atomic_write(handoff_home(), path, _seal(dict(data, **changes)))

    def activity(self, recorded, agents=(), commits=(), last_change=None):
        """facts.json whose dispatches and repo commits are dated relative to the record time."""
        from datetime import datetime, timezone
        path = self.seed_facts(files=3, repos=1, agents=0)
        repo = json.loads(path.read_text(encoding="utf-8"))["repos"][0]
        iso = (lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat())
        self.reseal(path, agents=[{"role": role, "desc": desc, "bg": False, "ts": recorded + delta}
                                  for role, desc, delta in agents],
                    repos=[dict(repo, commits=[{"hash": h, "subject": s, "ts": recorded + delta}
                                               for h, s, delta in commits])],
                    last_change_at=None if last_change is None else iso(recorded + last_change))
        return path

    def recorded_epoch(self):
        from datetime import datetime
        from provost_runtime.store import current
        return int(datetime.fromisoformat(current(self.root, "s1")["recorded_at"]).timestamp())

    def test_compact_reports_activity_since_the_last_record(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        recorded = self.recorded_epoch()
        self.activity(recorded, agents=[("implementer-sonnet", "early slice", -600),
                                        ("implementer", "build parser", 300),
                                        ("code-reviewer", "review parser", 1530)],
                      commits=[("new1234", "feat: parser", 900), ("old1234", "chore: before", -900)],
                      last_change=1000)
        text = self.start()
        self.assertIn("Since the last record", text)
        self.assertIn("Checkpoint is 25 min older than the latest captured activity; "
                      "2 dispatches / 1 commit after it.", text)
        self.assertIn("implementer: build parser", text)
        self.assertIn("code-reviewer: review parser", text)
        self.assertIn("new1234 feat: parser", text)
        self.assertNotIn("early slice", text)
        self.assertNotIn("old1234", text)
        order = [text.index(marker) for marker in ("Session facts", "small next", "Since the last record", "f0002")]
        self.assertEqual(order, sorted(order))

    def test_current_checkpoint_reports_no_later_activity(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        recorded = self.recorded_epoch()
        self.activity(recorded, agents=[("implementer", "before", -60)], commits=[("old1234", "x", -30)],
                      last_change=-30)
        self.assertIn("Checkpoint is not older than the latest captured activity; 0 dispatches / 0 commits after it.",
                      self.start())

    def test_compact_without_a_record_says_all_activity_is_unrecorded(self):
        self.activity(1_800_000_000, agents=[("implementer", "build parser", 0), ("Explore", "map seams", 60)],
                      commits=[("new1234", "feat: parser", 30)])
        text = self.start()
        self.assertIn("Since the last record: no decision record in this session yet; "
                      "2 dispatches / 1 commit captured, none recorded.", text)
        self.assertIn("Explore: map seams", text)
        self.assertIn("new1234 feat: parser", text)

    def test_activity_lists_are_capped_and_stay_within_budget(self):
        payload = self.payload_v1("n" * 3000)
        payload["decisions"] = ["d" * 900] * 30
        self.record_v1(self.root, "s1", payload, None, "r")
        recorded = self.recorded_epoch()
        self.activity(recorded, agents=[("implementer", "slice %03d %s" % (i, "x" * 110), 10 + i) for i in range(300)],
                      commits=[("h%05d" % i, "subject %d %s" % (i, "s" * 70), 10 + i) for i in range(20)],
                      last_change=400)
        text = self.start()
        self.assertIn("300 dispatches / 20 commits after it.", text)
        self.assertNotIn("slice 294", text)
        self.assertLessEqual(len(text), 8000)

    def test_activity_shrinks_to_its_headline_before_the_checkpoint_loses_its_digest(self):
        payload = self.payload_v1("keep next")
        payload.update(work_id="keep-work", blockers=["keep blocker"],
                       decisions=["decision %d %s" % (i, "d" * 1400) for i in range(4)])  # digest ~4.3 KB
        self.record_v1(self.root, "s1", payload, None, "r")
        recorded = self.recorded_epoch()
        path = self.activity(recorded, agents=[("implementer", "slice %d %s" % (i, "x" * 110), 10 + i) for i in range(5)],
                             commits=[("h%05d" % i, "subject %s" % ("s" * 90), 10 + i) for i in range(3)],
                             last_change=400)
        repo = json.loads(path.read_text(encoding="utf-8"))["repos"][0]
        self.reseal(path, repos=[dict(repo, root=repo["root"] + str(k)) for k in range(3)])
        # Sized so that either the activity details or the digest must go (the old order dropped the digest).
        text = self.start()
        self.assertIn("Observed payload digest", text)
        for kept in ("keep-work", "keep next", "keep blocker"):
            self.assertIn(kept, text)
        self.assertIn("Checkpoint is 6 min older than the latest captured activity; "
                      "5 dispatches / 9 commits after it.", text)
        self.assertNotIn("slice 4", text)
        self.assertNotIn("f0002", text)

    def test_undated_commits_are_reported_and_not_counted(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        recorded = self.recorded_epoch()
        path = self.activity(recorded, commits=[("new1234", "feat: x", 60)], last_change=60)
        repo = json.loads(path.read_text(encoding="utf-8"))["repos"][0]
        for undated, line in ((1, " 1 commit has no time and is not counted."),
                              (2, " 2 commits have no time and are not counted.")):
            commits = repo["commits"] + [{"hash": "old%d" % i, "subject": "s"} for i in range(undated)]
            self.reseal(path, repos=[dict(repo, commits=commits)])
            with self.subTest(undated=undated):
                self.assertIn("0 dispatches / 1 commit after it." + line, self.start())

    def test_counts_use_the_singular_for_one(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        recorded = self.recorded_epoch()
        self.activity(recorded, agents=[("implementer", "build parser", 300)],
                      commits=[("new1234", "feat: parser", 200)], last_change=300)
        text = self.start()
        self.assertIn("1 dispatch / 1 commit after it.", text)
        self.assertIn(": 1 commit: new1234 feat: parser", text)

    def test_checkpoint_without_any_captured_activity_makes_no_freshness_claim(self):
        record(self.root, "s1", self.payload("small next"), None, "r")
        self.activity(self.recorded_epoch())
        text = self.start()
        self.assertIn("No activity captured after the checkpoint.", text)
        self.assertNotIn("not older than", text)

    def test_record_shrinks_to_digest_then_to_its_path(self):
        payload = self.payload_v1("keep next")
        payload["decisions"] = ["decision %d %s" % (i, "d" * 330) for i in range(12)]  # full JSON still <= 4500
        written = self.record_v1(self.root, "s1", payload, None, "r")
        self.seed_facts(files=300, repos=5, agents=40, name_chars=300)
        text = self.start()
        self.assertIn("Observed payload digest", text)
        self.assertIn("decision 11", text)
        self.assertNotIn("decision 8 ", text)
        self.assertIn("Session facts", text)
        payload["decisions"] = ["decision %d %s" % (i, "d" * 1400) for i in range(12)]
        self.record_v1(self.root, "s1", payload, written["revision"], "r2")
        self.seed_facts(files=300, repos=5, agents=40, name_chars=400)
        text = self.start()
        self.assertNotIn("Observed payload", text)
        self.assertIn("full checkpoint: ", text)
        self.assertIn("records", text.split("full checkpoint: ")[1])
        self.assertIn("Session facts", text)
        self.assertIn("dirty 2/7", text)

    def test_partial_save_status_is_kept_with_the_facts(self):
        self.enable_facts()
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
        for chars in (2600, 9000):
            with self.subTest(chars=chars), \
                    mock.patch.object(hooks, "PLUGIN", Path("C:/" + "p" * chars)), \
                    mock.patch.object(hooks, "_policy", return_value="POLICY " * 3000):
                text = self.start()
                if chars < 8000:
                    self.assertIn("status --workspace", text)
                    self.assertNotIn("Writer limits", text)
                    self.assertIn("Session: s1", text)

    def test_every_oversized_combination_stays_within_budget(self):
        expected = None
        for files, repos, agents, chars, decisions in ((0, 0, 0, 10, 0), (400, 5, 300, 400, 30),
                                                       (400, 0, 0, 900, 0), (0, 5, 300, 900, 30)):
            with self.subTest(files=files, repos=repos, chars=chars):
                payload = self.payload_v1("n" * 3000)
                payload["decisions"] = ["d" * 900] * decisions
                expected = self.record_v1(self.root, "s1", payload, expected, "r-%d-%d" % (files, chars))["revision"]
                self.seed_facts(files, repos, agents, chars)
                self.start("compact")
                self.start("resume")

    def test_session_start_creates_the_facts_cache_dir_and_marker(self):
        facts_dir = self.config_path.parent / "cache" / "facts"
        self.start("startup")
        self.assertTrue(facts_dir.is_dir())
        self.assertEqual([p.name for p in facts_dir.iterdir()], ["s1.enabled"])
        self.cfg["handoff"] = False
        self.config_path.write_text(json.dumps(self.cfg))
        (facts_dir / "s1.enabled").unlink()
        facts_dir.rmdir()
        self.start("startup")
        self.assertFalse(facts_dir.exists())

    def test_marker_needs_resolved_workspace_and_usable_session(self):
        facts_dir = self.config_path.parent / "cache" / "facts"
        self.cfg["workspace_roots"] = []
        self.config_path.write_text(json.dumps(self.cfg))
        handle_event(self.event("startup"), self.config_path)
        self.assertFalse(facts_dir.exists())
        self.cfg["workspace_roots"] = [str(self.root)]
        self.config_path.write_text(json.dumps(self.cfg))
        handle_event(self.event("startup", session="bad.id"), self.config_path)
        self.assertFalse(facts_dir.exists())  # the hook could never use such a session id

    def test_corrupt_checkpoint_keeps_the_facts_section(self):
        from provost_runtime.store import session_dir
        written = record(self.root, "s1", self.payload("small next"), None, "r")
        (session_dir(self.root, "s1") / "records" / (written["revision"] + ".json")).write_text("{broken")
        path = self.seed_facts()
        text = self.start()
        self.assertIn("Checkpoint unavailable", text)
        self.assertIn("Session facts", text)
        self.assertIn(str(path), text)

    def test_capture_anomalies_are_surfaced(self):
        from provost_runtime import facts
        from provost_runtime.config import handoff_home
        from provost_runtime.store import _seal, atomic_write
        record(self.root, "s1", self.payload("small next"), None, "r")

        def seed(events, errors, **sizes):
            path = self.seed_facts(**sizes)
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("checksum")
            atomic_write(handoff_home(), path, _seal(dict(data, events_captured=events, spool_errors=errors)))
            return path

        path = seed(12, 0)
        self.assertNotIn("Facts capture", self.start())
        info = facts.summary(json.loads(path.read_text(encoding="utf-8")), path)
        self.assertEqual((info["events_captured"], info["spool_errors"]), (12, 0))
        seed(12, 3)
        self.assertIn("Facts capture: 12 events, 3 unreadable spool lines", self.start())
        seed(0, 0)
        self.assertIn("Facts capture: 0 events — capture may be failing", self.start())
        seed(0, 2, files=0, repos=5, name_chars=1500)  # the summary shrinks to its path
        text = self.start()
        self.assertIn("Session facts omitted for size", text)
        self.assertIn("capture may be failing", text)


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
