"""SessionStart compact/resume injection of state cards, open evidence and decisions (#22)."""
import json
from _support import FixtureCase
from provost_runtime import store
from provost_runtime.hooks import handle_event


class RecoveryInjection(FixtureCase):
    def start(self, source="compact"):
        output = handle_event({"session_id": "s1", "cwd": str(self.root), "hook_event_name": "SessionStart",
                               "source": source}, self.config_path)
        text = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("unavailable", output.get("systemMessage", ""))
        self.assertLessEqual(len(text), 8000)
        return text

    def write(self, work, next_step="next step", phase="implementation", decisions=(), evidence=()):
        """Record a card of `work` on top of its chain in s1; returns the stored card."""
        previous = store.current(self.root, "s1", work)
        request = dict(self.payload(next_step), work_id=work, phase=phase,
                       new_decisions=list(decisions), new_evidence=list(evidence))
        self.requests = getattr(self, "requests", 0) + 1
        return store.record(self.root, "s1", request, previous["revision"] if previous else None,
                            "r%d" % self.requests)

    def test_current_card_in_full_and_each_other_open_work_as_one_line(self):
        self.write("work-done", "done next DONE-MARK", phase="done")
        self.write("work-alpha", "alpha next " + "a" * 400)
        self.write("work-beta", "beta next BETA-MARK")
        text = self.start()
        beta = store.current(self.root, "s1")["payload"]
        self.assertIn("Observed payload: " + json.dumps(beta, ensure_ascii=False), text)
        lines = [line for line in text.splitlines() if "work-alpha" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("implementation", lines[0])
        self.assertIn("alpha next aaa", lines[0])
        self.assertLess(len(lines[0]), 300)  # next is truncated
        self.assertNotIn("a" * 400, text)
        self.assertNotIn("work-done", text)
        self.assertNotIn("DONE-MARK", text)

    def test_newest_fifteen_non_superseded_decisions_of_the_current_work(self):
        self.write("work-alpha", decisions=[{"text": "ALPHA-DECISION not of the current work"}])
        self.write("work-beta", decisions=[{"text": "decision-%02d made" % i} for i in range(1, 21)])
        self.write("work-beta", "later", decisions=[{"text": "decision-21 replaces 20", "supersedes": "D20"}])
        text = self.start("resume")
        self.assertIn("decision-21 replaces 20", text)
        self.assertNotIn("decision-20 made", text)
        for shown in range(6, 20):
            self.assertIn("decision-%02d made" % shown, text)
        self.assertNotIn("decision-05 made", text)
        self.assertNotIn("ALPHA-DECISION", text)
        self.assertLess(text.index("decision-06 made"), text.index("decision-19 made"))  # oldest first

    def test_decisions_fill_the_remaining_budget_newest_first(self):
        self.seed_facts(files=30, name_chars=60)
        self.write("work-beta", "beta next", decisions=[{"text": "LONG-%02d %s" % (i, "x" * 990)} for i in range(15)])
        text = self.start()
        self.assertIn("Observed payload: ", text)
        self.assertIn("LONG-14", text)
        self.assertNotIn("LONG-00", text)
        shown = sum("LONG-%02d" % i in text for i in range(15))
        self.assertGreater(shown, 2)
        self.assertIn("newest %d of 15" % shown, text)
        self.assertIn("Session facts", text)

    def test_open_evidence_is_the_latest_state_per_claim_with_a_passed_count(self):
        self.write("work-beta", evidence=[{"claim": "CLAIM-A", "state": "unverified", "ref": "r"},
                                          {"claim": "CLAIM-B", "state": "pass-observed", "ref": "r"},
                                          {"claim": "CLAIM-C", "state": "fail", "ref": "r"}])
        self.write("work-beta", "later", evidence=[{"claim": "CLAIM-A now", "state": "pass-observed", "ref": "t",
                                                    "updates": "E1"},
                                                   {"claim": "CLAIM-C again", "state": "stale", "ref": "t",
                                                    "updates": "E3"},
                                                   {"claim": "CLAIM-D", "state": "unverified", "ref": "t"}])
        text = self.start()
        self.assertIn("CLAIM-C again", text)
        self.assertIn("stale", text)
        self.assertIn("CLAIM-D", text)
        for closed in ("CLAIM-A", "CLAIM-B", "CLAIM-C:", "CLAIM-C (", "fail"):
            self.assertNotIn(closed, text)
        self.assertIn("Passed claims: 2", text)

    def test_schema_1_card_is_injected_and_labelled_not_migrated(self):
        payload = dict(self.payload_v1("legacy next", decisions=["LEGACY-DECISION kept"]), work_id="work-old")
        old = self.record_v1(self.root, "s1", payload, None, "old")
        text = self.start()
        self.assertIn("not migrated", text)
        self.assertIn("LEGACY-DECISION kept", text)
        # A schema 2 card without a log falls back to the newest schema 1 card of its chain.
        store.record(self.root, "s1", dict(self.payload("v2 next"), work_id="work-old"), old["revision"], "v2")
        text = self.start()
        self.assertIn("v2 next", text)
        self.assertIn("not migrated", text)
        self.assertIn("LEGACY-DECISION kept", text)

    def test_a_corrupt_log_does_not_break_injection(self):
        self.write("work-beta", "beta next KEEP", decisions=[{"text": "logged decision"}])
        path = store._log_paths(store.as_workspace(self.root), "work-beta")[0]
        with path.open("ab") as stream:
            stream.write(b"not json\n")
        self.seed_facts()
        text = self.start()
        self.assertIn("beta next KEEP", text)
        self.assertIn("could not be read", text)
        self.assertIn("CORRUPT_LOG", text)
        self.assertIn("Session facts", text)
        self.assertNotIn("Checkpoint unavailable", text)

    def append_raw(self, work, *entries):
        """Hand-written evidence lines (no target checks), as a damaged or imported log may hold."""
        path = store._log_paths(store.as_workspace(self.root), work)[0]
        with path.open("ab") as stream:
            for entry in entries:
                stream.write(json.dumps(dict({"kind": "evidence", "at": "2026-10-14T00:00:00+00:00",
                                              "session": "x", "ref": "r", "state": "fail"}, **entry)).encode() + b"\n")

    def test_odd_evidence_links_do_not_break_injection(self):
        self.write("work-beta", "beta next KEEP", evidence=[{"claim": "CLAIM-BASE", "state": "fail", "ref": "r"}])
        self.append_raw("work-beta", {"id": "E2", "claim": "CYCLE-A", "updates": "E3"},
                        {"id": "E3", "claim": "CYCLE-B", "updates": "E2"},
                        {"id": "E4", "claim": "DANGLING", "updates": "E99"},
                        {"id": "E5", "claim": "CROSS-KIND", "updates": "D1"})
        text = self.start()
        self.assertIn("beta next KEEP", text)
        for head in ("CLAIM-BASE", "DANGLING", "CROSS-KIND"):  # nothing updates them: each is a chain head
            self.assertIn(head, text)
        for cycled in ("CYCLE-A", "CYCLE-B"):  # each is updated by the other: neither is a head
            self.assertNotIn(cycled, text)

    def test_many_other_works_degrade_to_a_count(self):
        for i in range(60):
            self.write("work-%02d" % i, "next %02d %s" % (i, "n" * 300))
        text = self.start()
        self.assertIn("Other open works in this session: 59, omitted for size", text)
        self.assertNotIn("work-00 [", text)

    def test_a_work_without_log_or_old_card_says_no_entries_yet(self):
        self.write("work-beta", "beta next")
        self.assertIn("Decision log of work work-beta: no entries yet.", self.start())

    def test_fallback_ignores_old_cards_of_other_works_in_a_mixed_chain(self):
        from unittest import mock
        legacy = dict(self.payload_v1("legacy next", decisions=["OTHER-WORK-DECISION"]), work_id="work-legacy")
        old = self.record_v1(self.root, "s1", legacy, None, "old")
        # A chain written before per-work cards: the next card, of another work, names the old card as parent.
        with mock.patch.object(store, "current", return_value=old):
            store.record(self.root, "s1", dict(self.payload("new next"), work_id="work-new"), old["revision"], "new")
        text = self.start()
        self.assertIn("new next", text)
        self.assertNotIn("OTHER-WORK-DECISION", text)
        self.assertIn("Decision log of work work-new: no entries yet.", text)

    def test_snapshot_only_checkpoint_injects_its_work_log(self):
        self.write("work-beta", "snap next", decisions=[{"text": "SNAP-DECISION logged"}])
        store.snapshot(self.root, "s1", "e1")
        directory = store.session_dir(store.as_workspace(self.root), "s1")
        for path in list((directory / "records").glob("*.json")) + [directory / "current.json"]:
            path.unlink()
        text = self.start("resume")
        self.assertIn("snapshots", text)
        self.assertIn("snap next", text)
        self.assertIn("SNAP-DECISION logged", text)

    def test_secrets_are_redacted_and_long_items_are_capped(self):
        token = "ghp_" + "A" * 36
        self.write("work-alpha", "alpha next " + token)
        self.write("work-beta", "beta next " + token, decisions=[{"text": "use token " + token},
                                                               {"text": "HUGE " + "h" * 990},
                                                               {"text": "SMALL decision kept"}],
                   evidence=[{"claim": "claim " + token, "state": "fail", "ref": token}])
        text = self.start()
        self.assertNotIn(token, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("SMALL decision kept", text)
        huge = next(line for line in text.splitlines() if "HUGE" in line)
        self.assertLessEqual(len(huge), 400)
        self.assertTrue(huge.endswith("…"))

    def test_long_work_ids_are_clipped_in_titles(self):
        work = "w" * 300
        self.write(work, "long id next", decisions=[{"text": "LONG-ID decision"}])
        text = self.start()
        self.assertIn("LONG-ID decision", text)
        self.assertIn("Decisions of work " + "w" * 79 + "… (", text)
