import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _support import FixtureCase, REPO
from provost_runtime import store
from provost_runtime.config import workspace_key
from provost_runtime.hooks import bind, handle_event, recovery_context

MARK = "Checkpoint source: legacy store (read-only)"


class LegacyCase(FixtureCase):
    """Fixtures written the way the pre-S1 store did: <workspace>/.provost/handoffs/v1."""

    def run_cli(self, *args, data=None):
        env = dict(os.environ, PYTHONPATH=str(REPO / "scripts"), PROVOST_CONFIG=str(self.config_path),
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-m", "provost_runtime.cli", *args],
                              input=json.dumps(data) if data is not None else "", capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=30)

    def cli_json(self, *args, data=None):
        return json.loads(self.run_cli(*args, data=data).stdout)

    def legacy_dir(self, workspace, session):
        return Path(workspace) / ".provost" / "handoffs" / "v1" / "sessions" / store.digest(session)

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(store.encoded(data))

    def legacy_record(self, workspace, session, request="r1", next_step="legacy step", parent=None,
                      workspace_id=None):
        revision = store.digest(session + "\0record\0" + request)
        data = {"schema_version": 1, "kind": "record",
                "workspace_id": workspace_id or workspace_key(Path(workspace)),
                "session_id": session, "revision": revision, "parent_record": parent,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "transcript_cursor": None, "adopted_from": None, "payload": self.payload(next_step)}
        data = store._seal(data)
        directory = self.legacy_dir(workspace, session)
        self.put(directory / "records" / (revision + ".json"), data)
        self.put(directory / "current.json", {"revision": revision})
        return data

    def legacy_snapshot(self, workspace, session, source, event="e1"):
        ident = store.digest(session + "\0snapshot\0" + event)
        data = {k: v for k, v in source.items() if k != "checksum"}
        data.update(kind="snapshot", snapshot_id=ident, captured_at=datetime.now(timezone.utc).isoformat(),
                    parent_snapshot=None)
        data = store._seal(data)
        directory = self.legacy_dir(workspace, session)
        self.put(directory / "snapshots" / (ident + ".json"), data)
        self.put(directory / "latest.json", {"snapshot_id": ident})
        return data

    def legacy_binding(self, workspace, session, workspace_id=None, transcript="t.jsonl"):
        path = self.legacy_dir(workspace, session) / "binding.json"
        self.put(path, {"workspace_id": workspace_id or workspace_key(Path(workspace)), "session_id": session,
                        "worktree_path": str(workspace), "transcript_path": transcript, "last_cursor": None})
        return path

    def new_sessions(self):
        return sorted(p.relative_to(self.store_home).as_posix() for p in self.store_home.rglob("*"))

    def tree(self, workspace):
        base = Path(workspace) / ".provost"
        found = []
        for dirpath, dirs, files in os.walk(base):
            for name in dirs + files:
                path = Path(dirpath) / name
                info = path.stat()
                found.append((str(path.relative_to(base)), info.st_size, info.st_mtime_ns))
        return sorted(found)


