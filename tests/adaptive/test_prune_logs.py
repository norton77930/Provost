"""prune: no archive, idle done/orphan log cleanup, the unmigrated guard and migrate backup listing (#26)."""
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock
from _support import BASH, FixtureCase, REPO
from provost_runtime import migrate, store
from provost_runtime.config import handoff_home
from provost_runtime.hooks import bind
from provost_runtime.store import digest, prune, session_dir

RUNTIME = REPO / "hooks" / "runtime"
LONG = "Keep the parser API stable because two callers pin its signature"
DAY = 86400


def shift(directory, days):
    """Move a session dir days into the past: stored record/snapshot times (resealed) and every file mtime."""
    for kind, keys in (("records", ("recorded_at",)), ("snapshots", ("recorded_at", "captured_at"))):
        for path in (directory / kind).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("checksum")
            for key in keys:
                data[key] = (datetime.fromisoformat(data[key]) - timedelta(days=days)).isoformat()
            path.write_text(json.dumps(store._seal(data)), encoding="utf-8")
    stamp = datetime.now(timezone.utc).timestamp() - days * DAY
    for path in directory.rglob("*"):
        os.utime(path, (stamp, stamp))


class IdleLogPruneTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.requests = 0

    def write(self, work, phase="implementation", session="s1", decisions=(LONG,)):
        """Record a card of work on its chain in session, logging the given decisions."""
        bind(self.root, session, {})
        head = store.current(self.root, session, work)
        self.requests += 1
        request = dict(self.payload(), work_id=work, phase=phase,
                       new_decisions=[{"text": text} for text in decisions])
        return store.record(self.root, session, request, head["revision"] if head else None, "r%d" % self.requests)

    def paths(self, work):
        return store._log_paths(store.as_workspace(self.root), work)

    def backdate(self, work, days, session="s1", log=True):
        """Shift the stored times of the session's cards (resealed) and, with log, of the work's log entries
        and the log file's mtime."""
        for path in (session_dir(self.root, session) / "records").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("checksum")
            data["recorded_at"] = (datetime.fromisoformat(data["recorded_at"]) - timedelta(days=days)).isoformat()
            path.write_text(json.dumps(store._seal(data)), encoding="utf-8")
        if log:
            path = self.paths(work)[0]
            header, *entries = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(entries):
                entry = json.loads(line)
                entry["at"] = (datetime.fromisoformat(entry["at"]) - timedelta(days=days)).isoformat()
                entries[index] = json.dumps(entry)
            path.write_text("\n".join([header, *entries]) + "\n", encoding="utf-8")
            stamp = datetime.now(timezone.utc).timestamp() - days * DAY
            os.utime(path, (stamp, stamp))

    def logs(self, apply=False):
        return prune(self.root, apply=apply)["idle_logs"]

    def listed(self, result):
        return [(item["work_id"], item["kind"], item["removable"], item.get("reason")) for item in result["candidates"]]

    def orphan(self, work="gone", days=400):
        """A log of work whose only session dir is gone, its entries days old."""
        self.write(work, phase="done", session="gone-session")
        self.backdate(work, days, session="gone-session")
        shutil.rmtree(session_dir(self.root, "gone-session"))

    def test_a_done_work_idle_for_180_days_is_listed_and_its_log_removed_only_with_apply(self):
        self.write("A")
        self.write("A", phase="Done ")  # is_done ignores case and surrounding whitespace
        self.backdate("A", 200)
        log, lock = self.paths("A")
        dry = self.logs()
        self.assertEqual(self.listed(dry), [("A", "done", True, None)])
        self.assertLess(datetime.fromisoformat(dry["candidates"][0]["idle_since"]),
                        datetime.now(timezone.utc) - timedelta(days=199))
        self.assertEqual((dry["removed"], dry["older_than_days"]), (0, 180))
        self.assertTrue(log.is_file())
        result = self.logs(apply=True)
        self.assertEqual((result["removed"], result["removed_work_ids"], result["errors"]), (1, ["A"], []))
        self.assertFalse(log.exists())
        self.assertTrue(lock.is_file())  # the lock file is never deleted
        self.assertIsNotNone(store.current(self.root, "s1", "A"))  # cards are not touched

    def test_open_or_recent_works_are_untouched(self):
        self.write("open")
        self.backdate("open", 400)
        self.write("fresh-card", phase="done")
        self.write("fresh-log", phase="done", session="s2")
        self.backdate("fresh-log", 400, session="s2", log=False)  # its log entries are new
        result = self.logs(apply=True)
        self.assertEqual((result["candidates"], result["removed"]), ([], 0))
        for work in ("open", "fresh-card", "fresh-log"):
            self.assertTrue(self.paths(work)[0].is_file(), work)

    def test_the_newest_card_across_sessions_decides(self):
        self.write("A", phase="done", session="s1")
        self.write("A", phase="implementation", session="s2")
        self.backdate("A", 300, session="s1")
        self.backdate("A", 200, session="s2", log=False)
        self.assertEqual(self.logs()["candidates"], [])  # s2's newer card is open

    def recheck(self, change):
        """prune --apply of done work A, with change() run between the listing and the log lock."""
        self.write("A", phase="done")
        self.backdate("A", 200)
        real, pending = store._log_lock, [change]

        @contextmanager
        def change_first(ws, work_id):
            if pending:  # once: a change that appends takes this lock itself
                pending.pop()()
            with real(ws, work_id):
                yield

        with mock.patch.object(store, "_log_lock", change_first):
            return self.logs(apply=True)

    def test_a_reopen_between_listing_and_lock_keeps_the_log(self):
        result = self.recheck(lambda: self.write("A", phase="implementation", decisions=()))  # a card-only reopen
        self.assertEqual(self.listed(result), [("A", "done", True, None)])
        self.assertEqual((result["removed"], result["skipped"]), (0, [{"work_id": "A", "reason": "OPEN"}]))
        self.assertTrue(self.paths("A")[0].is_file())

    def test_a_new_entry_between_listing_and_lock_keeps_the_log(self):
        result = self.recheck(lambda: self.write("A", phase="done", session="s2", decisions=(LONG + " again",)))
        self.assertEqual((result["removed"], result["skipped"]), (0, [{"work_id": "A", "reason": "RECENT"}]))
        self.assertTrue(self.paths("A")[0].is_file())

    def test_a_log_gone_between_listing_and_lock_is_not_an_error(self):
        result = self.recheck(lambda: self.paths("A")[0].unlink())
        self.assertEqual(self.listed(result), [("A", "done", True, None)])
        self.assertEqual((result["removed"], result["skipped"], result["errors"]), (0, [], []))

    def test_a_leftover_appearing_between_listing_and_lock_keeps_the_log(self):
        leftover = session_dir(self.root, "any").parent / (".pruning-" + "c" * 64)
        result = self.recheck(lambda: leftover.mkdir(parents=True))
        self.assertEqual((result["removed"], result["skipped"]), (0, [{"work_id": "A", "reason": "LEFTOVER_PENDING"}]))
        self.assertTrue(self.paths("A")[0].is_file())

    def test_a_schema1_card_appearing_between_listing_and_lock_keeps_the_log(self):
        def schema1_card():
            bind(self.root, "late", {})
            self.record_v1(self.root, "late", dict(self.payload_v1(decisions=["Late decision"]), work_id="A",
                                                   phase="done"), None, "v1")
            self.backdate("A", 300, session="late", log=False)
        result = self.recheck(schema1_card)
        self.assertEqual((result["removed"], result["skipped"]), (0, [{"work_id": "A", "reason": "SCHEMA1_SOURCES"}]))

    def test_an_idle_orphan_log_is_removed_only_with_apply(self):
        self.orphan()
        self.assertEqual(self.listed(self.logs()), [("gone", "orphan", True, None)])
        self.assertTrue(self.paths("gone")[0].is_file())
        result = self.logs(apply=True)
        self.assertEqual((result["removed"], result["removed_work_ids"]), (1, ["gone"]))
        self.assertFalse(self.paths("gone")[0].exists())

    def test_a_recent_orphan_or_header_only_log_is_kept(self):
        self.orphan("young", days=10)
        log = self.paths("empty")[0]
        log.write_text(json.dumps({"kind": "log", "schema_version": 1, "workspace_id": store.as_workspace(
            self.root).repo_key, "work_id": "empty"}) + "\n", encoding="utf-8")
        self.assertEqual(self.logs(apply=True)["candidates"], [])  # header-only: its fresh mtime counts
        stamp = datetime.now(timezone.utc).timestamp() - 200 * DAY
        os.utime(log, (stamp, stamp))
        self.assertEqual(self.listed(self.logs()), [("empty", "orphan", True, None)])

    def assert_kept(self, reason):
        dry = self.logs()
        self.assertEqual(self.listed(dry), [("gone", "orphan", False, reason)])
        result = self.logs(apply=True)
        self.assertEqual((result["removed"], result["errors"]), (0, []))
        self.assertTrue(self.paths("gone")[0].is_file())

    def test_an_unreadable_session_keeps_every_log(self):
        self.orphan()
        records = session_dir(self.root, "broken-session") / "records"
        records.mkdir(parents=True)
        (records / ("a" * 64 + ".json")).write_text("not json", encoding="utf-8")
        self.assert_kept("SESSIONS_UNREADABLE")

    def test_a_pruning_leftover_keeps_every_log(self):
        self.orphan()
        (session_dir(self.root, "any").parent / (".pruning-" + "b" * 64)).mkdir(parents=True)
        self.assert_kept("LEFTOVER_PENDING")

    def test_a_schema1_card_with_decisions_keeps_the_log(self):
        self.orphan()
        bind(self.root, "old", {})
        self.record_v1(self.root, "old", dict(self.payload_v1(decisions=["Old decision"]), work_id="gone",
                                              phase="done"), None, "v1")
        self.backdate("gone", 400, session="old", log=False)
        self.assertEqual(self.listed(self.logs()), [("gone", "done", False, "SCHEMA1_SOURCES")])
        self.assertEqual(self.logs(apply=True)["removed"], 0)

    def test_a_schema1_archive_file_keeps_the_log(self):
        self.orphan()
        bind(self.root, "archived", {})
        card = self.record_v1(self.root, "archived", dict(self.payload_v1(decisions=["Archived decision"]),
                                                          work_id="gone", phase="done"), None, "v1")
        base = store._base(store.as_workspace(self.root))
        store.atomic_write(handoff_home(), store._archive_path(base / "archive", "gone"), card)
        shutil.rmtree(session_dir(self.root, "archived"))
        self.assert_kept("SCHEMA1_SOURCES")

    def test_a_migrated_log_keeps_its_grace_across_prune_runs(self):
        bind(self.root, "old", {})
        self.record_v1(self.root, "old", dict(self.payload_v1(decisions=["Decision only in a schema 1 card"]),
                                              work_id="legacy", phase="done"), None, "v1")
        shift(session_dir(self.root, "old"), 400)
        migrate.migrate(self.root, apply=True)
        self.assertLess(datetime.fromisoformat(store.read_log(self.root, "legacy")[0]["at"]),
                        datetime.now(timezone.utc) - timedelta(days=399))  # migrated entries keep their time
        first = prune(self.root, apply=True)
        self.assertEqual((first["expired_removed"], first["idle_logs"]["removed"]), (1, 0))
        second = prune(self.root, apply=True)["idle_logs"]
        self.assertEqual((second["candidates"], second["removed"]), ([], 0))  # the fresh file mtime counts
        self.assertTrue(self.paths("legacy")[0].is_file())

    def test_a_done_work_with_a_broken_chain_elsewhere_is_kept(self):
        self.write("A", phase="done", session="s1")
        self.write("A", session="s2")
        self.write("A", session="s2", decisions=())
        self.write("B", session="s2", decisions=())
        self.backdate("A", 300, session="s1")
        cards = sorted((session_dir(self.root, "s2") / "records").glob("*.json"))
        oldest = min(cards, key=lambda p: json.loads(p.read_text(encoding="utf-8"))["recorded_at"])
        oldest.unlink()  # s2's chain of A loses its first card: INCOMPLETE_CHAIN
        self.assertEqual(self.listed(self.logs()), [("A", "done", False, "INCOMPLETE_CHAIN")])
        self.assertEqual(self.logs(apply=True)["removed"], 0)
        self.assertTrue(self.paths("A")[0].is_file())


