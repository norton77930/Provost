import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime import facts
from provost_runtime.store import session_dir

SESSION = "sess-1"


class FactsCase(FixtureCase):
    def setUp(self):
        super().setUp()
        # Test repos live under the system temp dir, which the production list treats
        # as outside every repo; keep only the ~/.claude patterns for these fixtures.
        patcher = mock.patch.object(facts, "OUTSIDE", ("/.claude/jobs/", "/.claude/projects/"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.isolate_user_git_config()
        self.now = int(time.time())

    def repo(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", cwd=path)
        self.commit(path, "init", self.now - 3600)
        return Path(self.git("rev-parse", "--show-toplevel", cwd=path))

    def commit(self, repo, subject, when, *files):
        for name in files:
            (repo / name).write_text(subject, encoding="utf-8")
            self.git("add", name, cwd=repo)
        env = dict(os.environ, GIT_AUTHOR_DATE="@%d +0000" % when, GIT_COMMITTER_DATE="@%d +0000" % when)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                        "--allow-empty", "-m", subject], cwd=repo, env=env, check=True, capture_output=True)

    def spool(self):
        return self.root / "cache" / "facts" / (SESSION + ".jsonl")

    def write(self, *events):
        self.spool().parent.mkdir(parents=True, exist_ok=True)
        with self.spool().open("ab") as stream:
            for event in events:
                line = event if isinstance(event, str) else json.dumps(event)
                stream.write((line + "\n").encode("utf-8"))

    def edit(self, path, ts, cwd, agent=""):
        return {"v": 1, "ts": ts, "k": "edit", "cwd": str(cwd), "agent": agent, "tool": "Edit", "p": str(path)}

    def dir_event(self, path, ts, cwd):
        return {"v": 1, "ts": ts, "k": "dir", "cwd": str(cwd), "agent": "", "p": str(path)}

    def agent(self, role, ts, cwd, bg=False):
        return {"v": 1, "ts": ts, "k": "agent", "cwd": str(cwd), "agent": "", "role": role,
                "desc": "do " + role, "bg": bg}

    def fold(self, workspace):
        return facts.fold(self.config_path, workspace, SESSION)

    def stored(self, workspace):
        path = session_dir(workspace, SESSION) / "facts.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def repo_entry(self, data, root):
        return next(r for r in data["repos"] if os.path.normcase(r["root"]) == os.path.normcase(str(root)))


class FoldTests(FactsCase):
    def test_edits_dedupe_by_path_with_counts_and_agent_tags(self):
        repo = self.repo("a")
        target = repo / "x.py"
        self.write(self.edit(target, self.now - 30, repo),
                   self.edit(str(target).replace("\\", "/"), self.now - 20, repo, agent="implementer"),
                   self.edit(repo / "y.py", self.now - 10, repo))
        data = self.fold(repo)
        files = {Path(f["path"]).name: f for f in data["files"]}
        self.assertEqual(set(files), {"x.py", "y.py"})
        self.assertEqual(files["x.py"]["count"], 2)
        self.assertEqual(files["x.py"]["last_ts"], self.now - 20)
        self.assertEqual(sorted(files["x.py"]["agents"]), ["", "implementer"])
        self.assertEqual(files["y.py"]["agents"], [""])
        self.assertEqual(data["outside_repo_count"], 0)
        self.assertEqual(self.stored(repo), data)

    def test_dispatches_are_listed_in_time_order(self):
        repo = self.repo("a")
        self.write(self.agent("code-reviewer", self.now - 5, repo, bg=True), self.agent("implementer", self.now - 9, repo))
        data = self.fold(repo)
        self.assertEqual([(a["role"], a["bg"], a["ts"]) for a in data["agents"]],
                         [("implementer", False, self.now - 9), ("code-reviewer", True, self.now - 5)])
        self.assertEqual(data["agents"][0]["desc"], "do implementer")

    def test_outside_repo_edits_are_only_counted(self):
        repo = self.repo("a")
        home = self.repo("home/.claude")  # ~/.claude is itself a git repo
        self.write(self.edit(home / "jobs" / "j1" / "tmp" / "scratch.py", self.now - 9, repo),
                   self.edit(str(home / "projects" / "p" / "memory" / "m.md").replace("\\", "/"), self.now - 8, repo),
                   self.edit(self.root / "loose" / "n.txt", self.now - 7, repo),
                   self.edit(home / "CLAUDE.md", self.now - 6, repo),
                   self.edit(repo / "x.py", self.now - 5, repo))
        data = self.fold(repo)
        self.assertEqual(data["outside_repo_count"], 3)
        self.assertEqual(sorted(Path(f["path"]).name for f in data["files"]), ["CLAUDE.md", "x.py"])

    def test_outside_patterns_match_either_slash_style_and_case(self):
        with mock.patch.object(facts, "OUTSIDE", facts.OUTSIDE_DEFAULT):
            for path in (r"C:\Users\u\AppData\Local\Temp\claude\x.py", "/tmp/x", "C:/Users/u/.CLAUDE/Jobs/1/a",
                         r"C:\Users\u\.claude\projects\p\memory\m.md"):
                self.assertTrue(facts._outside(path), path)
            self.assertFalse(facts._outside(r"C:\Users\u\.claude\CLAUDE.md"))

    def test_dirty_entries_come_from_git(self):
        repo = self.repo("a")
        self.commit(repo, "add x", self.now - 3000, "x.py")
        (repo / "x.py").write_text("changed", encoding="utf-8")
        (repo / "new.py").write_text("new", encoding="utf-8")
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        entry = self.repo_entry(self.fold(repo), repo)
        self.assertEqual(sorted((d["status"], d["path"]) for d in entry["dirty"]), [(" M", "x.py"), ("??", "new.py")])
        self.assertEqual(entry["dirty_total"], 2)
        self.assertIsNone(entry["error"])
        self.assertTrue(entry["branch"])

    def test_only_commits_after_first_touch_are_listed(self):
        repo = self.repo("a")
        self.commit(repo, "before touch", self.now - 500, "x.py")
        self.write(self.edit(repo / "x.py", self.now - 100, repo))
        self.commit(repo, "after touch " + "s" * 100, self.now - 50, "x.py")
        data = self.fold(repo)
        entry = self.repo_entry(data, repo)
        self.assertEqual([c["subject"] for c in entry["commits"]], ["after touch " + "s" * 68])
        self.assertEqual(entry["first_touched_at"], facts._iso(self.now - 100))
        self.assertEqual(entry["dirty"], [])
        self.assertEqual(data["last_change_at"], facts._iso(self.now - 50))

    def test_restored_file_shows_no_dirty_entry_and_dirty_marks_its_mtime(self):
        repo = self.repo("a")
        self.commit(repo, "add x", self.now - 3000, "x.py")
        (repo / "x.py").write_text("edited", encoding="utf-8")
        os.utime(repo / "x.py", (self.now - 50, self.now - 50))
        self.write(self.edit(repo / "x.py", self.now - 100, repo))
        dirty = facts.fold(self.config_path, repo, SESSION, now=self.now)
        self.assertEqual(dirty["last_change_at"], facts._iso(self.now - 50))
        self.git("checkout", "--", "x.py", cwd=repo)
        data = self.fold(repo)
        self.assertEqual(self.repo_entry(data, repo)["dirty"], [])
        self.assertEqual([Path(f["path"]).name for f in data["files"]], ["x.py"])

    def test_dir_events_discover_other_repos_relative_and_absolute(self):
        main, other, third = self.repo("a"), self.repo("b"), self.repo("c")
        (third / "sub").mkdir()
        self.write(self.dir_event("../b", self.now - 9, main), self.dir_event(third / "sub", self.now - 8, main))
        roots = {os.path.normcase(r["root"]) for r in self.fold(main)["repos"]}
        self.assertEqual(roots, {os.path.normcase(str(p)) for p in (main, other, third)})

    def test_worktree_events_resolve_names(self):
        repo = self.repo("a")
        worktree = repo / ".claude" / "worktrees" / "wt"
        self.git("worktree", "add", "-q", "--detach", str(worktree), cwd=repo)
        self.write({"v": 1, "ts": self.now - 9, "k": "worktree", "cwd": str(repo), "agent": "",
                    "tool": "EnterWorktree", "p": "wt"})
        data = self.fold(repo)
        self.assertEqual(data["worktrees"], [{"path": "wt", "tool": "EnterWorktree", "ts": self.now - 9}])
        self.repo_entry(data, Path(self.git("rev-parse", "--show-toplevel", cwd=worktree)))

    def test_only_five_most_recent_repos_are_kept(self):
        roots = [self.repo("r%d" % i) for i in range(7)]
        self.write(*[self.dir_event(root, self.now - 100 + i, roots[6]) for i, root in enumerate(roots)])
        data = self.fold(roots[6])
        kept = {os.path.normcase(r["root"]) for r in data["repos"]}
        self.assertEqual(kept, {os.path.normcase(str(r)) for r in roots[2:]})
        self.assertEqual(data["repos_omitted"], 2)

    def test_git_timeout_marks_repos_and_fold_still_writes(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", 3))
        with mock.patch.object(facts, "subprocess", mock.Mock(run=run, TimeoutExpired=subprocess.TimeoutExpired)):
            data = self.fold(repo)
        self.assertTrue(run.called)
        self.assertTrue(all(call.kwargs["timeout"] <= facts.GIT_TIMEOUT for call in run.call_args_list))
        self.assertEqual(self.repo_entry(data, repo)["error"], "GIT_TIMEOUT")
        self.assertEqual(len(data["files"]), 1)
        self.assertEqual(self.stored(repo), data)

    def test_git_failure_keeps_the_previous_git_facts_of_a_repo(self):
        repo = self.repo("a")
        self.write(self.dir_event(repo, self.now - 500, repo))
        self.commit(repo, "during session", self.now - 100, "x.py")
        (repo / "x.py").write_text("edited", encoding="utf-8")
        first = self.repo_entry(self.fold(repo), repo)
        self.assertEqual(([c["subject"] for c in first["commits"]], first["dirty_total"]), (["during session"], 1))
        self.assertIsNotNone(first["branch"])
        self.write(self.dir_event(repo, self.now - 50, repo))
        run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", 3))
        with mock.patch.object(facts, "subprocess", mock.Mock(run=run, TimeoutExpired=subprocess.TimeoutExpired)):
            again = self.repo_entry(self.fold(repo), repo)
        self.assertTrue(run.called)
        self.assertEqual({k: again[k] for k in ("branch", "dirty", "dirty_total", "commits")},
                         {k: first[k] for k in ("branch", "dirty", "dirty_total", "commits")})
        self.assertEqual(again["error"], "GIT_TIMEOUT")
        self.assertEqual(again["last_touched_at"], facts._iso(self.now - 50))
        fresh = self.repo_entry(self.fold(repo), repo)  # git answers again: current facts, error cleared
        self.assertIsNone(fresh["error"])
        self.assertEqual(fresh["commits"], first["commits"])

    def test_git_failure_without_previous_entry_leaves_empty_git_facts(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", 3))
        with mock.patch.object(facts, "subprocess", mock.Mock(run=run, TimeoutExpired=subprocess.TimeoutExpired)):
            entry = self.repo_entry(self.fold(repo), repo)
        self.assertEqual({k: entry[k] for k in ("branch", "dirty", "dirty_total", "commits", "error")},
                         {"branch": None, "dirty": [], "dirty_total": 0, "commits": [], "error": "GIT_TIMEOUT"})

    def test_spent_git_budget_spawns_nothing(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        run = mock.Mock()
        with mock.patch.object(facts, "GIT_BUDGET", 0), \
                mock.patch.object(facts, "subprocess", mock.Mock(run=run, TimeoutExpired=subprocess.TimeoutExpired)):
            data = self.fold(repo)
        run.assert_not_called()
        self.assertEqual(self.repo_entry(data, repo)["error"], "GIT_TIMEOUT")

    def test_invalid_spool_lines_are_counted_and_skipped(self):
        repo = self.repo("a")
        good = self.edit(repo / "x.py", self.now - 9, repo)
        self.write("{not json", json.dumps(dict(good, v=2)), json.dumps(dict(good, k="bogus")),
                   json.dumps({k: v for k, v in good.items() if k != "ts"}), json.dumps(dict(good, v=True)), good)
        data = self.fold(repo)
        self.assertEqual(data["spool_errors"], 5)
        self.assertEqual(len(data["files"]), 1)
        self.write("[]")
        self.assertEqual(self.fold(repo)["spool_errors"], 6)

    def test_lone_surrogate_line_is_counted_and_the_spool_released(self):
        repo = self.repo("a")
        bad = json.dumps(self.agent("implementer", self.now - 9, repo))[:-1] + ',"desc":"x\\ud83d"}'
        self.assertRaises(UnicodeEncodeError, lambda: json.loads(bad)["desc"].encode("utf-8"))
        self.write(bad, self.edit(repo / "x.py", self.now - 8, repo))
        data = self.fold(repo)
        self.assertEqual((data["spool_errors"], data["events_captured"], data["agents"]), (1, 1, []))
        self.assertEqual(self.stored(repo)["spool_errors"], 1)
        self.assertFalse(self.spool().exists())
        self.assertEqual(list(self.spool().parent.glob("*.folding-*")), [])

    def test_events_captured_accumulates_across_folds(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo), "{bad")
        self.assertEqual((self.fold(repo)["events_captured"], self.stored(repo)["spool_errors"]), (1, 1))
        self.write(self.dir_event(repo, self.now - 5, repo), self.agent("implementer", self.now - 4, repo))
        self.assertEqual(self.fold(repo)["events_captured"], 3)
        self.assertEqual(self.fold(repo)["events_captured"], 3)

    def test_worktrees_keep_the_last_twenty(self):
        repo = self.repo("a")
        self.write(*[{"v": 1, "ts": self.now - 100 + i, "k": "worktree", "cwd": str(repo), "agent": "",
                      "tool": "EnterWorktree", "p": "wt-%02d" % i} for i in range(25)])
        data = self.fold(repo)
        self.assertEqual([w["path"] for w in data["worktrees"]], ["wt-%02d" % i for i in range(5, 25)])
        self.write({"v": 1, "ts": self.now, "k": "worktree", "cwd": str(repo), "agent": "", "tool": "ExitWorktree",
                    "p": "last"})
        data = self.fold(repo)
        self.assertEqual(len(data["worktrees"]), 20)
        self.assertEqual((data["worktrees"][0]["path"], data["worktrees"][-1]["path"]), ("wt-06", "last"))

    def test_split_multibyte_characters_are_replaced_not_rejected(self):
        repo = self.repo("a")
        line = json.dumps(dict(self.agent("implementer", self.now - 9, repo), desc="fix XY"), ensure_ascii=False)
        self.spool().parent.mkdir(parents=True)
        self.spool().write_bytes(line.encode("utf-8").replace(b"XY", "測".encode("utf-8")[:2]) + b"\n")
        data = self.fold(repo)
        self.assertEqual(data["spool_errors"], 0)
        self.assertEqual(data["agents"][0]["desc"], "fix �")

    def test_spool_truncated_but_lines_appended_mid_fold_survive(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        late_live = json.dumps(self.edit(repo / "late.py", self.now - 2, repo))
        late_claimed = json.dumps(self.edit(repo / "held.py", self.now - 1, repo))
        real = facts.atomic_write

        def append_then_write(*args):
            self.write(late_live)  # an append after the claim
            claimed, = self.spool().parent.glob(SESSION + ".jsonl.folding-*")
            with claimed.open("ab") as stream:  # an appender that opened the file before the claim
                stream.write((late_claimed + "\n").encode("utf-8"))
            return real(*args)

        with mock.patch.object(facts, "atomic_write", side_effect=append_then_write):
            first = self.fold(repo)
        self.assertEqual([Path(f["path"]).name for f in first["files"]], ["x.py"])
        self.assertEqual(sorted(self.spool().read_text(encoding="utf-8").splitlines()), sorted([late_live, late_claimed]))
        self.assertEqual(list(self.spool().parent.glob("*.folding-*")), [])
        second = self.fold(repo)
        self.assertEqual(sorted((Path(f["path"]).name, f["count"]) for f in second["files"]),
                         [("held.py", 1), ("late.py", 1), ("x.py", 1)])

    def test_interrupted_release_resumes_without_double_counting(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        with mock.patch.object(facts._Spool, "release"):
            self.fold(repo)
        claimed, = self.spool().parent.glob(SESSION + ".jsonl.folding-*")
        with claimed.open("ab") as stream:
            stream.write((json.dumps(self.edit(repo / "x.py", self.now - 1, repo)) + "\n").encode("utf-8"))
        data = self.fold(repo)
        self.assertEqual([(Path(f["path"]).name, f["count"]) for f in data["files"]], [("x.py", 2)])
        self.assertEqual(list(self.spool().parent.glob("*.folding-*")), [])

    def test_fold_is_idempotent_on_an_empty_spool(self):
        repo = self.repo("a")
        self.write(self.edit(repo / "x.py", self.now - 9, repo), self.agent("implementer", self.now - 8, repo))
        strip = lambda d: {k: v for k, v in d.items() if k not in ("updated_at", "checksum", "spool_consumed")}
        first = strip(self.fold(repo))
        self.assertFalse(self.spool().exists())
        self.assertEqual(strip(self.fold(repo)), first)
        self.assertEqual(strip(self.fold(repo)), first)

    def test_size_cap_drops_old_files_then_old_agents(self):
        repo = self.repo("a")
        self.write(*[self.edit(repo / ("f%04d-" % i + "n" * 60), self.now - 5000 + i, repo) for i in range(100)],
                   *[dict(self.agent("implementer", self.now - 1000 + i, repo), desc="d" * 120) for i in range(500)])
        data = self.fold(repo)
        self.assertLessEqual(len((session_dir(repo, SESSION) / "facts.json").read_bytes()), facts.MAX_BYTES)
        self.assertEqual((data["files"], data["files_omitted"]), ([], 100))
        self.assertGreater(data["agents_omitted"], 0)
        self.assertEqual(len(data["agents"]) + data["agents_omitted"], 500)
        self.assertEqual(data["agents"][-1]["ts"], self.now - 1000 + 499)

    def test_size_cap_keeps_newest_files(self):
        repo = self.repo("a")
        self.write(*[self.edit(repo / ("f%04d-" % i + "n" * 150), self.now - 5000 + i, repo) for i in range(400)])
        data = self.fold(repo)
        self.assertLessEqual(len((session_dir(repo, SESSION) / "facts.json").read_bytes()), facts.MAX_BYTES)
        self.assertGreater(data["files_omitted"], 0)
        self.assertEqual(len(data["files"]) + data["files_omitted"], 400)
        self.assertTrue(data["files"][-1]["path"].endswith("f0399-" + "n" * 150))
        self.assertEqual(data["agents_omitted"], 0)

    def test_size_cap_is_exact_when_the_omitted_counter_grows(self):
        # Overshoot by exactly 100 entries: dropping them turns the counter 0 into 100, two
        # bytes the per-entry estimate does not see, so it alone ends over the cap.
        from provost_runtime.store import _seal, encoded
        for key, counter in (("files", "files_omitted"), ("agents", "agents_omitted")):
            with self.subTest(key=key):
                entry = lambda i: {"path": "p%03d" % i, "ts": 1000 + i}
                data = {"files": [], "files_omitted": 0, "agents": [], "agents_omitted": 0,
                        "repos": [{"dirty": [], "dirty_total": 0}], "pad": ""}
                data[key] = [entry(i) for i in range(200)]
                drift = 100 * (len(encoded(entry(0))) + 1)
                size = lambda: len(encoded(_seal(data)))
                data["pad"] = "x" * (facts.MAX_BYTES - size() + drift)
                self.assertEqual(size() - facts.MAX_BYTES, drift)
                capped = facts._cap(data)
                self.assertLessEqual(len(encoded(_seal(capped))), facts.MAX_BYTES)
                self.assertEqual(capped[counter], 101)
                self.assertEqual(capped[key], [entry(i) for i in range(101, 200)])


class PreCompactTests(FactsCase):
    def event(self):
        return {"session_id": SESSION, "cwd": str(self.root), "hook_event_name": "PreCompact"}

    def setUp(self):
        super().setUp()
        self.enable_facts(SESSION)

    def precompact(self):
        from provost_runtime.hooks import handle_event
        return json.dumps(handle_event(self.event(), self.config_path))

    def snapshots(self):
        from provost_runtime.store import latest
        return latest(self.root, SESSION)

    def test_partial_missing_saves_facts_without_a_snapshot(self):
        self.write(self.agent("implementer", self.now - 9, self.root))
        output = self.precompact()
        self.assertIn("facts saved, decision record missing.", output)
        self.assertNotIn("NO_CHECKPOINT", output)
        self.assertEqual([a["role"] for a in self.stored(self.root)["agents"]], ["implementer"])
        self.assertFalse(self.spool().exists())
        self.assertIsNone(self.snapshots())
        self.assertFalse((session_dir(self.root, SESSION) / "snapshots").exists())

    def test_partial_repeats_then_complete_save_recovers(self):
        from provost_runtime.store import record
        first = self.precompact()
        self.assertEqual(first, self.precompact())
        record(self.root, SESSION, self.payload(), None, "r1")
        self.assertIn("handoff saving recovered", self.precompact())
        self.assertEqual(self.precompact(), "{}")

    def test_complete_when_facts_have_no_change_time(self):
        from provost_runtime.store import record
        record(self.root, SESSION, self.payload(), None, "r1")
        self.write(self.agent("implementer", self.now - 9, self.root))
        self.assertEqual(self.precompact(), "{}")
        self.assertIsNone(self.stored(self.root)["last_change_at"])
        self.assertIsNotNone(self.snapshots())

    def test_complete_when_record_is_newer_than_last_change(self):
        from provost_runtime.store import record
        repo = self.repo("app")
        self.write(self.edit(repo / "a.txt", self.now - 100, repo))
        self.fold(self.root)
        record(self.root, SESSION, self.payload(), None, "r1")
        self.assertEqual(self.precompact(), "{}")
        self.assertIsNotNone(self.snapshots())

    def test_partial_stale_when_change_is_newer_than_record(self):
        from provost_runtime.store import record
        record(self.root, SESSION, self.payload(), None, "r1")
        repo = self.repo("app")
        self.write(self.edit(repo / "a.txt", self.now + 120, repo))
        output = self.precompact()
        self.assertIn("facts saved, decision record is older than the latest change.", output)
        self.assertIsNotNone(self.snapshots())

    def test_fold_failure_without_record_is_a_failed_save(self):
        with mock.patch.object(facts, "fold", side_effect=RuntimeError("boom")):
            output = self.precompact()
        self.assertIn("handoff not saved, compaction continues. FACTS_FAILED", output)
        self.assertIsNone(self.snapshots())

    def test_precompact_snapshots_even_when_fold_raises(self):
        from provost_runtime.store import record
        record(self.root, SESSION, self.payload(), None, "r1")
        with mock.patch.object(facts, "fold", side_effect=RuntimeError("boom")) as fold:
            output = self.precompact()
        fold.assert_called_once()
        self.assertIn("decision record saved, facts not saved. FACTS_FAILED", output)
        self.assertIsNotNone(self.snapshots())


class LastChangeTests(FactsCase):
    """last_change_at: newest of hook edits, session commits and dirty files' mtimes, never the fold time."""

    def level(self, repo, now):
        from provost_runtime.hooks import save_level
        from provost_runtime.store import current
        return save_level(current(self.root, SESSION), facts.fold(self.config_path, repo, SESSION, now=now))

    def dirty_repo(self, mtime):
        repo = self.repo("app")
        self.commit(repo, "add x", self.now - 3000, "x.py")
        (repo / "x.py").write_text("edited", encoding="utf-8")
        os.utime(repo / "x.py", (mtime, mtime))
        self.write(self.dir_event(repo, self.now - 500, repo))
        return repo

    def test_old_dirty_file_does_not_make_a_fresh_record_stale(self):
        from provost_runtime.store import record
        repo = self.dirty_repo(self.now - 500)
        (repo / "gone.py").write_text("x", encoding="utf-8")
        self.git("add", "gone.py", cwd=repo)
        (repo / "gone.py").unlink()  # a dirty entry without a file is skipped
        record(self.root, SESSION, self.payload(), None, "r1")
        self.assertIsNone(self.level(repo, self.now + 60))
        self.assertEqual(self.stored(repo)["last_change_at"], facts._iso(self.now - 500))

    def test_dirty_file_modified_after_the_record_is_stale(self):
        from provost_runtime.store import record
        repo = self.dirty_repo(self.now - 500)
        record(self.root, SESSION, self.payload(), None, "r1")
        os.utime(repo / "x.py", (self.now + 120, self.now + 120))  # e.g. a script edit the hook never saw
        self.assertEqual(self.level(repo, self.now + 300), "partial-stale")

    def test_untracked_file_created_after_the_record_is_not_a_change(self):
        from provost_runtime.store import record
        repo = self.dirty_repo(self.now - 500)
        record(self.root, SESSION, self.payload(), None, "r1")
        (repo / "scratch.log").write_text("x", encoding="utf-8")
        os.utime(repo / "scratch.log", (self.now + 120, self.now + 120))
        self.assertIsNone(self.level(repo, self.now + 60))
        entry = self.repo_entry(self.stored(repo), repo)
        self.assertIn(("??", "scratch.log"), [(d["status"], d["path"]) for d in entry["dirty"]])
        self.assertEqual(self.stored(repo)["last_change_at"], facts._iso(self.now - 500))

    def test_future_mtime_is_clamped_to_now(self):
        repo = self.dirty_repo(self.now + 86400)
        data = facts.fold(self.config_path, repo, SESSION, now=self.now)
        self.assertEqual(data["last_change_at"], facts._iso(self.now))

    def test_commit_after_the_record_is_stale(self):
        from provost_runtime.store import record
        repo = self.repo("app")
        self.write(self.dir_event(repo, self.now - 500, repo))
        record(self.root, SESSION, self.payload(), None, "r1")
        self.commit(repo, "later", self.now + 120)
        self.assertEqual(self.level(repo, self.now), "partial-stale")


class ChangesTests(FactsCase):
    def test_changes_merge_stored_live_and_leftover_lines_without_writing(self):
        repo = self.repo("app")
        self.write(self.edit(repo / "x.py", self.now - 90, repo))
        with mock.patch.object(facts._Spool, "release"):  # an interrupted fold leaves its claimed file
            facts.fold(self.config_path, self.root, SESSION)
        claimed, = self.spool().parent.glob(SESSION + ".jsonl.folding-*")
        with claimed.open("ab") as stream:
            stream.write((json.dumps(self.edit(repo / "z.py", self.now - 60, repo)) + "\n").encode("utf-8"))
        self.write(self.edit(repo / "y.py", self.now - 50, repo), '{"partial')
        self.spool().write_bytes(self.spool().read_bytes()[:-1])  # a line still being appended
        self.commit(repo, "during session", self.now - 10)
        (repo / "dirty.txt").write_text("d", encoding="utf-8")
        target = session_dir(self.root, SESSION) / "facts.json"
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (target, claimed, self.spool())}
        listing = sorted(self.spool().parent.iterdir())
        data = facts.changes(self.config_path, self.root, SESSION)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        self.assertEqual(sorted(self.spool().parent.iterdir()), listing)
        self.assertEqual((data["pending_events"], data["pending_invalid"]), (2, 0))
        self.assertEqual(data["folded_at"], self.stored(self.root)["updated_at"])
        self.assertEqual(data["facts_path"], str(target))
        body = data["facts"]
        self.assertEqual(sorted((Path(f["path"]).name, f["count"]) for f in body["files"]),
                         [("x.py", 1), ("y.py", 1), ("z.py", 1)])
        entry = self.repo_entry(body, repo)
        self.assertEqual([c["subject"] for c in entry["commits"]], ["during session"])
        self.assertEqual([d["path"] for d in entry["dirty"]], ["dirty.txt"])
        self.assertNotIn("checksum", body)
        self.assertNotIn("spool_consumed", body)

    def test_changes_without_facts_or_spool_creates_nothing(self):
        data = facts.changes(self.config_path, self.root, SESSION)
        self.assertEqual((data["pending_events"], data["folded_at"], data["facts"]["files"]), (0, None, []))
        self.assertFalse((self.root / "cache").exists())
        self.assertFalse(session_dir(self.root, SESSION).exists())

    def test_cli_changes_reads_through_the_runtime(self):
        import contextlib
        import io
        from provost_runtime import cli
        from provost_runtime.hooks import bind
        repo = self.repo("app")
        bind(self.root, SESSION, {})
        self.write(self.edit(repo / "x.py", self.now - 9, repo))
        out = io.StringIO()
        with mock.patch.dict(os.environ, PROVOST_CONFIG=str(self.config_path)), contextlib.redirect_stdout(out):
            code = cli.main(["changes", "--workspace", str(self.root), "--session", SESSION])
        self.assertEqual(code, 0, out.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual([Path(f["path"]).name for f in data["facts"]["files"]], ["x.py"])
        self.assertTrue(self.spool().exists())


class RecoveryRefoldTests(FactsCase):
    """SessionStart(compact) folds the spool again, so activity after PreCompact reaches the recovery text."""

    def setUp(self):
        super().setUp()
        self.enable_facts(SESSION)

    def handle(self, name, source=None):
        from provost_runtime.hooks import handle_event
        event = {"session_id": SESSION, "cwd": str(self.root), "hook_event_name": name}
        if source:
            event["source"] = source
        return handle_event(event, self.config_path)

    def compact(self):
        output = self.handle("SessionStart", "compact")
        return output, output["hookSpecificOutput"]["additionalContext"]

    def test_dispatch_and_commit_after_precompact_appear_in_recovery(self):
        from provost_runtime.store import record
        repo = self.repo("app")
        record(self.root, SESSION, self.payload(), None, "r1")
        self.write(self.agent("implementer", self.now + 30, repo))
        self.handle("PreCompact")
        self.write(self.agent("code-reviewer", self.now + 90, repo))  # between PreCompact and the boundary
        self.commit(repo, "late fix", self.now + 120)
        output, text = self.compact()
        self.assertIn("2 dispatches / 1 commit after it.", text)
        self.assertIn("code-reviewer: do code-reviewer", text)
        self.assertIn("late fix", text)
        self.assertEqual([a["role"] for a in self.stored(self.root)["agents"]], ["implementer", "code-reviewer"])
        self.assertEqual([c["ts"] for c in self.repo_entry(self.stored(self.root), repo)["commits"]], [self.now + 120])
        self.assertFalse(self.spool().exists())
        self.assertNotIn("systemMessage", output)

    def test_failed_refold_falls_back_to_the_precompact_facts_silently(self):
        from provost_runtime.store import StoreError, record
        repo = self.repo("app")
        record(self.root, SESSION, self.payload(), None, "r1")
        self.write(self.agent("implementer", self.now + 30, repo))
        self.handle("PreCompact")
        self.write(self.agent("code-reviewer", self.now + 90, repo))
        for error in (StoreError("STORE_BUSY"), RuntimeError("boom")):
            with self.subTest(error=error), mock.patch.object(facts, "fold", side_effect=error) as fold:
                output, text = self.compact()
                fold.assert_called_once()
                self.assertIn("1 dispatch / 0 commits after it.", text)
                self.assertIn("implementer: do implementer", text)
                self.assertNotIn("code-reviewer", text)
                self.assertNotIn("systemMessage", output)
                self.assertNotIn("STORE_BUSY", text)
                self.assertNotIn("FACTS_FAILED", text)
                self.assertNotIn("Checkpoint unavailable", text)

    def test_refold_blocked_by_the_held_session_lock_keeps_the_claimed_spool(self):
        from provost_runtime.store import _lock, as_workspace, record, session_dir
        repo = self.repo("app")
        record(self.root, SESSION, self.payload(), None, "r1")
        self.write(self.agent("implementer", self.now + 30, repo))
        self.handle("PreCompact")
        self.write(self.agent("code-reviewer", self.now + 90, repo))
        ws = as_workspace(self.root)
        with _lock(ws, session_dir(ws, SESSION)):  # a real fold, not a patched one, meets STORE_BUSY
            output, text = self.compact()
        self.assertIn("1 dispatch / 0 commits after it.", text)
        self.assertNotIn("code-reviewer", text)
        self.assertNotIn("systemMessage", output)
        self.assertNotIn("STORE_BUSY", text)
        claimed = list(self.spool().parent.glob(self.spool().name + ".folding-*"))
        self.assertEqual(len(claimed), 1)
        self.assertIn(b"code-reviewer", claimed[0].read_bytes())
        self.assertEqual([a["role"] for a in self.stored(self.root)["agents"]], ["implementer"])
        output, text = self.compact()  # lock released: the claimed spool is resumed exactly once
        self.assertIn("2 dispatches / 0 commits after it.", text)
        self.assertEqual([a["role"] for a in self.stored(self.root)["agents"]], ["implementer", "code-reviewer"])
        self.assertEqual(list(self.spool().parent.glob(self.spool().name + ".folding-*")), [])

    def test_resume_and_unmarked_sessions_do_not_refold(self):
        self.write(self.agent("implementer", self.now + 30, self.root))
        with mock.patch.object(facts, "fold") as fold:
            self.handle("SessionStart", "resume")
            self.enable_facts(SESSION).unlink()
            self.compact()  # the marker this SessionStart creates does not count
        fold.assert_not_called()