class LegacyFallback(LegacyCase):
    def test_status_reads_legacy_checkpoint_when_new_store_is_empty(self):
        written = self.legacy_record(self.root, "s1")
        bind(self.root, "s1", {})
        status = self.cli_json("status", "--workspace", str(self.root), "--session", "s1")
        self.assertIsNone(status["expected"])
        self.assertIs(status["legacy"], True)
        self.assertEqual(status["checkpoint"]["revision"], written["revision"])
        self.assertEqual(status["checkpoint"]["payload"]["next"], "legacy step")
        self.assertIn(".provost", status["legacy_path"])
        self.assertIn(written["revision"], status["legacy_path"])

    def test_status_falls_back_to_a_legacy_snapshot_without_a_record(self):
        source = self.legacy_record(self.root, "s1")
        snap = self.legacy_snapshot(self.root, "s1", source)
        (self.legacy_dir(self.root, "s1") / "current.json").unlink()
        (self.legacy_dir(self.root, "s1") / "records" / (source["revision"] + ".json")).unlink()
        bind(self.root, "s1", {})
        status = self.cli_json("status", "--workspace", str(self.root), "--session", "s1")
        self.assertIs(status["legacy"], True)
        self.assertEqual(status["checkpoint"]["snapshot_id"], snap["snapshot_id"])
        self.assertIsNone(status["expected"])

    def test_recovery_context_labels_the_legacy_source(self):
        self.legacy_record(self.root, "s1")
        text = recovery_context(self.root, "s1")
        self.assertTrue(text.startswith(MARK), text)
        self.assertIn(".provost", text)
        self.assertIn("legacy step", text)
        self.assertIn("Saved observations, not current facts.", text)
        self.assertIn("read the checkpoint file for full details.", text)

    def test_compact_injection_carries_the_legacy_checkpoint(self):
        self.legacy_record(self.root, "s1")
        result = handle_event({"hook_event_name": "SessionStart", "cwd": str(self.root),
                               "session_id": "s1", "source": "compact"}, self.config_path)
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(MARK, text)
        self.assertIn("legacy step", text)

    def test_new_store_record_wins_over_legacy(self):
        self.legacy_record(self.root, "s1")
        bind(self.root, "s1", {})
        written = self.cli_json("record", "--workspace", str(self.root), "--session", "s1",
                                "--expected", "null", "--request-id", "n1", data=self.payload("new step"))
        status = self.cli_json("status", "--workspace", str(self.root), "--session", "s1")
        self.assertEqual(status["expected"], written["revision"])
        self.assertNotIn("legacy", status)
        self.assertEqual(status["checkpoint"]["payload"]["next"], "new step")
        text = recovery_context(self.root, "s1")
        self.assertNotIn("legacy", text.lower())
        self.assertIn("new step", text)
        self.assertNotIn("legacy step", text)

    def test_new_store_snapshot_alone_also_ends_the_fallback(self):
        self.legacy_record(self.root, "s1")
        store.record(self.root, "s1", self.payload("new step"), None, "n1")
        store.snapshot(self.root, "s1", "e1")
        for path in (store.session_dir(self.root, "s1") / "records").glob("*.json"):
            path.unlink()
        self.assertNotIn(MARK, recovery_context(self.root, "s1"))

    def test_other_sessions_legacy_checkpoint_is_not_used(self):
        self.legacy_record(self.root, "other")
        self.assertIn("No checkpoint for this session", recovery_context(self.root, "s1"))

    def test_worktree_finds_the_legacy_store_of_the_main_checkout(self):
        main, worktree = self.git_repo_with_worktree()
        self.cfg["workspace_roots"] = [str(main), str(worktree)]
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.legacy_record(main, "s1")
        bind(worktree, "s1", {})
        status = self.cli_json("status", "--workspace", str(worktree), "--session", "s1")
        self.assertIs(status["legacy"], True)
        self.assertEqual(status["checkpoint"]["payload"]["next"], "legacy step")
        self.assertIn(MARK, recovery_context(store.locate(worktree), "s1"))


