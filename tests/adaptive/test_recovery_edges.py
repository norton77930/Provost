from unittest import mock
from _support import FixtureCase
from provost_runtime import store
from provost_runtime.hooks import handle_event, recovery_context
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
        with mock.patch("provost_runtime.hooks.workspace_root", return_value=store.locate(self.root)), \
                mock.patch("provost_runtime.hooks.snapshot", side_effect=store.StoreError("WRITE_FAILED")):
            handle_event(event, self.config_path)
        result = handle_event({"hook_event_name": "SessionStart", "cwd": str(self.root),
                               "session_id": "s", "source": "resume"}, self.config_path)
        self.assertIn("previous handoff save failed", result.get("systemMessage", ""))


INSTRUCTION = ("If any assignment is not marked completed, verify that worker's actual state "
               "before dispatching; read the checkpoint file for full details.")
DIGEST_LABEL = "Observed payload digest (full JSON in the checkpoint file): "


class RecoveryPayloadInjection(FixtureCase):
    def sized_payload(self, evidence_chars):
        payload = self.payload("ship it")
        payload["work_id"] = "wid-1"
        payload["blockers"] = ["blocked on review"]
        payload["assignments"] = [
            {"task": "t%d" % i, "agent_id": "a%d" % i, "observed_state": "running " + "x" * 600,
             "write_scope": ["src/mod%d.py" % i]} for i in range(3)]
        payload["decisions"] = ["decision-%d" % i for i in range(5)]
        payload["evidence"] = [{"claim": "EVIDENCE-MARK " + "e" * evidence_chars, "ref": "r", "state": "unverified"}]
        payload["sources"] = [{"kind": "file", "ref": "SOURCES-MARK"}]
        return payload

    def recover(self, payload):
        store.record(self.root, "s1", payload, None, "r1")
        return recovery_context(self.root, "s1")

    def test_mid_size_payload_is_inlined_in_full(self):
        payload = self.payload("n" * 2000)
        text = self.recover(payload)
        stored = store.current(self.root, "s1")["payload"]
        self.assertIn("Observed payload: " + json.dumps(stored, ensure_ascii=False), text)
        self.assertIn(INSTRUCTION, text)

    def test_oversized_payload_yields_digest(self):
        text = self.recover(self.sized_payload(5000))
        self.assertNotIn("Observed payload: ", text)
        self.assertIn(INSTRUCTION, text)
        self.assertLess(text.index(INSTRUCTION), text.index(DIGEST_LABEL))
        body = text.split(DIGEST_LABEL, 1)[1]
        self.assertLessEqual(len(body), 4500)
        digest = json.loads(body)
        self.assertEqual(list(digest), ["work_id", "phase", "next", "blockers", "assignments", "decisions"])
        self.assertEqual((digest["work_id"], digest["phase"], digest["next"]), ("wid-1", "implementation", "ship it"))
        self.assertEqual(digest["blockers"], ["blocked on review"])
        self.assertEqual(len(digest["assignments"]), 3)
        self.assertTrue(all(len(item) <= 300 for item in digest["assignments"]))
        self.assertIn("t0", digest["assignments"][0])
        self.assertEqual(digest["decisions"], ["decision-2", "decision-3", "decision-4"])
        self.assertNotIn("EVIDENCE-MARK", text)
        self.assertNotIn("SOURCES-MARK", text)

    def test_digest_is_hard_capped_and_deterministic(self):
        payload = self.sized_payload(5000)
        payload["blockers"] = ["b" * 3000, "c" * 3000]
        text = self.recover(payload)
        self.assertLessEqual(len(text.split(DIGEST_LABEL, 1)[1]), 4500)
        self.assertEqual(text, recovery_context(self.root, "s1"))

    def test_compact_with_4500_char_payload_stays_under_session_cap(self):
        payload = self.sized_payload(0)
        payload["next"] = "n" * (4500 - len(json.dumps(payload, ensure_ascii=False)) + len(payload["next"]))
        self.assertEqual(len(json.dumps(payload, ensure_ascii=False)), 4500)
        store.record(self.root, "s1", payload, None, "r1")
        stored = store.current(self.root, "s1")["payload"]
        output = handle_event({"session_id": "s1", "cwd": str(self.root),
                               "hook_event_name": "SessionStart", "source": "compact"}, self.config_path)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context), 8000)
        self.assertIn("Observed payload: " + json.dumps(stored, ensure_ascii=False), context)
        self.assertIn(INSTRUCTION, context)
