"""work_states and the link checks of the store (#133): its value, a link at every level, and writes."""
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime import store
from provost_runtime.config import handoff_home
from provost_runtime.store import StoreError, digest, session_dir

ONE, TWO, THREE, FOUR, FIVE, SIX = ("session-%s" % name for name in ("one", "two", "three", "four", "five", "six"))
BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


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


class WorkStatesGuardTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.ws = store.as_workspace(self.root)
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def records(self, session):
        return session_dir(self.ws, session) / "records"

    def card_path(self, session, card):
        return self.records(session) / (card["revision"] + ".json")

    def write(self, session, work, request_id, expected=None, *, phase="implementation", minutes=0):
        """Record a card, then reseal it as recorded `minutes` after BASE (a fixed order of cards)."""
        request = dict(self.payload(request_id), work_id=work, phase=phase, new_decisions=[], new_evidence=[])
        card = store.record(self.ws, session, request, expected and expected["revision"], request_id)
        body = {k: v for k, v in card.items() if k != "checksum"}
        body["recorded_at"] = (BASE + timedelta(minutes=minutes)).isoformat()
        card = store._seal(body)
        store.atomic_write(handoff_home(), self.card_path(session, card), card)
        return card

    @staticmethod
    def item(session, card, phase="implementation"):
        return {"session": session, "revision": card["revision"], "phase": phase, "recorded_at": card["recorded_at"]}

    def relink(self, path, *, directory=True):
        """Move path outside the store and put a link to it in its place; skip when the OS allows no link."""
        target = self.outside / ("%s-%s" % (path.parent.name[:8], path.name))
        shutil.move(str(path), str(target))
        if not make_link(target, path, directory=directory):
            shutil.move(str(target), str(path))
            self.skipTest("OS permits no %s link" % ("directory" if directory else "file"))

    def two_sessions(self):
        """ONE holds work A (two cards) and a horizon.json; TWO holds work C. Returns the works of TWO alone."""
        a1 = self.write(ONE, "A", "a1", minutes=1)
        self.a2 = self.write(ONE, "A", "a2", a1, minutes=2)
        (session_dir(self.ws, ONE) / "horizon.json").write_text(
            json.dumps({"records": None, "snapshots": None}), encoding="utf-8")
        c1 = self.write(TWO, "C", "c1", minutes=3)
        return {"C": {**self.item(TWO, c1), "open": True, "sessions": [self.item(TWO, c1)]}}

    def works_of_one(self):
        return {"A": {**self.item(ONE, self.a2), "open": True, "sessions": [self.item(ONE, self.a2)]}}

    def linked_one(self):
        return [{"session_dir": digest(ONE), "error": "LINKED_STORAGE"}]

    # --- the pinned value ------------------------------------------------------------------

    def test_the_value_of_a_store_with_several_sessions_a_broken_chain_and_unreadable_sessions(self):
        a1 = self.write(ONE, "A", "a1", minutes=1)
        a2 = self.write(ONE, "A", "a2", a1, minutes=5)
        b1 = self.write(ONE, "B", "b1", phase="done", minutes=2)
        a3 = self.write(TWO, "A", "a3", minutes=7)
        c1 = self.write(TWO, "C", "c1", minutes=3)
        d1 = self.write(THREE, "D", "d1", minutes=1)
        self.write(THREE, "D", "d2", d1, minutes=4)
        e1 = self.write(THREE, "E", "e1", minutes=6)
        self.card_path(THREE, d1).unlink()  # D's oldest card is gone: its chain is incomplete
        self.write(FOUR, "F", "f1", minutes=2)
        # FOUR: the record read first (to learn the session id) is not JSON.
        (self.records(FOUR) / ("0" * 64 + ".json")).write_text("not json", encoding="utf-8")
        self.write(SIX, "G", "g1", minutes=2)
        # SIX: a later file is not JSON and nothing attributes it to a work.
        (self.records(SIX) / ("f" * 64 + ".json")).write_text("not json", encoding="utf-8")
        session_dir(self.ws, FIVE).mkdir(parents=True)  # no records: not listed
        (session_dir(self.ws, FIVE).parent / "not-a-session").mkdir()
        sessions_a = [self.item(TWO, a3), self.item(ONE, a2)]
        expected = {
            "works": {
                "A": {**self.item(TWO, a3), "open": True, "sessions": sessions_a},
                "B": {**self.item(ONE, b1, "done"), "open": False, "sessions": [self.item(ONE, b1, "done")]},
                "C": {**self.item(TWO, c1), "open": True, "sessions": [self.item(TWO, c1)]},
                "E": {**self.item(THREE, e1), "open": True, "sessions": [self.item(THREE, e1)]},
            },
            "skipped": sorted(({"session_dir": digest(s), "error": "CORRUPT_RECORD"} for s in (FOUR, SIX)),
                              key=lambda entry: entry["session_dir"]),
            "broken": [{"session": THREE, "work_id": "D", "error": "INCOMPLETE_CHAIN"}],
        }
        self.assertEqual(store.work_states(self.ws), expected)
        self.assertEqual(store.work_states(self.root), expected)  # resolved from a path as well

    def test_an_empty_store_has_no_works(self):
        self.assertEqual(store.work_states(self.ws), {"works": {}, "skipped": [], "broken": []})

    def test_a_file_where_a_session_dir_belongs(self):
        expected = self.two_sessions()
        stray = session_dir(self.ws, THREE)
        stray.write_text("x", encoding="utf-8")
        # POSIX: lstat of <file>/records fails with ENOTDIR (an OSError, not a missing path).
        # Windows: the path is missing, so the entry holds no records and is not listed.
        skipped = [] if os.name == "nt" else [{"session_dir": stray.name, "error": "CORRUPT_RECORD"}]
        self.assertEqual(store.work_states(self.ws),
                         {"works": {**expected, **self.works_of_one()}, "skipped": skipped, "broken": []})

    # --- a link at each level --------------------------------------------------------------

    def test_a_linked_sessions_directory_raises(self):
        self.two_sessions()
        self.relink(session_dir(self.ws, ONE).parent)
        with self.assertRaises(StoreError) as caught:
            store.work_states(self.ws)
        self.assertEqual(str(caught.exception), "LINKED_STORAGE")

    def test_a_linked_session_directory_is_skipped(self):
        expected = self.two_sessions()
        self.relink(session_dir(self.ws, ONE))
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_linked_records_directory_is_skipped(self):
        expected = self.two_sessions()
        self.relink(self.records(ONE))
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_linked_card_file_is_skipped(self):
        expected = self.two_sessions()
        for path in sorted(self.records(ONE).glob("*.json"))[:1]:  # the card read first to learn the session id
            self.relink(path, directory=False)
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_linked_card_file_read_later_is_skipped(self):
        expected = self.two_sessions()
        for path in sorted(self.records(ONE).glob("*.json"))[1:]:
            self.relink(path, directory=False)
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_dangling_card_link(self):
        expected = self.two_sessions()
        for name, error in (("0" * 64, "CORRUPT_RECORD"), ("f" * 64, "LINKED_STORAGE")):
            with self.subTest(name=name[:1]):
                link = self.records(ONE) / (name + ".json")
                if not make_link(self.outside / "missing.json", link, directory=False):
                    self.skipTest("OS permits no file link")
                # Read first (to learn the session id), a dangling link reads as unparseable; read later,
                # the guard of the card read refuses it.
                self.assertEqual(store.work_states(self.ws), {
                    "works": expected, "skipped": [{"session_dir": digest(ONE), "error": error}], "broken": []})
                link.unlink()

    def test_a_linked_horizon_is_skipped(self):
        expected = self.two_sessions()
        self.relink(session_dir(self.ws, ONE) / "horizon.json", directory=False)
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_linked_current_index_is_skipped(self):
        expected = self.two_sessions()
        self.relink(session_dir(self.ws, ONE) / "current.json", directory=False)
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_link_placed_between_two_calls_is_seen_by_the_second(self):
        expected = self.two_sessions()
        self.assertEqual(store.work_states(self.ws),
                         {"works": {**expected, **self.works_of_one()}, "skipped": [], "broken": []})
        self.relink(session_dir(self.ws, ONE))
        self.assertEqual(store.work_states(self.ws), {"works": expected, "skipped": self.linked_one(), "broken": []})

    def test_a_write_after_a_call_still_checks_every_component(self):
        self.two_sessions()
        head = store.current(self.ws, ONE, "A")
        store.work_states(self.ws)
        self.relink(session_dir(self.ws, ONE))
        request = dict(self.payload("a3"), work_id="A", new_decisions=[], new_evidence=[])
        with self.assertRaises(StoreError) as caught:
            store.record(self.ws, ONE, request, head["revision"], "a3")
        self.assertEqual(str(caught.exception), "LINKED_STORAGE")
        with self.assertRaises(StoreError) as caught:
            store.atomic_write(handoff_home(), session_dir(self.ws, ONE) / "current.json", {"revision": "x"})
        self.assertEqual(str(caught.exception), "LINKED_STORAGE")

    def test_guard_below_a_regular_file(self):
        """One lstat per component: a component below a regular file is ENOTDIR on POSIX (raised as
        before) and a missing path on Windows (skipped as before), with or without `checked`."""
        regular = handoff_home() / "file"
        regular.write_text("x", encoding="utf-8")
        for checked in (None, set()):
            with self.subTest(checked=checked):
                if os.name == "nt":
                    self.assertIsNone(store.guard(handoff_home(), regular / "child", checked=checked))
                else:
                    with self.assertRaises(NotADirectoryError):
                        store.guard(handoff_home(), regular / "child", checked=checked)
                self.assertFalse(checked)  # a file is never remembered as checked

    # --- the work of one call --------------------------------------------------------------

    def counted(self, method):
        """work_states() with Path.<method> counting its calls per path: (value, {normcased path: calls})."""
        real, calls = getattr(Path, method), Counter()

        def counting(path, *args, **kwargs):
            calls[os.path.normcase(str(path))] += 1
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, method, counting):
            value = store.work_states(self.ws)
        return value, calls

    def many_sessions(self, sessions=3, works=2, cards=2):
        """sessions x works chains of `cards` cards each; returns (dirs below the store home, card files)."""
        for s in range(sessions):
            for w in range(works):
                card = None
                for c in range(cards):
                    card = self.write("s%d" % s, "W%d" % w, "s%dw%dc%d" % (s, w, c), card, minutes=s * 10 + w * 3 + c)
        home = handoff_home().absolute()
        sessions_dir = session_dir(self.ws, "s0").parent.absolute()
        ladder = [sessions_dir] + list(sessions_dir.parents)
        dirs = ladder[:ladder.index(home)]  # sessions, repo dir, repos, v2
        dirs += [p for s in range(sessions) for p in (session_dir(self.ws, "s%d" % s).absolute(),
                                                      self.records("s%d" % s).absolute())]
        files = [p.absolute() for s in range(sessions) for p in self.records("s%d" % s).glob("*.json")]
        return dirs, files

    def test_one_call_checks_each_store_directory_once(self):
        sessions, works, cards = 3, 2, 2
        dirs, cards_on_disk = self.many_sessions(sessions, works, cards)
        self.assertEqual(len(cards_on_disk), sessions * works * cards)
        value, calls = self.counted("lstat")
        self.assertEqual((len(value["works"]), value["skipped"], value["broken"]), (works, [], []))
        key = lambda path: os.path.normcase(str(path))  # noqa: E731
        self.assertEqual({str(p): calls[key(p)] for p in dirs}, {str(p): 1 for p in dirs})
        self.assertEqual({str(p): calls[key(p)] for p in cards_on_disk}, {str(p): 1 for p in cards_on_disk})
        # Files read: each card, plus current.json and horizon.json of each session.
        files = len(cards_on_disk) + 2 * sessions
        depth = 7  # v2/repos/<repo>/sessions/<session>/records/<card>
        total = sum(calls.values())
        self.assertLessEqual(total, files + len(dirs) + sessions)  # current.json also orders a session's chains
        self.assertLess(total * 2, files * depth)  # before #133: 356 lstat calls (Python 3.12)

    def test_one_call_reads_each_card_once(self):
        _, cards_on_disk = self.many_sessions(sessions=2, works=2, cards=2)
        _, reads = self.counted("read_text")
        first = [sorted(self.records(s).glob("*.json"))[0] for s in ("s0", "s1")]
        self.assertEqual({str(p): reads[os.path.normcase(str(p.absolute()))] for p in first},
                         {str(p): 1 for p in first})
        self.assertEqual(sorted(reads[os.path.normcase(str(p))] for p in cards_on_disk), [1] * len(cards_on_disk))
