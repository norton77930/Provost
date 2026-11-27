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

// A merge commit named in the claim (e.g. "based on main 2246db3") must not become the head.
test("candidates skips a merge commit named in the claim", () => {
  const fx = fixture([]);
  try {
    const g = (when, ...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-C", fx.repo, ...a],
      { encoding: "utf8", env: { ...process.env, GIT_AUTHOR_DATE: iso(when), GIT_COMMITTER_DATE: iso(when) } }).trim();
    g(T0, "checkout", "-q", "-b", "side");
    commit(fx.repo, "s.txt", "feat: side", T0 + 2000e3);
    g(T0, "checkout", "-q", "-");
    commit(fx.repo, "m.txt", "feat: main", T0 + 2100e3);
    g(T0 + 2400e3, "merge", "--no-ff", "-q", "-m", "Merge side", "side");
    const merge = g(T0, "rev-parse", "HEAD");
    const proj = path.join(fx.root, ".claude", "projects", "p1");
    fs.writeFileSync(path.join(proj, "d3ec1e03-0000.jsonl"), JSON.stringify({
      type: "assistant", timestamp: iso(T0), cwd: fx.repo,
      message: { content: [{ type: "tool_use", name: "Agent", input: { subagent_type: "code-reviewer", prompt: `Review the change based on main ${merge.slice(0, 7)}` } }] },
    }) + "\n");
    const rows = candidates(fx);
    assert.deepStrictEqual(rows.map(r => [r.head, r.headFrom]), [[fx.sha.a.slice(0, 12), "window"]]);
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});

// commits mode: git only. Old commits are dated 2020, so they are older than --fix-days.
function lines(n, tag) { return Array.from({ length: n }, (_, i) => `${tag}${i}`).join("\n") + "\n"; }
function commitsRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provost-casestest-"));
  const repo = path.join(root, "repo");
  fs.mkdirSync(repo);
  const g = (when, ...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, ...a],
    { encoding: "utf8", env: { ...process.env, GIT_AUTHOR_DATE: iso(when), GIT_COMMITTER_DATE: iso(when) } }).trim();
  const put = (file, text, subject, when) => { fs.writeFileSync(path.join(repo, file), text); g(when, "add", "."); g(when, "commit", "-qm", subject); return g(when, "rev-parse", "HEAD"); };
  g(T0, "init", "-q");
  const sha = { root: put("init.txt", "init\n", "chore: init", T0) };
  sha.ok = put("ok.txt", lines(300, "ok"), "feat: in range", T0 + 1000e3);
  sha.small = put("small.txt", lines(10, "s"), "feat: too small", T0 + 2000e3);
  sha.big = put("big.txt", lines(1300, "b"), "feat: too big", T0 + 3000e3);
  sha.young = put("young.txt", lines(300, "y"), "feat: too young", Date.now() - 86400e3);
  sha.fixed = put("fixed.txt", lines(300, "f"), "feat: later fixed", T0 + 4000e3);
  put("fixed.txt", lines(3, "g") + lines(297, "f").split("\n").slice(3).join("\n"), "fix: repair fixed", T0 + 5000e3);
  g(T0 + 6000e3, "checkout", "-q", "-b", "side");
  sha.side = put("side.txt", lines(300, "d"), "feat: side work", T0 + 6000e3);
  g(T0, "checkout", "-q", "-");
  put("main2.txt", lines(300, "m"), "feat: main work", T0 + 7000e3);
  g(T0 + 8000e3, "merge", "--no-ff", "-q", "-m", "Merge side", "side");
  sha.merge = g(T0, "rev-parse", "HEAD");
  return { root, repo, sha };
}
function commitsMode(fx, ...extra) {
  const r = spawnSync(process.execPath, [TOOL, "commits", "--repo", fx.repo, "--tsv", ...extra],
    { encoding: "utf8", env: { ...process.env, HOME: fx.root, USERPROFILE: fx.root } });
  assert.strictEqual(r.status, 0, r.stderr);
  const [head, ...rows] = r.stdout.trim().split("\n").map(l => l.split("\t"));
  return { rows: rows.map(cells => Object.fromEntries(head.map((c, i) => [c, cells[i]]))), head, stderr: r.stderr };
}

