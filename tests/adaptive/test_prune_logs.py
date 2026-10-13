"""prune: no archive, done-work log cleanup and migrate backup listing (#26)."""
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock
from _support import BASH, FixtureCase, REPO
from provost_runtime import store
from provost_runtime.config import handoff_home
from provost_runtime.hooks import bind
from provost_runtime.store import prune, session_dir

RUNTIME = REPO / "hooks" / "runtime"
LONG = "Keep the parser API stable because two callers pin its signature"


class DoneLogPruneTests(FixtureCase):
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
        """Shift the stored times of the session's cards (resealed) and, with log, of the work's log entries."""
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

    def done_logs(self, result):
        return result["done_logs"]

    def test_a_done_work_idle_for_180_days_is_listed_and_its_log_removed_only_with_apply(self):
        self.write("A")
        self.write("A", phase="done")
        self.backdate("A", 200)
        log, lock = self.paths("A")
        dry = self.done_logs(prune(self.root))
        self.assertEqual([item["work_id"] for item in dry["candidates"]], ["A"])
        self.assertLess(datetime.fromisoformat(dry["candidates"][0]["idle_since"]),
                        datetime.now(timezone.utc) - timedelta(days=199))
        self.assertEqual((dry["removed"], dry["older_than_days"]), (0, 180))
        self.assertTrue(log.is_file())
        result = self.done_logs(prune(self.root, apply=True))
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
        result = self.done_logs(prune(self.root, apply=True))
        self.assertEqual((result["candidates"], result["removed"]), ([], 0))
        for work in ("open", "fresh-card", "fresh-log"):
            self.assertTrue(self.paths(work)[0].is_file(), work)

    def test_the_newest_card_across_sessions_decides(self):
        self.write("A", phase="done", session="s1")
        self.write("A", phase="implementation", session="s2")
        self.backdate("A", 300, session="s1")
        self.backdate("A", 200, session="s2", log=False)
        self.assertEqual(self.done_logs(prune(self.root))["candidates"], [])  # s2's newer card is open

    def test_a_reopen_between_listing_and_lock_keeps_the_log(self):
        self.write("A", phase="done")
        self.backdate("A", 200)
        real = store._log_lock

        @contextmanager
        def reopen_first(ws, work_id):
            self.write("A", phase="implementation", decisions=())  # a card-only reopen
            with real(ws, work_id):
                yield

        with mock.patch.object(store, "_log_lock", reopen_first):
            result = self.done_logs(prune(self.root, apply=True))
        self.assertEqual([item["work_id"] for item in result["candidates"]], ["A"])
        self.assertEqual((result["removed"], result["skipped"]), (0, [{"work_id": "A", "reason": "OPEN"}]))
        self.assertTrue(self.paths("A")[0].is_file())

    def test_a_log_without_cards_is_an_orphan_listed_never_removed(self):
        self.write("gone", phase="done")
        self.backdate("gone", 400)
        shutil.rmtree(session_dir(self.root, "s1"))
        result = self.done_logs(prune(self.root, apply=True))
        self.assertEqual(([item["work_id"] for item in result["orphans"]], result["candidates"], result["removed"]),
                         (["gone"], [], 0))
        self.assertTrue(self.paths("gone")[0].is_file())

    def test_a_done_work_with_a_broken_chain_elsewhere_is_skipped(self):
        self.write("A", phase="done", session="s1")
        self.write("A", session="s2")
        self.write("A", session="s2", decisions=())
        self.write("B", session="s2", decisions=())
        self.backdate("A", 300, session="s1")
        cards = sorted((session_dir(self.root, "s2") / "records").glob("*.json"))
        oldest = min(cards, key=lambda p: json.loads(p.read_text(encoding="utf-8"))["recorded_at"])
        oldest.unlink()  # s2's chain of A loses its first card: INCOMPLETE_CHAIN
        result = self.done_logs(prune(self.root, apply=True))
        self.assertEqual((result["candidates"], result["removed"]), ([], 0))
        self.assertIn({"work_id": "A", "reason": "INCOMPLETE_CHAIN"}, result["skipped"])
        self.assertTrue(self.paths("A")[0].is_file())


class NoArchivePruneTests(FixtureCase):
    def test_an_expired_session_is_removed_without_writing_an_archive(self):
        bind(self.root, "old", {})
        store.record(self.root, "old", dict(self.payload(), work_id="alpha"), None, "r1")
        directory = session_dir(self.root, "old")
        stamp = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
        for path in directory.rglob("*"):
            os.utime(path, (stamp, stamp))
        for path in (directory / "records").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("checksum")
            data["recorded_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            path.write_text(json.dumps(store._seal(data)), encoding="utf-8")
            os.utime(path, (stamp, stamp))
        dry = prune(self.root)
        self.assertEqual(len(dry["expired"]), 1)
        self.assertNotIn("archive", dry["expired"][0])
        result = prune(self.root, apply=True)
        self.assertEqual(result["expired_removed"], 1)
        self.assertFalse(directory.exists())
        self.assertFalse((directory.parent.parent / "archive").exists())


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

    def test_cli_lists_done_work_logs(self):
        bind(self.root, "s1", {})
        store.record(self.root, "s1", dict(self.payload(), work_id="A", phase="done",
                                           new_decisions=[{"text": LONG}]), None, "r1")
        code, result = self.runtime()
        self.assertEqual(code, 0, result)
        self.assertEqual((result["done_logs"]["candidates"], result["done_logs"]["orphans"], result["log_backups"]),
                         ([], [], []))
