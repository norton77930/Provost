import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime.store import StoreError, record, snapshot, latest, current, list_snapshots, resume, session_dir


class StoreTests(FixtureCase):
    def test_record_compare_and_swap_and_request_id(self):
        first = record(self.root, "s1", self.payload(), None, "r1")
        self.assertEqual(record(self.root, "s1", self.payload(), None, "r1"), first)
        with self.assertRaises(StoreError):
            record(self.root, "s1", self.payload("different"), None, "r1")
        with self.assertRaises(StoreError):
            record(self.root, "s1", self.payload(), None, "r2")
        second = record(self.root, "s1", self.payload("verify"), first["revision"], "r2")
        self.assertEqual(second["parent_record"], first["revision"])
        self.assertEqual(current(self.root, "s1")["payload"]["next"], "verify")

    def test_snapshot_is_idempotent_self_contained_and_session_scoped(self):
        old = self.seed_snapshot()
        self.assertEqual(snapshot(self.root, "s1", "e1"), old)
        self.assertEqual(old["payload"]["next"], "collect result")
        self.assertIsNone(latest(self.root, "s2"))
        self.assertEqual(len(list_snapshots(self.root)), 1)
        newer = snapshot(self.root, "s1", "e2")
        self.assertEqual(newer["parent_snapshot"], old["snapshot_id"])

    def test_failed_write_keeps_previous_snapshot(self):
        old = self.seed_snapshot()
        with mock.patch("provost_runtime.store.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(StoreError):
                snapshot(self.root, "s1", "e2")
        self.assertEqual(latest(self.root, "s1")["snapshot_id"], old["snapshot_id"])

    def test_corrupt_index_recovers_complete_chain(self):
        self.seed_snapshot()
        newest = snapshot(self.root, "s1", "e2")
        directory = session_dir(self.root, "s1")
        (directory / "latest.json").write_text("{broken")
        (directory / "snapshots" / "partial.json").write_text("{")
        self.assertEqual(latest(self.root, "s1")["snapshot_id"], newest["snapshot_id"])

    def test_resume_is_explicit_new_chain_and_does_not_take_worker_ownership(self):
        payload = self.payload()
        payload["assignments"] = [{"task": "T1", "agent_id": "old-worker", "observed_state": "running", "write_scope": ["src/a"]}]
        record(self.root, "s1", payload, None, "r1")
        source = snapshot(self.root, "s1", "e1")
        result = resume(self.root, "s2", source["snapshot_id"])
        self.assertEqual(result["adopted_from"], source["snapshot_id"])
        self.assertEqual(result["payload"]["assignments"][0]["observed_state"], "needs-verification")
        self.assertEqual(current(self.root, "s1")["payload"]["assignments"][0]["observed_state"], "running")
        with self.assertRaises(StoreError):
            resume(self.root, "s3", "../outside")

    def test_validation_no_record_no_snapshot_and_size_limit(self):
        with self.assertRaises(StoreError):
            snapshot(self.root, "s1", "no-record")
        for payload in [{}, {**self.payload(), "execute": "bad"}, self.payload("x" * 33000)]:
            with self.subTest(payload_size=len(str(payload))), self.assertRaises(StoreError):
                record(self.root, "s1", payload, None, "r")

    def test_wrong_workspace_or_schema_is_not_restored(self):
        source = self.seed_snapshot()
        path = session_dir(self.root, "s1") / "snapshots" / (source["snapshot_id"] + ".json")
        source["workspace_id"] = "other"
        path.write_text(json.dumps(source))
        with self.assertRaises(StoreError):
            latest(self.root, "s1")

    def test_linked_storage_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.root / ".provost", target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit symlink creation")
        with self.assertRaises(StoreError):
            record(self.root, "s1", self.payload(), None, "r")
        self.assertEqual(list(outside.iterdir()), [])

    def isolate_user_git_config(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-gitcfg-")
        self.addCleanup(tmp.cleanup)
        empty_config = Path(tmp.name) / "empty-gitconfig"
        empty_config.write_text("", encoding="utf-8")
        xdg = Path(tmp.name) / "xdg"
        xdg.mkdir()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty_config), "XDG_CONFIG_HOME": str(xdg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_git_storage_requires_exclusion_and_must_not_be_tracked(self):
        self.isolate_user_git_config()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        with self.assertRaises(StoreError):
            record(self.root, "s1", self.payload(), None, "r")
        (self.root / ".git/info/exclude").write_text("/.provost/handoffs/\n")
        record(self.root, "s1", self.payload(), None, "r")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".provost/handoffs"], check=True, capture_output=True)
        with self.assertRaises(StoreError):
            snapshot(self.root, "s1", "e")

    def test_list_is_read_only(self):
        self.assertEqual(list_snapshots(self.root), [])
        self.assertFalse((self.root / ".provost").exists())
