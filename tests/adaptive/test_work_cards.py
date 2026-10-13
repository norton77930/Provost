"""State cards per (session, work_id), driven through the runtime CLI in an isolated store."""
import json
import os
import subprocess
from _support import BASH, FixtureCase, REPO
from provost_runtime.config import handoff_home
from provost_runtime.hooks import bind, handle_event
from provost_runtime.store import _seal, as_workspace, atomic_write, digest, session_dir

RUNTIME = REPO / "hooks" / "runtime"
STATUS_KEYS = {"workspace", "session_id", "expected", "checkpoint", "facts", "template", "log"}


class WorkCardTests(FixtureCase):
    def setUp(self):
        super().setUp()
        bind(self.root, "s1", {})

    def runtime(self, *args, data=None):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>` for session s1."""
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        result = subprocess.run([BASH, str(RUNTIME), *args, "--workspace", str(self.root), "--session", "s1"],
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def card(self, work, step):
        return dict(self.payload(step), work_id=work)

    def record(self, work, step, expected, request_id):
        code, data = self.runtime("record", "--expected", expected or "null", "--request-id", request_id,
                                  data=self.card(work, step))
        return data if code == 0 else data["error"]

    def status(self, work=None):
        code, data = self.runtime("status", *(("--work", work) if work is not None else ()))
        self.assertEqual(code, 0, data)
        return data

    def cards(self):
        """{work_id: revisions} of the record files on disk."""
        found = {}
        for path in (session_dir(self.root, "s1") / "records").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            found.setdefault(data["payload"]["work_id"], set()).add(data["revision"])
        return found

    def legacy_chain(self, works, start=0):
        """One old-style chain, oldest first, as runtimes before per-work cards wrote it."""
        ws = as_workspace(self.root)
        directory, parent, written = session_dir(ws, "s1"), None, []
        for index, work in enumerate(works, start):
            revision = digest("s1\0record\0legacy%d" % index)
            data = _seal({"schema_version": 1, "kind": "record", "workspace_id": ws.repo_key,
                          "worktree_path": str(ws.root), "session_id": "s1", "revision": revision,
                          "parent_record": parent, "recorded_at": "2026-10-01T00:00:%02d+00:00" % index,
                          "transcript_cursor": None, "adopted_from": None,
                          "payload": dict(self.payload_v1("legacy %d" % index), work_id=work)})
            atomic_write(handoff_home(), directory / "records" / (revision + ".json"), data)
            parent = revision
            written.append(data)
        atomic_write(handoff_home(), directory / "current.json", {"revision": parent})
        return written

    def test_two_works_in_one_session_keep_separate_cards(self):
        a = self.record("A", "next for A", None, "a1")
        b = self.record("B", "next for B", None, "b1")
        self.assertIsNone(b["parent_record"])
        for work, card in (("A", a), ("B", b)):
            status = self.status(work)
            self.assertEqual(status["expected"], card["revision"])
            self.assertEqual(status["checkpoint"]["payload"]["next"], "next for " + work)
        status = self.status()
        self.assertEqual((status["expected"], status["checkpoint"]["payload"]["work_id"]), (b["revision"], "B"))
        self.assertEqual(status["other_works"], [{"work_id": "A", "phase": "implementation",
                                                  "revision": a["revision"], "recorded_at": a["recorded_at"]}])
        # Writing A again makes it the session's current card.
        a2 = self.record("A", "A again", a["revision"], "a2")
        status = self.status()
        self.assertEqual((status["expected"], status["checkpoint"]["parent_record"]), (a2["revision"], a["revision"]))
        self.assertEqual([item["work_id"] for item in status["other_works"]], ["B"])
        unknown = self.status("C")
        self.assertEqual((unknown["expected"], unknown["checkpoint"]), (None, None))
        self.assertEqual([item["work_id"] for item in unknown["other_works"]], ["A", "B"])

    def test_each_work_chain_is_trimmed_to_its_newest_five(self):
        b1 = self.record("B", "b1", None, "b1")
        expected, a = None, []
        for index in range(7):
            a.append(self.record("A", "a%d" % index, expected, "a%d" % index))
            expected = a[-1]["revision"]
        b2 = self.record("B", "b2", b1["revision"], "b2")
        self.assertEqual(self.cards(), {"A": {card["revision"] for card in a[2:]},
                                        "B": {b1["revision"], b2["revision"]}})
        self.assertEqual(self.status("A")["expected"], a[-1]["revision"])
        self.assertEqual(self.status("B")["checkpoint"]["parent_record"], b1["revision"])

    def test_expected_revision_lock_applies_per_work(self):
        a1 = self.record("A", "a1", None, "a1")
        b1 = self.record("B", "b1", None, "b1")
        self.assertEqual(self.record("A", "a2", b1["revision"], "a2"), "REVISION_CONFLICT")
        self.assertEqual(self.record("A", "a2", None, "a2"), "REVISION_CONFLICT")
        self.assertEqual(self.record("A", "a2", a1["revision"], "a2")["parent_record"], a1["revision"])
        self.assertEqual(self.status("B")["expected"], b1["revision"])

    def test_request_ids_stay_idempotent_with_several_works(self):
        a1 = self.record("A", "a1", None, "a1")
        b1 = self.record("B", "b1", None, "b1")
        self.assertEqual(self.record("A", "a1", None, "a1"), a1)  # replay returns the stored card
        self.assertEqual(self.status()["expected"], b1["revision"])  # and does not make it current
        self.assertEqual(self.record("B", "a1", b1["revision"], "a1"), "REQUEST_ID_REUSED")
        expected = a1["revision"]
        for index in range(2, 7):
            expected = self.record("A", "a%d" % index, expected, "a%d" % index)["revision"]
        # a1 was trimmed from A's chain: retrying it, or reusing it for B, is refused.
        self.assertEqual(self.record("A", "a1", None, "a1"), "REQUEST_ID_EXPIRED")
        self.assertEqual(self.record("B", "b2", b1["revision"], "a1"), "REQUEST_ID_EXPIRED")
        self.assertEqual(self.status("B")["expected"], b1["revision"])

    def test_single_work_session_status_is_unchanged(self):
        first = self.record("A", "a1", None, "a1")
        second = self.record("A", "a2", first["revision"], "a2")
        status = self.status()
        self.assertEqual(set(status), STATUS_KEYS)
        card = {k: v for k, v in second.items() if k not in ("warnings", "echo")}  # record output extras
        self.assertEqual((status["expected"], status["checkpoint"]), (second["revision"], card))
        self.assertEqual(self.status("A"), status)

    def test_legacy_chain_continues_with_its_head_work_and_starts_new_works_fresh(self):
        legacy = self.legacy_chain(["A", "A", "B"])
        status = self.status()
        self.assertEqual(set(status), STATUS_KEYS)  # older works inside the legacy chain are not listed
        self.assertEqual((status["expected"], status["checkpoint"]), (legacy[-1]["revision"], legacy[-1]))
        self.assertEqual(self.status("B")["expected"], legacy[-1]["revision"])
        self.assertIsNone(self.status("A")["expected"])
        self.assertEqual(self.record("A", "a new", legacy[-1]["revision"], "a1"), "REVISION_CONFLICT")
        a = self.record("A", "a new", None, "a1")
        self.assertIsNone(a["parent_record"])
        b = self.record("B", "b new", legacy[-1]["revision"], "b1")
        self.assertEqual(b["parent_record"], legacy[-1]["revision"])
        status = self.status()
        self.assertEqual((status["expected"], [item["work_id"] for item in status["other_works"]]),
                         (b["revision"], ["A"]))

    def test_trimmed_legacy_chain_and_a_new_trimmed_chain_read_together(self):
        legacy = self.legacy_chain(["A", "B", "A", "B", "A", "B", "B"])
        directory = session_dir(self.root, "s1")
        for card in legacy[:2]:
            (directory / "records" / (card["revision"] + ".json")).unlink()
        atomic_write(handoff_home(), directory / "horizon.json", {"records": legacy[1]["revision"], "snapshots": None})
        expected, a = None, []
        for index in range(6):
            a.append(self.record("A", "a%d" % index, expected, "a%d" % index))
            expected = a[-1]["revision"]
        b = self.record("B", "b new", legacy[-1]["revision"], "b1")
        self.assertEqual(self.cards()["A"] - {card["revision"] for card in legacy}, {card["revision"] for card in a[1:]})
        self.assertEqual(self.status("A")["expected"], a[-1]["revision"])
        self.assertEqual(self.status("B")["checkpoint"]["parent_record"], legacy[-1]["revision"])
        self.assertEqual(self.status()["expected"], b["revision"])
        self.assertEqual(self.record("A", "a0", None, "a0"), "REQUEST_ID_EXPIRED")

    def cli(self, session, *args, data=None):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>` for another session."""
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        result = subprocess.run([BASH, str(RUNTIME), *args, "--workspace", str(self.root), "--session", session],
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def reseal(self, card, **changes):
        """Write a stored record with changes, checksum-valid, under its (possibly changed) revision."""
        body = {k: v for k, v in card.items() if k != "checksum"} | changes
        path = session_dir(self.root, "s1") / "records" / (body["revision"] + ".json")
        path.write_text(json.dumps(_seal(body)), encoding="utf-8")

    def horizon(self):
        return json.loads((session_dir(self.root, "s1") / "horizon.json").read_text(encoding="utf-8"))["records"]

    def test_equal_recorded_at_is_resolved_by_the_current_index(self):
        a = self.record("A", "a", None, "a1")
        b = self.record("B", "b", None, "b1")
        self.reseal(b, recorded_at=a["recorded_at"])
        index = session_dir(self.root, "s1") / "current.json"
        for card in (b, a):  # one of the two orders contradicts revision order
            atomic_write(handoff_home(), index, {"revision": card["revision"]})
            self.assertEqual(self.status()["expected"], card["revision"])

    def test_interrupted_trim_with_a_list_horizon(self):
        records = session_dir(self.root, "s1") / "records"
        heads, written = {}, {"A": [], "B": []}

        def write(work):
            name = "%s%d" % (work, len(written[work]))
            card = self.record(work, name, heads.get(work), name)
            heads[work] = card["revision"]
            written[work].append(card)

        def ids(a, b):
            return sorted([written["A"][a]["revision"], written["B"][b]["revision"]])
        for _ in range(6):
            write("A")
            write("B")
        self.assertEqual(self.horizon(), ids(0, 0))
        write("A")
        # The process died after the horizon write, before deleting A1.
        leftover = records / (written["A"][1]["revision"] + ".json")
        stored = {k: v for k, v in written["A"][1].items() if k not in ("warnings", "echo")}  # record output extras
        leftover.write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual(self.horizon(), ids(1, 0))
        self.assertEqual((self.status("A")["expected"], self.status("B")["expected"]), (heads["A"], heads["B"]))
        write("B")  # trimming the other chain leaves A's leftover
        self.assertTrue(leftover.exists())
        self.assertEqual(self.horizon(), ids(1, 1))
        write("A")  # A's own next trim removes it
        self.assertEqual(self.horizon(), ids(2, 1))
        self.assertEqual(self.cards(), {work: {card["revision"] for card in written[work][-5:]} for work in "AB"})
        self.assertEqual((self.status("A")["expected"], self.status("B")["expected"]), (heads["A"], heads["B"]))

    def test_resume_into_a_session_holding_other_works(self):
        for session in ("s2", "s3"):
            bind(self.root, session, {})
        self.record("A", "a", None, "a1")
        code, taken = self.runtime("snapshot", "--request-id", "e1")
        self.assertEqual(code, 0, taken)
        code, _ = self.cli("s2", "record", "--expected", "null", "--request-id", "b1", data=self.card("B", "b"))
        self.assertEqual(code, 0)
        code, adopted = self.cli("s2", "resume", "--from", taken["snapshot_id"])
        self.assertEqual((code, adopted.get("adopted_from")), (0, taken["snapshot_id"]), adopted)
        code, status = self.cli("s2", "status")
        self.assertEqual((status["expected"], [item["work_id"] for item in status["other_works"]]),
                         (adopted["revision"], ["B"]))
        code, _ = self.cli("s3", "record", "--expected", "null", "--request-id", "a1", data=self.card("A", "own"))
        self.assertEqual(code, 0)
        self.assertEqual(self.cli("s3", "resume", "--from", taken["snapshot_id"]), (1, {"error": "REVISION_CONFLICT"}))

    def test_snapshot_and_precompact_capture_the_most_recently_written_card(self):
        a = self.record("A", "a1", None, "a1")
        b = self.record("B", "b1", None, "b1")
        self.record("A", "a2", a["revision"], "a2")
        code, taken = self.runtime("snapshot", "--request-id", "e1")
        self.assertEqual((code, taken["payload"]["next"]), (0, "a2"), taken)
        self.record("B", "b2", b["revision"], "b2")
        handle_event({"hook_event_name": "PreCompact", "trigger": "auto", "session_id": "s1",
                      "cwd": str(self.root)}, self.config_path)
        code, listing = self.runtime("list")
        self.assertEqual([item["work_id"] for item in listing if item["parent_snapshot"] == taken["snapshot_id"]],
                         ["B"])

    def test_heads_of_different_works_sharing_a_live_ancestor_are_ambiguous(self):
        a1 = self.record("A", "a1", None, "a1")
        self.record("A", "a2", a1["revision"], "a2")
        self.reseal(a1, revision=digest("s1\0record\0fork"), parent_record=a1["revision"],
                    payload=self.card("B", "fork"))
        for args in ((), ("--work", "B")):
            self.assertEqual(self.runtime("status", *args), (1, {"error": "AMBIGUOUS_CHAIN"}))

    def test_empty_work_is_an_invalid_argument(self):
        self.assertEqual(self.runtime("status", "--work", ""), (1, {"error": "INVALID_ARGUMENTS"}))
