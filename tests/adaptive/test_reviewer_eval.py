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
const diff = path.join(cwd, ".changes", "change.patch");
fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({
  cwd, args: process.argv.slice(2), stdin,
  diff: fs.existsSync(diff) ? fs.readFileSync(diff, "utf8") : null,
  app: fs.existsSync(path.join(cwd, "src", "app.js")) ? fs.readFileSync(path.join(cwd, "src", "app.js"), "utf8") : null,
  review: fs.existsSync(path.join(cwd, "REVIEW.md")) ? fs.readFileSync(path.join(cwd, "REVIEW.md"), "utf8") : null,
  notes: fs.existsSync(path.join(cwd, "ctx", "notes.md")) ? fs.readFileSync(path.join(cwd, "ctx", "notes.md"), "utf8") : null,
  review: fs.existsSync(path.join(cwd, "REVIEW.md")) ? fs.readFileSync(path.join(cwd, "REVIEW.md"), "utf8") : null,
  notes: fs.existsSync(path.join(cwd, "ctx", "notes.md")) ? fs.readFileSync(path.join(cwd, "ctx", "notes.md"), "utf8") : null,
}) + "\n");
const reply = JSON.parse(fs.readFileSync(path.join(cwd, "reply.json"), "utf8"));
if (reply.exit) { process.stderr.write("boom"); process.exit(reply.exit); }
process.stdout.write(JSON.stringify({ type: "result", is_error: false, total_cost_usd: reply.cost, result: reply.text }));
"""

# Stands in for the judge: logs the call (cwd contents included) and answers by the first marker of FAKE_JUDGE_MAP
# (a JSON file {marker: {text, cost}}) found in the prompt.
FAKE_JUDGE = r"""
const fs = require("fs");
const stdin = fs.readFileSync(0, "utf8");
fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({
  cwd: process.cwd(), cwdFiles: fs.readdirSync(process.cwd()), args: process.argv.slice(2), stdin }) + "\n");
const map = JSON.parse(fs.readFileSync(process.env.FAKE_JUDGE_MAP, "utf8"));
const key = Object.keys(map).find(k => stdin.includes(k));
if (!key) { process.stderr.write("no marker"); process.exit(4); }
process.stdout.write(JSON.stringify({ type: "result", is_error: false, total_cost_usd: map[key].cost, result: map[key].text }));
"""

# Stands in for the judge: logs the call (cwd contents included) and answers by the first marker of FAKE_JUDGE_MAP
# (a JSON file {marker: {text, cost}}) found in the prompt.
FAKE_JUDGE = r"""
const fs = require("fs");
const stdin = fs.readFileSync(0, "utf8");
fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify({
  cwd: process.cwd(), cwdFiles: fs.readdirSync(process.cwd()), args: process.argv.slice(2), stdin }) + "\n");
