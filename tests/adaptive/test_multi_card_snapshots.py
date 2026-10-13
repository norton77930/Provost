"""Multi-card snapshots and resume selection (#23), driven through the runtime CLI and the hooks."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from _support import BASH, FixtureCase, REPO
from provost_runtime import store
from provost_runtime.config import handoff_home
from provost_runtime.hooks import bind, handle_event

RUNTIME = REPO / "hooks" / "runtime"


class MultiCardSnapshotTests(FixtureCase):
    def setUp(self):
        super().setUp()
        for session in ("s1", "s2", "s3", "s4"):
            bind(self.root, session, {})
        self.requests = 0

    def runtime(self, *args, session="s1", data=None):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>`."""
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        tail = ["--workspace", str(self.root)] + (["--session", session] if session else [])
        result = subprocess.run([BASH, str(RUNTIME), *args, *tail],
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def write(self, work, step="next", phase="implementation", session="s1", decisions=(), evidence=(),
              assignments=()):
        previous = store.current(self.root, session, work)
        self.requests += 1
        request = dict(self.payload(step), work_id=work, phase=phase, assignments=list(assignments),
                       new_decisions=list(decisions), new_evidence=list(evidence))
        return store.record(self.root, session, request, previous["revision"] if previous else None,
                            "r%d" % self.requests)

    def snapshot(self, request_id="e1", session="s1"):
        code, taken = self.runtime("snapshot", "--request-id", request_id, session=session)
        self.assertEqual(code, 0, taken)
        return taken

    def listing(self, snapshot_id):
        code, items = self.runtime("list", session=None)
        self.assertEqual(code, 0, items)
        return next(item for item in items if item["snapshot_id"] == snapshot_id)

    def three_open_one_done(self):
        running = {"task": "T1", "agent_id": "worker-1", "observed_state": "running", "write_scope": ["src/a"]}
        self.write("A", "a next", assignments=[running])
        self.write("B", "b next")
        self.write("D", "d next", phase="done")
        self.write("C", "c next")
        return self.snapshot()

    def test_a_snapshot_holds_every_open_card_and_list_shows_its_works(self):
        taken = self.three_open_one_done()
        self.assertEqual((taken["snapshot_version"], taken["payload"]["work_id"]), (2, "C"))  # the current card
        self.assertEqual([card["payload"]["work_id"] for card in taken["other_cards"]], ["B", "A"])
        self.assertEqual(self.listing(taken["snapshot_id"])["works"],
                         [{"work_id": "C", "phase": "implementation"}, {"work_id": "B", "phase": "implementation"},
                          {"work_id": "A", "phase": "implementation"}])
        self.assertEqual(self.snapshot(), taken)  # idempotent per request id

    def test_a_done_current_card_is_not_held_unless_nothing_is_open(self):
        self.write("A", "a next")
        self.write("D", "d next", phase="done")
        taken = self.snapshot()
        self.assertEqual(([taken["payload"]["work_id"]], taken["other_cards"]), (["A"], []))
        self.write("A", "a done", phase="done")
        only = self.snapshot("e2")
        self.assertEqual((only["payload"]["work_id"], only["other_cards"], only["parent_snapshot"]),
                         ("A", [], taken["snapshot_id"]))

    def test_precompact_snapshots_every_open_card(self):
        self.write("A", "a next")
        self.write("B", "b next")
        handle_event({"hook_event_name": "PreCompact", "trigger": "auto", "session_id": "s1",
                      "cwd": str(self.root)}, self.config_path)
        snap = store.latest(self.root, "s1")
        self.assertEqual([snap["payload"]["work_id"]] + [c["payload"]["work_id"] for c in snap["other_cards"]],
                         ["B", "A"])

    def test_resume_without_a_work_lists_the_choices_and_adopts_nothing(self):
        taken = self.three_open_one_done()
        code, out = self.runtime("resume", "--from", taken["snapshot_id"], session="s2")
        self.assertEqual((code, out["error"]), (1, "WORK_CHOICE_REQUIRED"), out)
        self.assertEqual([(c["work_id"], c["phase"], c["next"]) for c in out["choices"]],
                         [("C", "implementation", "c next"), ("B", "implementation", "b next"),
                          ("A", "implementation", "a next")])
        self.assertEqual(out["snapshot_id"], taken["snapshot_id"])
        self.assertEqual(store.cards(self.root, "s2"), [])

    def test_resume_adopts_the_named_works(self):
        taken = self.three_open_one_done()
        code, one = self.runtime("resume", "--from", taken["snapshot_id"], "--work", "A", session="s2")
        self.assertEqual((code, one["payload"]["work_id"], one["adopted_from"]), (0, "A", taken["snapshot_id"]), one)
        self.assertEqual(one["payload"]["assignments"][0]["observed_state"], "needs-verification")
        self.assertEqual(one["adopted_at"], one["recorded_at"])
        self.assertIn("reconciliation", one)
        self.assertEqual([card["payload"]["work_id"] for card in store.cards(self.root, "s2")], ["A"])
        code, two = self.runtime("resume", "--from", taken["snapshot_id"], "--work", "A", "--work", "B", session="s3")
        self.assertEqual((code, two["adopted_from"]), (0, taken["snapshot_id"]), two)
        self.assertEqual([card["payload"]["work_id"] for card in two["cards"]], ["B", "A"])
        # The snapshot's order is kept: B, its more recently written card, is the session's current card.
        self.assertEqual([card["payload"]["work_id"] for card in store.cards(self.root, "s3")], ["B", "A"])
        self.assertEqual(self.runtime("resume", "--from", taken["snapshot_id"], "--work", "A", "--work", "B",
                                      session="s3")[1]["cards"], two["cards"])  # a replay adopts nothing new
        self.assertEqual(store.read_log(self.root, "A"), [])  # resume writes nothing to the logs

    def test_resume_refusals(self):
        taken = self.three_open_one_done()
        code, out = self.runtime("resume", "--from", taken["snapshot_id"], "--work", "D", session="s2")
        self.assertEqual((code, out["error"], [c["work_id"] for c in out["choices"]]),
                         (1, "WORK_NOT_IN_SNAPSHOT", ["C", "B", "A"]))
        self.assertEqual(self.runtime("resume", "--from", taken["snapshot_id"], "--work", "A"),
                         (1, {"error": "SAME_SESSION_ADOPTION"}))
        self.write("A", "own A", session="s4")
        self.assertEqual(self.runtime("resume", "--from", taken["snapshot_id"], "--work", "B", "--work", "A",
                                      session="s4"), (1, {"error": "REVISION_CONFLICT"}))
        self.assertEqual([card["payload"]["work_id"] for card in store.cards(self.root, "s4")], ["A"])  # B not adopted
        self.assertEqual(self.runtime("status", "--work", "A", "--work", "B", session="s4"),
                         (1, {"error": "INVALID_ARGUMENTS"}))

    def test_a_one_work_snapshot_adopts_without_a_name(self):
        self.write("A", "a next")
        taken = self.snapshot()
        code, adopted = self.runtime("resume", "--from", taken["snapshot_id"], session="s2")
        self.assertEqual((code, adopted["payload"]["work_id"], adopted["adopted_from"]),
                         (0, "A", taken["snapshot_id"]), adopted)

    def test_the_adoption_marker_carries_down_the_adopted_chain(self):
        self.write("A", "a next")
        taken = self.snapshot()
        adopted = store.resume(self.root, "s2", taken["snapshot_id"])
        later = self.write("A", "a later", session="s2")
        self.assertEqual((later["adopted_at"], later["adopted_from"]), (adopted["adopted_at"], None))
        own = self.write("B", "b next", session="s2")
        self.assertNotIn("adopted_at", own)

    def old_snapshot(self, card, event="old"):
        """A snapshot as runtimes before multi-card snapshots wrote it: one copied card, no other_cards."""
        ident = store.digest("s1\0snapshot\0" + event)
        data = {k: v for k, v in card.items() if k != "checksum"}
        data.update(kind="snapshot", snapshot_id=ident, captured_at=datetime.now(timezone.utc).isoformat(),
                    parent_snapshot=None)
        directory = store.session_dir(self.root, "s1")
        store.atomic_write(handoff_home(), directory / "snapshots" / (ident + ".json"), store._seal(data))
        store.atomic_write(handoff_home(), directory / "latest.json", {"snapshot_id": ident})
        return ident

    def drop_records(self, session="s1"):
        directory = store.session_dir(self.root, session)
        for path in list((directory / "records").glob("*.json")) + [directory / "current.json"]:
            path.unlink()

    def test_old_single_card_snapshots_still_list_resume_search_and_migrate(self):
        legacy = dict(self.payload_v1("legacy next", decisions=["OLDSNAP decision kept"]), work_id="L")
        card = self.record_v1(self.root, "s1", legacy, None, "old")
        ident = self.old_snapshot(card)
        self.drop_records()
        self.assertEqual(self.listing(ident)["works"], [{"work_id": "L", "phase": "implementation"}])
        code, found = self.runtime("search", "--query", "OLDSNAP", session=None)
        self.assertEqual([(hit["source"], hit["work_id"]) for hit in found["results"]], [("snapshot", "L")], found)
        code, shown = self.runtime("search", "--show", found["results"][0]["ref"], session=None)
        self.assertEqual((code, shown["payload"]["decisions"]), (0, ["OLDSNAP decision kept"]), shown)
        code, out = self.runtime("migrate", session=None)
        self.assertEqual([(w["work_id"], w["sources"], w["decisions"]) for w in out["works"]], [("L", 1, 1)], out)
        code, adopted = self.runtime("resume", "--from", ident, session="s2")
        self.assertEqual((code, adopted["schema_version"], adopted["adopted_from"]), (0, 1, ident), adopted)

    def test_every_card_of_a_snapshot_is_a_search_and_migrate_source(self):
        for work in ("A", "B"):
            payload = dict(self.payload_v1(work + " next", decisions=["MULTI decision of " + work]), work_id=work)
            self.record_v1(self.root, "s1", payload, None, "v1-" + work)
        taken = self.snapshot()
        self.drop_records()
        code, found = self.runtime("search", "--query", "MULTI", session=None)
        hits = {hit["work_id"]: hit for hit in found["results"]}
        self.assertEqual((sorted(hits), {hit["source"] for hit in hits.values()}), (["A", "B"], {"snapshot"}), found)
        for work, hit in hits.items():
            code, shown = self.runtime("search", "--show", hit["ref"], session=None)
            self.assertEqual((code, shown["work_id"], shown["payload"]["decisions"]),
                             (0, work, ["MULTI decision of " + work]), shown)
        self.assertNotEqual(hits["A"]["ref"], hits["B"]["ref"])
        code, out = self.runtime("migrate", session=None)
        self.assertEqual([(w["work_id"], w["sources"], w["decisions"]) for w in out["works"]],
                         [("A", 1, 1), ("B", 1, 1)], out)
        self.assertEqual(taken["payload"]["work_id"], "B")

    def test_injection_labels_evidence_that_passed_before_adoption(self):
        self.write("A", "a next", evidence=[{"claim": "OLD-PASS claim", "state": "pass-observed", "ref": "t1"},
                                            {"claim": "OLD-FAIL claim", "state": "fail", "ref": "t2"}])
        taken = self.snapshot()
        store.resume(self.root, "s2", taken["snapshot_id"])
        self.write("A", "a later", session="s2",
                   evidence=[{"claim": "NEW-PASS claim", "state": "pass-observed", "ref": "t3"}])
        own = self.inject("s1")
        self.assertNotIn("passed before adoption", own)
        self.assertIn("Passed claims: 2.", own)  # the log is per work: s1 counts both passes
        text = self.inject("s2")
        line = next(line for line in text.splitlines() if "OLD-PASS claim" in line)
        self.assertIn("passed before adoption, not re-verified", line)
        self.assertIn("OLD-FAIL claim", text)
        self.assertNotIn("NEW-PASS claim", text)
        self.assertIn("Passed claims: 1.", text)  # only the claim observed after the adoption

    def inject(self, session):
        output = handle_event({"session_id": session, "cwd": str(self.root), "hook_event_name": "SessionStart",
                               "source": "resume"}, self.config_path)
        return output["hookSpecificOutput"]["additionalContext"]

    def test_snapshot_only_recovery_lists_the_snapshots_other_open_works(self):
        self.write("A", "a next")
        self.write("B", "b next")
        self.snapshot()
        self.drop_records()
        text = self.inject("s1")
        self.assertIn("b next", text)
        line = next(line for line in text.splitlines() if line.startswith("- A ["))
        self.assertIn("a next", line)

    def test_prune_archives_every_card_of_a_multi_card_snapshot(self):
        from test_prune import backdate
        self.write("A", "a next", session="s4")
        self.write("B", "b next", session="s4")
        self.snapshot(session="s4")
        self.drop_records("s4")
        backdate(store.session_dir(self.root, "s4"), 40)
        code, dry = self.runtime("prune", session="s1")
        self.assertEqual([item["archive"] for item in dry["expired"]], [["A", "B"]], dry)
        code, applied = self.runtime("prune", "--apply", session="s1")
        self.assertEqual((code, applied["expired_removed"]), (0, 1), applied)
        archive = store._base(store.as_workspace(self.root)) / "archive"
        self.assertEqual(sorted(json.loads(path.read_text(encoding="utf-8"))["payload"]["work_id"]
                                for path in archive.glob("*.json")), ["A", "B"])
        archived = json.loads(store._archive_path(archive, "A").read_text(encoding="utf-8"))
        shifted = datetime.fromisoformat(archived["recorded_at"]) + timedelta(days=40)
        self.assertLess(abs((shifted - datetime.now(timezone.utc)).total_seconds()), 600)  # the backdated copy
