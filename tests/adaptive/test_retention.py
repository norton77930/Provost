import json
import os
import subprocess
import sys
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind, handle_event
from provost_runtime.store import StoreError, current, latest, record, session_dir, snapshot


class RetentionTests(FixtureCase):
    def chain(self, count, session="s1"):
        """count records written in order; returns them oldest first."""
        written, expected = [], None
        for index in range(count):
            data = record(self.root, session, self.payload("step %d" % index), expected, "r%d" % index)
            written.append(data)
            expected = data["revision"]
        return written

    def files(self, kind):
        return {path.stem for path in (session_dir(self.root, "s1") / kind).glob("*.json")}

    def horizon(self):
        return json.loads((session_dir(self.root, "s1") / "horizon.json").read_text(encoding="utf-8"))

    def test_records_keep_the_newest_five_by_chain_order(self):
        written = self.chain(7)
        self.assertEqual(self.files("records"), {data["revision"] for data in written[2:]})
        self.assertEqual(current(self.root, "s1")["revision"], written[-1]["revision"])
        self.assertEqual(self.horizon(), {"records": written[1]["revision"], "snapshots": None})
        index = json.loads((session_dir(self.root, "s1") / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(index["revision"], written[-1]["revision"])

    def test_status_expected_is_the_head_after_trimming(self):
        written = self.chain(7)
        bind(self.root, "s1", {})
        env = dict(os.environ, PYTHONPATH=str(REPO / "scripts"), PROVOST_CONFIG=str(self.config_path),
                   PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-m", "provost_runtime.cli", "status", "--workspace", str(self.root),
                                 "--session", "s1"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["expected"], written[-1]["revision"])

    def test_snapshots_keep_the_newest_three(self):
        record(self.root, "s1", self.payload(), None, "r")
        taken = [snapshot(self.root, "s1", "e%d" % index) for index in range(5)]
        self.assertEqual(self.files("snapshots"), {data["snapshot_id"] for data in taken[2:]})
        self.assertEqual(latest(self.root, "s1")["snapshot_id"], taken[-1]["snapshot_id"])
        self.assertEqual(self.horizon()["snapshots"], taken[1]["snapshot_id"])
        index = json.loads((session_dir(self.root, "s1") / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(index["snapshot_id"], taken[-1]["snapshot_id"])

    def test_missing_middle_record_of_a_trimmed_chain_is_incomplete(self):
        written = self.chain(7)
        (session_dir(self.root, "s1") / "records" / (written[4]["revision"] + ".json")).unlink()
        with self.assertRaisesRegex(StoreError, "^INCOMPLETE_CHAIN$"):
            current(self.root, "s1")

    def test_inconsistent_or_malformed_horizon_is_corrupt(self):
        self.chain(7)
        path = session_dir(self.root, "s1") / "horizon.json"
        for text in (json.dumps({"records": "a" * 64, "snapshots": None}), "{broken",
                     json.dumps({"records": 5, "snapshots": None}), json.dumps(["x"])):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(StoreError, "^CORRUPT_RECORD$"):
                    current(self.root, "s1")

    def test_crash_after_horizon_write_reads_and_next_record_cleans_up(self):
        written = self.chain(5)
        # Horizon written for a sixth record, then the process died before deleting.
        sixth = record(self.root, "s1", self.payload("six"), written[-1]["revision"], "r5")
        records = session_dir(self.root, "s1") / "records"
        self.assertEqual(len(self.files("records")), 5)
        removed = written[0]
        (records / (removed["revision"] + ".json")).write_text(
            json.dumps(removed, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        self.assertEqual(current(self.root, "s1")["revision"], sixth["revision"])
        self.assertEqual(self.horizon()["records"], removed["revision"])
        seventh = record(self.root, "s1", self.payload("seven"), sixth["revision"], "r6")
        self.assertEqual(self.files("records"), {d["revision"] for d in written[2:]} | {sixth["revision"], seventh["revision"]})
        self.assertEqual(current(self.root, "s1")["revision"], seventh["revision"])

    def test_crash_with_several_leftovers_below_the_horizon(self):
        written = self.chain(7)
        records = session_dir(self.root, "s1") / "records"
        for data in written[:2]:
            (records / (data["revision"] + ".json")).write_text(
                json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        self.assertEqual(current(self.root, "s1")["revision"], written[-1]["revision"])

    def test_retry_of_a_trimmed_request_id_is_expired_and_changes_nothing(self):
        written = self.chain(7)
        before = (self.files("records"), self.horizon())
        head = written[-1]["revision"]
        # Same call as the trimmed second record, and a reuse of that id against the head.
        for expected in (written[0]["revision"], head):
            with self.subTest(expected=expected), self.assertRaisesRegex(StoreError, "^REQUEST_ID_EXPIRED$"):
                record(self.root, "s1", self.payload("step 1"), expected, "r1")
        # Older trimmed ids are no longer known; the compare-and-swap still refuses the stale retry.
        with self.assertRaisesRegex(StoreError, "^REVISION_CONFLICT$"):
            record(self.root, "s1", self.payload("step 0"), None, "r0")
        self.assertEqual((self.files("records"), self.horizon()), before)
        self.assertEqual(current(self.root, "s1")["revision"], head)

    def test_retry_of_a_trimmed_snapshot_event_is_expired(self):
        record(self.root, "s1", self.payload(), None, "r")
        taken = [snapshot(self.root, "s1", "e%d" % index) for index in range(4)]
        with self.assertRaisesRegex(StoreError, "^REQUEST_ID_EXPIRED$"):
            snapshot(self.root, "s1", "e0")
        self.assertEqual(latest(self.root, "s1")["snapshot_id"], taken[-1]["snapshot_id"])

    def test_precompact_and_compact_injection_after_trimming(self):
        self.chain(7)
        event = {"session_id": "s1", "cwd": str(self.root)}
        for index in range(4):
            record(self.root, "s1", self.payload("later %d" % index), current(self.root, "s1")["revision"], "x%d" % index)
            handle_event(event | {"hook_event_name": "PreCompact", "trigger": "auto"}, self.config_path)
        self.assertEqual(len(self.files("snapshots")), 3)
        self.assertEqual(latest(self.root, "s1")["payload"]["next"], "later 3")
        output = handle_event(event | {"hook_event_name": "SessionStart", "source": "compact"}, self.config_path)
        self.assertIn("later 3", output["hookSpecificOutput"]["additionalContext"])
