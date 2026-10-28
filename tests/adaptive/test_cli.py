import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from _support import FixtureCase, REPO
from provost_runtime.hooks import bind


class CliTests(FixtureCase):
    def run_cli(self, *args, data=None, scripts=None, env=None, stdin=None):
        env = dict(os.environ, PYTHONPATH=str(scripts or REPO / "scripts"),
                   PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1", **(env or {}))
        return subprocess.run([sys.executable, "-m", "provost_runtime.cli", *args],
                              input=stdin if stdin is not None else json.dumps(data) if data is not None else "",
                              capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=30)

    def test_doctor_is_read_only_and_reports_selected_mode(self):
        result = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["mode"], "adaptive")
        self.assertNoStore()

    def test_doctor_reports_legacy_for_an_excluded_workspace(self):
        self.config_path.write_text(json.dumps(dict(self.cfg, all_workspaces=True, exclude_roots=[str(self.root)])),
                                    encoding="utf-8")
        result = self.run_cli("doctor", "--workspace", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data["mode"], data["handoff"]), ("legacy", False))
        self.assertNoStore()

    def test_doctor_reports_store_home_and_repo_key(self):
        from provost_runtime.config import workspace_key
        data = json.loads(self.run_cli("doctor", "--workspace", str(self.root)).stdout)
        self.assertEqual(data.get("store_home"), str(self.store_home))
        self.assertEqual(data.get("repo_key"), workspace_key(self.root))
        self.assertNoStore()

    def test_worktree_and_main_checkout_read_each_other_through_status(self):
        main, worktree = self.git_repo_with_worktree()
        self.cfg["workspace_roots"] = [str(main), str(worktree)]
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        bind(worktree, "s1", {})
        written = self.run_cli("record", "--workspace", str(worktree), "--session", "s1",
                               "--expected", "null", "--request-id", "r1", data=self.payload("wt"))
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        revision = json.loads(written.stdout)["revision"]
        status = json.loads(self.run_cli("status", "--workspace", str(main), "--session", "s1").stdout)
        self.assertEqual((status.get("expected"), status["checkpoint"]["payload"]["next"]), (revision, "wt"))
        written = self.run_cli("record", "--workspace", str(main), "--session", "s1",
                               "--expected", revision, "--request-id", "r2", data=self.payload("main"))
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        status = json.loads(self.run_cli("status", "--workspace", str(worktree), "--session", "s1").stdout)
        self.assertEqual(status["checkpoint"]["payload"]["next"], "main")

    def test_allowlisted_main_checkout_enables_its_worktree_sessions(self):
        from provost_runtime.hooks import handle_event
        from provost_runtime.store import record, session_dir
        main, worktree = self.git_repo_with_worktree()
        self.cfg["workspace_roots"] = [str(main)]
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        output = handle_event({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1",
                               "cwd": str(worktree)}, self.config_path)
        self.assertIn("Native handoff runtime active", output.get("hookSpecificOutput", {}).get("additionalContext", ""))
        self.assertTrue((session_dir(main, "s1") / "binding.json").is_file())
        revision = record(main, "s1", self.payload("main"), None, "r1")["revision"]
        status = self.run_cli("status", "--workspace", str(worktree), "--session", "s1")
        self.assertEqual(status.returncode, 0, status.stdout)
        self.assertEqual(json.loads(status.stdout)["expected"], revision)

    def test_record_snapshot_list_and_resume_roundtrip(self):
        bind(self.root, "s1", {})
        bind(self.root, "s2", {})
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1",
                              "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.run_cli("snapshot", "--workspace", str(self.root), "--session", "s1", "--request-id", "e1")
        ident = json.loads(data.stdout)["snapshot_id"]
        listings = self.run_cli("list", "--workspace", str(self.root))
        self.assertEqual(len(json.loads(listings.stdout)), 1)
        result = self.run_cli("resume", "--workspace", str(self.root), "--session", "s2", "--from", ident)
        self.assertEqual(json.loads(result.stdout)["adopted_from"], ident)

    def test_disabled_manual_write_is_rejected(self):
        self.config_path.unlink()
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1",
                              "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 1)
        self.assertNoStore()

    def test_malformed_hook_is_nonblocking_and_does_not_echo_input(self):
        result = self.run_cli("native-hook", data=["sensitive-marker"])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("sensitive-marker", result.stdout)
        self.assertNotIn("block", json.loads(result.stdout).get("decision", ""))

    def test_record_input_file_replaces_stdin_and_keeps_limits(self):
        bind(self.root, "s1", {})
        args = ("record", "--workspace", str(self.root), "--session", "s1", "--expected", "null")
        path = self.root / "payload.json"
        path.write_text(json.dumps(self.payload("from file")), encoding="utf-8")
        result = self.run_cli(*args, "--request-id", "r1", "--input", str(path), data={"ignored": "stdin"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["payload"]["next"], "from file")
        big = self.root / "big.json"
        big.write_text(json.dumps(self.payload("é" * 20000), ensure_ascii=False), encoding="utf-8")
        result = self.run_cli(*args, "--request-id", "r2", "--input", str(big))
        self.assertEqual(json.loads(result.stdout), {"error": "PAYLOAD_TOO_LARGE"})
        for name, content in (("missing.json", None), ("bad.json", "{broken")):
            target = self.root / name
            if content is not None:
                target.write_text(content, encoding="utf-8")
            result = self.run_cli(*args, "--request-id", "r3", "--input", str(target))
            self.assertEqual(json.loads(result.stdout), {"error": "INPUT_OR_IO_ERROR"}, name)

    def test_record_reads_stdin_as_utf8_regardless_of_locale(self):
        bind(self.root, "s1", {})
        # Simulate a non-UTF-8 console: stdin's text encoding must not decide the payload.
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1", "--expected", "null",
                              "--request-id", "r1", stdin=json.dumps(self.payload("é 中文 ✓"), ensure_ascii=False),
                              env={"PYTHONIOENCODING": "latin-1", "PYTHONUTF8": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["payload"]["next"], "é 中文 ✓")

    def copy_runtime(self, name, newline=None):
        root = self.root / name
        (root / "scripts").mkdir(parents=True)
        (root / "hooks").mkdir()
        shutil.copytree(REPO / "scripts" / "provost_runtime", root / "scripts" / "provost_runtime",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for script in ("runtime", "native-event", "_python.sh"):
            shutil.copyfile(REPO / "hooks" / script, root / "hooks" / script)
        if newline:
            for path in [*(root / "scripts" / "provost_runtime").glob("*.py"), *(root / "hooks").iterdir()]:
                text = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline)
                path.write_bytes(text)
        return root

    def doctor(self, runtime):
        result = self.run_cli("doctor", "--workspace", str(self.root), scripts=runtime / "scripts")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_doctor_reports_runtime_root_and_hash(self):
        data = json.loads(self.run_cli("doctor", "--workspace", str(self.root)).stdout)
        self.assertEqual(Path(data["runtime_root"]).resolve(), REPO.resolve())
        self.assertRegex(data["runtime_hash"], r"^[0-9a-f]{64}$")
        for key in ("mode", "python_supported", "handoff", "storage_ready", "storage_error", "host_verified"):
            self.assertIn(key, data)

    def test_runtime_hash_ignores_crlf_but_tracks_content_and_absence(self):
        lf, crlf = self.copy_runtime("lf", b"\n"), self.copy_runtime("crlf", b"\r\n")
        self.assertEqual(Path(self.doctor(crlf)["runtime_root"]).resolve(), crlf.resolve())
        base = self.doctor(lf)["runtime_hash"]
        self.assertEqual(self.doctor(crlf)["runtime_hash"], base)
        with (crlf / "hooks" / "runtime").open("ab") as handle:
            handle.write(b"# changed\r\n")
        self.assertNotEqual(self.doctor(crlf)["runtime_hash"], base)
        (lf / "hooks" / "native-event").unlink()
        self.assertNotEqual(self.doctor(lf)["runtime_hash"], base)

    def test_runtime_hash_covers_the_shared_python_resolver(self):
        runtime = self.copy_runtime("resolver")
        base = self.doctor(runtime)["runtime_hash"]
        (runtime / "hooks" / "_python.sh").unlink()
        self.assertNotEqual(self.doctor(runtime)["runtime_hash"], base)

    def test_digest_prints_files_and_digest_the_stop_check_accepts(self):
        from provost_runtime.acceptance import unmet_state
        source = self.root / "src" / "app.py"
        source.parent.mkdir()
        source.write_bytes(b"print('reviewed')\n")
        (self.root / "README.md").write_bytes(b"# r\n")
        self.config_path.unlink()  # no config, no handoff: the digest still works
        result = self.run_cli("digest", "--cwd", str(self.root), "src/app.py", "README.md", "src/app.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(set(data), {"files", "digest"})
        self.assertEqual(data["files"], sorted({str(self.root / "src" / "app.py"), str(self.root / "README.md")}))
        review = {"id": "code-review", "kind": "review", "check": "c", "status": "pass", **data}
        self.assertIsNone(unmet_state(review, None, str(self.root)))
        source.write_bytes(b"print('changed')\n")
        self.assertIn("reviewed files changed", unmet_state(review, None, str(self.root)))

    def test_digest_of_a_missing_file_or_no_file_is_an_error(self):
        for args in (("--cwd", str(self.root), "missing.py"), ("--cwd", str(self.root))):
            with self.subTest(args):
                result = self.run_cli("digest", *args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("error", json.loads(result.stdout))

    def test_record_after_transcript_moved_carries_a_cursor(self):
        bound = self.root / "projects" / "D--Repo" / "sess.jsonl"
        bind(self.root, "s1", {"transcript_path": str(bound)})
        moved = self.root / "projects" / "D--Repo--claude-worktrees-x" / "sess.jsonl"
        moved.parent.mkdir(parents=True)
        moved.write_bytes(b"{}\n")
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1",
                              "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["transcript_cursor"]["bytes"], 3)

    def test_record_clears_reminder_flags_and_tolerates_a_missing_dir(self):
        bind(self.root, "s1", {})
        facts = self.config_path.parent / "cache" / "facts"
        facts.mkdir(parents=True)
        keep = [facts / "s1.jsonl", facts / "s2.pending-edit", facts / "s1.enabled"]
        flags = [facts / f"s1.{name}" for name in ("pending-edit", "pending-dispatch", "reminded")]
        args = ("record", "--workspace", str(self.root), "--session", "s1", "--expected", "null")
        outputs = []
        for _ in range(2):  # the idempotent replay clears re-armed flags too
            for path in keep + flags:
                path.touch()
            result = self.run_cli(*args, "--request-id", "r1", data=self.payload())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outputs.append(result.stdout)
            self.assertEqual([p.name for p in flags if p.exists()], [])
            self.assertTrue(all(p.exists() for p in keep))
        self.assertEqual(json.loads(outputs[0]), json.loads(outputs[1]))
        self.assertNotIn("error", json.loads(outputs[0]))
        shutil.rmtree(facts.parent)
        result = self.run_cli(*args[:-1], json.loads(outputs[0])["revision"], "--request-id", "r2",
                              data=self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(facts.parent.exists())

    def test_done_record_closes_the_declaration_window(self):
        bind(self.root, "s1", {})
        facts = self.config_path.parent / "cache" / "facts"
        facts.mkdir(parents=True)
        window = [facts / f"s1.{name}" for name in ("declared", "undeclared", "declare-reminded")]
        cursor = facts / "s1.declare-cursor"
        for path in window + [cursor]:
            path.touch()
        args = ("record", "--workspace", str(self.root), "--session", "s1")
        result = self.run_cli(*args, "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all(p.exists() for p in window))  # a work in progress keeps its declaration
        result = self.run_cli(*args, "--expected", json.loads(result.stdout)["revision"], "--request-id", "r2",
                              data=dict(self.payload(), phase=" Done "))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([p.name for p in window if p.exists()], [])
        self.assertTrue(cursor.exists())

    def test_reminder_rearms_after_record(self):
        hook = REPO / "hooks" / "native-facts"
        bind(self.root, "s1", {})
        marker = self.enable_facts("s1")

        def run(data):
            result = self.run_bash(hook, stdin=json.dumps({"session_id": "s1", **data}))
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        edit = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}}
        prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "next"}
        self.assertEqual(run(edit), {})
        self.assertIn("hookSpecificOutput", run(prompt))
        self.assertEqual(run(prompt), {})
        result = self.run_cli("record", "--workspace", str(self.root), "--session", "s1",
                              "--expected", "null", "--request-id", "r1", data=self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(run(prompt), {})
        self.assertTrue(marker.exists())  # record clears the reminder flags, never the enablement marker
        run(edit)
        self.assertIn("hookSpecificOutput", run(prompt))

    def test_doctor_host_verified_follows_binding_evidence(self):
        data = json.loads(self.run_cli("doctor", "--workspace", str(self.root)).stdout)
        self.assertIs(data["host_verified"], False)
        self.assertIsNone(data["last_binding_at"])
        self.assertNoStore()
        bind(self.root, "s1", {})
        data = json.loads(self.run_cli("doctor", "--workspace", str(self.root)).stdout)
        self.assertIs(data["host_verified"], True)
        self.assertRegex(data["last_binding_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00$")

    def test_status_reports_facts_and_prefills_recent_sources(self):
        from provost_runtime.store import split_request
        bind(self.root, "s1", {})
        args = ("status", "--workspace", str(self.root), "--session", "s1")
        status = json.loads(self.run_cli(*args).stdout)
        self.assertIsNone(status["facts"])
        self.assertEqual(status["template"]["sources"], [])
        path = self.seed_facts(files=12)
        status = json.loads(self.run_cli(*args).stdout)
        facts = status["facts"]
        self.assertEqual(facts["path"], str(path))
        self.assertEqual([(r["dirty"], r["dirty_total"], r["commits"], r["branch"]) for r in facts["repos"]],
                         [(2, 7, 1, "main")])
        self.assertEqual((facts["agents"], facts["outside_repo_count"], facts["files"]),
                         ({"code-reviewer": 1, "implementer": 1}, 4, 12))
        self.assertEqual((facts["events_captured"], facts["spool_errors"]), (15, 0))
        template = status["template"]
        self.assertEqual(set(template), {"work_id", "phase", "sources", "assignments", "blockers", "next",
                                         "suggested_skills", "new_decisions", "new_evidence"})
        self.assertEqual([s["ref"][:13] for s in template["sources"]], ["C:/repo/f%04d" % i for i in range(11, 1, -1)])
        self.assertTrue(all(s["kind"] == "file" and set(s) == {"kind", "ref"} for s in template["sources"]))
        split_request(dict(template, work_id="w", phase="p", next="n"))
        self.assertIsNone(status["expected"])

    def test_changes_is_registered_and_read_only(self):
        from provost_runtime.store import session_dir
        args = ("changes", "--workspace", str(self.root), "--session", "s1")
        self.assertEqual(json.loads(self.run_cli(*args).stdout), {"error": "SESSION_NOT_BOUND"})
        bind(self.root, "s1", {})
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data["session_id"], data["pending_events"], data["folded_at"]), ("s1", 0, None))
        self.assertEqual(data["facts"]["files"], [])
        self.assertFalse((session_dir(self.root, "s1") / "facts.json").exists())
        self.assertFalse((self.config_path.parent / "cache").exists())
