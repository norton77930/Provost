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