class SessionPruneTests(FixtureCase):
    def old_session(self, name, payload, version=2, days=40):
        bind(self.root, name, {})
        if version == 1:
            self.record_v1(self.root, name, payload, None, "r1")
        else:
            store.record(self.root, name, payload, None, "r1")
        directory = session_dir(self.root, name)
        shift(directory, days)
        return directory

    def test_an_expired_session_is_removed_without_writing_an_archive(self):
        directory = self.old_session("old", dict(self.payload(), work_id="alpha"))
        dry = prune(self.root)
        self.assertEqual(len(dry["expired"]), 1)
        self.assertNotIn("archive", dry["expired"][0])
        result = prune(self.root, apply=True)
        self.assertEqual(result["expired_removed"], 1)
        self.assertFalse(directory.exists())
        self.assertFalse((directory.parent.parent / "archive").exists())

    def test_an_expired_session_with_only_invalid_records_is_removed(self):
        directory = self.old_session("old", dict(self.payload(), work_id="alpha"))
        path = next((directory / "records").glob("*.json"))
        stamp = path.stat().st_mtime
        path.write_text("not json", encoding="utf-8")
        os.utime(path, (stamp, stamp))
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["errors"], result["skipped"]), (1, [], []))
        self.assertFalse(directory.exists())

    def test_unmigrated_schema1_decisions_keep_the_session_until_migrate_apply(self):
        directory = self.old_session("old", dict(self.payload_v1(decisions=["Decision only in a schema 1 card"]),
                                                 work_id="alpha"), version=1)
        plain = self.old_session("plain", dict(self.payload_v1(), work_id="beta"), version=1)  # nothing to import
        dry = prune(self.root)
        self.assertEqual([item["session_dir"] for item in dry["expired"]], [plain.name])
        self.assertIn({"entry": directory.name, "reason": "UNMIGRATED_DECISIONS"}, dry["skipped"])
        result = prune(self.root, apply=True)
        self.assertEqual(result["expired_removed"], 1)
        self.assertTrue(directory.is_dir())
        self.assertTrue(migrate.migrate(self.root, apply=True)["works"][0]["decisions"])
        result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["skipped"]), (1, []))
        self.assertFalse(directory.exists())

    def test_an_unmigrated_leftover_is_kept(self):
        directory = self.old_session("old", dict(self.payload_v1(evidence=[{"claim": "c", "ref": "t", "state": "fail"}]),
                                                 work_id="alpha"), version=1)
        leftover = directory.parent / (".pruning-" + directory.name)
        directory.rename(leftover)
        result = prune(self.root, apply=True)
        self.assertEqual((result["reclaimed"], result["skipped"]),
                         (0, [{"entry": leftover.name, "reason": "UNMIGRATED_DECISIONS"}]))
        self.assertTrue(leftover.is_dir())

    def test_the_removal_recheck_applies_the_unmigrated_guard(self):
        directory = self.old_session("old", dict(self.payload_v1(decisions=["Decision only in a schema 1 card"]),
                                                 work_id="alpha"), version=1)
        calls = []
        real = store._unmigrated_guard

        def lenient_listing(ws):
            held = real(ws)

            def guard_after_listing(scan):
                calls.append(scan.unmigrated)
                if len(calls) > 1:
                    held(scan)
            return guard_after_listing

        with mock.patch.object(store, "_unmigrated_guard", lenient_listing):
            result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["expired"], result["skipped"]),
                         (0, [], [{"entry": directory.name, "reason": "UNMIGRATED_DECISIONS"}]))
        self.assertTrue((directory / "records").is_dir())
        self.assertEqual(list(directory.parent.glob(".pruning-*")), [])


