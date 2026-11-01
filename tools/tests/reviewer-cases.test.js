// Tests for tools/reviewer-cases.js candidates (#80). Run: node --test tools/tests/
// The tool reads sessions from os.homedir()/.claude/projects, so each run gets a temp HOME/USERPROFILE.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs"), path = require("path"), os = require("os");
const { spawnSync, execFileSync } = require("child_process");

const TOOL = path.join(__dirname, "..", "reviewer-cases.js");
const T0 = Date.parse("2020-01-01T12:00:00Z"); // first dispatch
const iso = ms => new Date(ms).toISOString();

function commit(repo, file, subject, ms) {
  fs.writeFileSync(path.join(repo, file), subject + "\n");
  const env = { ...process.env, GIT_AUTHOR_DATE: iso(ms), GIT_COMMITTER_DATE: iso(ms) };
  const g = (...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, ...a], { encoding: "utf8", env }).trim();
  g("add", "."); g("commit", "-qm", subject);
  return g("rev-parse", "HEAD");
}

// A repo where tickets #25, #28 and #22 commit in one window after the dispatch, plus a session that
// dispatches code-reviewer once per claim, one second apart.
function fixture(claims) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provost-casestest-"));
  const repo = path.join(root, "repo");
  fs.mkdirSync(repo);
  execFileSync("git", ["-C", repo, "init", "-q"]);
  commit(repo, "init.txt", "chore: init", T0 - 3600e3);
  const sha = {
    a: commit(repo, "a.txt", "feat(search): search the decision log (#25)", T0 + 600e3),
    b: commit(repo, "b.txt", "fix(store): migrate review follow-ups (#28)", T0 + 1200e3),
    c: commit(repo, "c.txt", "feat(board): show gates (#22)", T0 + 1800e3),
  };
  const proj = path.join(root, ".claude", "projects", "p1");
  fs.mkdirSync(proj, { recursive: true });
  const lines = claims.map((claim, i) => JSON.stringify({
    type: "assistant", timestamp: iso(T0 + i * 1000), cwd: repo,
    message: { content: [{ type: "tool_use", name: "Agent", input: { subagent_type: "code-reviewer", prompt: claim } }] },
  }));
  fs.writeFileSync(path.join(proj, "d3ec1e03-0000.jsonl"), lines.join("\n") + "\n");
  return { root, repo, sha };
}

function candidates(fx) {
  const r = spawnSync(process.execPath, [TOOL, "candidates", "--repo", fx.repo, "--tsv"],
    { encoding: "utf8", env: { ...process.env, HOME: fx.root, USERPROFILE: fx.root } });
  assert.strictEqual(r.status, 0, r.stderr);
  const [head, ...rows] = r.stdout.trim().split("\n").map(l => l.split("\t"));
  return rows.map(cells => Object.fromEntries(head.map((c, i) => [c, cells[i]])));
}

test("a claim naming a ticket gets that ticket's commit, not the first commit in the window", () => {
  const fx = fixture([
    "Review ticket #28: migrate existing records",
    "Review ticket #22 board gates",
    "25: search the decision log",
    "Please review the (22) change",
  ]);
  try {
    const rows = candidates(fx);
    assert.deepStrictEqual(rows.map(r => [r.head, r.headFrom]), [
      [fx.sha.b.slice(0, 12), "ticket"],
      [fx.sha.c.slice(0, 12), "ticket"],
      [fx.sha.a.slice(0, 12), "ticket"],
      [fx.sha.c.slice(0, 12), "ticket"],
    ]);
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});

test("a ticket with no matching subject keeps the window head, flagged window?", () => {
  const fx = fixture(["Review ticket #31 something else", "Review the latest change"]);
  try {
    const rows = candidates(fx);
    assert.deepStrictEqual(rows.map(r => [r.head, r.headFrom]), [
      [fx.sha.a.slice(0, 12), "window?"],
      [fx.sha.a.slice(0, 12), "window"],
    ]);
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});
