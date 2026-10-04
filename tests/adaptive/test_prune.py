import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind
from provost_runtime.store import digest, prune, record, session_dir, snapshot as store_snapshot

DAY = 86400


def make_link(target, link, *, directory=True):
    """Symlink, or on Windows without symlink rights a junction (directories only).
    Returns False when neither can be created."""
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except OSError:
        if not directory:
            return False
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return True
        except (ImportError, AttributeError, OSError):
            return False


def age(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def backdate(directory, days):
    """Move every record/snapshot stamp (stored and file mtime) of a session dir days into the past."""
    when = datetime.now(timezone.utc) - timedelta(days=days)
    for kind, key in (("records", "recorded_at"), ("snapshots", "captured_at")):
        for path in (directory / kind).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data[key] = when.isoformat()
            path.write_text(json.dumps(data), encoding="utf-8")
            age(path, days)


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
        os.lstat(link)  # the link entry itself is still there
        self.assertIn({"entry": link.name, "reason": "LINKED_STORAGE"}, result["skipped"])

    def test_linked_binding_file_in_a_real_session_dir_is_skipped(self):
        real = self.bound("real", 30)
        outside = self.root / "outside-binding.json"
        outside.write_text("{}", encoding="utf-8")
        age(outside, 30)
        (real / "binding.json").unlink()
        if not make_link(outside, real / "binding.json", directory=False):
            self.skipTest("OS does not permit file symlinks")
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(outside.exists())
        self.assertIn({"entry": real.name, "reason": "LINKED_STORAGE"}, result["skipped"])

    def test_rebind_after_the_move_lands_in_a_fresh_dir(self):
        from unittest import mock
        from provost_runtime import store
        original_dir = self.bound("resumed", 30)
        real_only = store._only_binding

        def rebind_on_recheck(root, directory):
            if directory.name.startswith(".pruning-"):
                bind(self.root, "resumed", {})
            return real_only(root, directory)

        with mock.patch.object(store, "_only_binding", rebind_on_recheck):
            result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["removed"], 1)
        self.assertTrue((original_dir / "binding.json").exists())
        self.assertGreater((original_dir / "binding.json").stat().st_mtime, time.time() - DAY)
        self.assertEqual(list(original_dir.parent.glob(".pruning-*")), [])

    def test_leftover_staging_dirs_are_reclaimed_before_their_session(self):
        session = self.bound("again", 30)
        sessions = session.parent
        empty = sessions / (".pruning-" + "a" * 64)
        empty.mkdir()
        stale = sessions / (".pruning-" + session.name)
        stale.mkdir()
        (stale / "binding.json").write_text("{}", encoding="utf-8")
        age(stale / "binding.json", 30)
        busy = sessions / (".pruning-" + "b" * 64)
        busy.mkdir()
        (busy / "records").mkdir()
        dry = prune(self.root, older_than_days=14)
        self.assertEqual(sorted((x["staging_dir"], x["state"]) for x in dry["leftovers"]),
                         sorted([(empty.name, "reclaimable"), (stale.name, "reclaimable"), (busy.name, "kept")]))
        self.assertTrue(empty.exists() and stale.exists())
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual((result["reclaimed"], result["removed"], result["errors"]), (2, 1, []))
        self.assertFalse(empty.exists() or stale.exists() or session.exists())
        self.assertTrue((busy / "records").exists())

    def test_reclaim_keeps_a_fresh_binding_that_arrives_after_the_check(self):
        # POSIX: another prune can rename a session (with a just-rewritten binding)
        # onto an empty leftover after it was judged reclaimable.
        from unittest import mock
        from provost_runtime import store
        self.bound("anchor", 1)
        leftover = session_dir(self.root, "anchor").parent / (".pruning-" + "c" * 64)
        leftover.mkdir()
        real_state = store._leftover_state

        def fresh_after_check(root, directory, cutoff):
            state = real_state(root, directory, cutoff)
            if directory == leftover:
                (leftover / "binding.json").write_text("{}", encoding="utf-8")
            return state

        with mock.patch.object(store, "_leftover_state", fresh_after_check):
            result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["reclaimed"], 0)
        self.assertTrue((leftover / "binding.json").exists())

    def test_final_rmdir_failure_leaves_an_empty_leftover_that_a_later_run_reclaims(self):
        from unittest import mock
        from pathlib import Path
        session = self.bound("stuck", 30)
        real_rmdir = Path.rmdir

        def fail_on_staging(self_path):
            if self_path.name.startswith(".pruning-"):
                raise PermissionError("in use")
            return real_rmdir(self_path)

        with mock.patch.object(Path, "rmdir", fail_on_staging):
            first = prune(self.root, older_than_days=14, apply=True)
        leftover = session.parent / (".pruning-" + session.name)
        self.assertEqual(first["removed"], 0)
        self.assertEqual(first["errors"], [{"session_dir": session.name, "error": "PermissionError",
                                            "left_at": leftover.name}])
        self.assertTrue(leftover.is_dir())
        self.assertEqual(list(leftover.iterdir()), [])
        second = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(second["reclaimed"], 1)
        self.assertFalse(leftover.exists())

    def test_recheck_store_error_moves_the_dir_back(self):
        from unittest import mock
        from provost_runtime import store
        session = self.bound("flagged", 30)
        real_only = store._only_binding

        def linked_on_recheck(root, directory):
            if directory.name.startswith(".pruning-"):
                raise store.StoreError("LINKED_STORAGE")
            return real_only(root, directory)

        with mock.patch.object(store, "_only_binding", linked_on_recheck):
            result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["removed"], 0)
        self.assertTrue((session / "binding.json").exists())
        self.assertEqual(list(session.parent.glob(".pruning-*")), [])
        self.assertIn({"entry": session.name, "reason": "LINKED_STORAGE"}, result["skipped"])

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
        self.assertEqual(result["errors"], [{"session_dir": old.name, "error": "PermissionError", "left_at": None}])
        self.assertTrue((old / "binding.json").exists())
        self.assertEqual(list(old.parent.glob(".pruning-*")), [])

    def test_missing_store_is_empty_and_creates_nothing(self):
        result = prune(self.root, older_than_days=14, apply=True)
        self.assertEqual(result["candidates"], [])
        self.assertNoStore()

    def test_age_must_be_positive(self):
        from provost_runtime.store import StoreError
        for bad in (0, -1):
            with self.assertRaises(StoreError):
                prune(self.root, older_than_days=bad)


