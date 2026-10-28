import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "reviewer-eval.js"
HAVE_TOOLS = bool(shutil.which("node") and shutil.which("git"))
# Inherited from a git hook or `git rebase -x`, these would point fixture git calls at another repo.
GIT_REPO_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
                "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
                "GIT_SHALLOW_FILE", "GIT_COMMON_DIR")

# Stands in for the Claude Code CLI: logs how it was called and answers with reply.json from its cwd,
# which is the temporary checkout of the case's head commit.
FAKE_CLI = r"""
const fs = require("fs"), path = require("path");
const stdin = fs.readFileSync(0, "utf8");
const cwd = process.cwd();
const diff = path.join(cwd, ".review", "diff.patch");
fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({
  cwd, args: process.argv.slice(2), stdin,
  diff: fs.existsSync(diff) ? fs.readFileSync(diff, "utf8") : null,
  app: fs.existsSync(path.join(cwd, "src", "app.js")) ? fs.readFileSync(path.join(cwd, "src", "app.js"), "utf8") : null,
}) + "\n");
const reply = JSON.parse(fs.readFileSync(path.join(cwd, "reply.json"), "utf8"));
if (reply.exit) { process.stderr.write("boom"); process.exit(reply.exit); }
process.stdout.write(JSON.stringify({ type: "result", is_error: false, total_cost_usd: reply.cost, result: reply.text }));
"""

REPLIES = {
    # Expected src/app.js lines "2,9-10": line 20 is exactly 10 past line 10.
    "a-hit": "Important 2 · Nit 0 · Pre-existing 0\n\n**I1** (Important) `.\\src\\app.js:20` drops the guard.\n"
             "I2. Important: `lib/other.js:3-4` leaks a handle.\n",
    # 21-25 is 11 past line 10: right file, lines off. The Nit cites the exact line and must not count.
    "b-file": "Important 1 · Nit 1\n\n- I1 (Important): ./src/app.js:21-25 wrong bound.\n\n## Nits\n"
              "- Nit: src/app.js:9 naming.\n",
    "c-miss": "Important 1\n\nI1 — lib/other.js:9 races on close.\n",
    "d-clean-alarm": "Important 1\n\n### I1 src/app.js:4 unchecked input\n",
    "e-clean-ok": "Important 0 · Nit 1\n\nNo material finding.\n\n- Nit: src/app.js:1 typo.\n",
}


