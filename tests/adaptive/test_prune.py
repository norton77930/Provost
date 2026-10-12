import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind
from provost_runtime.store import StoreError, digest, prune, record, session_dir, snapshot as store_snapshot

# A directory created where a regular file stands: Windows reports the file as existing,
# POSIX reports the path as not a directory.
BLOCKED_DIR_ERROR = "FileExistsError" if os.name == "nt" else "NotADirectoryError"

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


ACTIVITY_FILES = ("binding.json", "compaction.json", "current.json", "latest.json", "horizon.json")


def age_activity(directory, days):
    """Age the session-level files whose mtime counts as activity."""
    for name in ACTIVITY_FILES:
        if (directory / name).exists():
            age(directory / name, days)


def backdate(directory, days):
    """Move every activity stamp of a session dir days into the past: stored and file
    mtimes of records/snapshots, and the session-level files' mtimes. Stored stamps
    keep their order and the files stay checksum-valid."""
    from provost_runtime.store import _seal
    for kind, keys in (("records", ("recorded_at",)), ("snapshots", ("recorded_at", "captured_at"))):
        for path in (directory / kind).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("checksum", None)
            for key in keys:
                data[key] = (datetime.fromisoformat(data[key]) - timedelta(days=days)).isoformat()
            path.write_text(json.dumps(_seal(data)), encoding="utf-8")
            age(path, days)
    age_activity(directory, days)


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

    def test_expiry_default_is_30_days_and_only_its_own_age_changes_it(self):
        mid = self.session("mid", 20)
        self.assertEqual(prune(self.root)["expired"], [])
        self.assertEqual(prune(self.root, older_than_days=10)["expired"], [])
        self.assertEqual(self.expired(prune(self.root, expired_older_than_days=10)), [mid.name])
        from provost_runtime.store import StoreError
        for bad in (0, -1, True):
            with self.assertRaises(StoreError):
                prune(self.root, expired_older_than_days=bad)

    def test_recent_session_file_activity_keeps_old_records_from_expiring(self):
        for name in ACTIVITY_FILES:
            with self.subTest(name=name):
                resumed = self.session("r-" + name, 40, snapshot=True)
                age(resumed / name, 0) if (resumed / name).exists() else (resumed / name).write_text("{}")
                result = prune(self.root, apply=True)
                self.assertNotIn(resumed.name, self.expired(result))
                self.assertTrue((resumed / "records").is_dir())

    def test_current_session_is_never_removed_even_when_expired(self):
        mine = self.session("mine", 90)
        result = prune(self.root, keep_session="mine", apply=True)
        self.assertEqual((result["expired"], result["expired_removed"]), ([], 0))
        self.assertTrue(mine.is_dir())

    def test_current_sessions_leftover_staging_dir_is_never_reclaimed(self):
        mine = self.session("mine", 90)
        leftover = mine.parent / (".pruning-" + mine.name)
        mine.rename(leftover)
        result = prune(self.root, keep_session="mine", apply=True)
        self.assertEqual((result["leftovers"], result["reclaimed"]),
                         ([{"staging_dir": leftover.name, "state": "kept"}], 0))
        self.assertTrue((leftover / "records").is_dir())

    def forge_foreign(self, directory, name):
        """Rewrite one stored file of a session with another repo's workspace_id, keeping its age."""
        path = directory / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["workspace_id"] = "f" * 64
        info = path.stat()
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (info.st_atime, info.st_mtime))

    def test_a_session_of_another_repo_is_skipped_never_deleted(self):
        rec = self.session("x-rec", 90, snapshot=True)
        self.forge_foreign(rec, next((rec / "records").glob("*.json")).relative_to(rec))
        snap = self.session("x-snap", 90, snapshot=True)
        self.forge_foreign(snap, next((snap / "snapshots").glob("*.json")).relative_to(snap))
        bound = self.session("x-bind", 90)
        self.forge_foreign(bound, "binding.json")
        parked = self.session("x-park", 90)
        self.forge_foreign(parked, "binding.json")
        leftover = parked.parent / (".pruning-" + parked.name)
        parked.rename(leftover)
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired"], result["expired_removed"], result["reclaimed"]), ([], 0, 0))
        for foreign in (rec, snap, bound):
            self.assertIn({"entry": foreign.name, "reason": "FOREIGN_REPO"}, result["skipped"])
            self.assertTrue((foreign / "records").is_dir())
        self.assertEqual(result["leftovers"], [{"staging_dir": leftover.name, "state": "kept"}])
        self.assertTrue((leftover / "records").is_dir())

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
        age_activity(old, 60)
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
        self.assertEqual(dry["leftovers"], [{"staging_dir": leftover.name, "state": "reclaimable",
                                             "archive": ["fixture"]}])
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


