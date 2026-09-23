from unittest import mock
from _support import FixtureCase
from provost_runtime import store
from provost_runtime.hooks import handle_event
import json


class RecoveryEdges(FixtureCase):
    def test_complete_record_survives_interrupted_index_update(self):
        old = store.record(self.root, "s", self.payload(), None, "r1")
        write = store.atomic_write
        def fail_index(root, path, data):
            if path.name == "current.json":
                raise store.StoreError("WRITE_FAILED")
            return write(root, path, data)
        with mock.patch.object(store, "atomic_write", side_effect=fail_index):
            with self.assertRaises(store.StoreError):
                store.record(self.root, "s", self.payload("new"), old["revision"], "r2")
        recovered = store.current(self.root, "s")
        self.assertEqual(recovered["payload"]["next"], "new")
        self.assertEqual(store.record(self.root, "s", self.payload("new"), old["revision"], "r2"), recovered)
        store.record(self.root, "s", self.payload("newer"), recovered["revision"], "r3")
        store.record(self.root, "s", self.payload("new"), old["revision"], "r2")
        self.assertEqual(store.current(self.root, "s")["payload"]["next"], "newer")

    def test_busy_store_refuses_without_replacing_history(self):
        old = self.seed_snapshot()
        with store._lock(self.root, store.session_dir(self.root, "s1")):
            with self.assertRaisesRegex(store.StoreError, "STORE_BUSY"):
                store.snapshot(self.root, "s1", "busy")
        self.assertEqual(store.latest(self.root, "s1"), old)

    def test_subdirectory_save_failure_is_visible_on_root_resume(self):
        child = self.root / "src"
        child.mkdir()
        event = {"hook_event_name": "PreCompact", "cwd": str(child), "session_id": "s"}
        with mock.patch("provost_runtime.hooks.workspace_root", return_value=self.root):
            handle_event(event, self.config_path)
        result = handle_event({"hook_event_name": "SessionStart", "cwd": str(self.root),
                               "session_id": "s", "source": "resume"}, self.config_path)
        self.assertIn("previous handoff save failed", result.get("systemMessage", ""))
