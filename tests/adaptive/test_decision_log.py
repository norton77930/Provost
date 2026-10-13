"""Append-only decision log per work_id, driven through the runtime CLI in an isolated store."""
import hashlib
import json
import os
import subprocess
from unittest import mock
from _support import BASH, FixtureCase, REPO
from provost_runtime import store
from provost_runtime.hooks import bind, recovery_context

RUNTIME = REPO / "hooks" / "runtime"


class DecisionLogTests(FixtureCase):
    def setUp(self):
        super().setUp()
        bind(self.root, "s1", {})

    def env(self):
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        return env

    def command(self, *args, session="s1", workspace=None):
        return [BASH, str(RUNTIME), *args, "--workspace", str(workspace or self.root), "--session", session]

    def runtime(self, *args, data=None, session="s1", workspace=None):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>`."""
        result = subprocess.run(self.command(*args, session=session, workspace=workspace),
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=self.env(), timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def request(self, work="A", decisions=(), evidence=(), step="next step"):
        return dict(self.payload(step), work_id=work, new_decisions=list(decisions), new_evidence=list(evidence))

    def record(self, data, expected=None, request_id="r1", session="s1", workspace=None):
        code, out = self.runtime("record", "--expected", expected or "null", "--request-id", request_id,
                                 data=data, session=session, workspace=workspace)
        return out if code == 0 else out["error"]

    def status(self, work=None, session="s1", workspace=None):
        code, out = self.runtime("status", *(("--work", work) if work else ()), session=session, workspace=workspace)
        self.assertEqual(code, 0, out)
        return out

    def log_file(self, work="A"):
        found = list(self.store_home.glob("v2/repos/*/logs/%s.jsonl" % hashlib.sha256(work.encode()).hexdigest()))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_record_appends_new_entries_and_returns_their_ids(self):
        first = self.record(self.request(decisions=[{"text": "Keep the parser API because callers pin it",
                                                     "refs": ["abc1234"]},
                                                    {"text": "Split the lexer into its own module"}],
                                         evidence=[{"claim": "parser tests pass", "state": "fail", "ref": "t.py"}]))
        self.assertEqual(first["log_entries"], ["D1", "D2", "E1"])
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(set(first["payload"]), {"work_id", "phase", "sources", "assignments", "blockers", "next",
                                                 "suggested_skills"})
        second = self.record(self.request(decisions=[{"text": "Keep the lexer inline after all", "supersedes": "D2"}],
                                          evidence=[{"claim": "parser tests pass", "state": "pass-observed",
                                                     "ref": "t.py", "updates": "E1"}]),
                             first["revision"], "r2")
        self.assertEqual(second["log_entries"], ["D3", "E2"])
        log = self.status()["log"]
        self.assertEqual(log["total"], 5)
        self.assertEqual([entry["id"] for entry in log["newest"]], ["D1", "D2", "E1", "D3", "E2"])
        d1, e2 = log["newest"][0], log["newest"][4]
        self.assertEqual((d1["kind"], d1["session"], d1["text"], d1["refs"]),
                         ("decision", "s1", "Keep the parser API because callers pin it", ["abc1234"]))
        self.assertEqual(log["newest"][3]["supersedes"], "D2")
        self.assertEqual((e2["kind"], e2["claim"], e2["state"], e2["ref"], e2["updates"]),
                         ("evidence", "parser tests pass", "pass-observed", "t.py", "E1"))
        self.assertTrue(d1["at"])
        # A record without new entries assigns none and leaves the log as it is.
        third = self.record(self.request(), second["revision"], "r3")
        self.assertEqual(third["log_entries"], [])
        self.assertEqual(self.status()["log"]["total"], 5)

    def test_status_shows_only_the_newest_ten_entries_of_the_shown_work(self):
        self.record(self.request(decisions=[{"text": "decision %02d" % i} for i in range(12)]))
        self.record(self.request("B", decisions=[{"text": "other work"}]), request_id="b1")
        log = self.status("A")["log"]
        self.assertEqual((log["total"], [e["id"] for e in log["newest"]]), (12, ["D%d" % i for i in range(3, 13)]))
        self.assertEqual([e["text"] for e in self.status()["log"]["newest"]], ["other work"])
        self.assertEqual(self.status("C")["log"], {"total": 0, "newest": []})

    def test_status_template_is_a_valid_record_request(self):
        template = self.status()["template"]
        self.assertEqual(set(template), {"work_id", "phase", "sources", "assignments", "blockers", "next",
                                         "suggested_skills", "new_decisions", "new_evidence"})
        filled = dict(template, work_id="A", phase="p", next="n", new_decisions=[{"text": "use the template"}])
        self.assertEqual(self.record(filled)["log_entries"], ["D1"])

    def test_old_fields_are_rejected_with_guidance(self):
        for old in ({"decisions": ["keep it"]}, {"evidence": []}, {"decisions": [], "evidence": []}):
            with self.subTest(old=sorted(old)):
                data = dict(self.request(), **old)
                for field in ("new_decisions", "new_evidence"):
                    data.pop(field)
                code, out = self.runtime("record", "--expected", "null", "--request-id", "r1", data=data)
                self.assertEqual((code, out["error"]), (1, "LEGACY_FIELDS"))
                self.assertIn("new_decisions", out["detail"])
                self.assertIn("new_evidence", out["detail"])
        self.assertIsNone(self.status()["checkpoint"])
        self.assertEqual(list(self.store_home.glob("v2/repos/*/logs/*.jsonl")), [])

    def test_structurally_invalid_entries_are_rejected_before_anything_is_written(self):
        cases = {"INVALID_DECISION": [{"text": 5}, {"text": ""}, {"refs": []}, {"text": "t", "refs": "abc"},
                                      {"text": "t", "refs": [1]}, {"text": "t", "supersedes": 3},
                                      {"text": "t", "extra": 1}, "plain string"],
                 "INVALID_EVIDENCE": [{"claim": "c", "state": "passed", "ref": "r"}, {"claim": "c", "state": "fail"},
                                      {"claim": "", "state": "fail", "ref": "r"},
                                      {"claim": "c", "state": "fail", "ref": "r", "updates": ["E1"]},
                                      {"claim": "c", "state": "fail", "ref": "r", "id": "E9"}]}
        for code, items in cases.items():
            for item in items:
                with self.subTest(item=item):
                    data = (self.request(decisions=[item]) if code == "INVALID_DECISION"
                            else self.request(evidence=[item]))
                    self.assertEqual(self.record(data), code)
        self.assertEqual(self.record(dict(self.request(), new_decisions={"text": "t"})), "INVALID_DECISION")
        self.assertEqual(self.record(dict(self.request(), new_evidence="none")), "INVALID_EVIDENCE")
        self.assertIsNone(self.status()["checkpoint"])
        self.assertEqual(self.status()["log"]["total"], 0)

    def test_replaying_a_request_never_appends_twice(self):
        data = self.request(decisions=[{"text": "Ship the tracer bullet first"}],
                            evidence=[{"claim": "suite green", "state": "unverified", "ref": "ci"}])
        first = self.record(data)
        self.assertEqual(self.record(data), first)
        self.assertEqual(self.status()["log"]["total"], 2)
        self.assertEqual(self.record(self.request(decisions=[{"text": "Something else"}])), "REQUEST_ID_REUSED")
        self.assertEqual(self.record(self.request()), "REQUEST_ID_REUSED")
        self.assertEqual(self.status()["log"]["total"], 2)

    def test_a_record_interrupted_after_its_append_is_completed_by_a_replay(self):
        data = self.request(decisions=[{"text": "first"}, {"text": "second"}])
        real = store.atomic_write

        def fail_card(base, path, payload):
            if path.parent.name == "records":
                raise store.StoreError("WRITE_FAILED")
            return real(base, path, payload)
        with mock.patch.object(store, "atomic_write", fail_card):
            with self.assertRaises(store.StoreError):
                store.record(self.root, "s1", data, None, "r1")
        self.assertEqual(self.status("A")["log"]["total"], 2)  # appended, but no card
        replay = self.record(data)
        self.assertEqual(replay["log_entries"], ["D1", "D2"])
        self.assertEqual(self.status()["log"]["total"], 2)
        # The same request id with other entries is still refused.
        self.assertEqual(self.record(self.request(decisions=[{"text": "first"}]), request_id="r1"),
                         "REQUEST_ID_REUSED")

    def test_a_torn_last_line_is_skipped_on_read_and_dropped_by_the_next_append(self):
        first = self.record(self.request(decisions=[{"text": "kept decision"}]))
        path = self.log_file()
        with path.open("ab") as stream:
            stream.write(b'{"id":"D2","kind":"decision","te')  # a crash in the middle of an append
        self.assertEqual([e["text"] for e in self.status()["log"]["newest"]], ["kept decision"])
        second = self.record(self.request(decisions=[{"text": "next decision"}]), first["revision"], "r2")
        self.assertEqual(second["log_entries"], ["D2"])
        self.assertEqual([e["text"] for e in self.status()["log"]["newest"]], ["kept decision", "next decision"])
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        for line in path.read_bytes().splitlines():
            json.loads(line)

    def test_a_corrupt_complete_line_fails_closed(self):
        first = self.record(self.request(decisions=[{"text": "kept decision"}]))
        with self.log_file().open("ab") as stream:
            stream.write(b"not json\n")
        self.assertEqual(self.status()["log"], {"error": "CORRUPT_LOG"})
        self.assertEqual(self.record(self.request(decisions=[{"text": "more"}]), first["revision"], "r2"),
                         "CORRUPT_LOG")

    def test_two_worktrees_of_one_repo_append_to_one_log(self):
        main, worktree = self.git_repo_with_worktree()
        self.cfg["workspace_roots"] = [str(main), str(worktree)]
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        bind(main, "s-main", {})
        bind(worktree, "s-wt", {})
        a = self.record(self.request(decisions=[{"text": "from main"}]), session="s-main", workspace=main)
        b = self.record(self.request(decisions=[{"text": "from worktree"}],
                                     evidence=[{"claim": "c", "state": "fail", "ref": "r"}]),
                        session="s-wt", workspace=worktree)
        c = self.record(self.request(decisions=[{"text": "main again"}]), a["revision"], "r2",
                        session="s-main", workspace=main)
        self.assertEqual((a["log_entries"], b["log_entries"], c["log_entries"]), (["D1"], ["D2", "E1"], ["D3"]))
        for session, workspace in (("s-main", main), ("s-wt", worktree)):
            log = self.status("A", session=session, workspace=workspace)["log"]
            self.assertEqual([(e["id"], e["session"]) for e in log["newest"]],
                             [("D1", "s-main"), ("D2", "s-wt"), ("E1", "s-wt"), ("D3", "s-main")])

    def test_concurrent_appends_from_different_sessions_get_unique_ids(self):
        sessions = ["c%d" % i for i in range(6)]
        for session in sessions:
            bind(self.root, session, {})
        procs = [subprocess.Popen(self.command("record", "--expected", "null", "--request-id", "r1", session=session),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=self.env())
                 for session in sessions]
        for proc, session in zip(procs, sessions):
            proc.stdin.write(json.dumps(self.request(decisions=[{"text": "by " + session}, {"text": "again"}])))
            proc.stdin.close()
        outputs = []
        for proc in procs:
            outputs.append(json.loads(proc.stdout.read()))
            proc.wait(timeout=60)
            proc.stdout.close()
            proc.stderr.close()
        self.assertTrue(all("log_entries" in out for out in outputs), outputs)
        ids = [ident for out in outputs for ident in out["log_entries"]]
        self.assertEqual(sorted(ids, key=lambda i: int(i[1:])), ["D%d" % i for i in range(1, 13)])
        for out in outputs:  # one record's entries are contiguous
            first, second = (int(i[1:]) for i in out["log_entries"])
            self.assertEqual(second, first + 1)
        self.assertEqual(self.status("A")["log"]["total"], 12)

    def test_version_1_cards_stay_readable_and_continue_as_version_2(self):
        legacy = dict(self.payload("legacy next"), work_id="A", decisions=["old decision"],
                      evidence=[{"claim": "old claim", "ref": "r", "state": "pass-observed"}])
        old = self.record_v1(self.root, "s1", legacy, None, "old")
        status = self.status()
        self.assertEqual((status["expected"], status["checkpoint"]["schema_version"]), (old["revision"], 1))
        self.assertEqual(status["checkpoint"]["payload"]["decisions"], ["old decision"])
        self.assertIn("old decision", recovery_context(self.root, "s1"))
        new = self.record(self.request(decisions=[{"text": "new decision"}]), old["revision"], "r2")
        self.assertEqual((new["schema_version"], new["parent_record"], new["log_entries"]), (2, old["revision"], ["D1"]))
        self.assertNotIn("decisions", new["payload"])

    def test_resume_adopts_version_1_and_version_2_snapshots(self):
        legacy = dict(self.payload("legacy next"), work_id="A", decisions=["old decision"],
                      evidence=[{"claim": "old claim", "ref": "r", "state": "pass-observed"}])
        self.record_v1(self.root, "s1", legacy, None, "old")
        v1 = store.snapshot(self.root, "s1", "e1")
        bind(self.root, "s2", {})
        code, adopted = self.runtime("resume", "--from", v1["snapshot_id"], session="s2")
        self.assertEqual(code, 0, adopted)
        self.assertEqual((adopted["schema_version"], adopted["payload"]["decisions"],
                          adopted["payload"]["evidence"][0]["state"]), (1, ["old decision"], "unverified"))
        card = self.record(self.request("B", decisions=[{"text": "b decision"}]), request_id="b1")
        v2 = store.snapshot(self.root, "s1", "e2")
        self.assertEqual(v2["revision"], card["revision"])
        bind(self.root, "s3", {})
        code, adopted = self.runtime("resume", "--from", v2["snapshot_id"], session="s3")
        self.assertEqual(code, 0, adopted)
        self.assertEqual((adopted["schema_version"], adopted["log_entries"]), (2, []))
        self.assertEqual(self.status("B", session="s3")["log"]["total"], 1)  # resume wrote nothing to the log

    def test_recovery_digest_of_a_version_2_card_has_no_decisions(self):
        data = self.request(decisions=[{"text": "logged, not in the card"}])
        data["sources"] = [{"kind": "file", "ref": "src/%d/%s" % (i, "s" * 500)} for i in range(10)]  # not in the digest
        self.record(data)
        text = recovery_context(self.root, "s1")
        self.assertIn("Observed payload digest", text)
        digest = json.loads(text.split("Observed payload digest (full JSON in the checkpoint file): ", 1)[1])
        self.assertNotIn("decisions", digest)
