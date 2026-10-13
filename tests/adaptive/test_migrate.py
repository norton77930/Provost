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

    def migrate(self, *args, code=0):
        result = subprocess.run([BASH, str(RUNTIME), "migrate", "--workspace", str(self.root), *args],
                                capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=60)
        out = json.loads(result.stdout or "null")
        self.assertEqual(result.returncode, code, (out, result.stderr))
        return out

    def card(self, session, work, decisions=(), evidence=(), step="next"):
        """Write a schema 1 card on the session's chain of work; returns the stored record."""
        head = store.current(self.root, session, work)
        payload = dict(self.payload_v1(step, decisions, evidence), work_id=work)
        return self.record_v1(self.root, session, payload, head["revision"] if head else None,
                              "card-%d" % next(self.requests))

    @staticmethod
    def evidence(claim, state, ref="t.py"):
        return {"claim": claim, "ref": ref, "state": state}

    def base(self):
        return store._base(store.as_workspace(self.root))

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
        self.assertNotIn(snap["snapshot_id"], [e["record"] for e in log])  # its states repeat c's

    def test_a_snapshot_whose_record_was_trimmed_is_a_source_with_its_capture_time(self):
        self.card("s1", "A", ["Only the snapshot keeps this decision"], [self.evidence("claim", "fail")])
        snap = store.snapshot(self.root, "s1", "e1")
        for _ in range(5):
            self.card("s1", "A")  # trims the first card
        self.migrate("--apply")
        self.assertEqual([(e["id"], e["at"], e["session"], e["record"]) for e in self.log("A")],
                         [("D1", snap["captured_at"], "s1", snap["snapshot_id"]),
                          ("E1", snap["captured_at"], "s1", snap["snapshot_id"])])

    def test_archive_files_are_sources(self):
        gone = self.card("gone", "A", ["Archived decision"], [self.evidence("archived claim", "pass-observed")])
        ws = store.as_workspace(self.root)
        store._archive(handoff_home(), ws, self.base() / "archive", {"A": gone})
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
        ws = store.as_workspace(self.root)
        store._archive(handoff_home(), ws, self.base() / "archive", {"A": old})
        sources = self.files("sessions", "archive")
        logs = self.files("logs")
        out = self.migrate("--apply")
        self.assertEqual((out["applied"], self.works(out)["A"]["decisions"], self.works(out)["B"]["evidence"]),
                         (True, 1, 1))
        self.assertEqual(self.files("sessions", "archive"), sources)  # byte-identical, nothing added
        self.assertEqual(Path(out["backup"]).parent, self.base() / "backups")
        self.assertEqual({os.path.basename(p): data for p, data in self.files("backups").items()},
                         {os.path.basename(p): data for p, data in logs.items() if p.endswith(".jsonl")})
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