@unittest.skipUnless(HAVE_TOOLS, "node and git are required")
class ReviewerEvalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-re-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.scratch = self.tmp / "scratch"  # TMP for the tool: checkouts must be gone afterwards
        self.scratch.mkdir()
        self.repo = self.tmp / "src repo"  # a space on purpose
        self.repo.mkdir()
        self.log = self.tmp / "fake.log"
        self.fake = self.tmp / "fake" / "claude.js"
        self.fake.parent.mkdir()
        self.fake.write_text(FAKE_CLI, encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_ENV}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), TMP=str(self.scratch),
                        TEMP=str(self.scratch), TMPDIR=str(self.scratch), FAKE_LOG=str(self.log))
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "code-reviewer.md").write_text(
            "---\nname: code-reviewer\nmodel: fable\neffort: high\ntools: Read, Glob, Grep\n---\n\nReview.\n",
            encoding="utf-8")
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.write({"src/app.js": "".join(f"line {i}\n" for i in range(1, 31)), "reply.json": "{}"})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.cases = {}

    def git(self, *args):
        out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.repo,
                             env=self.env, check=True, capture_output=True, text=True, encoding="utf-8")
        return out.stdout.strip()

    def write(self, files):
        for name, text in files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", newline="\n")

    def add_case(self, cid, reply, expected, origin="seeded", case_json=None):
        """One commit per case on main; the case diff is that commit against its parent."""
        base = self.git("rev-parse", "HEAD")
        app = (self.repo / "src" / "app.js").read_text(encoding="utf-8").replace("line 10\n", f"line 10 {cid}\n")
        self.write({"src/app.js": app, "reply.json": json.dumps(reply)})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", cid)
        head = self.git("rev-parse", "HEAD")
        d = self.home / ".claude" / "provost" / "evals" / "reviewer" / "cases" / cid
        d.mkdir(parents=True)
        body = case_json if case_json is not None else json.dumps({
            "repo": str(self.repo), "base": base, "head": head, "claim": f"claim for {cid}",
            "expected": expected, "origin": origin, "confirmedBy": "t", "savedAt": "2026-10-29T00:00:00Z"})
        (d / "case.json").write_text(body, encoding="utf-8")
        self.cases[cid] = (base, head)

    def build_cases(self):
        exp = [{"file": "src/app.js", "lines": "2,9-10", "summary": "guard"}]
        self.add_case("a-hit", {"cost": 0.5, "text": REPLIES["a-hit"]}, exp)
        self.add_case("b-file", {"cost": 0.5, "text": REPLIES["b-file"]}, exp)
        self.add_case("c-miss", {"cost": 0.5, "text": REPLIES["c-miss"]}, exp, origin="escape")
        self.add_case("d-clean-alarm", {"cost": 0.25, "text": REPLIES["d-clean-alarm"]}, [], origin="clean")
        self.add_case("e-clean-ok", {"cost": 0.25, "text": REPLIES["e-clean-ok"]}, [], origin="clean")
        self.add_case("f-cli-error", {"exit": 3}, exp)
        self.add_case("g-bad-json", {"cost": 9}, exp, case_json="{not json")

    def repo_state(self):
        return [self.git("worktree", "list", "--porcelain"), self.git("status", "--porcelain"),
                self.git("branch", "--list"), self.git("rev-parse", "HEAD"), self.git("stash", "list")]

    def run_tool(self, *args):
        out = subprocess.run(["node", str(TOOL), "run", "--claude", str(self.fake), "--tsv", *args], env=self.env,
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        cols = lines[0].split("\t")
        return [dict(zip(cols, ln.split("\t"))) for ln in lines[1:]], out.stderr

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]

    def test_scores_defect_and_clean_cases(self):
        self.build_cases()
        before = self.repo_state()
        rows, err = self.run_tool("--label", "base")
        by = {r["id"]: r for r in rows}
        self.assertEqual([r["id"] for r in rows],
                         ["a-hit", "b-file", "c-miss", "d-clean-alarm", "e-clean-ok", "f-cli-error", "g-bad-json"])
        pick = lambda r: (r["kind"], r["important"], r["hit"], r["fileHit"], r["falseAlarm"], r["status"])
        self.assertEqual(pick(by["a-hit"]), ("defect", "2", "yes", "yes", "", "ok"))
        self.assertEqual(pick(by["b-file"]), ("defect", "1", "no", "yes", "", "ok"))
        self.assertEqual(pick(by["c-miss"]), ("defect", "1", "no", "no", "", "ok"))
        self.assertEqual(pick(by["d-clean-alarm"]), ("clean", "1", "", "", "yes", "ok"))
        self.assertEqual(pick(by["e-clean-ok"]), ("clean", "0", "", "", "no", "ok"))
        self.assertTrue(by["f-cli-error"]["status"].startswith("error:"), by["f-cli-error"])
        self.assertTrue(by["g-bad-json"]["status"].startswith("error:"), by["g-bad-json"])
        self.assertEqual(by["c-miss"]["origin"], "escape")
        self.assertEqual(by["a-hit"]["costUsd"], "0.5000")
        # Errors are excluded from every rate.
        self.assertIn("cases: 7 (ok 5, error 2)", err)
        self.assertIn("recall: 1/3", err)
        self.assertIn("file-only recall: 2/3", err)
        self.assertIn("false-alarm rate: 1/2", err)
        self.assertIn("mean Important: 1.00", err)
        self.assertIn("total cost: $2.0000", err)
        self.assertIn("stopped by ceiling: no", err)

        # The CLI ran once per runnable case, read-only, as the installed role, inside a checkout of head.
        calls = self.calls()
        self.assertEqual(len(calls), 6)
        for call in calls:
            a = call["args"]
            self.assertIn("-p", a)
            self.assertEqual(a[a.index("--agent") + 1], "code-reviewer")
            self.assertEqual(a[a.index("--tools") + 1], "Read,Glob,Grep")
            self.assertEqual(a[a.index("--allowedTools") + 1], "Read,Glob,Grep")
            self.assertEqual(a[a.index("--output-format") + 1], "json")
            self.assertEqual(a[a.index("--model") + 1], "fable")
            self.assertEqual(a[a.index("--effort") + 1], "high")
            self.assertTrue(Path(call["cwd"]).resolve().is_relative_to(self.scratch.resolve()), call["cwd"])
            self.assertFalse(Path(call["cwd"]).exists(), "temporary checkout left behind")
            self.assertIn(".review/diff.patch", call["stdin"])
            self.assertIn("I1", call["stdin"])
        first = calls[0]
        self.assertIn("Claim to review: claim for a-hit", first["stdin"])
        self.assertIn("line 10 a-hit", first["app"])
        self.assertIn("diff --git a/src/app.js b/src/app.js", first["diff"])
        self.assertIn("+line 10 a-hit", first["diff"])
        # Only the tool's own checkouts: git/Windows may leave unrelated GUID dirs in TMP.
        self.assertEqual(list(self.scratch.glob("provost-reval-*")), [])
        self.assertEqual(self.repo_state(), before)

        # The run TSV lands under the fixture HOME.
        runs = list((self.home / ".claude" / "provost" / "evals" / "reviewer" / "runs").glob("*-base.tsv"))
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs[0].name, r"^\d{8}T\d{6}-base\.tsv$")
        saved = runs[0].read_text(encoding="utf-8").splitlines()
        self.assertTrue(saved[0].startswith("id\torigin\tkind\timportant\thit\tfileHit\tfalseAlarm\tcostUsd\tstatus\treport"))
        self.assertEqual(len([ln for ln in saved[1:] if not ln.startswith("#")]), 7)

        # Each reviewer report is saved verbatim next to the TSV, under the same stamp and label.
        stem = runs[0].stem
        rdir = runs[0].parent / stem
        self.assertTrue(rdir.is_dir(), rdir)
        for cid in ("a-hit", "b-file", "c-miss", "d-clean-alarm", "e-clean-ok"):
            self.assertEqual(by[cid]["report"], f"{stem}/{cid}.md")
            self.assertEqual((runs[0].parent / by[cid]["report"]).read_text(encoding="utf-8"), REPLIES[cid])
        self.assertEqual(by["f-cli-error"]["report"], f"{stem}/f-cli-error.err.txt")
        err_txt = (runs[0].parent / by["f-cli-error"]["report"]).read_text(encoding="utf-8")
        self.assertIn("cli exit 3", err_txt)
        self.assertIn("boom", err_txt)
        self.assertEqual(by["g-bad-json"].get("report", ""), "")  # run_tool strips the trailing empty cell
        self.assertEqual(sorted(p.name for p in rdir.iterdir()),
                         ["a-hit.md", "b-file.md", "c-miss.md", "d-clean-alarm.md", "e-clean-ok.md",
                          "f-cli-error.err.txt"])

    def test_ids_filter(self):
        self.build_cases()
        rows, err = self.run_tool("--ids", "e-clean-ok,a-hit")
        self.assertEqual([r["id"] for r in rows], ["a-hit", "e-clean-ok"])
        self.assertEqual(len(self.calls()), 2)

    def test_max_cost_stops_after_crossing_case(self):
        self.build_cases()
        rows, err = self.run_tool("--ids", "a-hit,b-file,c-miss", "--max-cost", "0.9")
        self.assertEqual([r["id"] for r in rows], ["a-hit", "b-file"])
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("stopped by ceiling: yes", err)
        self.assertIn("total cost: $1.0000", err)

    def test_markdown_default(self):
        self.build_cases()
        out = subprocess.run(["node", str(TOOL), "run", "--claude", str(self.fake), "--ids", "a-hit"], env=self.env,
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith("| id | origin | kind |"))
        self.assertTrue(lines[0].endswith("| status | report |"), lines[0])
        self.assertIn("| a-hit |", lines[2])
        self.assertRegex(lines[2], r"\| \d{8}T\d{6}-run/a-hit\.md \|$")


if __name__ == "__main__":
    unittest.main()