class MigrateDryRunTests(FixtureCase):
    def old_session(self, name, work, decisions):
        bind(self.root, name, {})
        self.record_v1(self.root, name, dict(self.payload_v1(decisions=decisions), work_id=work), None, "r1")
        directory = session_dir(self.root, name)
        shift(directory, 40)
        return directory

    def test_the_dry_run_runs_once_per_prune_and_writes_nothing(self):
        first = self.old_session("one", "alpha", ["Decision of alpha in a schema 1 card"])
        second = self.old_session("two", "beta", ["Decision of beta in a schema 1 card"])
        before = sorted(p.relative_to(self.store_home) for p in self.store_home.rglob("*"))
        with mock.patch.object(migrate, "migrate", wraps=migrate.migrate) as dry:
            result = prune(self.root)
        self.assertEqual(dry.call_count, 1)
        self.assertNotIn("apply", dry.call_args.kwargs)
        self.assertEqual(sorted(item["entry"] for item in result["skipped"]), sorted([first.name, second.name]))
        self.assertEqual(sorted(p.relative_to(self.store_home) for p in self.store_home.rglob("*")), before)

    def test_a_failed_dry_run_keeps_every_session_with_schema1_decisions(self):
        directory = self.old_session("one", "alpha", ["Decision of alpha in a schema 1 card"])
        migrate.migrate(self.root, apply=True)  # imported: normally removable
        with mock.patch.object(migrate, "migrate", side_effect=store.StoreError("CORRUPT_LOG")):
            result = prune(self.root, apply=True)
        self.assertEqual((result["expired_removed"], result["skipped"]),
                         (0, [{"entry": directory.name, "reason": "UNMIGRATED_DECISIONS"}]))
        self.assertEqual(prune(self.root, apply=True)["expired_removed"], 1)