class LegacyBinding(LegacyCase):
    """A session bound before the store switch keeps working without a new SessionStart."""

    def test_status_accepts_a_legacy_only_binding_without_writing(self):
        written = self.legacy_record(self.root, "s1")
        self.legacy_binding(self.root, "s1")
        before = self.tree(self.root)
        status = self.cli_json("status", "--workspace", str(self.root), "--session", "s1")
        self.assertIs(status["legacy_binding"], True)
        self.assertIs(status["legacy"], True)
        self.assertEqual(status["checkpoint"]["revision"], written["revision"])
        self.assertEqual(self.tree(self.root), before)
        self.assertEqual(self.new_sessions(), [])

    def test_record_creates_the_new_binding_and_later_status_is_plain(self):
        self.legacy_binding(self.root, "s1", transcript="legacy-transcript.jsonl")
        before = self.tree(self.root)
        written = self.cli_json("record", "--workspace", str(self.root), "--session", "s1",
                                "--expected", "null", "--request-id", "n1", data=self.payload("new step"))
        self.assertIn("revision", written)
        self.assertEqual(self.tree(self.root), before)
        bound = bind_read(self.root, "s1")
        self.assertEqual(bound["transcript_path"], "legacy-transcript.jsonl")
        status = self.cli_json("status", "--workspace", str(self.root), "--session", "s1")
        self.assertNotIn("legacy_binding", status)
        self.assertEqual(status["expected"], written["revision"])

    def test_snapshot_creates_the_new_binding_from_a_legacy_one(self):
        self.legacy_binding(self.root, "s1")
        self.cli_json("record", "--workspace", str(self.root), "--session", "s1",
                      "--expected", "null", "--request-id", "n1", data=self.payload("new step"))
        (store.session_dir(store.locate(self.root), "s1") / "binding.json").unlink()
        snap = self.cli_json("snapshot", "--workspace", str(self.root), "--session", "s1", "--request-id", "e1")
        self.assertIn("snapshot_id", snap)
        self.assertEqual(bind_read(self.root, "s1")["session_id"], "s1")

    def test_legacy_binding_of_another_workspace_is_a_mismatch_and_writes_nothing(self):
        self.legacy_binding(self.root, "s1", workspace_id=workspace_key(self.root / "elsewhere"))
        before = self.tree(self.root)
        for args in (("status",), ("record", "--expected", "null", "--request-id", "n1"),
                     ("snapshot", "--request-id", "e1")):
            result = self.run_cli(args[0], "--workspace", str(self.root), "--session", "s1", *args[1:],
                                  data=self.payload())
            self.assertEqual(json.loads(result.stdout), {"error": "BINDING_MISMATCH"}, args)
        self.assertEqual(self.tree(self.root), before)
        self.assertEqual(self.new_sessions(), [])

    def test_unreadable_legacy_binding_is_reported(self):
        path = self.legacy_binding(self.root, "s1")
        path.write_bytes(b"{not json")
        result = self.run_cli("status", "--workspace", str(self.root), "--session", "s1")
        self.assertEqual(json.loads(result.stdout), {"error": "BINDING_UNREADABLE"})

    def test_no_binding_anywhere_is_not_bound(self):
        self.legacy_record(self.root, "s1")
        for args in (("status",), ("record", "--expected", "null", "--request-id", "n1"),
                     ("snapshot", "--request-id", "e1")):
            result = self.run_cli(args[0], "--workspace", str(self.root), "--session", "s1", *args[1:],
                                  data=self.payload())
            self.assertEqual(json.loads(result.stdout), {"error": "SESSION_NOT_BOUND"}, args)
        self.assertEqual(self.new_sessions(), [])

    def test_resume_still_requires_a_new_store_binding(self):
        self.legacy_binding(self.root, "new")
        result = self.run_cli("resume", "--workspace", str(self.root), "--session", "new", "--from", "a" * 64)
        self.assertEqual(json.loads(result.stdout), {"error": "SESSION_NOT_BOUND"})

    def test_worktree_finds_the_main_checkouts_legacy_binding(self):
        main, worktree = self.git_repo_with_worktree()
        self.cfg["workspace_roots"] = [str(main), str(worktree)]
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.legacy_binding(main, "s1")
        status = self.cli_json("status", "--workspace", str(worktree), "--session", "s1")
        self.assertIs(status["legacy_binding"], True)
        written = self.cli_json("record", "--workspace", str(worktree), "--session", "s1",
                                "--expected", "null", "--request-id", "n1", data=self.payload("wt step"))
        self.assertIn("revision", written)
        self.assertEqual(bind_read(worktree, "s1")["worktree_path"], str(store.locate(worktree).root))


def bind_read(workspace, session):
    from provost_runtime.hooks import binding
    return binding(store.locate(workspace), session)


