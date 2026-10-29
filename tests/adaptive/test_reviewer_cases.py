import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "reviewer-cases.js"
HAVE_TOOLS = bool(shutil.which("node") and shutil.which("git"))
# Inherited from a git hook or `git rebase -x`, these would point fixture git calls at another repo.
GIT_REPO_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
                "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
                "GIT_SHALLOW_FILE", "GIT_COMMON_DIR")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_TOOLS, "node and git are required")
class ReviewerCasesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-rc-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "my repo"  # a space on purpose
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_ENV}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home))
        self.git("init", "-q")

    def git(self, *args, when=None):
        env = dict(self.env)
        if when:
            env.update(GIT_AUTHOR_DATE=iso(when), GIT_COMMITTER_DATE=iso(when))
        out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.repo,
                             env=env, check=True, capture_output=True, text=True, encoding="utf-8")
        return out.stdout.strip()

    def commit(self, when, subject, files):
        for name, text in files.items():
            (self.repo / name).write_text(text, encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject, when=when)
        return self.git("rev-parse", "HEAD")

    def review(self, session, when, cwd, prompt="a claim", subagent="code-reviewer"):
        proj = self.home / ".claude" / "projects" / "D--proj"
        proj.mkdir(parents=True, exist_ok=True)
        line = {"type": "assistant", "timestamp": iso(when), "cwd": str(cwd), "message": {"content": [
            {"type": "tool_use", "id": "t-" + session, "name": "Agent",
             "input": {"subagent_type": subagent, "description": "review", "prompt": prompt}}]}}
        with open(proj / (session + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    def run_tool(self, *args, check=True):
        out = subprocess.run(["node", str(TOOL), *args], env=self.env, capture_output=True, text=True,
                             encoding="utf-8")
        if check:
            self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def candidates(self, *extra):
        out = self.run_tool("candidates", "--repo", str(self.repo), "--tsv", *extra)
        lines = out.stdout.strip().splitlines()
        cols = lines[0].split("\t")
        return [dict(zip(cols, ln.split("\t"))) for ln in lines[1:]], out.stderr

    def build_history(self):
        a = self.commit(utc(2026, 1, 1), "init", {"f.txt": "a1\na2\na3\n"})
        b = self.commit(utc(2026, 1, 10, 11), "add b lines", {"f.txt": "a1\na2\na3\nb1\nb2\n"})
        c = self.commit(utc(2026, 1, 12), "fix off-by-one", {"f.txt": "a1\na2\na3\nb1-fixed\nb2\n"})
        d = self.commit(utc(2026, 2, 1, 11), "add g", {"g.txt": "g1\ng2\n"})
        self.commit(utc(2026, 2, 5), "docs tweak", {"f.txt": "a1\na2\na3\nb1-fixed\nb2 docs\n"})
        return a, b, c, d

    def test_defect_clean_and_skip_rows(self):
        a, b, c, d = self.build_history()
        # Review 1 ran from a worktree below the repo; review 2 from the repo itself, spelled differently.
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo / ".claude" / "worktrees" / "x",
                    prompt="claim one sk-abcdefghijklmnopqrstuv")
        self.review("22222222-bbbb", utc(2026, 2, 1, 10), str(self.repo).replace("\\", "/").upper(),
                    prompt="claim two")
        self.review("33333333-cccc", utc(2026, 3, 1, 10), self.repo, prompt="claim three")
        # Not counted: another repo, another role.
        self.review("44444444-dddd", utc(2026, 1, 10, 10), self.tmp / "other")
        self.review("55555555-eeee", utc(2026, 1, 10, 10), self.repo, subagent="implementer")
        rows, err = self.candidates()
        by = {r["session"]: r for r in rows}
        self.assertEqual(sorted(by), ["11111111", "22222222", "33333333"])
        r1 = by["11111111"]
        self.assertEqual((r1["kind"], r1["base"], r1["head"], r1["fix"], r1["file"], r1["lines"]),
                         ("defect", a[:12], b[:12], c[:12], "f.txt", "4"))
        self.assertEqual((r1["headFrom"], by["22222222"]["headFrom"]), ("window", "window"))
        self.assertEqual(r1["ts"], "2026-01-10 10:00")
        self.assertIn("claim one [REDACTED]", r1["claim"])
        r2 = by["22222222"]
        self.assertEqual((r2["kind"], r2["head"], r2["fix"]), ("clean", d[:12], ""))
        r3 = by["33333333"]
        self.assertEqual((r3["kind"], r3["head"], r3["note"]), ("skip", "", "no commit within 3h"))
        self.assertIn("defect=1", err)
        self.assertIn("clean=1", err)
        self.assertIn("skip=1", err)

    def test_head_is_commit_named_in_claim(self):
        a = self.commit(utc(2026, 1, 1), "init", {"f.txt": "a1\na2\n"})
        b = self.commit(utc(2026, 1, 10, 9), "add b", {"f.txt": "a1\na2\nb1\n"})
        self.commit(utc(2026, 1, 10, 11), "add g", {"g.txt": "g1\n"})  # within 3h after the dispatch
        fix = self.commit(utc(2026, 1, 12), "fix b", {"f.txt": "a1\na2\nb1-fixed\n"})
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo, prompt=f"Review commit {b[:7]} for errors")
        rows, _ = self.candidates()
        self.assertEqual([(r["kind"], r["base"], r["head"], r["headFrom"], r["fix"], r["lines"]) for r in rows],
                         [("defect", a[:12], b[:12], "claim", fix[:12], "3")])

    def test_unresolvable_claim_hash_falls_back_to_window(self):
        self.commit(utc(2026, 1, 1), "init", {"f.txt": "a1\n"})
        b = self.commit(utc(2026, 1, 10, 11), "add b", {"f.txt": "a1\nb1\n"})
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo, prompt="Review commit deadbeef1 and cafe")
        rows, _ = self.candidates()
        self.assertEqual([(r["kind"], r["head"], r["headFrom"]) for r in rows], [("clean", b[:12], "window")])

    def test_skip_row_has_empty_head_from(self):
        self.commit(utc(2026, 1, 1), "init", {"f.txt": "a1\n"})
        self.review("11111111-aaaa", utc(2026, 3, 1, 10), self.repo)
        rows, _ = self.candidates()
        self.assertEqual([(r["kind"], r["headFrom"]) for r in rows], [("skip", "")])

    def test_non_fix_commit_makes_no_candidate(self):
        self.build_history()
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo)
        rows, _ = self.candidates()
        # Only "fix off-by-one" counts; "docs tweak" touched b2 too but is not a fix.
        self.assertEqual([(r["kind"], r["lines"]) for r in rows], [("defect", "4")])

    def test_fix_hunks_merge_into_line_ranges_and_spaced_file_names(self):
        text = [f"l{i}" for i in range(1, 13)]
        self.commit(utc(2026, 1, 1), "init", {"x.txt": "x\n"})
        head = self.commit(utc(2026, 1, 10, 11), "add file", {"my file.txt": "\n".join(text) + "\n"})
        text[1], text[8], text[9] = "L2", "L9", "L10"
        fix = self.commit(utc(2026, 1, 12), "Fixed two spots", {"my file.txt": "\n".join(text) + "\n"})
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo)
        rows, _ = self.candidates()
        self.assertEqual([(r["kind"], r["head"], r["fix"], r["file"], r["lines"]) for r in rows],
                         [("defect", head[:12], fix[:12], "my file.txt", "2,9-10")])

    def test_since_filters_reviews(self):
        self.build_history()
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo)
        self.review("22222222-bbbb", utc(2026, 2, 1, 10), self.repo)
        rows, _ = self.candidates("--since", "2026-02-01")
        self.assertEqual([r["session"] for r in rows], ["22222222"])

    def test_young_head_is_pending_age_until_fix_days_lowered(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.commit(now - timedelta(days=2), "init", {"f.txt": "a\n"})
        self.commit(now - timedelta(hours=23), "add b", {"f.txt": "a\nb\n"})
        self.review("11111111-aaaa", now - timedelta(hours=24), self.repo)
        rows, err = self.candidates()
        self.assertEqual([r["kind"] for r in rows], ["pending-age"])
        self.assertIn("pending-age=1", err)
        rows, _ = self.candidates("--fix-days", "0")
        self.assertEqual([r["kind"] for r in rows], ["clean"])

    def test_markdown_default(self):
        self.build_history()
        self.review("11111111-aaaa", utc(2026, 1, 10, 10), self.repo)
        out = self.run_tool("candidates", "--repo", str(self.repo)).stdout.splitlines()
        self.assertTrue(out[0].startswith("| kind | repo | ts | session |"))
        self.assertIn("| head | headFrom | fix |", out[0])
        self.assertTrue(out[1].startswith("|---|"))
        self.assertIn("| defect |", out[2])

    def save_args(self, **over):
        a = {"id": "case-1", "repo": str(self.repo), "base": "b" * 40, "head": "c" * 40, "claim": "the claim",
             "origin": "escape", "confirmed-by": "norton",
             "expected": json.dumps([{"file": "f.txt", "lines": "4", "summary": "off by one"}])}
        a.update(over)
        return [x for k, v in a.items() if v is not None for x in ("--" + k, v)]

    def cases_dir(self):
        return self.home / ".claude" / "provost" / "evals" / "reviewer" / "cases"

    def test_save_writes_case_json(self):
        self.run_tool("save", *self.save_args())
        data = json.loads((self.cases_dir() / "case-1" / "case.json").read_text(encoding="utf-8"))
        saved_at = data.pop("savedAt")
        self.assertRegex(saved_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
        self.assertEqual(data, {"repo": str(self.repo), "base": "b" * 40, "head": "c" * 40, "claim": "the claim",
                                "expected": [{"file": "f.txt", "lines": "4", "summary": "off by one"}],
                                "origin": "escape", "confirmedBy": "norton"})

    def test_save_clean_defaults_expected_to_empty(self):
        self.run_tool("save", *self.save_args(origin="clean", expected=None))
        data = json.loads((self.cases_dir() / "case-1" / "case.json").read_text(encoding="utf-8"))
        self.assertEqual((data["expected"], data["origin"]), ([], "clean"))

    def test_save_refuses_overwrite(self):
        self.run_tool("save", *self.save_args())
        path = self.cases_dir() / "case-1" / "case.json"
        before = path.read_text(encoding="utf-8")
        out = self.run_tool("save", *self.save_args(claim="other"), check=False)
        self.assertEqual(out.returncode, 1)
        self.assertIn("exists", out.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_save_rejects_bad_input(self):
        clean_with_expected = dict(origin="clean", expected='[{"file":"f","lines":"1","summary":"s"}]')
        bad = [dict(id="../evil"), dict(id="a b"), dict(origin="bogus"), dict(expected="not json"),
               dict(expected='{"file": "f"}'), clean_with_expected, {"confirmed-by": None}]
        for over in bad:
            with self.subTest(over=over):
                out = self.run_tool("save", *self.save_args(**over), check=False)
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertTrue(out.stderr.strip())
        self.assertEqual(list(self.cases_dir().glob("*")) if self.cases_dir().exists() else [], [])
        self.assertFalse((self.home / ".claude" / "provost" / "evals" / "evil").exists())


if __name__ == "__main__":
    unittest.main()