class BackupListingTests(FixtureCase):
    def env(self):
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        return env

    def runtime(self, *args):
        result = subprocess.run([BASH, str(RUNTIME), "prune", "--workspace", str(self.root), *args],
                                capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def test_migrate_backups_are_listed_with_their_age_and_never_deleted(self):
        backups = store._base(store.as_workspace(self.root)) / "backups"
        old = backups / "logs-20260101T000000000000Z"
        old.mkdir(parents=True)
        (old / ("a" * 64 + ".jsonl")).write_text("", encoding="utf-8")
        (backups / "other").mkdir()
        for args in ((), ("--apply",)):
            code, result = self.runtime(*args)
            self.assertEqual(code, 0, result)
            self.assertEqual([item["dir"] for item in result["log_backups"]], [old.name])
            item = result["log_backups"][0]
            self.assertEqual(item["created_at"], "2026-01-01T00:00:00+00:00")
            self.assertGreaterEqual(item["age_days"], (datetime.now(timezone.utc)
                                                       - datetime(2026, 1, 1, tzinfo=timezone.utc)).days)
        self.assertTrue(old.is_dir())

    def test_cli_lists_idle_logs(self):
        bind(self.root, "s1", {})
        store.record(self.root, "s1", dict(self.payload(), work_id="A", phase="done",
                                           new_decisions=[{"text": LONG}]), None, "r1")
        code, result = self.runtime()
        self.assertEqual(code, 0, result)
        self.assertEqual((result["idle_logs"]["candidates"], result["log_backups"]), ([], []))
        self.assertEqual(digest("A") + ".jsonl", store._log_paths(store.as_workspace(self.root), "A")[0].name)