class LegacyValidation(LegacyCase):
    def test_tampered_legacy_checksum_is_an_error_not_a_checkpoint(self):
        written = self.legacy_record(self.root, "s1")
        path = self.legacy_dir(self.root, "s1") / "records" / (written["revision"] + ".json")
        path.write_bytes(path.read_bytes().replace(b"legacy step", b"forged step"))
        bind(self.root, "s1", {})
        result = self.run_cli("status", "--workspace", str(self.root), "--session", "s1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"error": "CORRUPT_RECORD"})
        with self.assertRaisesRegex(store.StoreError, "CORRUPT_RECORD"):
            recovery_context(self.root, "s1")

    def test_legacy_object_of_another_workspace_is_an_identity_mismatch(self):
        self.legacy_record(self.root, "s1", workspace_id=workspace_key(self.root / "elsewhere"))
        bind(self.root, "s1", {})
        result = self.run_cli("status", "--workspace", str(self.root), "--session", "s1")
        self.assertEqual(json.loads(result.stdout), {"error": "RECORD_IDENTITY_MISMATCH"})
        with self.assertRaisesRegex(store.StoreError, "RECORD_IDENTITY_MISMATCH"):
            recovery_context(self.root, "s1")

    def test_incomplete_legacy_chain_is_an_error(self):
        first = self.legacy_record(self.root, "s1", request="r1")
        self.legacy_record(self.root, "s1", request="r2", parent=first["revision"])
        (self.legacy_dir(self.root, "s1") / "records" / (first["revision"] + ".json")).unlink()
        with self.assertRaisesRegex(store.StoreError, "INCOMPLETE_CHAIN"):
            recovery_context(self.root, "s1")

    def test_linked_legacy_store_is_rejected(self):
        outside = self.root / "outside"
        self.legacy_record(outside, "s1")
        link = self.root / ".provost"
        try:
            os.symlink(outside / ".provost", link, target_is_directory=True)
        except OSError:
            try:
                import _winapi
                _winapi.CreateJunction(str(outside / ".provost"), str(link))
            except (ImportError, AttributeError, OSError):
                self.skipTest("OS permits neither symlinks nor junctions")
        with self.assertRaisesRegex(store.StoreError, "LINKED_STORAGE"):
            recovery_context(self.root, "s1")


class LegacyReadOnly(LegacyCase):
    def test_no_command_writes_under_the_legacy_directories(self):
        record = self.legacy_record(self.root, "s1")
        snap = self.legacy_snapshot(self.root, "s1", record)
        bind(self.root, "s2", {})
        bind(self.root, "s1", {})
        before = self.tree(self.root)
        self.assertTrue(before)
        recovery_context(self.root, "s1")
        for args in (("status", "--session", "s1"), ("list",), ("list", "--legacy"), ("doctor",),
                     ("resume", "--session", "s2", "--from", snap["snapshot_id"])):
            result = self.run_cli(args[0], "--workspace", str(self.root), *args[1:])
            self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        self.assertEqual(self.tree(self.root), before)
        self.assertEqual([p.name for p in (self.root / ".provost" / "handoffs" / "v1").iterdir()], ["sessions"])


class LegacyListAndResume(LegacyCase):
    def seeded(self):
        record = self.legacy_record(self.root, "old")
        return self.legacy_snapshot(self.root, "old", record)

    def test_list_legacy_shows_legacy_snapshots_and_plain_list_does_not(self):
        snap = self.seeded()
        self.assertEqual(self.cli_json("list", "--workspace", str(self.root)), [])
        listed = self.cli_json("list", "--workspace", str(self.root), "--legacy")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["snapshot_id"], snap["snapshot_id"])
        self.assertEqual((listed[0]["session_id"], listed[0]["work_id"], listed[0]["legacy"]),
                         ("old", "fixture", True))

    def test_list_legacy_reports_a_corrupt_snapshot_as_an_error_entry(self):
        snap = self.seeded()
        path = self.legacy_dir(self.root, "old") / "snapshots" / (snap["snapshot_id"] + ".json")
        path.write_bytes(path.read_bytes().replace(b"legacy step", b"forged step"))
        listed = self.cli_json("list", "--workspace", str(self.root), "--legacy")
        self.assertEqual(listed, [{"snapshot_id": snap["snapshot_id"], "error": "CORRUPT_RECORD", "legacy": True}])

    def test_resume_adopts_a_legacy_snapshot_into_the_new_store(self):
        snap = self.seeded()
        bind(self.root, "new", {})
        result = self.cli_json("resume", "--workspace", str(self.root), "--session", "new",
                               "--from", snap["snapshot_id"])
        self.assertEqual(result["adopted_from"], snap["snapshot_id"])
        self.assertEqual(result["payload"]["next"], "legacy step")
        adopted = store.current(self.root, "new")
        self.assertEqual(adopted["revision"], result["revision"])
        self.assertEqual(adopted["workspace_id"], store.locate(self.root).repo_key)
        self.assertIn("reconciliation", result)
        self.assertNotIn(MARK, result["reconciliation"])

    def test_resume_marks_adopted_state_as_unverified(self):
        record = self.legacy_record(self.root, "old")
        payload = self.payload()
        payload["assignments"] = [{"task": "t", "agent_id": "a", "observed_state": "running", "write_scope": []}]
        payload["evidence"] = [{"claim": "c", "ref": "r", "state": "pass-observed"}]
        record = self.legacy_record(self.root, "old", request="r2", parent=record["revision"])
        record = store._seal({**{k: v for k, v in record.items() if k != "checksum"}, "payload": payload})
        directory = self.legacy_dir(self.root, "old")
        self.put(directory / "records" / (record["revision"] + ".json"), record)
        snap = self.legacy_snapshot(self.root, "old", record)
        bind(self.root, "new", {})
        self.cli_json("resume", "--workspace", str(self.root), "--session", "new", "--from", snap["snapshot_id"])
        adopted = store.current(self.root, "new")["payload"]
        self.assertEqual(adopted["assignments"][0]["observed_state"], "needs-verification")
        self.assertEqual(adopted["evidence"][0]["state"], "unverified")

    def test_resume_of_the_legacy_snapshots_own_session_is_rejected(self):
        snap = self.seeded()
        bind(self.root, "old", {})
        result = self.run_cli("resume", "--workspace", str(self.root), "--session", "old",
                              "--from", snap["snapshot_id"])
        self.assertEqual(json.loads(result.stdout), {"error": "SAME_SESSION_ADOPTION"})
        self.assertIsNone(store.current(self.root, "old"))

    def test_resume_of_an_unknown_snapshot_still_reports_not_found(self):
        bind(self.root, "new", {})
        result = self.run_cli("resume", "--workspace", str(self.root), "--session", "new", "--from", "a" * 64)
        self.assertEqual(json.loads(result.stdout), {"error": "SNAPSHOT_NOT_FOUND"})


class LegacyDoctor(LegacyCase):
    def test_doctor_reports_legacy_store_presence(self):
        self.assertIs(self.cli_json("doctor", "--workspace", str(self.root))["legacy_store_present"], False)
        self.assertNoStore()
        self.legacy_record(self.root, "s1")
        self.assertIs(self.cli_json("doctor", "--workspace", str(self.root))["legacy_store_present"], True)

    def test_doctor_sees_the_main_checkouts_legacy_store_from_a_worktree(self):
        main, worktree = self.git_repo_with_worktree()
        self.cfg["workspace_roots"] = [str(main), str(worktree)]
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.assertIs(self.cli_json("doctor", "--workspace", str(worktree))["legacy_store_present"], False)
        self.legacy_record(main, "s1")
        self.assertIs(self.cli_json("doctor", "--workspace", str(worktree))["legacy_store_present"], True)
