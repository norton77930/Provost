import json
import os
from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime.config import workspace_key
from provost_runtime.store import (StoreError, current, digest, latest, list_snapshots, record, resume,
                                    session_dir, snapshot)


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
            os.symlink(outside, self.store_home / "v2", target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit symlink creation")
        with self.assertRaises(StoreError):
            record(self.root, "s1", self.payload(), None, "r")
        self.assertEqual(list(outside.iterdir()), [])

    def test_git_checkout_needs_no_exclusion_and_stays_clean(self):
        self.isolate_user_git_config()
        self.git("init", "-q")
        record(self.root, "s1", self.payload(), None, "r")
        snapshot(self.root, "s1", "e")
        # Only the fixture's own config file; no handoff state in the work tree.
        self.assertEqual(self.git("status", "--porcelain", "--ignored", "--untracked-files=all"),
                         "?? user-config.json")

    def test_list_is_read_only(self):
        self.assertEqual(list_snapshots(self.root), [])
        self.assertNoStore()


class RepoStoreTests(FixtureCase):
    def repo_dir(self, workspace):
        # A key prefix keeps record paths under Windows MAX_PATH; files hold the full key.
        return self.store_home / "v2" / "repos" / workspace_key(workspace)[:16]

    def test_store_files_land_in_the_store_home_keyed_by_directory(self):
        data = record(self.root, "s1", self.payload(), None, "r1")
        path = self.repo_dir(self.root) / "sessions" / digest("s1") / "records" / (data["revision"] + ".json")
        self.assertTrue(path.is_file())
        self.assertEqual(data["workspace_id"], workspace_key(self.root))
        self.assertFalse((self.root / ".provost").exists())

    def test_worktree_and_main_checkout_share_one_store(self):
        main, worktree = self.git_repo_with_worktree()
        expected = self.repo_dir(main) / "sessions" / digest("s1")
        self.assertEqual(session_dir(main, "s1"), expected)
        self.assertEqual(session_dir(worktree, "s1"), expected)
        first = record(worktree, "s1", self.payload("from worktree"), None, "r1")
        self.assertEqual(first["workspace_id"], workspace_key(main))
        self.assertEqual(Path(first["worktree_path"]), worktree)
        self.assertEqual(current(main, "s1")["payload"]["next"], "from worktree")
        record(main, "s1", self.payload("from main"), first["revision"], "r2")
        self.assertEqual(current(worktree, "s1")["payload"]["next"], "from main")
        for workspace in (main, worktree):
            self.assertFalse((workspace / ".provost").exists())

    def test_store_inside_a_git_work_tree_is_refused(self):
        self.isolate_user_git_config()
        repo = self.root / "repo"
        repo.mkdir()
        self.git("init", "-q", cwd=repo)
        inside = repo / "handoffs"
        with mock.patch.dict(os.environ, PROVOST_HANDOFF_HOME=str(inside)):
            with self.assertRaisesRegex(StoreError, "^STORE_INSIDE_REPO$"):
                record(self.root, "s1", self.payload(), None, "r")
        self.assertFalse(inside.exists())
        self.assertFalse((self.root / ".provost").exists())

    def test_bare_repository_has_no_resolvable_repo_key(self):
        self.isolate_user_git_config()
        bare = self.root / "bare.git"
        self.git("init", "-q", "--bare", str(bare))
        with self.assertRaisesRegex(StoreError, "^REPO_KEY_UNRESOLVED$"):
            record(bare, "s1", self.payload(), None, "r")
        self.assertNoStore(bare)
