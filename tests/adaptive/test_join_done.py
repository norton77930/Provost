"""Joining another session's work, concurrent sessions on one work, and done works, driven through the runtime CLI."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from _support import BASH, FixtureCase, REPO
from provost_runtime import store
from provost_runtime.config import handoff_home
from provost_runtime.hooks import bind

RUNTIME = REPO / "hooks" / "runtime"
LONG = "Keep the parser API stable because two callers pin its signature"  # >= 40 characters
ONE = "11111111-aaaa-4bbb-8ccc-000000000001"
TWO = "22222222-aaaa-4bbb-8ccc-000000000002"
THREE = "33333333-aaaa-4bbb-8ccc-000000000003"


class JoinAndDoneTests(FixtureCase):
    def setUp(self):
        super().setUp()
        for session in (ONE, TWO, THREE):
            bind(self.root, session, {})

    def env(self):
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        return env

    def command(self, *args, session=ONE):
        return [BASH, str(RUNTIME), *args, "--workspace", str(self.root), "--session", session]

    def runtime(self, *args, data=None, session=ONE):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>`."""
        result = subprocess.run(self.command(*args, session=session),
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=self.env(), timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def request(self, work="A", decisions=(), step="next step", **card):
        return dict(self.payload(step), work_id=work, new_decisions=list(decisions), new_evidence=[], **card)

    def record(self, data, expected=None, request_id="r1", session=ONE, join=False):
        """The record output, or the error output when the record was refused."""
        code, out = self.runtime("record", "--expected", expected or "null", "--request-id", request_id,
                                 *(("--join",) if join else ()), data=data, session=session)
        self.assertEqual(code == 0, "error" not in out, out)
        return out

    def total(self, work="A"):
        code, out = self.runtime("status", "--work", work)
        self.assertEqual(code, 0, out)
        return out["log"]["total"]

    def codes(self, out):
        return [warning["code"] for warning in out["warnings"]]

    def age(self, session, work, minutes):
        """Rewrite the session's head card of work as recorded `minutes` ago (resealed)."""
        head = store.current(self.root, session, work)
        data = {k: v for k, v in head.items() if k != "checksum"}
        data["recorded_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        path = store.session_dir(self.root, session) / "records" / (head["revision"] + ".json")
        store.atomic_write(handoff_home(), path, store._seal(data))

    # --- join guard -----------------------------------------------------------------------

    def test_a_first_write_to_work_logged_by_another_session_needs_join(self):
        self.record(self.request(decisions=[{"text": LONG}]))
        for data in (self.request(decisions=[{"text": LONG + " too"}]), self.request()):
            with self.subTest(entries=bool(data["new_decisions"])):
                out = self.record(data, session=TWO)
                self.assertEqual(out["error"], "JOIN_REQUIRED")
                self.assertIn("--join", out["detail"])
                self.assertIn(ONE[:8], out["detail"])
        self.assertEqual(self.total(), 1)
        code, status = self.runtime("status", "--work", "A", session=TWO)
        self.assertIsNone(status["checkpoint"])

    def test_join_admits_the_first_write_and_later_writes_need_no_flag(self):
        self.record(self.request(decisions=[{"text": LONG}]))
        joined = self.record(self.request(decisions=[{"text": LONG + " too"}]), session=TWO, join=True)
        self.assertEqual(joined["log_entries"], ["D2"])
        later = self.record(self.request(decisions=[{"text": LONG + " third"}]), joined["revision"], "r2",
                            session=TWO)
        self.assertEqual(later["log_entries"], ["D3"])
        self.assertEqual(self.total(), 3)

    def test_an_own_earlier_card_counts_as_joined(self):
        own = self.record(self.request())  # a card with no entries: nothing to join yet
        self.record(self.request(decisions=[{"text": LONG}]), session=TWO)  # first to log
        out = self.record(self.request(decisions=[{"text": LONG + " too"}]), own["revision"], "r2")
        self.assertEqual(out["log_entries"], ["D2"])

    def test_an_adopted_card_counts_as_joined(self):
        self.record(self.request(decisions=[{"text": LONG}]))
        code, taken = self.runtime("snapshot", "--request-id", "e1")
        self.assertEqual(code, 0, taken)
        code, adopted = self.runtime("resume", "--from", taken["snapshot_id"], session=TWO)
        self.assertEqual(code, 0, adopted)
        out = self.record(self.request(decisions=[{"text": LONG + " too"}]), adopted["revision"], "r2", session=TWO)
        self.assertEqual(out["log_entries"], ["D2"])

    def test_entries_of_the_same_session_only_need_no_join(self):
        first = self.record(self.request(decisions=[{"text": LONG}]))
        out = self.record(self.request("B", decisions=[{"text": LONG}]), request_id="b1")
        self.assertEqual(out["log_entries"], ["D1"])
        self.assertEqual(self.record(self.request(decisions=[{"text": LONG + " too"}]), first["revision"],
                                     "r2")["log_entries"], ["D2"])

    def test_only_an_unjoined_record_without_entries_reads_the_log(self):
        first = self.record(self.request(decisions=[{"text": LONG}]))
        path, _ = store._log_paths(store.as_workspace(self.root), "A")
        with path.open("ab") as stream:
            stream.write(b"not json\n")
        self.assertNotIn("error", self.record(self.request(), first["revision"], "r2"))
        self.assertEqual(self.record(self.request(), session=TWO)["error"], "CORRUPT_LOG")
        self.assertNotIn("error", self.record(self.request(), session=TWO, join=True))

    def test_an_exact_replay_of_a_joined_first_write_needs_no_join(self):
        self.record(self.request(decisions=[{"text": LONG}]))
        data = self.request(decisions=[{"text": LONG + " too"}])
        joined = self.record(data, session=TWO, join=True)
        replay = self.record(data, session=TWO)
        self.assertEqual((replay["revision"], replay["log_entries"]), (joined["revision"], ["D2"]))
        self.assertEqual(self.total(), 2)

    def test_three_sessions_racing_to_log_a_new_work_admit_only_one(self):
        sessions = [ONE, TWO, THREE]
        procs = [subprocess.Popen(self.command("record", "--expected", "null", "--request-id", "r1", session=session),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=self.env())
                 for session in sessions]
        for proc, session in zip(procs, sessions):
            proc.stdin.write(json.dumps(self.request(decisions=[{"text": "%s by %s" % (LONG, session)}])))
            proc.stdin.close()
        outputs = []
        for proc in procs:
            outputs.append(json.loads(proc.stdout.read()))
            proc.wait(timeout=60)
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(sorted(out.get("error", "ok") for out in outputs),
                         ["JOIN_REQUIRED", "JOIN_REQUIRED", "ok"], outputs)
        self.assertEqual(self.total(), 1)

    # --- concurrent sessions --------------------------------------------------------------

    def test_another_sessions_card_within_two_hours_warns(self):
        self.record(self.request())  # a card only: the log holds nothing of ONE
        self.age(ONE, "A", 90)
        out = self.record(self.request(), session=TWO)
        self.assertEqual(self.codes(out), ["CONCURRENT_SESSION"])
        message = out["warnings"][0]["message"]
        self.assertIn(ONE[:8], message)
        self.assertIn("90 min", message)
        self.assertNotIn(ONE, message)
        # Own cards never warn; another session's card older than 2 hours neither.
        self.age(ONE, "A", 121)
        self.assertEqual(self.codes(self.record(self.request(), out["revision"], "r2", session=TWO)), [])

    def test_a_card_of_another_work_does_not_warn(self):
        self.record(self.request("B"))
        self.assertEqual(self.codes(self.record(self.request(), session=TWO)), [])

    # --- done -----------------------------------------------------------------------------

    def test_done_closes_a_work_and_a_later_write_reopens_it_with_a_warning(self):
        first = self.record(self.request(decisions=[{"text": LONG}]))
        done = self.record(self.request(phase="done"), first["revision"], "r2")
        self.assertEqual(self.codes(done), [])
        again = self.record(self.request(phase="done", step="still done"), done["revision"], "r3")
        self.assertEqual(self.codes(again), [])
        reopened = self.record(self.request(phase="review"), again["revision"], "r4")
        self.assertEqual(self.codes(reopened), ["REOPENED"])
        self.assertIn("done", reopened["warnings"][0]["message"])
        self.assertEqual(self.codes(self.record(self.request(), reopened["revision"], "r5")), [])

    def test_done_ignores_case_and_surrounding_space(self):
        first = self.record(self.request(phase="Done"))
        self.assertFalse(store.work_states(self.root)["works"]["A"]["open"])
        reopened = self.record(self.request(), first["revision"], "r2")
        self.assertEqual(self.codes(reopened), ["REOPENED"])
        closed = self.record(self.request(phase=" done "), reopened["revision"], "r3")
        self.assertEqual(self.codes(closed), [])
        self.assertEqual(self.codes(self.record(self.request(phase="DONE"), closed["revision"], "r4")), [])
        self.assertFalse(store.work_states(self.root)["works"]["A"]["open"])

    def test_a_closed_work_is_not_listed_among_other_open_works_on_compaction(self):
        from provost_runtime.hooks import handle_event
        self.record(self.request("work-closed", phase=" Done "), request_id="c1")
        self.record(self.request("work-alpha"), request_id="a1")
        self.record(self.request("work-beta"), request_id="b1")
        output = handle_event({"session_id": ONE, "cwd": str(self.root), "hook_event_name": "SessionStart",
                               "source": "compact"}, self.config_path)
        text = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("work-alpha", text)
        self.assertNotIn("work-closed", text)

    def test_a_work_done_by_another_session_reopens_on_join(self):
        self.record(self.request(decisions=[{"text": LONG}], phase="done"))
        self.age(ONE, "A", 180)
        out = self.record(self.request(), session=TWO, join=True)
        self.assertEqual(self.codes(out), ["REOPENED"])

    # --- work_states ----------------------------------------------------------------------

    def test_work_states_reports_the_newest_card_per_work_and_lists_a_corrupt_chain(self):
        a1 = self.record(self.request())
        self.age(ONE, "A", 30)
        self.record(self.request("B", phase="done"), request_id="b1")
        a2 = self.record(self.request(step="by two"), session=TWO)
        self.record(self.request("C"), session=THREE)
        broken = store.current(self.root, THREE, "C")
        path = store.session_dir(self.root, THREE) / "records" / (broken["revision"] + ".json")
        path.write_text(path.read_text(encoding="utf-8").replace("next step", "edited"), encoding="utf-8")
        states = store.work_states(self.root)
        self.assertEqual(sorted(states["works"]), ["A", "B"])
        a = states["works"]["A"]
        self.assertEqual((a["session"], a["revision"], a["phase"], a["open"]),
                         (TWO, a2["revision"], "implementation", True))
        self.assertEqual([(item["session"], item["revision"]) for item in a["sessions"]],
                         [(TWO, a2["revision"]), (ONE, a1["revision"])])
        self.assertEqual((states["works"]["B"]["session"], states["works"]["B"]["open"]), (ONE, False))
        self.assertEqual(states["skipped"], [])
        self.assertEqual(states["broken"], [{"session": THREE, "work_id": "C", "error": "CORRUPT_RECORD"}])

    def test_work_states_reports_an_unparseable_record_as_corrupt(self):
        self.record(self.request(), session=THREE)
        head = store.current(self.root, THREE, "A")
        (store.session_dir(self.root, THREE) / "records" / (head["revision"] + ".json")).write_text(
            "not json", encoding="utf-8")
        self.assertEqual(store.work_states(self.root)["skipped"],
                         [{"session_dir": store.digest(THREE), "error": "CORRUPT_RECORD"}])

    def test_work_states_is_empty_without_sessions(self):
        self.assertEqual(store.work_states(self.root), {"works": {}, "skipped": [], "broken": []})