class ArchivePruneTests(FixtureCase):
    """An expired session's newest record per work_id survives in the repo-level archive."""

    def session(self, name, age_days, *works, snapshot=False):
        """A session recording one record per (work_id, next) in order, stamped age_days ago."""
        bind(self.root, name, {})
        previous = None
        for index, (work, step) in enumerate(works):
            previous = record(self.root, name, dict(self.payload(step), work_id=work), previous,
                              "r%d" % index)["revision"]
        if snapshot:
            store_snapshot(self.root, name, "e1")
        directory = session_dir(self.root, name)
        backdate(directory, age_days)
        return directory

    def archived(self, work_id):
        import hashlib
        name = hashlib.sha256(work_id.encode("utf-8")).hexdigest()[:32] + ".json"
        return session_dir(self.root, "any").parent.parent / "archive" / name

    def stored(self, directory, step):
        """The stored record whose payload.next is step, as parsed JSON."""
        for path in (directory / "records").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["payload"]["next"] == step:
                return data
        raise AssertionError(step)

    def test_apply_archives_the_newest_record_of_each_work_id_before_removal(self):
        from unittest import mock
        from provost_runtime import store
        old = self.session("old", 40, ("alpha", "a1"), ("beta", "b1"), ("alpha", "a2"))
        expected = {"alpha": self.stored(old, "a2"), "beta": self.stored(old, "b1")}
        real_remove = store._remove_expired
        seen = []

        def remove_after_archive(*args):
            seen.append(all(self.archived(work).is_file() for work in expected))
            return real_remove(*args)

        with mock.patch.object(store, "_remove_expired", remove_after_archive):
            result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"], result["skipped"]), (1, [], []))
        self.assertEqual(result["expired"][0]["archive"], ["alpha", "beta"])
        self.assertEqual(seen, [True])
        self.assertFalse(old.exists())
        for work, data in expected.items():
            self.assertEqual(json.loads(self.archived(work).read_text(encoding="utf-8")), data)

    def test_dry_run_lists_the_work_ids_and_writes_nothing(self):
        old = self.session("old", 40, ("alpha", "a1"), ("beta", "b1"))
        before = sorted(p.relative_to(self.store_home) for p in self.store_home.rglob("*"))
        dry = prune(self.root)
        self.assertEqual([(e["session_dir"], e["archive"]) for e in dry["expired"]], [(old.name, ["alpha", "beta"])])
        self.assertEqual(sorted(p.relative_to(self.store_home) for p in self.store_home.rglob("*")), before)
        self.assertFalse(self.archived("alpha").parent.exists())

    def test_newer_records_replace_older_archives_and_older_ones_never_do(self):
        self.session("older", 60, ("alpha", "older"))
        prune(self.root, expired_older_than_days=50, apply=True)
        self.assertEqual(json.loads(self.archived("alpha").read_text(encoding="utf-8"))["payload"]["next"], "older")
        newer = self.session("newer", 40, ("alpha", "newer"))
        expected = self.stored(newer, "newer")
        prune(self.root, apply=True)
        self.assertEqual(json.loads(self.archived("alpha").read_text(encoding="utf-8")), expected)
        ancient = self.session("ancient", 90, ("alpha", "ancient"))
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["expired"][0]["archive"]), (1, ["alpha"]))
        self.assertFalse(ancient.exists())
        self.assertEqual(json.loads(self.archived("alpha").read_text(encoding="utf-8")), expected)

    def test_a_failed_archive_keeps_the_session(self):
        from unittest import mock
        old = self.session("old", 40, ("alpha", "a1"))
        archive = self.archived("alpha").parent
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text("not a dir", encoding="utf-8")
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"]),
                         (0, [{"session_dir": old.name, "error": BLOCKED_DIR_ERROR, "left_at": None}]))
        self.assertTrue((old / "records").is_dir())
        archive.unlink()
        with mock.patch("provost_runtime.store.atomic_write", side_effect=StoreError("WRITE_FAILED")):
            result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["skipped"]),
                         (0, [{"entry": old.name, "reason": "WRITE_FAILED"}]))
        self.assertTrue((old / "records").is_dir())
        self.archived("alpha").write_text("{}", encoding="utf-8")
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["skipped"]),
                         (0, [{"entry": old.name, "reason": "CORRUPT_ARCHIVE"}]))
        self.assertEqual(self.archived("alpha").read_text(encoding="utf-8"), "{}")

    def test_invalid_records_are_skipped_and_listed_without_blocking_removal(self):
        old = self.session("old", 40, ("alpha", "a1"), ("beta", "b1"))
        path = next(p for p in (old / "records").glob("*.json")
                    if json.loads(p.read_text(encoding="utf-8"))["payload"]["work_id"] == "beta")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["payload"]["next"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
        age(path, 40)
        dry = prune(self.root)
        item = dry["expired"][0]
        self.assertEqual((item["archive"], item["archive_skipped"]),
                         (["alpha"], [{"file": "records/" + path.name, "reason": "CORRUPT_RECORD"}]))
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"]), (1, []))
        self.assertTrue(self.archived("alpha").is_file())
        self.assertFalse(self.archived("beta").exists())

    def test_a_session_with_only_invalid_records_is_removed_with_nothing_archived(self):
        old = self.session("old", 40, ("alpha", "a1"))
        path = next((old / "records").glob("*.json"))
        path.write_text("not json", encoding="utf-8")
        age(path, 40)
        result = prune(self.root, apply=True)
        item = result["expired"][0]
        self.assertEqual((item["archive"], item["archive_skipped"]),
                         ([], [{"file": "records/" + path.name, "reason": "CORRUPT_RECORD"}]))
        self.assertEqual((result["expired_removed"], result["errors"], result["skipped"]), (1, [], []))
        self.assertFalse(old.exists())
        self.assertFalse(self.archived("alpha").parent.exists())

    def test_a_failed_archive_keeps_the_leftover(self):
        from unittest import mock
        old = self.session("parked", 40, ("alpha", "a1"))
        leftover = old.parent / (".pruning-" + old.name)
        old.rename(leftover)
        archive = self.archived("alpha").parent
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text("not a dir", encoding="utf-8")
        result = prune(self.root, apply=True)
        self.assertEqual((result["reclaimed"], result["errors"]),
                         (0, [{"session_dir": leftover.name, "error": BLOCKED_DIR_ERROR, "left_at": leftover.name}]))
        self.assertTrue((leftover / "records").is_dir())
        archive.unlink()
        with mock.patch("provost_runtime.store.atomic_write", side_effect=StoreError("WRITE_FAILED")):
            result = prune(self.root, apply=True)
        self.assertEqual((result["reclaimed"], result["skipped"]),
                         (0, [{"entry": leftover.name, "reason": "WRITE_FAILED"}]))
        self.assertTrue((leftover / "records").is_dir())

    def test_a_snapshot_keeps_a_trimmed_record_archivable(self):
        bind(self.root, "trim", {})
        first = record(self.root, "trim", dict(self.payload("a1"), work_id="alpha"), None, "r0")
        store_snapshot(self.root, "trim", "e1")
        previous = first["revision"]
        for index in range(1, 6):
            previous = record(self.root, "trim", dict(self.payload("b%d" % index), work_id="beta"), previous,
                              "r%d" % index)["revision"]
        old = session_dir(self.root, "trim")
        self.assertFalse((old / "records" / (first["revision"] + ".json")).exists())
        backdate(old, 40)
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired"][0]["archive"], result["expired_removed"]), (["alpha", "beta"], 1))
        archived = json.loads(self.archived("alpha").read_text(encoding="utf-8"))
        shifted = (datetime.fromisoformat(first["recorded_at"]) - timedelta(days=40)).isoformat()
        from provost_runtime.store import _seal
        self.assertEqual(archived, _seal({k: v for k, v in first.items() if k != "checksum"} | {"recorded_at": shifted}))

    def test_equal_recorded_at_prefers_the_chain_newer_record_over_the_larger_revision(self):
        from provost_runtime.store import _seal
        bind(self.root, "tie", {})
        # Request ids whose child revision sorts below its parent's, so revision order would pick the parent.
        first, second = next((a, b) for a, b in (("r%d" % i, "r%d" % (i + 1)) for i in range(100))
                             if digest("tie\0record\0" + b) < digest("tie\0record\0" + a))
        parent = record(self.root, "tie", dict(self.payload("parent"), work_id="alpha"), None, first)
        child = record(self.root, "tie", dict(self.payload("child"), work_id="alpha"), parent["revision"], second)
        old = session_dir(self.root, "tie")
        for data in (parent, child):
            path = old / "records" / (data["revision"] + ".json")
            body = {k: v for k, v in data.items() if k != "checksum"} | {"recorded_at": parent["recorded_at"]}
            path.write_text(json.dumps(_seal(body)), encoding="utf-8")
        backdate(old, 40)
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["expired"][0]["archive"]), (1, ["alpha"]))
        self.assertEqual(json.loads(self.archived("alpha").read_text(encoding="utf-8"))["payload"]["next"], "child")

    def test_an_expired_leftover_is_archived_before_it_is_reclaimed(self):
        old = self.session("parked", 40, ("alpha", "a1"))
        expected = self.stored(old, "a1")
        leftover = old.parent / (".pruning-" + old.name)
        old.rename(leftover)
        dry = prune(self.root)
        self.assertEqual(dry["leftovers"], [{"staging_dir": leftover.name, "state": "reclaimable", "archive": ["alpha"]}])
        self.assertFalse(self.archived("alpha").exists())
        result = prune(self.root, apply=True)
        self.assertEqual((result["reclaimed"], result["errors"]), (1, []))
        self.assertFalse(leftover.exists())
        self.assertEqual(json.loads(self.archived("alpha").read_text(encoding="utf-8")), expected)


