"""Maintainer redaction of decision log entries, driven through the runtime CLI in an isolated store."""
import hashlib
import json
import os
import subprocess
from unittest import mock
from _support import BASH, FixtureCase, REPO
from provost_runtime import redact, store
from provost_runtime.hooks import bind

RUNTIME = REPO / "hooks" / "runtime"
SECRET = "hunter2-db-password"


class RedactTests(FixtureCase):
    def setUp(self):
        super().setUp()
        bind(self.root, "s1", {})

    def runtime(self, *args, data=None):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args> --workspace <root>`."""
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        result = subprocess.run([BASH, str(RUNTIME), *args, "--workspace", str(self.root)],
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def record(self, decisions=(), evidence=(), expected=None, request_id="r1"):
        data = dict(self.payload("next step"), work_id="A", new_decisions=list(decisions), new_evidence=list(evidence))
        code, out = self.runtime("record", "--session", "s1", "--expected", expected or "null",
                                 "--request-id", request_id, data=data)
        return out if code == 0 else out["error"]

    def redact(self, entry, *reason, work="A"):
        code, out = self.runtime("redact", "--work", work, "--entry", entry, *(("--reason",) + reason if reason else ()))
        return out if code == 0 else out["error"]

    def status(self):
        code, out = self.runtime("status", "--session", "s1", "--work", "A")
        self.assertEqual(code, 0, out)
        return out

    def log_path(self, work="A"):
        key = store.as_workspace(self.root).repo_key
        return (self.store_home / "v2" / "repos" / key[:16] / "logs"
                / (hashlib.sha256(work.encode("utf-8")).hexdigest() + ".jsonl"))

    def entries(self):
        return {entry["id"]: entry for entry in self.status()["log"]["newest"]}

    def test_a_decision_loses_its_text_and_keeps_its_identity_and_links(self):
        first = self.record([{"text": "Use the staging DB with password " + SECRET, "refs": ["abc1234"]}])
        self.record([{"text": "Use a vault reference for the staging DB instead", "refs": ["def5678"],
                      "supersedes": "D1"}], expected=first["revision"], request_id="r2")
        before = self.entries()
        out = self.redact("D1", "leaked a DB password")
        self.assertEqual((out["work_id"], out["changed"]), ("A", True))
        after = self.entries()
        d1 = after["D1"]
        self.assertEqual(d1["text"], redact.REMOVED)
        self.assertEqual(d1["redaction_reason"], "leaked a DB password")
        self.assertTrue(store._when(d1["redacted_at"]))
        for key in ("id", "kind", "at", "session", "record", "refs"):
            self.assertEqual(d1[key], before["D1"][key], key)
        self.assertEqual(out["entry"], d1)
        self.assertEqual(after["D2"], before["D2"])  # the superseding link still resolves to D1
        self.assertNotIn(SECRET, json.dumps(self.status()))
        self.assertNotIn(SECRET.encode(), self.log_path().read_bytes())

    def test_evidence_loses_its_claim_and_ref_and_keeps_its_state_and_update_link(self):
        first = self.record(evidence=[{"claim": "login works with " + SECRET, "state": "fail", "ref": SECRET}])
        self.record(evidence=[{"claim": "login works", "state": "pass-observed", "ref": "ci", "updates": "E1"}],
                    expected=first["revision"], request_id="r2")
        before = self.entries()
        self.assertEqual(self.redact("E2")["changed"], True)  # an entry with an update link
        self.redact("E1")
        after = self.entries()
        for ident in ("E1", "E2"):
            self.assertEqual((after[ident]["claim"], after[ident]["ref"]), (redact.REMOVED, redact.REMOVED))
            self.assertEqual(after[ident]["state"], before[ident]["state"])
        self.assertEqual(after["E2"]["updates"], "E1")
        self.assertNotIn("redaction_reason", after["E1"])
        self.assertNotIn(SECRET, json.dumps(self.status()))

    def test_redacting_again_succeeds_without_change(self):
        self.record([{"text": "secret " + SECRET}])
        first = self.redact("D1", "first reason")
        raw = self.log_path().read_bytes()
        again = self.redact("D1", "other reason")
        self.assertEqual((again["changed"], again["entry"]), (False, first["entry"]))
        self.assertEqual(self.log_path().read_bytes(), raw)

    def test_unknown_entries_and_bad_arguments_are_refused(self):
        self.record([{"text": "kept"}])
        raw = self.log_path().read_bytes()
        self.assertEqual(self.redact("D2"), "ENTRY_NOT_FOUND")
        self.assertEqual(self.redact("E1"), "ENTRY_NOT_FOUND")
        self.assertEqual(self.redact("x"), "ENTRY_NOT_FOUND")
        self.assertEqual(self.redact("D1", work="B"), "ENTRY_NOT_FOUND")  # a work without a log
        self.assertEqual(self.redact("D1", ""), "INVALID_REASON")
        self.assertEqual(self.runtime("redact", "--work", "A")[1]["error"], "INVALID_ARGUMENTS")
        self.assertEqual(self.runtime("redact", "--entry", "D1")[1]["error"], "INVALID_ARGUMENTS")
        self.assertEqual(self.log_path().read_bytes(), raw)
        self.assertEqual(list(self.log_path().parent.glob(".pending-*")), [])

    def test_a_torn_tail_is_dropped_and_appends_continue_the_ids(self):
        first = self.record([{"text": "secret " + SECRET}, {"text": "kept"}])
        with self.log_path().open("ab") as stream:
            stream.write(b'{"id":"D3","kind":"decision","text":"' + SECRET.encode())  # a crash mid-append
        self.redact("D1")
        raw = self.log_path().read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(SECRET.encode(), raw)
        self.assertEqual(len([json.loads(line) for line in raw.splitlines()]), 3)
        second = self.record([{"text": "after the redaction"}], expected=first["revision"], request_id="r2")
        self.assertEqual(second["log_entries"], ["D3"])
        self.assertEqual([e["id"] for e in self.status()["log"]["newest"]], ["D1", "D2", "D3"])

    def test_replaying_a_request_whose_entries_were_redacted_is_refused(self):
        decisions = [{"text": "secret " + SECRET}]
        self.record(decisions)
        self.redact("D1")
        self.assertEqual(self.record(decisions), "REQUEST_ID_REUSED")
        # Also when the card was never written (an interrupted record): the replay cannot complete it.
        real = store.atomic_write

        def fail_card(base, path, payload):
            if path.parent.name == "records":
                raise store.StoreError("WRITE_FAILED")
            return real(base, path, payload)
        data = dict(self.payload("n"), work_id="A", new_decisions=[{"text": "other " + SECRET}], new_evidence=[])
        with mock.patch.object(store, "atomic_write", fail_card), self.assertRaises(store.StoreError):
            store.record(self.root, "s1", data, self.status()["expected"], "r2")
        self.redact("D2")
        with self.assertRaises(store.StoreError) as caught:
            store.record(self.root, "s1", data, self.status()["expected"], "r2")
        self.assertEqual(str(caught.exception), "REQUEST_ID_REUSED")
        self.assertEqual(self.status()["log"]["total"], 2)

    def test_redact_holds_the_log_lock_while_it_rewrites(self):
        self.record([{"text": "secret " + SECRET}])
        _, lock = store._log_paths(store.as_workspace(self.root), "A")
        with mock.patch.object(store, "LOG_LOCK_WAIT", 0.2):
            with store._locked(lock), self.assertRaises(store.StoreError) as caught:
                redact.redact(self.root, "A", "D1")
            self.assertEqual(str(caught.exception), "STORE_BUSY")
            seen = []
            real = redact._replace

            def record_meanwhile(home, path, data):
                data2 = dict(self.payload("n"), work_id="A", new_decisions=[{"text": "racing"}], new_evidence=[])
                try:
                    store.record(self.root, "s1", data2, self.status()["expected"], "r2")
                except store.StoreError as exc:
                    seen.append(str(exc))
                return real(home, path, data)
            with mock.patch.object(redact, "_replace", record_meanwhile):
                redact.redact(self.root, "A", "D1")
        self.assertEqual(seen, ["STORE_BUSY"])
        self.assertEqual([e["text"] for e in self.status()["log"]["newest"]], [redact.REMOVED])

    def test_a_failed_replace_is_write_failed_and_leaves_the_log_as_it_was(self):
        self.record([{"text": "secret " + SECRET}])
        raw = self.log_path().read_bytes()
        # Windows refuses os.replace while a reader has the log open.
        with mock.patch.object(redact.os, "replace", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(store.StoreError) as caught:
                redact.redact(self.root, "A", "D1")
        self.assertEqual(str(caught.exception), "WRITE_FAILED")
        self.assertEqual(self.log_path().read_bytes(), raw)
        self.assertEqual(list(self.log_path().parent.glob(".pending-*")), [])
        self.assertTrue(redact.redact(self.root, "A", "D1")["changed"])  # retryable

    def test_the_reader_accepts_only_well_formed_redaction_fields(self):
        self.record([{"text": "kept"}])
        raw = self.log_path().read_bytes()
        header, line = raw.splitlines()
        entry = json.loads(line)
        cases = {"ok": ({"redacted_at": "2026-10-14T00:00:00+00:00", "redaction_reason": "r"}, None),
                 "bad time": ({"redacted_at": "yesterday"}, "CORRUPT_LOG"),
                 "reason alone": ({"redaction_reason": "r"}, "CORRUPT_LOG"),
                 "reason type": ({"redacted_at": "2026-10-14T00:00:00+00:00", "redaction_reason": 5}, "CORRUPT_LOG"),
                 "other field": ({"redacted_by": "me"}, "CORRUPT_LOG")}
        for name, (extra, error) in cases.items():
            with self.subTest(name):
                self.log_path().write_bytes(header + b"\n" + json.dumps(dict(entry, **extra)).encode() + b"\n")
                log = self.status()["log"]
                self.assertEqual(log.get("error"), error, log)
