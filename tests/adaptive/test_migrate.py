"""migrate: import decisions and evidence of schema 1 cards, snapshots and archive files into decision logs."""
import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path
from _support import BASH, FixtureCase, REPO
from provost_runtime import store
from provost_runtime.config import handoff_home

RUNTIME = REPO / "hooks" / "runtime"


class MigrateTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.requests = itertools.count()

    def env(self):
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        return env

    def runtime(self, command, *args, code=0):
        """Parsed JSON output of `bash hooks/runtime <command> --workspace <root> <args>`."""
        result = subprocess.run([BASH, str(RUNTIME), command, "--workspace", str(self.root), *args],
                                capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=60)
        out = json.loads(result.stdout or "null")
        self.assertEqual(result.returncode, code, (out, result.stderr))
        return out

    def migrate(self, *args, code=0):
        return self.runtime("migrate", *args, code=code)

    def card(self, session, work, decisions=(), evidence=(), step="next", request_id=None):
        """Write a schema 1 card on the session's chain of work; returns the stored record."""
        head = store.current(self.root, session, work)
        payload = dict(self.payload_v1(step, decisions, evidence), work_id=work)
        return self.record_v1(self.root, session, payload, head["revision"] if head else None,
                              request_id or "card-%d" % next(self.requests))

    @staticmethod
    def evidence(claim, state, ref="t.py"):
        return {"claim": claim, "ref": ref, "state": state}

    def base(self):
        return store._base(store.as_workspace(self.root))

    def archive(self, card):
        """An archive file of the card's work as prune wrote them before the decision log (it writes none now)."""
        store.atomic_write(handoff_home(), store._archive_path(self.base() / "archive", card["payload"]["work_id"]), card)

    def files(self, *parts):
        """{relative path: bytes} of every file under the store home (or under base/<part> for each part)."""
        roots = [self.base() / part for part in parts] if parts else [self.store_home]
        return {path.relative_to(self.store_home).as_posix(): path.read_bytes()
                for root in roots for path in root.rglob("*") if path.is_file()}

    def tree(self):
        return sorted(path.relative_to(self.store_home).as_posix() for path in self.store_home.rglob("*"))

    def log(self, work):
        return store.read_log(self.root, work)

    def works(self, out):
        return {item["work_id"]: {k: v for k, v in item.items() if k != "work_id"} for item in out["works"]}

    def test_dry_run_lists_imports_per_work_and_writes_nothing(self):
        self.card("s1", "A", ["Keep the parser API because callers pin it"], [self.evidence("suite green", "fail")])
        self.card("s1", "A", ["Keep the parser API because callers pin it", "Split the lexer out"],
                  [self.evidence("suite green", "pass-observed")])
        self.card("s1", "B", ["Use one lock per work"])
        store.snapshot(self.root, "s1", "e1")  # copies the card of B
        self.card("s2", "A", ["Split the lexer out", "Ship behind a flag"])
        before, tree = self.files(), self.tree()
        out = self.migrate()
        self.assertEqual((out["applied"], out["backup"], out["skipped"]), (False, None, []))
        self.assertEqual(self.works(out), {
            "A": {"sources": 3, "decisions": 3, "evidence": 2, "already_migrated": 0},
            "B": {"sources": 2, "decisions": 1, "evidence": 0, "already_migrated": 0}})
        self.assertEqual((self.files(), self.tree()), (before, tree))

    def test_apply_dedups_restated_decisions_keeping_the_first_time_and_session(self):
        first = self.card("s1", "A", ["Keep the parser API because callers pin it"])
        second = self.card("s1", "A", ["Keep the parser API because callers pin it", "Split the lexer out"])
        other = self.card("s2", "A", ["Split the lexer out", "Keep the parser API because callers pin it",
                                      "Ship behind a flag"])
        out = self.migrate("--apply")
        self.assertEqual(self.works(out)["A"], {"sources": 3, "decisions": 3, "evidence": 0, "already_migrated": 0})
        self.assertEqual([(e["id"], e["text"], e["at"], e["session"], e["record"], e["migrated"]) for e in self.log("A")],
                         [("D1", "Keep the parser API because callers pin it", first["recorded_at"], "s1",
                           first["revision"], True),
                          ("D2", "Split the lexer out", second["recorded_at"], "s1", second["revision"], True),
                          ("D3", "Ship behind a flag", other["recorded_at"], "s2", other["revision"], True)])

    def test_evidence_with_the_same_claim_is_chained_by_updates_in_time_order(self):
        a = self.card("s1", "A", evidence=[self.evidence("suite green", "fail"), self.evidence("lint clean", "pass-observed")])
        self.card("s1", "A", evidence=[self.evidence("suite green", "fail", "ci"), self.evidence("lint clean", "pass-observed")])
        c = self.card("s1", "A", evidence=[self.evidence("suite green", "pass-observed")])
        snap = store.snapshot(self.root, "s1", "e1")
        later = self.card("s2", "A", evidence=[self.evidence("suite green", "stale")])
        self.migrate("--apply")
        log = self.log("A")
        self.assertEqual([(e["id"], e["claim"], e["state"], e.get("updates"), e["record"]) for e in log],
                         [("E1", "suite green", "fail", None, a["revision"]),
                          ("E2", "lint clean", "pass-observed", None, a["revision"]),
                          ("E3", "suite green", "pass-observed", "E1", c["revision"]),
                          ("E4", "suite green", "stale", "E3", later["revision"])])
        self.assertEqual(snap["revision"], c["revision"])  # a copy of c: same card, its states add nothing

    def test_a_snapshot_whose_record_was_trimmed_stands_for_its_card(self):
        first = self.card("s1", "A", ["Only the snapshot keeps this decision"], [self.evidence("claim", "fail")])
        snap = store.snapshot(self.root, "s1", "e1")
        for _ in range(5):
            self.card("s1", "A")  # trims the first card
        self.assertFalse(any(self.base().glob("sessions/*/records/%s.json" % first["revision"])))
        self.migrate("--apply")
        self.assertNotEqual(snap["captured_at"], first["recorded_at"])
        self.assertEqual([(e["id"], e["at"], e["session"], e["record"]) for e in self.log("A")],
                         [("D1", first["recorded_at"], "s1", first["revision"]),
                          ("E1", first["recorded_at"], "s1", first["revision"])])

    def test_archive_files_are_sources(self):
        gone = self.card("gone", "A", ["Archived decision"], [self.evidence("archived claim", "pass-observed")])
        self.archive(gone)
        shutil.rmtree(store.session_dir(self.root, "gone"))
        out = self.migrate("--apply")
        self.assertEqual(self.works(out)["A"]["sources"], 1)
        self.assertEqual([(e["id"], e["session"], e["record"], e["at"]) for e in self.log("A")],
                         [("D1", "gone", gone["revision"], gone["recorded_at"]),
                          ("E1", "gone", gone["revision"], gone["recorded_at"])])

    def test_existing_log_entries_are_kept_and_ids_continue_after_them(self):
        old = self.card("s1", "A", ["Old decision from a schema 1 card"], [self.evidence("old claim", "fail")])
        live = store.record(self.root, "s1", dict(self.payload("live"), work_id="A",
                                                  new_decisions=[{"text": "Live decision"}],
                                                  new_evidence=[{"claim": "live claim", "state": "fail", "ref": "r"}]),
                            old["revision"], "live")
        self.migrate("--apply")
        log = self.log("A")
        self.assertEqual([(e["id"], e.get("migrated", False)) for e in log],
                         [("D1", False), ("E1", False), ("D2", True), ("E2", True)])
        self.assertEqual(log[0]["record"], live["revision"])
        # Views order by time: the migrated entries are older than the live ones.
        self.assertEqual([e["id"] for e in store.newest_entries(log, 10)], ["D2", "E2", "D1", "E1"])

    def test_apply_backs_up_logs_never_touches_old_files_and_a_second_apply_imports_nothing(self):
        old = self.card("s1", "A", ["Old decision from a schema 1 card"])
        store.record(self.root, "s1", dict(self.payload("live"), work_id="A", new_decisions=[{"text": "Live decision"}]),
                     old["revision"], "live")
        self.card("s1", "B", ["Decision of B"], [self.evidence("b claim", "fail")])
        store.snapshot(self.root, "s1", "e1")
        self.archive(old)
        sources = self.files("sessions", "archive")
        logs = self.files("logs")
        out = self.migrate("--apply")
        self.assertEqual((out["applied"], self.works(out)["A"]["decisions"], self.works(out)["B"]["evidence"]),
                         (True, 1, 1))
        self.assertEqual(self.files("sessions", "archive"), sources)  # byte-identical, nothing added
        self.assertEqual(Path(out["backup"]).parent, self.base() / "backups")
        # One copy per imported work, taken under its lock: A's log as it was, B had none (empty copy).
        path_a, path_b = (store._log_paths(store.as_workspace(self.root), w)[0] for w in ("A", "B"))
        self.assertEqual({os.path.basename(p): data for p, data in self.files("backups").items()},
                         {path_a.name: logs[path_a.relative_to(self.store_home).as_posix()], path_b.name: b""})
        after = self.files("logs")
        again = self.migrate("--apply")
        self.assertEqual(self.works(again), {
            "A": {"sources": 2, "decisions": 0, "evidence": 0, "already_migrated": 1},
            "B": {"sources": 2, "decisions": 0, "evidence": 0, "already_migrated": 2}})
        self.assertIsNone(again["backup"])
        self.assertEqual(self.files("logs"), after)
        self.assertEqual(len(list((self.base() / "backups").iterdir())), 1)

    def test_invalid_sources_are_skipped_and_listed(self):
        self.card("s1", "A", ["Valid decision"])
        bad = store.session_dir(self.root, "bad")
        self.card("bad", "A", ["Tampered decision"])
        tampered = next((bad / "records").glob("*.json"))
        data = json.loads(tampered.read_text(encoding="utf-8"))
        data["payload"]["decisions"].append("Injected decision")
        tampered.write_text(json.dumps(data), encoding="utf-8")
        garbage = bad / "snapshots" / ("0" * 64 + ".json")
        garbage.parent.mkdir()
        garbage.write_text("not json", encoding="utf-8")
        archive = self.base() / "archive" / ("1" * 32 + ".json")
        archive.parent.mkdir()
        archive.write_text("{}", encoding="utf-8")
        out = self.migrate("--apply")
        prefix = "sessions/" + bad.name + "/"
        self.assertEqual(sorted((s["file"], s["reason"]) for s in out["skipped"]),
                         sorted([(prefix + "records/" + tampered.name, "CORRUPT_RECORD"),
                                 (prefix + "snapshots/" + garbage.name, "CORRUPT_RECORD"),
                                 ("archive/" + archive.name, "CORRUPT_RECORD")]))
        self.assertEqual([e["text"] for e in self.log("A")], ["Valid decision"])

    def test_items_the_log_cannot_hold_are_skipped_and_listed(self):
        self.card("s1", "A", ["", "Kept decision"], [self.evidence("", "fail")])
        out = self.migrate("--apply")
        self.assertEqual(sorted(s["reason"] for s in out["skipped"]), ["INVALID_DECISION", "INVALID_EVIDENCE"])
        self.assertEqual([e["text"] for e in self.log("A")], ["Kept decision"])

    def test_schema_2_cards_contribute_nothing_and_no_sources_means_no_works(self):
        store.record(self.root, "s1", dict(self.payload(), work_id="A", new_decisions=[{"text": "Live only"}]),
                     None, "r1")
        before = self.files()
        out = self.migrate("--apply")
        self.assertEqual((out["works"], out["skipped"], out["backup"]), ([], [], None))
        self.assertEqual(self.files(), before)

    def test_a_corrupt_log_fails_closed_for_its_work_only(self):
        self.card("s1", "A", ["Decision of A"])
        self.card("s1", "B", ["Decision of B"])
        path, _ = store._log_paths(store.as_workspace(self.root), "A")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not json\n")
        out = self.migrate("--apply")
        self.assertEqual(self.works(out)["A"]["error"], "CORRUPT_LOG")
        self.assertEqual(path.read_bytes(), b"not json\n")
        self.assertEqual([e["text"] for e in self.log("B")], ["Decision of B"])

    def chain(self, work="A"):
        return [(e["id"], e["state"], e.get("updates"), e["session"], e["record"])
                for e in self.log(work) if e["kind"] == "evidence"]

    def assertRerunImportsNothing(self, work="A"):
        logs = self.files("logs")
        again = self.migrate("--apply")
        self.assertEqual((self.works(again)[work]["decisions"], self.works(again)[work]["evidence"],
                          again["backup"]), (0, 0, None))
        self.assertEqual(self.files("logs"), logs)
        return again

    def test_a_rerun_after_a_record_is_trimmed_but_its_snapshot_survives_imports_nothing(self):
        first = self.card("s1", "A", ["Decision a snapshot keeps"], [self.evidence("claim", "fail")])
        store.snapshot(self.root, "s1", "e1")
        self.card("s1", "A", evidence=[self.evidence("claim", "pass-observed")])
        self.migrate("--apply")
        for _ in range(4):
            self.card("s1", "A")  # trims the first card only; the pass card stays
        self.assertFalse(any(self.base().glob("sessions/*/records/%s.json" % first["revision"])))
        again = self.assertRerunImportsNothing()
        self.assertEqual(self.works(again)["A"]["already_migrated"], 3)

    def test_fail_pass_fail_within_one_card_keeps_the_full_chain(self):
        card = self.card("s1", "A", evidence=[self.evidence("claim", "fail"), self.evidence("claim", "pass-observed"),
                                              self.evidence("claim", "fail")])
        self.migrate("--apply")
        self.assertEqual(self.chain(), [("E1", "fail", None, "s1", card["revision"]),
                                        ("E2", "pass-observed", "E1", "s1", card["revision"]),
                                        ("E3", "fail", "E2", "s1", card["revision"])])
        self.assertRerunImportsNothing()

    def test_a_rerun_after_migrated_text_was_rewritten_imports_nothing(self):
        self.card("s1", "A", ["Decision holding a secret"], [self.evidence("claim holding a secret", "fail")])
        self.card("s1", "A", evidence=[self.evidence("claim holding a secret", "pass-observed")])
        self.migrate("--apply")
        path, _ = store._log_paths(store.as_workspace(self.root), "A")
        lines = path.read_bytes().splitlines()
        rewritten = [lines[0]]
        for line in lines[1:]:
            entry = json.loads(line)
            entry.update({"text": "[removed]"} if entry["kind"] == "decision" else {"claim": "[removed]"})
            rewritten.append(store.encoded(entry))
        path.write_bytes(b"".join(line + b"\n" for line in rewritten))
        self.assertRerunImportsNothing()

    def test_a_torn_tail_is_cut_and_the_backup_holds_only_committed_lines(self):
        live = store.record(self.root, "s2", dict(self.payload(), work_id="A",
                                                  new_decisions=[{"text": "Live decision"}]), None, "r1")
        path, _ = store._log_paths(store.as_workspace(self.root), "A")
        committed = path.read_bytes()
        with path.open("ab") as stream:
            stream.write(b'{"id":"D2","kind":"dec')
        self.card("s1", "A", ["Old decision"])
        out = self.migrate("--apply")
        self.assertEqual((Path(out["backup"]) / path.name).read_bytes(), committed)
        self.assertEqual([(e["id"], e.get("migrated", False)) for e in self.log("A")], [("D1", False), ("D2", True)])
        self.assertEqual(self.log("A")[0]["record"], live["revision"])
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        for line in path.read_bytes().splitlines():
            json.loads(line)

    def test_a_failed_backup_aborts_the_work_without_appending(self):
        self.card("s1", "A", ["Decision of A"])
        backups = self.base() / "backups"
        backups.parent.mkdir(parents=True, exist_ok=True)
        backups.write_text("not a dir", encoding="utf-8")
        out = self.migrate("--apply")
        self.assertEqual((self.works(out)["A"], out["backup"]), ({"sources": 1, "error": "WRITE_FAILED"}, None))
        self.assertEqual(self.log("A"), [])
        self.assertEqual(backups.read_text(encoding="utf-8"), "not a dir")
        backups.unlink()
        self.assertEqual(self.works(self.migrate("--apply"))["A"]["decisions"], 1)

    def test_interleaved_sessions_on_one_claim_chain_in_time_order(self):
        cards = [self.card(session, "A", evidence=[self.evidence("shared claim", state)])
                 for session, state in (("s2", "fail"), ("s1", "pass-observed"), ("s2", "fail"),
                                        ("s1", "pass-observed"))]
        self.migrate("--apply")
        self.assertEqual(self.chain(), [("E1", "fail", None, "s2", cards[0]["revision"]),
                                        ("E2", "pass-observed", "E1", "s1", cards[1]["revision"]),
                                        ("E3", "fail", "E2", "s2", cards[2]["revision"]),
                                        ("E4", "pass-observed", "E3", "s1", cards[3]["revision"])])

    def restamp(self, session, data, recorded_at):
        path = store.session_dir(self.root, session) / "records" / (data["revision"] + ".json")
        body = json.loads(path.read_text(encoding="utf-8"))
        body.pop("checksum")
        path.write_text(json.dumps(store._seal(dict(body, recorded_at=recorded_at))), encoding="utf-8")

    def test_equal_times_in_one_session_follow_parent_descent_not_file_names(self):
        ids = ("tie-%d" % i for i in range(10000))
        parent_id = next(r for r in ids if store.digest("s1\0record\0" + r) > "c")
        parent = self.card("s1", "A", evidence=[self.evidence("claim", "fail")], request_id=parent_id)
        child_id = next(r for r in ids if store.digest("s1\0record\0" + r) < "4")
        child = self.card("s1", "A", evidence=[self.evidence("claim", "pass-observed")], request_id=child_id)
        self.assertLess(child["revision"], parent["revision"])  # file order alone would put the child first
        for data in (parent, child):
            self.restamp("s1", data, parent["recorded_at"])
        self.migrate("--apply")
        self.assertEqual(self.chain(), [("E1", "fail", None, "s1", parent["revision"]),
                                        ("E2", "pass-observed", "E1", "s1", child["revision"])])

    def test_a_rewritten_decision_restated_by_an_older_surviving_card_is_not_imported_again(self):
        self.card("s1", "A", ["Decision edited by hand later"])
        self.card("s2", "A", ["Decision edited by hand later"])  # restates it: run 1 imports nothing from it
        self.card("s1", "A", ["Later decision"])
        self.migrate("--apply")
        self.rewrite_log(lambda entries: [dict(e, text="edited") if e["id"] == "D1" else e for e in entries])
        for _ in range(4):
            self.card("s1", "A")  # trims the card the decision was imported from
        self.assertRerunImportsNothing()  # the high-water mark keeps the older restatement out

    def test_a_work_with_a_redacted_migrated_entry_is_no_longer_migrated(self):
        secret = "Decision quoting ghp_" + "x" * 30
        self.card("s1", "A", [secret])
        self.card("s1", "A", ["Later decision"])
        self.card("s2", "A", [secret])  # the newest card only restates: the high-water mark does not cover it
        self.card("s1", "B", ["Decision of B"])
        self.migrate("--apply")
        self.assertEqual([e["text"] for e in self.log("A")], [secret, "Later decision"])
        self.assertTrue(self.runtime("redact", "--work", "A", "--entry", "D1")["changed"])
        for _ in range(4):
            self.card("s1", "A")  # trims the card the decision was imported from
        self.card("s1", "B", ["Another decision of B"])
        path, _ = store._log_paths(store.as_workspace(self.root), "A")
        before = path.read_bytes()
        for args in ((), ("--apply",)):
            out = self.migrate(*args)
            self.assertEqual(self.works(out)["A"], {"sources": 2, "error": "REDACTED_ENTRIES"})
        self.assertEqual(self.works(out)["B"]["decisions"], 1)  # other works continue
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(b"ghp_", before)

    def test_a_work_left_busy_by_one_run_is_imported_by_the_next(self):
        from unittest import mock
        from provost_runtime import migrate
        self.card("s1", "A", ["Decision of A"])
        self.card("s1", "B", ["Decision of B"], [self.evidence("b claim", "fail")])
        _, lock = store._log_paths(store.as_workspace(self.root), "B")
        lock.parent.mkdir(parents=True)
        with mock.patch.object(store, "LOG_LOCK_WAIT", 0.2), store._locked(lock):
            first = migrate.migrate(self.root, apply=True)
        self.assertEqual((self.works(first)["A"]["decisions"], self.works(first)["B"]),
                         (1, {"sources": 1, "error": "STORE_BUSY"}))
        self.assertEqual(self.log("B"), [])
        again = self.migrate("--apply")
        self.assertEqual(self.works(again), {
            "A": {"sources": 1, "decisions": 0, "evidence": 0, "already_migrated": 1},
            "B": {"sources": 1, "decisions": 1, "evidence": 1, "already_migrated": 0}})
        self.assertEqual([e["id"] for e in self.log("A") + self.log("B")], ["D1", "D1", "E1"])

    def adopt(self, session="s9"):
        """Resume the newest snapshot of s1 into session: a new schema 1 card with evidence unverified."""
        snap = store.snapshot(self.root, "s1", "adopt-%d" % next(self.requests))
        return store.resume(self.root, session, snap["snapshot_id"])

    def test_an_adopted_schema_1_card_is_imported_by_the_next_apply(self):
        first = self.card("s1", "A", ["Decision before adoption"], [self.evidence("claim", "pass-observed")])
        self.migrate("--apply")
        adopted = self.adopt()
        self.assertEqual(adopted["schema_version"], 1)
        out = self.migrate("--apply")
        # Sources: the card, the snapshot resume read (same card), and the adopted card.
        self.assertEqual(self.works(out)["A"], {"sources": 3, "decisions": 0, "evidence": 1, "already_migrated": 2})
        self.assertEqual(self.chain(), [("E1", "pass-observed", None, "s1", first["revision"]),
                                        ("E2", "unverified", "E1", "s9", adopted["revision"])])

    def rewrite_log(self, change, work="A"):
        """Rewrite the entries of a work's log through change(entries) -> entries (a hand edit)."""
        path, _ = store._log_paths(store.as_workspace(self.root), work)
        header, *lines = path.read_bytes().splitlines()
        entries = change([json.loads(line) for line in lines])
        path.write_bytes(b"".join(line + b"\n" for line in [header] + [store.encoded(e) for e in entries]))

    def test_a_link_falls_back_to_a_unique_record_claim_and_state(self):
        self.card("s1", "A", ["Decision of the card"], [self.evidence("claim", "fail")])
        self.migrate("--apply")
        self.rewrite_log(lambda entries: [e for e in entries if e["kind"] == "evidence"])  # position no longer matches
        adopted = self.adopt()
        self.migrate("--apply")
        self.assertEqual(self.chain(), [("E1", "fail", None, "s1", self.log("A")[0]["record"]),
                                        ("E2", "unverified", "E1", "s9", adopted["revision"])])
        self.assertEqual([e["kind"] for e in self.log("A")], ["evidence", "evidence"])  # the decision stays out

    def test_an_ambiguous_link_fails_the_work_with_migrate_chain_unresolved(self):
        self.card("s1", "B", ["Decision of B"])
        self.card("s1", "A", ["Decision of the card"], [self.evidence("claim", "fail")])  # s1's current card
        self.migrate("--apply")
        self.rewrite_log(lambda entries: entries + [dict(entries[-1], id="E2")])  # two matches for one key
        path, _ = store._log_paths(store.as_workspace(self.root), "A")
        before = path.read_bytes()
        self.adopt()
        out = self.migrate("--apply")
        self.assertEqual(self.works(out)["A"], {"sources": 3, "error": "MIGRATE_CHAIN_UNRESOLVED"})
        self.assertEqual(self.works(out)["B"]["already_migrated"], 1)
        self.assertEqual(path.read_bytes(), before)