class FactsPruneTests(FixtureCase):
    """facts.json is ordinary session content; a pruned session takes its facts-cache files along."""

    def setUp(self):
        super().setUp()
        self.cache = self.root / "cache" / "facts"
        self.cache.mkdir(parents=True)

    def cache_files(self, session):
        names = [session + ".jsonl", session + ".jsonl.folding-" + "a" * 32,
                 session + ".pending-edit", session + ".pending-dispatch", session + ".reminded",
                 session + ".enabled"]
        for name in names:
            (self.cache / name).write_text("x", encoding="utf-8")
        return sorted(str(self.cache / name) for name in names)

    def expired_session(self, name, days):
        bind(self.root, name, {})
        record(self.root, name, self.payload(), None, "r1")
        self.seed_facts(session=name)
        directory = session_dir(self.root, name)
        backdate(directory, days)
        age(directory / "facts.json", days)
        return directory

    def test_expired_session_with_facts_is_listed_with_its_cache_and_removed_together(self):
        old = self.expired_session("old", 40)
        fresh = self.expired_session("fresh", 2)
        listed = self.cache_files("old")
        kept = self.cache_files("fresh") + [str(self.cache / "old2.jsonl"), str(self.cache / "s-old.jsonl")]
        for path in kept[-2:]:
            Path(path).write_text("x", encoding="utf-8")
        dry = prune(self.root, facts_cache=self.cache)
        self.assertEqual([(e["session_dir"], sorted(e["facts_cache"])) for e in dry["expired"]], [(old.name, listed)])
        self.assertTrue(all(Path(p).exists() for p in listed))
        result = prune(self.root, facts_cache=self.cache, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"]), (1, []))
        self.assertFalse(old.exists())
        self.assertEqual([p for p in listed if Path(p).exists()], [])
        self.assertTrue(fresh.is_dir() and all(Path(p).exists() for p in kept))

    def test_binding_only_candidate_takes_its_cache(self):
        bind(self.root, "gone", {})
        age(session_dir(self.root, "gone") / "binding.json", 30)
        listed = self.cache_files("gone")
        dry = prune(self.root, facts_cache=self.cache)
        self.assertEqual(sorted(dry["candidates"][0]["facts_cache"]), listed)
        result = prune(self.root, facts_cache=self.cache, apply=True)
        self.assertEqual((result["removed"], result["errors"]), (1, []))
        self.assertEqual([p for p in listed if Path(p).exists()], [])

    def test_cache_is_kept_without_a_matching_session_id_or_cache_dir(self):
        bind(self.root, "named", {})
        path = session_dir(self.root, "named") / "binding.json"
        path.write_text(json.dumps(dict(json.loads(path.read_text(encoding="utf-8")), session_id="other")),
                        encoding="utf-8")
        age(path, 30)
        listed = self.cache_files("other")
        dry = prune(self.root, facts_cache=self.cache)
        self.assertNotIn("facts_cache", dry["candidates"][0])
        prune(self.root, facts_cache=self.cache, apply=True)
        self.assertTrue(all(Path(p).exists() for p in listed))

    def test_failed_delete_keeps_the_cache_until_the_leftover_is_reclaimed(self):
        from unittest import mock
        old = self.expired_session("stuck", 90)
        listed = self.cache_files("stuck")
        with mock.patch("provost_runtime.store.shutil.rmtree", side_effect=PermissionError("in use")):
            first = prune(self.root, facts_cache=self.cache, apply=True)
        self.assertEqual(first["expired_removed"], 0)
        self.assertTrue(all(Path(p).exists() for p in listed))
        dry = prune(self.root, facts_cache=self.cache)
        self.assertEqual(sorted(dry["leftovers"][0]["facts_cache"]), listed)
        second = prune(self.root, facts_cache=self.cache, apply=True)
        self.assertEqual((second["reclaimed"], second["errors"]), (1, []))
        self.assertFalse((old.parent / (".pruning-" + old.name)).exists())
        self.assertEqual([p for p in listed if Path(p).exists()], [])

    def unrecorded_session(self, name, days, kind):
        """A bound session holding only facts.json or compaction.json, its files aged days."""
        bind(self.root, name, {})
        directory = session_dir(self.root, name)
        if kind == "facts":
            self.seed_facts(session=name)
        else:
            (directory / "compaction.json").write_text(json.dumps({"observed": True, "session_id": name}),
                                                       encoding="utf-8")
        for path in directory.iterdir():
            age(path, days)
        return directory

    def test_unrecorded_facts_or_compaction_sessions_expire_on_their_newest_file(self):
        for kind in ("facts", "compaction"):
            with self.subTest(kind=kind):
                old = self.unrecorded_session(kind + "-old", 40, kind)
                young = self.unrecorded_session(kind + "-young", 2, kind)
                recorded = self.expired_session(kind + "-recorded", 40)
                listed = self.cache_files(kind + "-old")
                dry = prune(self.root, facts_cache=self.cache)
                items = {e["session_dir"]: e for e in dry["expired"]}
                self.assertEqual(sorted(items), sorted([old.name, recorded.name]))
                self.assertEqual((items[old.name]["records"], items[old.name]["snapshots"]), (0, 0))
                self.assertEqual(items[recorded.name]["records"], 1)
                self.assertEqual(sorted(items[old.name]["facts_cache"]), listed)
                self.assertTrue(old.is_dir() and all(Path(p).exists() for p in listed))
                self.assertNotIn("orphan_facts_cache", dry)
                result = prune(self.root, facts_cache=self.cache, apply=True)
                self.assertEqual((result["expired_removed"], result["errors"]), (2, []))
                self.assertFalse(old.exists() or recorded.exists())
                self.assertEqual([p for p in listed if Path(p).exists()], [])
                self.assertTrue(young.is_dir())
                self.assertNotIn("orphan_facts_cache", result)

    def test_orphan_cache_files_older_than_the_expiry_are_listed_and_removed_only_with_apply(self):
        old = self.cache_files("gone")
        for path in old:
            age(Path(path), 40)
        young = self.cache_files("recent")
        for path in young:
            age(Path(path), 2)
        bind(self.root, "live", {})
        bound = self.cache_files("live")
        kept = self.cache_files("current")
        unsafe = [self.cache / "bad name.jsonl", self.cache / "x.unknown"]
        for path in [*map(Path, bound + kept), *unsafe]:
            path.write_text("x", encoding="utf-8")
            age(path, 40)
        linked = self.cache / "linked.jsonl"
        target = self.root / "elsewhere"
        target.mkdir()
        has_link = make_link(target, linked)
        dry = prune(self.root, facts_cache=self.cache, keep_session="current")
        orphans = dry["orphan_facts_cache"]
        self.assertEqual([(c["session_id"], sorted(c["files"])) for c in orphans["candidates"]], [("gone", old)])
        self.assertLess(datetime.fromisoformat(orphans["candidates"][0]["newest"]).timestamp(), time.time() - 39 * DAY)
        reasons = {Path(s["path"]).name: s["reason"] for s in orphans["skipped"]}
        self.assertEqual({k: v for k, v in reasons.items() if k != "linked.jsonl"},
                         {"bad name.jsonl": "UNEXPECTED_NAME", "x.unknown": "UNEXPECTED_NAME"})
        if has_link:
            self.assertEqual(reasons["linked.jsonl"], "LINKED_STORAGE")
        self.assertEqual((orphans["removed"], orphans["errors"]), (0, []))
        self.assertTrue(all(Path(p).exists() for p in old))
        result = prune(self.root, facts_cache=self.cache, keep_session="current", apply=True)
        self.assertEqual((result["orphan_facts_cache"]["removed"], result["orphan_facts_cache"]["errors"]), (6, []))
        self.assertEqual([p for p in old if Path(p).exists()], [])
        self.assertTrue(all(p.exists() for p in [*map(Path, young + bound + kept), *unsafe, target]))
        self.assertTrue(session_dir(self.root, "live").is_dir())


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

    def test_cli_prune_lists_and_removes_the_facts_cache_beside_the_config(self):
        self.aged("old", 30)
        cache = self.config_path.parent / "cache" / "facts"
        cache.mkdir(parents=True)
        spool, other = cache / "old.jsonl", cache / "new.jsonl"
        for path in (spool, other):
            path.write_text("x", encoding="utf-8")
        code, data = self.run_cli("prune", "--workspace", str(self.root))
        self.assertEqual((code, data["candidates"][0]["facts_cache"]), (0, [str(spool)]))
        self.assertTrue(spool.exists())
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--apply")
        self.assertEqual((code, data["removed"]), (0, 1))
        self.assertFalse(spool.exists())
        self.assertTrue(other.exists())

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

    def test_cli_older_than_days_is_binding_only_and_expiry_has_its_own_flag(self):
        self.expired_session("ten", 10)
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--older-than-days", "3", "--apply")
        self.assertEqual((code, data["expired"], data["older_than_days"], data["expired_older_than_days"]),
                         (0, [], 3, 30))
        self.assertTrue(session_dir(self.root, "ten").exists())
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--expired-older-than-days", "3", "--apply")
        self.assertEqual((code, data["expired_removed"], data["older_than_days"], data["expired_older_than_days"]),
                         (0, 1, 14, 3))
        self.assertFalse(session_dir(self.root, "ten").exists())
        code, data = self.run_cli("prune", "--workspace", str(self.root), "--expired-older-than-days", "0")
        self.assertEqual((code, data), (1, {"error": "INVALID_ARGUMENTS"}))

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
