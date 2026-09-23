import json
from _support import FixtureCase
import test_cli
from provost_runtime.hooks import bind, handle_event
from provost_runtime.store import record, snapshot, resume, session_dir, list_snapshots

class ReviewFixTests(FixtureCase):
    run_cli = test_cli.CliTests.run_cli
    def test_manual_writes_require_workspace_session_binding(self):
        other = self.root / "other"
        other.mkdir()
        self.cfg["workspace_roots"].append(str(other))
        self.config_path.write_text(json.dumps(self.cfg))
        bind(self.root, "s1", {})
        for command, extra in (("record", ["--expected", "null", "--request-id", "r"]),
                               ("snapshot", ["--request-id", "e"]),
                               ("resume", ["--from", "a" * 64])):
            result = self.run_cli(command, "--workspace", str(other), "--session", "s1",
                                  *extra, data=self.payload())
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["error"], "SESSION_NOT_BOUND")
        self.assertFalse((other / ".provost").exists())

class RecoveryReviewFixTests(FixtureCase):
    def test_unrelated_corrupt_history_does_not_block_selected_resume(self):
        good = self.seed_snapshot()
        record(self.root, "bad", self.payload(), None, "r")
        bad = snapshot(self.root, "bad", "e")
        path = session_dir(self.root, "bad") / "snapshots" / (bad["snapshot_id"] + ".json")
        path.write_text("{broken")
        result = resume(self.root, "new", good["snapshot_id"])
        self.assertEqual(result["adopted_from"], good["snapshot_id"])
        entries = list_snapshots(self.root)
        self.assertTrue(any(e.get("snapshot_id") == good["snapshot_id"] for e in entries))
        self.assertTrue(any(e.get("error") == "CORRUPT_RECORD" for e in entries))

    def test_allowlist_fallback_explains_why_without_workspace_writes(self):
        self.cfg["workspace_roots"] = []
        self.config_path.write_text(json.dumps(self.cfg))
        output = handle_event({"hook_event_name": "SessionStart", "source": "startup",
                               "session_id": "s", "cwd": str(self.root)}, self.config_path)
        self.assertIn("not enabled", output.get("systemMessage", ""))
        self.assertFalse((self.root / ".provost").exists())