test("commits lists an old, in-range, unfixed commit with the clean-case claim", () => {
  const fx = commitsRepo();
  try {
    const { rows, head } = commitsMode(fx);
    assert.deepStrictEqual(head, ["repo", "sha", "parent", "date", "lines", "files", "subject", "claim"]);
    const r = rows.find(x => x.sha === fx.sha.ok.slice(0, 12));
    assert.ok(r, "ok commit listed");
    assert.strictEqual(r.parent, fx.sha.root.slice(0, 12));
    assert.strictEqual(r.date, "2020-01-01");
    assert.strictEqual(r.lines, "300");
    assert.strictEqual(r.files, "1");
    assert.strictEqual(r.claim, `Commit ${fx.sha.ok.slice(0, 7)} "feat: in range". Review whether this change is correct and complete.`);
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});

test("commits excludes merges, size misfits, young commits and commits a later fix touched", () => {
  const fx = commitsRepo();
  try {
    const { rows, stderr } = commitsMode(fx);
    const listed = new Set(rows.map(r => r.sha));
    for (const k of ["merge", "small", "big", "young", "fixed", "root"]) assert.ok(!listed.has(fx.sha[k].slice(0, 12)), `${k} excluded`);
    for (const k of ["ok", "side"]) assert.ok(listed.has(fx.sha[k].slice(0, 12)), `${k} listed`);
    assert.match(stderr, /; merge=\d+; size=\d+; young=1; fixed=1/);
    assert.strictEqual(commitsMode(fx, "--since", "2021-01-01").rows.length, 0);
    assert.ok(commitsMode(fx, "--min-lines", "5", "--max-lines", "20").rows.some(r => r.sha === fx.sha.small.slice(0, 12)));
    assert.ok(commitsMode(fx, "--fix-days", "0").rows.some(r => r.sha === fx.sha.young.slice(0, 12)));
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});

// A side-branch commit and its cherry-picked copy on main share one git patch-id (identical diff, so the
// copy is identical rather than carrying an extra docs file, which would change the patch-id).
function patchIdRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provost-casestest-"));
  const repo = path.join(root, "repo");
  fs.mkdirSync(repo);
  const g = (when, ...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, ...a],
    { encoding: "utf8", env: { ...process.env, GIT_AUTHOR_DATE: iso(when), GIT_COMMITTER_DATE: iso(when) } }).trim();
  const put = (file, text, subject, when) => { fs.writeFileSync(path.join(repo, file), text); g(when, "add", "."); g(when, "commit", "-qm", subject); return g(when, "rev-parse", "HEAD"); };
  g(T0, "init", "-q", "-b", "main");
  put("init.txt", "init\n", "chore: init", T0);
  g(T0, "checkout", "-q", "-b", "side");
  const sha = {};
  sha.x = put("x.txt", lines(300, "x"), "feat: x", T0 + 1000e3); // later fixed through its main copy
  sha.z = put("z.txt", lines(300, "z"), "feat: z", T0 + 1500e3); // copied to main, never fixed
  sha.only = put("only.txt", lines(300, "o"), "feat: side only", T0 + 1800e3); // no copy, no fix
  g(T0, "checkout", "-q", "main");
  g(T0 + 2000e3, "cherry-pick", sha.x);
  sha.xCopy = g(T0, "rev-parse", "HEAD");
  g(T0 + 2500e3, "cherry-pick", sha.z);
  sha.zCopy = g(T0, "rev-parse", "HEAD");
  put("x.txt", lines(3, "y") + lines(297, "x").split("\n").slice(3).join("\n"), "fix: repair x", T0 + 3000e3);
  return { root, repo, sha };
}

test("commits treats a cherry-picked copy as the same commit: a fix on the copy marks the side commit fixed", () => {
  const fx = patchIdRepo();
  try {
    const { rows, stderr } = commitsMode(fx);
    const listed = rows.map(r => r.sha);
    for (const k of ["x", "xCopy"]) assert.ok(!listed.includes(fx.sha[k].slice(0, 12)), `${k} excluded`);
    assert.ok(listed.includes(fx.sha.only.slice(0, 12)), "side-only commit without an equivalent stays listed");
    // an unfixed group is one row, the copy reachable from HEAD
    assert.ok(listed.includes(fx.sha.zCopy.slice(0, 12)), "main copy of z listed");
    assert.ok(!listed.includes(fx.sha.z.slice(0, 12)), "side copy of z dropped");
    assert.strictEqual(rows.length, 2);
    assert.match(stderr, /fixed=1; dup=2/);
  } finally { fs.rmSync(fx.root, { recursive: true, force: true }); }
});
