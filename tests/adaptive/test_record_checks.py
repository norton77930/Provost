"""Record checks (hard rejections, soft warnings) and the record echo, driven through the runtime CLI."""
import json
import os
import subprocess
import time
from datetime import datetime
from _support import BASH, FixtureCase, REPO
from provost_runtime.hooks import bind

RUNTIME = REPO / "hooks" / "runtime"
LONG = "Keep the parser API stable because two callers pin its signature"  # >= 40 characters


class RecordChecksTests(FixtureCase):
    def setUp(self):
        super().setUp()
        bind(self.root, "s1", {})

    def runtime(self, *args, data=None, session="s1"):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>`."""
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        result = subprocess.run([BASH, str(RUNTIME), *args, "--workspace", str(self.root), "--session", session],
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def request(self, work="A", decisions=(), evidence=(), step="next step", **card):
        return dict(self.payload(step), work_id=work, new_decisions=list(decisions), new_evidence=list(evidence),
                    **card)

    def record(self, data, expected=None, request_id="r1"):
        """The record output, or the error output when the record was refused."""
        code, out = self.runtime("record", "--expected", expected or "null", "--request-id", request_id, data=data)
        self.assertEqual(code == 0, "error" not in out, out)
        return out

    def log(self, work="A"):
        code, out = self.runtime("status", "--work", work)
        self.assertEqual(code, 0, out)
        return out["log"]

    def codes(self, out):
        return [warning["code"] for warning in out["warnings"]]

    def assertRefused(self, out, code, *, work="A", total=0, detail=()):
        self.assertEqual(out["error"], code)
        for text in detail:
            self.assertIn(text, out["detail"])
        self.assertEqual(self.log(work)["total"], total)

    # --- hard rejections -------------------------------------------------------------------

    def test_empty_decision_text_is_refused(self):
        for text in ("", "   \n"):
            with self.subTest(text=text):
                out = self.record(self.request(decisions=[{"text": LONG}, {"text": text}]))
                self.assertRefused(out, "DECISION_TEXT_EMPTY", detail=("new_decisions[1]",))
        code, status = self.runtime("status")
        self.assertIsNone(status["checkpoint"])

    def test_decision_text_over_1000_characters_is_refused(self):
        out = self.record(self.request(decisions=[{"text": "x" * 1001}]))
        self.assertRefused(out, "DECISION_TEXT_TOO_LONG", detail=("1001", "1000"))
        self.assertEqual(self.record(self.request(decisions=[{"text": "x" * 1000}]))["log_entries"], ["D1"])

    def seeded(self):
        """Work A with D1 and E1; returns the record output."""
        return self.record(self.request(decisions=[{"text": LONG}],
                                        evidence=[{"claim": "parser tests pass", "state": "fail", "ref": "t.py"}]))

    def test_a_missing_target_is_refused(self):
        first = self.seeded()
        for item, field in (({"text": LONG, "supersedes": "D9"}, "new_decisions"),
                            ({"claim": "parser tests pass", "state": "fail", "ref": "t", "updates": "E9"},
                             "new_evidence"),
                            ({"text": LONG, "supersedes": "latest"}, "new_decisions")):
            with self.subTest(item=item):
                out = self.record(self.request(**{("decisions" if field == "new_decisions" else "evidence"): [item]}),
                                  first["revision"], "r2")
                self.assertRefused(out, "TARGET_NOT_FOUND", total=2)

    def test_a_target_in_another_work_is_refused(self):
        self.record(self.request("B", decisions=[{"text": LONG}]), request_id="b1")
        first = self.seeded()
        out = self.record(self.request(decisions=[{"text": LONG + " again", "supersedes": "B:D1"}]),
                          first["revision"], "r2")
        self.assertRefused(out, "TARGET_OTHER_WORK", total=2, detail=("B",))
        self.assertEqual(self.log("B")["total"], 1)

    def test_a_target_of_the_wrong_kind_is_refused(self):
        first = self.seeded()
        for kind, item in (("decisions", {"text": LONG + " again", "supersedes": "E1"}),
                           ("evidence", {"claim": "c", "state": "fail", "ref": "t", "updates": "D1"})):
            with self.subTest(kind=kind):
                out = self.record(self.request(**{kind: [item]}), first["revision"], "r2")
                self.assertRefused(out, "TARGET_WRONG_KIND", total=2)

    def test_superseding_an_already_superseded_entry_names_the_latest_of_its_chain(self):
        first = self.seeded()
        second = self.record(self.request(decisions=[{"text": LONG + " (second)", "supersedes": "D1"}]),
                             first["revision"], "r2")
        third = self.record(self.request(decisions=[{"text": LONG + " (third)", "supersedes": "D2"}]),
                            second["revision"], "r3")
        out = self.record(self.request(decisions=[{"text": LONG + " (fourth)", "supersedes": "D1"}]),
                          third["revision"], "r4")
        self.assertRefused(out, "ALREADY_SUPERSEDED", total=4, detail=("D1", "D3"))
        # Two entries of one request superseding the same target are refused too.
        out = self.record(self.request(decisions=[{"text": LONG + " (x)", "supersedes": "D3"},
                                                  {"text": LONG + " (y)", "supersedes": "D3"}]),
                          third["revision"], "r5")
        self.assertRefused(out, "ALREADY_SUPERSEDED", total=4)

    # --- soft warnings ---------------------------------------------------------------------

    def test_a_short_decision_is_written_with_a_warning(self):
        out = self.record(self.request(decisions=[{"text": "use lexer"}, {"text": LONG}]))
        self.assertEqual(out["log_entries"], ["D1", "D2"])
        self.assertEqual(self.codes(out), ["SHORT_DECISION"])
        self.assertIn("D1", out["warnings"][0]["message"])

    def test_an_exact_duplicate_of_a_current_decision_is_skipped(self):
        first = self.seeded()
        out = self.record(self.request(decisions=[{"text": LONG}, {"text": LONG + " too"}]), first["revision"], "r2")
        self.assertEqual(out["log_entries"], ["D2"])
        self.assertEqual(self.codes(out), ["DUPLICATE_SKIPPED"])
        self.assertIn("D1", out["warnings"][0]["message"])
        self.assertEqual(self.log()["total"], 3)
        # The text of a superseded decision may be logged again.
        third = self.record(self.request(decisions=[{"text": LONG + " now", "supersedes": "D1"}]),
                            out["revision"], "r3")
        again = self.record(self.request(decisions=[{"text": LONG}]), third["revision"], "r4")
        self.assertEqual((again["log_entries"], self.codes(again)), (["D4"], []))

    def activity(self, agents=(), commits=(), files=()):
        """facts.json for s1 with dispatches, commits and edited files at the given epoch seconds."""
        path = self.seed_facts(files=0, repos=1, agents=0)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("checksum")
        data["agents"] = [{"role": role, "desc": desc, "bg": False, "ts": ts} for role, desc, ts in agents]
        data["repos"][0]["commits"] = [{"hash": h, "subject": "s", "ts": ts} for h, ts in commits]
        data["files"] = [{"path": "C:/repo/%s" % name, "count": 1, "last_ts": ts, "agents": [""]}
                         for name, ts in files]
        from provost_runtime.config import handoff_home
        from provost_runtime.store import _seal, atomic_write
        atomic_write(handoff_home(), path, _seal(data))

    @staticmethod
    def later(out, seconds=600):
        """An epoch second `seconds` after the record: still after every record a test writes next."""
        return int(datetime.fromisoformat(out["recorded_at"]).timestamp()) + seconds

    def test_commits_since_the_last_record_must_be_referenced_by_a_hash_prefix(self):
        first = self.seeded()
        self.activity(commits=[("1a2b3c4d5e", self.later(first)), ("99887766aa", self.later(first)),
                               ("0ld0ld0ld", self.later(first, -600))])
        out = self.record(self.request(decisions=[{"text": LONG + " for 1a2b3c4"}]), first["revision"], "r2")
        self.assertEqual(self.codes(out), ["UNREFERENCED_COMMITS"])
        message = out["warnings"][0]["message"]
        self.assertIn("99887766aa", message)
        self.assertNotIn("1a2b3c4d5e", message)
        self.assertNotIn("0ld0ld0ld", message)  # before the last record
        # A 6-character prefix does not count.
        out = self.record(self.request(decisions=[{"text": LONG + " for 1a2b3c4 again", "refs": ["998877"]}]),
                          out["revision"], "r3")
        self.assertEqual(self.codes(out), ["UNREFERENCED_COMMITS"])
        self.assertIn("99887766aa", out["warnings"][0]["message"])
        # A 7+ character prefix in an entry's refs, text or evidence ref clears it (case-insensitive).
        out = self.record(self.request(evidence=[{"claim": "build", "state": "fail", "ref": "9988776"}],
                                       decisions=[{"text": LONG + " fixes 1A2B3C4D"}]), out["revision"], "r4")
        self.assertEqual(self.codes(out), [])

    def test_commits_count_from_the_last_record_of_any_work(self):
        a = self.seeded()
        time.sleep(1.1)
        b = self.record(self.request("B", decisions=[{"text": LONG}]), request_id="b1")
        between = self.later(b, 0)  # after A's record, not after B's
        self.assertGreater(between, datetime.fromisoformat(a["recorded_at"]).timestamp())
        self.activity(commits=[("abcdef1234", between)])
        out = self.record(self.request(decisions=[{"text": LONG + " again"}]), a["revision"], "r2")
        self.assertEqual(self.codes(out), [])

    def test_dispatches_since_the_last_record_must_be_mentioned(self):
        first = self.seeded()
        self.activity(agents=[("implementer-sonnet", "build parser", self.later(first)),
                              ("code-reviewer", "review lexer", self.later(first))])
        out = self.record(self.request(decisions=[{"text": LONG + " after review lexer"}]), first["revision"], "r2")
        self.assertEqual(self.codes(out), ["UNMENTIONED_DISPATCHES"])
        self.assertIn("implementer-sonnet", out["warnings"][0]["message"])
        self.assertNotIn("code-reviewer", out["warnings"][0]["message"])
        # A card assignment naming the role mentions it.
        assignment = {"task": "T1", "agent_id": "implementer-sonnet", "observed_state": "done", "write_scope": []}
        out = self.record(self.request(decisions=[{"text": LONG + " after review lexer again"}],
                                       assignments=[assignment]), out["revision"], "r3")
        self.assertEqual(self.codes(out), [])

    def test_captured_activity_without_a_new_decision_warns(self):
        first = self.seeded()
        self.activity(files=[("parser.py", self.later(first))])
        out = self.record(self.request(), first["revision"], "r2")
        self.assertEqual(self.codes(out), ["NO_NEW_DECISION"])
        out = self.record(self.request(decisions=[{"text": LONG + " again"}]), out["revision"], "r3")
        self.assertEqual(self.codes(out), [])
        # A skipped duplicate is not a new decision.
        out = self.record(self.request(decisions=[{"text": LONG}]), out["revision"], "r4")
        self.assertEqual(self.codes(out), ["DUPLICATE_SKIPPED", "NO_NEW_DECISION"])

    def test_no_activity_and_no_decision_is_quiet(self):
        first = self.seeded()
        self.assertEqual(self.codes(self.record(self.request(), first["revision"], "r2")), [])

    def test_a_log_over_300_entries_warns(self):
        decisions = [{"text": "%s %03d" % (LONG, i)} for i in range(300)]
        out = self.record(self.request(decisions=decisions))
        self.assertEqual(self.codes(out), [])
        out = self.record(self.request(decisions=[{"text": LONG + " one more"}]), out["revision"], "r2")
        self.assertEqual(self.codes(out), ["LOG_LARGE"])
        self.assertIn("301", out["warnings"][0]["message"])

    # --- echo -----------------------------------------------------------------------------

    def test_echo_shows_ids_card_open_evidence_newest_decisions_and_warnings(self):
        first = self.record(self.request(decisions=[{"text": "%s %d" % (LONG, i)} for i in range(4)],
                                         evidence=[{"claim": "parser tests pass", "state": "fail", "ref": "t.py"},
                                                   {"claim": "lexer tests pass", "state": "fail", "ref": "l.py"}]))
        out = self.record(self.request(decisions=[{"text": "short one"}, {"text": LONG + " 9", "supersedes": "D4"}],
                                       evidence=[{"claim": "parser tests pass", "state": "pass-observed",
                                                  "ref": "t.py", "updates": "E1"}],
                                       step="Review T1 result", phase="review"),
                          first["revision"], "r2")
        echo = out["echo"]
        self.assertIn("D5, D6, E3", echo)
        self.assertIn("phase: review", echo)
        self.assertIn("next: Review T1 result", echo)
        self.assertIn("E2 [fail] lexer tests pass", echo)
        self.assertNotIn("parser tests pass", echo)  # its chain ends pass-observed
        for shown in ("D6 ", "D5 ", "D3 "):
            self.assertIn(shown, echo)
        self.assertNotIn("D4 ", echo)  # superseded
        self.assertNotIn("D2 ", echo)  # not among the 3 newest
        self.assertIn("SHORT_DECISION", echo)
        self.assertLessEqual(len(echo), 1500)

    def test_echo_is_capped_trimming_decisions_then_evidence_then_warnings(self):
        long_text = "y" * 900
        evidence = [{"claim": "claim %02d %s" % (i, "z" * 120), "state": "fail", "ref": "r"} for i in range(12)]
        out = self.record(self.request(decisions=[{"text": long_text + str(i)} for i in range(3)] + [{"text": "s"}],
                                       evidence=evidence))
        echo = out["echo"]
        self.assertLessEqual(len(echo), 1500)
        self.assertIn("next: next step", echo)
        self.assertIn("SHORT_DECISION", echo)  # warnings kept
        self.assertIn("Recent decisions: 3 more, see status", echo)
        self.assertRegex(echo, r"Open evidence:(.|\n)*\d+ more, see status")
        # Only decisions trimmed when that suffices.
        out = self.record(self.request("B", decisions=[{"text": long_text + str(i)} for i in range(3)],
                                       evidence=evidence[:2], step="n" * 700), request_id="b1")
        self.assertLessEqual(len(out["echo"]), 1500)
        self.assertIn("claim 01", out["echo"])
        self.assertIn("more, see status", out["echo"])

    def test_echo_always_keeps_the_card_within_the_cap(self):
        many = [{"text": "short %d" % i} for i in range(60)]
        out = self.record(self.request(step="n" * 5000, decisions=many))
        echo = out["echo"]
        self.assertLessEqual(len(echo), 1500)
        self.assertIn("next: nnnn", echo)
        self.assertIn("more, see status", echo)

    def test_record_output_keeps_its_fields_and_adds_warnings_and_echo(self):
        out = self.seeded()
        for field in ("schema_version", "kind", "revision", "payload", "log_entries", "recorded_at"):
            self.assertIn(field, out)
        self.assertEqual(out["warnings"], [])
        self.assertIsInstance(out["echo"], str)