class ExpiredPruneTests(FixtureCase):
    def session(self, name, age_days=0, snapshot=False):
        """A session with one record (and optionally a snapshot) stamped age_days ago."""
        bind(self.root, name, {})
        record(self.root, name, self.payload(), None, "r1")
        if snapshot:
            store_snapshot(self.root, name, "e1")
        directory = session_dir(self.root, name)
        backdate(directory, age_days)
        return directory

    def expired(self, result):
        return sorted(item["session_dir"] for item in result["expired"])

    def test_dry_run_lists_an_expired_session_and_apply_removes_it(self):
        old = self.session("old", 40, snapshot=True)
        fresh = self.session("fresh", 2)
        dry = prune(self.root)
        self.assertEqual(self.expired(dry), [old.name])
        item = dry["expired"][0]
        self.assertEqual((item["records"], item["snapshots"]), (1, 1))
        self.assertLess(datetime.fromisoformat(item["newest"]).timestamp(), time.time() - 39 * DAY)
        self.assertTrue(old.is_dir() and fresh.is_dir())
        self.assertEqual((dry["applied"], dry["expired_removed"]), (False, 0))
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"]), (1, []))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())
        self.assertEqual(list(old.parent.glob(".pruning-*")), [])

    def test_expiry_default_is_30_days_and_an_explicit_age_applies_to_it(self):
        mid = self.session("mid", 20)
        self.assertEqual(prune(self.root)["expired"], [])
        self.assertEqual(self.expired(prune(self.root, older_than_days=10)), [mid.name])

    def test_current_session_is_never_removed_even_when_expired(self):
        mine = self.session("mine", 90)
        result = prune(self.root, keep_session="mine", apply=True)
        self.assertEqual((result["expired"], result["expired_removed"]), ([], 0))
        self.assertTrue(mine.is_dir())

    def test_a_locked_session_is_skipped_with_a_reason(self):
        from provost_runtime.store import _lock
        busy = self.session("busy", 90)
        with _lock(self.root, busy):
            result = prune(self.root, apply=True)
        self.assertEqual(result["expired"], [])
        self.assertIn({"entry": busy.name, "reason": "STORE_BUSY"}, result["skipped"])
        self.assertTrue(busy.is_dir())
        self.assertEqual(self.expired(prune(self.root)), [busy.name])

    def test_dry_run_and_probe_create_no_lock_file(self):
        old = self.session("old", 90)
        (old / ".lock").unlink(missing_ok=True)
        prune(self.root)
        self.assertFalse((old / ".lock").exists())

    def test_a_record_written_after_the_move_restores_the_dir(self):
        from unittest import mock
        from provost_runtime import store
        old = self.session("late", 90)
        real_scan = store._scan

        def writer_arrives(home, directory):
            if directory.name.startswith(".pruning-"):
                (directory / "records" / ("f" * 64 + ".json")).write_text(
                    json.dumps({"recorded_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
            return real_scan(home, directory)

        with mock.patch.object(store, "_scan", writer_arrives):
            result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"]), (0, []))
        self.assertTrue((old / "records" / ("f" * 64 + ".json")).exists())
        self.assertEqual(list(old.parent.glob(".pruning-*")), [])

    def test_a_lock_taken_after_the_move_restores_the_dir(self):
        from unittest import mock
        from provost_runtime import store
        old = self.session("lk", 90)
        probes = []

        def held_after_move(home, directory):
            probes.append(directory.name)
            return directory.name.startswith(".pruning-")

        with mock.patch.object(store, "_lock_held", held_after_move):
            result = prune(self.root, apply=True)
        self.assertEqual(result["expired_removed"], 0)
        self.assertEqual(len(probes), 2)
        self.assertTrue((old / "records").is_dir())
        self.assertEqual(list(old.parent.glob(".pruning-*")), [])

    def test_missing_or_unreadable_timestamps_fall_back_to_file_mtime(self):
        old = self.session("old", 0)
        for path in (old / "records").glob("*.json"):
            path.write_text("not json", encoding="utf-8")
            age(path, 60)
        young = self.session("young", 0)
        for path in (young / "records").glob("*.json"):
            path.write_text("{}", encoding="utf-8")
        result = prune(self.root)
        self.assertEqual(self.expired(result), [old.name])

    def test_unexpected_content_and_foreign_names_are_kept(self):
        extra = self.session("extra", 90)
        (extra / "notes.txt").write_text("x", encoding="utf-8")
        stray = extra.parent / "not-a-session"
        (stray / "records").mkdir(parents=True)
        (stray / "records" / ("a" * 64 + ".json")).write_text("{}", encoding="utf-8")
        age(stray / "records" / ("a" * 64 + ".json"), 90)
        result = prune(self.root, apply=True)
        self.assertEqual(result["expired"], [])
        self.assertIn({"entry": extra.name, "reason": "UNEXPECTED_CONTENT"}, result["skipped"])
        self.assertTrue((extra / "notes.txt").exists() and stray.exists())

    def test_linked_session_directory_is_never_followed(self):
        outside = self.root / "outside"
        (outside / "records").mkdir(parents=True)
        (outside / "records" / ("a" * 64 + ".json")).write_text("{}", encoding="utf-8")
        age(outside / "records" / ("a" * 64 + ".json"), 90)
        anchor = self.session("anchor", 0)
        link = anchor.parent / ("f" * 64)
        if not make_link(outside, link):
            self.skipTest("OS permits neither symlinks nor junctions")
        result = prune(self.root, apply=True)
        self.assertEqual(result["expired"], [])
        self.assertIn({"entry": link.name, "reason": "LINKED_STORAGE"}, result["skipped"])
        self.assertTrue((outside / "records" / ("a" * 64 + ".json")).exists())

    def test_an_interrupted_delete_leaves_a_leftover_a_later_run_reclaims(self):
        from unittest import mock
        old = self.session("stuck", 90)
        with mock.patch("provost_runtime.store.shutil.rmtree", side_effect=PermissionError("in use")):
            first = prune(self.root, apply=True)
        leftover = old.parent / (".pruning-" + old.name)
        self.assertEqual((first["expired_removed"], first["errors"]),
                         (0, [{"session_dir": old.name, "error": "PermissionError", "left_at": leftover.name}]))
        self.assertTrue(leftover.is_dir())
        dry = prune(self.root)
        self.assertEqual(dry["leftovers"], [{"staging_dir": leftover.name, "state": "reclaimable"}])
        second = prune(self.root, apply=True)
        self.assertEqual((second["reclaimed"], second["errors"]), (1, []))
        self.assertFalse(leftover.exists())

    def test_a_fresh_leftover_is_kept(self):
        fresh = self.session("fresh", 0)
        leftover = fresh.parent / (".pruning-" + "d" * 64)
        fresh.rename(leftover)
        result = prune(self.root, apply=True)
        self.assertEqual((result["leftovers"], result["reclaimed"]),
                         ([{"staging_dir": leftover.name, "state": "kept"}], 0))
        self.assertTrue(leftover.is_dir())

    def test_legacy_stores_are_never_touched(self):
        old = self.session("old", 90)
        legacy = self.root / ".provost" / "handoffs" / "v1" / "sessions" / old.name
        (legacy / "records").mkdir(parents=True)
        for target in (legacy / "records" / ("a" * 64 + ".json"), legacy / "binding.json"):
            target.write_text("{}", encoding="utf-8")
            age(target, 400)

        def listing():
            base = self.root / ".provost"
            return sorted((p.relative_to(base).as_posix(), p.stat().st_mtime_ns) for p in base.rglob("*"))

        before = listing()
        result = prune(self.root, apply=True)
        self.assertEqual(result["expired_removed"], 1)
        self.assertEqual(listing(), before)


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
        for bad in ("0", "abc", "1.5"):
            code, data = self.run_cli("prune", "--workspace", str(self.root), "--older-than-days", bad)
            self.assertEqual((code, data), (1, {"error": "INVALID_ARGUMENTS"}), bad)

    def test_doctor_reports_null_stale_count_for_a_linked_store(self):
        outside = self.root / "outside-sessions"
        outside.mkdir()
        parent = session_dir(self.root, "any").parent.parent
        parent.mkdir(parents=True)
        if not make_link(outside, parent / "sessions"):
            self.skipTest("OS permits neither symlinks nor junctions")
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual((code, data["stale_sessions"]), (0, None))

    def test_doctor_reports_stale_session_count(self):
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual(data["stale_sessions"], 0)
        self.aged("old", 30)
        self.aged("young", 1)
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual((code, data["stale_sessions"]), (0, 1))

    def expired_session(self, name, days):
        bind(self.root, name, {})
        record(self.root, name, self.payload(), None, "r1")
        backdate(session_dir(self.root, name), days)

    def test_cli_prune_lists_expired_sessions_and_apply_removes_them(self):
        self.expired_session("old", 45)
        code, data = self.run_cli("prune", "--workspace", str(self.root))
        self.assertEqual((code, [e["session_dir"] for e in data["expired"]]), (0, [digest("old")]))
        self.assertTrue(session_dir(self.root, "old").exists())
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--apply", "--session", "old")
        self.assertEqual((code, data["expired_removed"]), (0, 0))
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--apply")
        self.assertEqual((code, data["expired_removed"]), (0, 1))
        self.assertFalse(session_dir(self.root, "old").exists())

    def test_cli_binding_only_default_stays_14_days_beside_the_30_day_expiry(self):
        self.aged("bound", 20)
        self.expired_session("mid", 20)
        code, data = self.run_cli("prune", "--workspace", str(self.root))
        self.assertEqual((len(data["candidates"]), data["older_than_days"], data["expired"]), (1, 14, []))

    def test_doctor_reports_expired_session_count_and_never_deletes(self):
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual(data["expired_sessions"], 0)
        self.expired_session("old", 45)
        self.expired_session("young", 1)
        code, data = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual((code, data["expired_sessions"], data["stale_sessions"]), (0, 1, 0))
        self.assertTrue(session_dir(self.root, "old").exists())
