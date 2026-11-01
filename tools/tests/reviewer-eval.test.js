// Tests for tools/reviewer-eval.js (#79). Run: node --test tools/tests/
// The tool resolves ~/.claude from os.homedir(), so each run gets a temp HOME/USERPROFILE.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs"), path = require("path"), os = require("os");
const { spawnSync, execFileSync } = require("child_process");

const TOOL = path.join(__dirname, "..", "reviewer-eval.js");

function git(repo, ...a) {
  return execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, ...a], { encoding: "utf8" }).trim();
}

// Temp home with a two-commit repo and one case per id; claim = claims[id] or the id.
function fixture(ids, claims = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provost-evaltest-"));
  const repo = path.join(root, "repo");
  fs.mkdirSync(repo);
  git(repo, "init", "-q");
  fs.writeFileSync(path.join(repo, "a.txt"), "one\n");
  git(repo, "add", "."); git(repo, "commit", "-qm", "first");
  const base = git(repo, "rev-parse", "HEAD");
  fs.writeFileSync(path.join(repo, "a.txt"), "one\ntwo\n");
  git(repo, "commit", "-qam", "second");
  const head = git(repo, "rev-parse", "HEAD");
  const cases = path.join(root, "cases");
  for (const id of ids) {
    fs.mkdirSync(path.join(cases, id), { recursive: true });
    fs.writeFileSync(path.join(cases, id, "case.json"), JSON.stringify({ repo, base, head, claim: claims[id] || id, expected: [], origin: "clean" }));
  }
  // Fake CLI: reads the prompt on stdin; a claim of "LIMIT" gets the usage-limit reply (exit 1).
  const cli = path.join(root, "fake-claude.js");
  fs.writeFileSync(cli, `
let s = ""; process.stdin.on("data", d => s += d).on("end", () => {
  if (/Claim to review: LIMIT/.test(s)) {
    console.log(JSON.stringify({ type: "result", is_error: true, result: "You've hit your 5-hour limit \\u00b7 resets 3pm", total_cost_usd: 0 }));
    process.exit(1);
  }
  console.log(JSON.stringify({ type: "result", is_error: false, result: "No Important findings.", total_cost_usd: 0.01 }));
});`);
  return { root, cases, cli };
}

const runTool = (fx, extra = []) => spawnSync(process.execPath, [TOOL, "run", "--cases-dir", fx.cases, "--claude", fx.cli, "--label", "t", "--tsv", ...extra],
  { encoding: "utf8", env: { ...process.env, HOME: fx.root, USERPROFILE: fx.root } });

test("usage-limit reply stops the run, is saved as .err.txt and lists a rerun", () => {
  const fx = fixture(["c1", "c2", "c3", "c4"], { c2: "LIMIT" });
  try {
    const r = runTool(fx);
    const runsDir = path.join(fx.root, ".claude", "provost", "evals", "reviewer", "runs");
    const rows = r.stdout.trim().split("\n").slice(1).map(l => l.split("\t"));
    assert.deepStrictEqual(rows.map(x => [x[0], x[8]]), [["c1", "ok"], ["c2", "error:usage-limit"]]);
    const rep = path.join(runsDir, fs.readdirSync(runsDir).find(d => !d.endsWith(".tsv")));
    assert.ok(fs.existsSync(path.join(rep, "c1.md")), "c1.md exists");
    assert.ok(fs.existsSync(path.join(rep, "c2.err.txt")), "c2.err.txt exists");
    assert.ok(!fs.existsSync(path.join(rep, "c2.md")), "no c2.md");
    assert.match(fs.readFileSync(path.join(rep, "c2.err.txt"), "utf8"), /hit your 5-hour limit/);
    assert.ok(!fs.existsSync(path.join(rep, "c3.md")) && !fs.existsSync(path.join(rep, "c4.md")), "later cases not run");
    assert.match(r.stderr, /not run: c3, c4/);
    assert.match(r.stderr, /--ids c2,c3,c4/);
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});