const map = JSON.parse(fs.readFileSync(process.env.FAKE_JUDGE_MAP, "utf8"));
const key = Object.keys(map).find(k => stdin.includes(k));
if (!key) { process.stderr.write("no marker"); process.exit(4); }
process.stdout.write(JSON.stringify({ type: "result", is_error: false, total_cost_usd: map[key].cost, result: map[key].text }));
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
        self.judge = self.tmp / "fake" / "judge.js"
        self.judge.write_text(FAKE_JUDGE, encoding="utf-8")
        self.judge_map = self.tmp / "judge_map.json"
        self.env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_ENV}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), TMP=str(self.scratch),
                        TEMP=str(self.scratch), TMPDIR=str(self.scratch), FAKE_LOG=str(self.log),
                        FAKE_JUDGE_MAP=str(self.judge_map))
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "code-reviewer.md").write_text(
            "---\nname: code-reviewer\nmodel: fable\neffort: high\ntools: Read, Glob, Grep\n---\n\nReview.\n",
            encoding="utf-8")
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.write({"src/app.js": "".join(f"line {i}\n" for i in range(1, 31)), "reply.json": "{}",
                    "REVIEW.md": "archived review\n"})
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
            self.assertIn(".changes/change.patch", call["stdin"])
            self.assertNotIn(".review", call["stdin"])
            self.assertNotIn("eval", call["stdin"].lower())
            self.assertTrue(Path(call["cwd"]).name.startswith("wt-"), call["cwd"])
            self.assertIn("I1", call["stdin"])
        first = calls[0]
        self.assertIn("Claim to review: claim for a-hit", first["stdin"])
        self.assertIn("line 10 a-hit", first["app"])
        self.assertIn("diff --git a/src/app.js b/src/app.js", first["diff"])
        self.assertIn("+line 10 a-hit", first["diff"])
        # Only the tool's own checkouts: git/Windows may leave unrelated GUID dirs in TMP.
        self.assertEqual(list(self.scratch.glob("wt-*")), [])
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

    def run_raw(self, *args):
        return subprocess.run(["node", str(TOOL), *args], env=self.env, capture_output=True, text=True,
                              encoding="utf-8")

    def test_inject_copies_and_overrides(self):
        self.build_cases()
        local = self.tmp / "local"
        local.mkdir()
        (local / "REVIEW.md").write_text("local rules\n", encoding="utf-8")
        (local / "n.md").write_text("notes\n", encoding="utf-8")
        rows, err = self.run_tool("--ids", "a-hit", "--inject", f"REVIEW.md={local / 'REVIEW.md'}",
                                  "--inject", f"ctx/notes.md={local / 'n.md'}")
        call = self.calls()[0]
        self.assertEqual(call["review"].strip(), "local rules")  # overrides the archived file
        self.assertEqual(call["notes"].strip(), "notes")  # parent dir created
        self.assertIn("injected: REVIEW.md, ctx/notes.md", err)
        self.assertEqual(list(self.scratch.glob("wt-*")), [])

    def test_no_inject_keeps_archived_file(self):
        self.build_cases()
        rows, err = self.run_tool("--ids", "a-hit")
        call = self.calls()[0]
        self.assertEqual(call["review"].strip(), "archived review")
        self.assertIsNone(call["notes"])
        self.assertIn("injected: none", err)

    def test_inject_rejects_bad_dest_and_missing_src(self):
        self.build_cases()
        src = self.tmp / "src.md"
        src.write_text("x\n", encoding="utf-8")
        for dest in ("../x", "a/../../x", "a\\..\\b", "/abs/x", "C:\\x"):
            out = self.run_raw("run", "--claude", str(self.fake), "--ids", "a-hit", "--inject", f"{dest}={src}")
            self.assertNotEqual(out.returncode, 0, dest)
            self.assertIn("--inject DEST must", out.stderr, dest)
        out = self.run_raw("run", "--claude", str(self.fake), "--ids", "a-hit", "--inject",
                           f"REVIEW.md={self.tmp / 'missing.md'}")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("SRC does not exist", out.stderr)
        self.assertEqual(self.calls(), [])  # nothing ran

    def decisions(self):
        f = self.home / ".claude" / "provost" / "evals" / "reviewer" / "decisions.tsv"
        lines = f.read_text(encoding="utf-8").splitlines()
        cols = lines[0].split("\t")
        return cols, [dict(zip(cols, ln.split("\t"))) for ln in lines[1:]]

    def test_decisions_log_header_and_rows(self):
        self.build_cases()
        local = self.tmp / "REVIEW.md"
        local.write_text("r\n", encoding="utf-8")
        self.run_tool("--ids", "a-hit,d-clean-alarm", "--label", "base", "--hypothesis", "tabs\tand\nnewlines")
        self.run_tool("--ids", "a-hit,d-clean-alarm", "--label", "inj", "--inject", f"REVIEW.md={local}")
        cols, rows = self.decisions()
        self.assertEqual(cols, ["utc", "command", "label", "hypothesis", "cases", "ok", "recall", "falseAlarm",
                                "judgeYes", "costUsd", "injected", "verdict"])
        self.assertEqual(len(rows), 2)  # one header only, one row per finished command
        self.assertRegex(rows[0]["utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((rows[0]["command"], rows[0]["label"], rows[0]["hypothesis"]), ("run", "base", "tabs and newlines"))
        self.assertEqual((rows[0]["cases"], rows[0]["ok"], rows[0]["recall"], rows[0]["falseAlarm"]), ("2", "2", "1/1", "1/1"))
        self.assertEqual((rows[0]["costUsd"], rows[0]["injected"]), ("0.7500", "none"))
        self.assertEqual((rows[1]["label"], rows[1]["injected"], rows[1]["hypothesis"]), ("inj", "REVIEW.md", ""))
        self.assertEqual((rows[0]["verdict"], rows[1]["verdict"]), ("", ""))

    # --- rescore ---------------------------------------------------------------------------------

    def build_rescore(self):
        app = (self.repo / "src" / "app.js").read_text(encoding="utf-8").replace("line 2\n", "line 2 fixed-marker\n")
        self.write({"src/app.js": app})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fix guard")
        fix = self.git("rev-parse", "--short=8", "HEAD")
        exp = [{"file": "src/app.js", "lines": "2,9-10", "summary": f"guard missing; follow-up {fix}"}]
        for cid, expected in (("r-yes", exp), ("r-no", exp), ("r-garbage", exp), ("r-zero", exp), ("r-clean", [])):
            self.add_case(cid, {"cid": cid}, expected)
        run = self.home / ".claude" / "provost" / "evals" / "reviewer" / "runs" / "20261101T000000-old"
        run.mkdir(parents=True)
        reports = {
            "r-yes": "Important 1\n\n**I1** (Important) `src/app.js:20` YESMARK drops the guard.\n",
            "r-no": "Important 1\n\nI1 lib/other.js:9 NOMARK races on close.\n",
            "r-garbage": "Important 1\n\nI1 src/app.js:3 GARBMARK odd.\n",
            "r-zero": "Important 0\n\nNo material finding.\n",
            "r-clean": "Important 1\n\n### I1 src/app.js:4 CLEANMARK unchecked input\n",
            "r-nocase": "Important 1\n\nI1 src/app.js:4 NOCASEMARK\n",
        }
        for cid, text in reports.items():
            (run / f"{cid}.md").write_text(text, encoding="utf-8")
        (run / "r-err.err.txt").write_text("cli exit 3\n", encoding="utf-8")
        self.judge_map.write_text(json.dumps({
            "YESMARK": {"cost": 0.1, "text": 'Looks the same.\n{"verdict":"yes","finding":"F1"}'},
            "NOMARK": {"cost": 0.1, "text": '{"verdict":"no","finding":"none"}'},
            "GARBMARK": {"cost": 0.1, "text": "I cannot say."},
        }), encoding="utf-8")
        return fix

    def run_rescore(self, *args):
        out = subprocess.run(["node", str(TOOL), "rescore", "--claude", str(self.judge), *args], env=self.env,
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stderr

    def test_rescore_judges_blind(self):
        fix = self.build_rescore()
        err = self.run_rescore("--run", "20261101T000000-old", "--hypothesis", "old prompt")
        calls = self.calls()
        self.assertEqual(len(calls), 3)  # r-garbage, r-no, r-yes: not r-zero (no Important), r-clean, r-nocase
        for call in calls:
            a = call["args"]
            self.assertIn("-p", a)
            self.assertEqual(a[a.index("--model") + 1], "sonnet")
            self.assertEqual(a[a.index("--tools") + 1], "")
            self.assertEqual(a[a.index("--output-format") + 1], "json")
            self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
            self.assertIn("--no-session-persistence", a)
            self.assertNotIn("--agent", a)
            self.assertEqual(call["cwdFiles"], [])
            self.assertTrue(Path(call["cwd"]).resolve().is_relative_to(self.scratch.resolve()), call["cwd"])
            self.assertFalse(Path(call["cwd"]).exists(), "temporary judge dir left behind")
            low = call["stdin"].lower()
            for word in ("reviewer", "eval", "prompt", "20261101"):
                self.assertNotIn(word, low)
            self.assertNotIn("I1", call["stdin"])
            self.assertIn("F1", call["stdin"])
            self.assertIn("guard missing", call["stdin"])
        yes = next(c for c in calls if "YESMARK" in c["stdin"])
        self.assertIn(f"commit {fix}", yes["stdin"])
        self.assertIn("+line 2 fixed-marker", yes["stdin"])  # the fix diff for the expected file
        self.assertEqual(list(self.scratch.glob("wt-*")), [])

        runs = self.home / ".claude" / "provost" / "evals" / "reviewer" / "runs"
        lines = (runs / "20261101T000000-old.rescore.tsv").read_text(encoding="utf-8").splitlines()
        cols = lines[0].split("\t")
        self.assertEqual(cols, ["run", "id", "kind", "important", "lineHit", "fileHit", "judge", "finding", "costUsd", "status"])
        by = {r["id"]: r for r in (dict(zip(cols, ln.split("\t"))) for ln in lines[1:] if not ln.startswith("#"))}
        self.assertEqual(sorted(by), ["r-clean", "r-garbage", "r-no", "r-yes", "r-zero"])  # r-nocase / .err.txt skipped
        pick = lambda r: (r["kind"], r["important"], r["lineHit"], r["fileHit"], r["judge"], r["finding"], r["costUsd"], r["status"])
        self.assertEqual(pick(by["r-yes"]), ("defect", "1", "yes", "yes", "yes", "F1", "0.1000", "ok"))
        self.assertEqual(pick(by["r-no"]), ("defect", "1", "no", "no", "no", "none", "0.1000", "ok"))
        self.assertEqual(pick(by["r-garbage"]), ("defect", "1", "yes", "yes", "error", "", "0.1000", "ok"))
        self.assertEqual(pick(by["r-zero"]), ("defect", "0", "no", "no", "no", "none", "0.0000", "ok"))
        self.assertEqual(pick(by["r-clean"]), ("clean", "1", "", "", "", "", "0.0000", "ok"))
        self.assertIn("lineHit=yes (2): judge yes 1, judge no 0, judge unsure 0, judge error 1", err)
        self.assertIn("lineHit=no (2): judge yes 0, judge no 2, judge unsure 0, judge error 0", err)
        self.assertIn("total cost: $0.3000", err)

        cols, rows = self.decisions()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["command"], r["label"], r["hypothesis"], r["cases"], r["ok"]),
                         ("rescore", "20261101T000000-old", "old prompt", "5", "5"))
        self.assertEqual((r["recall"], r["falseAlarm"], r["judgeYes"], r["costUsd"], r["verdict"]),
                         ("2/4", "1/1", "1/4", "0.3000", ""))

    def test_rescore_max_cost_stops(self):
        self.build_rescore()
        err = self.run_rescore("--run", "20261101T000000-old", "--max-cost", "0.15")
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("stopped by ceiling: yes", err)


if __name__ == "__main__":
    unittest.main()
