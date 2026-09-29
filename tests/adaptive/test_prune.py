import json
import os
import subprocess
import sys
import time
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind
from provost_runtime.store import digest, prune, record, session_dir

DAY = 86400


class PruneTests(FixtureCase):
    def bound(self, session, age_days):
        bind(self.root, session, {})
        path = session_dir(self.root, session) / "binding.json"
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
        return session_dir(self.root, session)

    def names(self, result):
        return sorted(c["session_dir"] for c in result["candidates"])

    def test_dry_run_lists_old_binding_only_sessions_and_deletes_nothing(self):
        old = self.bound("old", 30)
        result = prune(self.root, older_than_days=14)
        self.assertEqual(self.names(result), [old.name])
        self.assertEqual(result["candidates"][0]["session_id"], "old")
        self.assertFalse(result["applied"])
        self.assertEqual(result["removed"], 0)
        self.assertTrue((old / "binding.json").exists())

    def test_apply_removes_only_the_candidates(self):
        old, young = self.bound("old", 30), self.bound("young", 2)
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue((young / "binding.json").exists())

    def test_sessions_with_any_other_content_are_kept(self):
        with_record = self.bound("with-record", 30)
        record(self.root, "with-record", self.payload(), None, "r1")
        os.utime(with_record / "binding.json", (time.time() - 30 * DAY,) * 2)
        extra = self.bound("extra", 30)
        (extra / "notes.txt").write_text("x", encoding="utf-8")
        nested = self.bound("nested", 30)
        (nested / "sub").mkdir()
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["candidates"], [])
        for kept in (with_record, extra, nested):
            self.assertTrue((kept / "binding.json").exists(), kept.name)

    def test_keep_session_is_never_a_candidate(self):
        mine = self.bound("mine", 30)
        result = prune(self.root, older_than_days=14, keep_session="mine", apply=True)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(mine.exists())

    def test_foreign_directory_names_are_ignored(self):
        self.bound("real", 30)
        stray = session_dir(self.root, "real").parent / "not-a-session"
        stray.mkdir()
        (stray / "binding.json").write_text("{}", encoding="utf-8")
        os.utime(stray / "binding.json", (time.time() - 30 * DAY,) * 2)
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(self.names(result), [digest("real")])
        self.assertTrue(stray.exists())

    def test_linked_session_directory_is_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "binding.json").write_text("{}", encoding="utf-8")
        os.utime(outside / "binding.json", (time.time() - 30 * DAY,) * 2)
        self.bound("anchor", 1)
        link = session_dir(self.root, "anchor").parent / ("f" * 64)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            try:  # Windows without symlink rights: a junction is also a reparse point
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            except (ImportError, AttributeError, OSError):
                self.skipTest("OS permits neither symlinks nor junctions")
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["candidates"], [])
        self.assertTrue((outside / "binding.json").exists())

    def test_concurrent_rebind_after_the_check_survives(self):
        # A resume of the stale session rewrites binding.json between the
        # candidate check and the delete; the fresh binding must be kept.
        from unittest import mock
        from provost_runtime import store
        self.bound("resumed", 30)
        original = store._stale_binding

        def check_then_rebind(root, entry, cutoff):
            found = original(root, entry, cutoff)
            if found is not None:
                bind(self.root, "resumed", {})
            return found

        with mock.patch.object(store, "_stale_binding", check_then_rebind):
            result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["removed"], 0)
        self.assertTrue((session_dir(self.root, "resumed") / "binding.json").exists())

    def test_failed_delete_is_reported_and_leaves_the_binding(self):
        from unittest import mock
        from pathlib import Path
        old = self.bound("locked", 30)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["removed"], 0)
        self.assertEqual([e["session_dir"] for e in result["errors"]], [old.name])
        self.assertTrue((old / "binding.json").exists())

    def test_missing_store_is_empty_and_creates_nothing(self):
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["candidates"], [])
        self.assertFalse((self.root / ".provost").exists())

    def test_age_must_be_positive(self):
        from provost_runtime.store import StoreError
        for bad in (0, -1):
            with self.assertRaises(StoreError):
                prune(self.root, older_than_days=bad)


class PruneCliTests(FixtureCase):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(REPO / "scripts"),
                   PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-m", "provost_runtime.cli", *args], input="",
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        return result.returncode, json.loads(result.stdout)

    def aged(self, session, age_days):
        bind(self.root, session, {})
        stamp = time.time() - age_days * DAY
        os.utime(session_dir(self.root, session) / "binding.json", (stamp, stamp))

    def test_cli_prune_is_dry_run_unless_apply(self):
        self.aged("old", 30)
        code, data = self.run_cli("prune", "--workspace", str(self.root))
        self.assertEqual(code, 0, data)
        self.assertEqual(len(data["candidates"]), 1)
        self.assertTrue(session_dir(self.root, "old").exists())
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--apply")
        self.assertEqual((code, data["removed"]), (0, 1))
        self.assertFalse(session_dir(self.root, "old").exists())

    def test_cli_prune_honours_age_and_kept_session(self):
        self.aged("a", 5)
        self.aged("b", 30)
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--older-than-days", "3",
                                  "--session", "b")
        self.assertEqual(code, 0, data)
        self.assertEqual([c["session_id"] for c in data["candidates"]], ["a"])
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--older-than-days", "0")
        self.assertEqual((code, data), (1, {"error": "INVALID_ARGUMENTS"}))

    def test_doctor_reports_stale_session_count(self):
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual(data["stale_sessions"], 0)
        self.aged("old", 30)
        self.aged("young", 1)
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual((code, data["stale_sessions"]), (0, 1))
