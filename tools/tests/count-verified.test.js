// Tests for tools/audit/count_verified.js. Run: node --test tools/tests/
// Every fixture is a fresh temp git repo; the real repositories are never read or written.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs"), path = require("path"), os = require("os");
const { spawnSync } = require("child_process");

const TOOL = path.join(__dirname, "..", "audit", "count_verified.js");

function git(repo, args, env) {
  const r = spawnSync("git", ["-C", repo, ...args], { encoding: "utf8", env: { ...process.env, ...env } });
  assert.strictEqual(r.status, 0, r.stderr);
  return r.stdout;
}
function mkRepo() {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "provost-countverified-"));
  git(repo, ["init", "-q"]);
  git(repo, ["config", "user.name", "Test"]);
  git(repo, ["config", "user.email", "test@example.com"]);
  git(repo, ["config", "commit.gpgsign", "false"]);
  return repo;
}
// Commits an empty commit with `message`; `date` (ISO) sets author and committer dates.
function commit(repo, message, date) {
  const file = path.join(repo, ".git", "msg.txt");
  fs.writeFileSync(file, message);
  const env = date ? { GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date } : {};
  git(repo, ["commit", "-q", "--allow-empty", "-F", file], env);
}
function run(...args) {
  const r = spawnSync(process.execPath, [TOOL, ...args], { encoding: "utf8" });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr };
}
function json(...args) {
  const r = run("--json", ...args);
  assert.strictEqual(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}
const AGENT = "Claude-Session: https://claude.ai/code/session_x";
const withRepo = (fn) => {
  const repo = mkRepo();
  try { return fn(repo); } finally { fs.rmSync(repo, { recursive: true, force: true }); }
};

test("a human commit is skipped and counted as human", () => withRepo((repo) => {
  commit(repo, "human work\n\nVerified: machine\n");
  const t = json(repo).total;
  assert.strictEqual(t.agent, 0);
  assert.strictEqual(t.human, 1);
  assert.strictEqual(t.verified, 0);
}));

test("agent commit with `Verified: machine, review` is verified with both kinds", () => withRepo((repo) => {
  commit(repo, `feat: x\n\nbody\n\nVerified: machine, review\n${AGENT}\n`);
  const out = json("--list", repo);
  assert.strictEqual(out.total.agent, 1);
  assert.strictEqual(out.total.verified, 1);
  assert.strictEqual(out.total.kinds.machine, 1);
  assert.strictEqual(out.total.kinds.review, 1);
  assert.strictEqual(out.total.kinds.live, 0);
  assert.deepStrictEqual(out.commits[0].kinds, ["machine", "review"]);
  assert.strictEqual(out.commits[0].state, "verified");
}));

test("`Verified: none — docs only` is none with a reason", () => withRepo((repo) => {
  commit(repo, `docs: y\n\nVerified: none — docs only\n${AGENT}\n`);
  const out = json("--list", repo);
  assert.strictEqual(out.total.none, 1);
  assert.strictEqual(out.total.noneReason, 1);
  assert.strictEqual(out.commits[0].state, "none");
  assert.strictEqual(out.commits[0].reason, "docs only");
}));

test("`none` accepts `--` and `-` separators", () => withRepo((repo) => {
  commit(repo, `a\n\nVerified: none -- typo\n${AGENT}\n`);
  commit(repo, `b\n\nVerified: None - typo\n${AGENT}\n`);
  const t = json(repo).total;
  assert.strictEqual(t.none, 2);
  assert.strictEqual(t.noneReason, 2);
}));

test("`Verified: none` without a reason is none without reason", () => withRepo((repo) => {
  commit(repo, `docs: z\n\nVerified: none\n${AGENT}\n`);
  commit(repo, `docs: w\n\nVerified: none —\n${AGENT}\n`);
  const t = json(repo).total;
  assert.strictEqual(t.none, 2);
  assert.strictEqual(t.noneReason, 0);
}));

test("agent commit without a trailer is missing", () => withRepo((repo) => {
  commit(repo, `feat: q\n\nno trailer here\n${AGENT}\n`);
  const out = json("--list", repo);
  assert.strictEqual(out.total.missing, 1);
  assert.strictEqual(out.commits[0].state, "missing");
}));

test("`Verified: machine, bogus` is verified and counts one unknown", () => withRepo((repo) => {
  commit(repo, `feat: u\n\nVerified: machine, bogus\n${AGENT}\n`);
  const t = json(repo).total;
  assert.strictEqual(t.verified, 1);
  assert.strictEqual(t.kinds.machine, 1);
  assert.strictEqual(t.kinds.unknown, 1);
}));

test("`Verified: bogus` is malformed; so is an empty value", () => withRepo((repo) => {
  commit(repo, `feat: m\n\nVerified: bogus\n${AGENT}\n`);
  commit(repo, `feat: n\n\nVerified:\n${AGENT}\n`);
  const t = json(repo).total;
  assert.strictEqual(t.malformed, 2);
  assert.strictEqual(t.verified, 0);
}));

test("squash-style body: Verified in its own paragraph, lowercase co-authored-by", () => withRepo((repo) => {
  commit(repo, "feat: squash (#1)\n\n* step one\n\nVerified: machine\n\nCo-authored-by: Claude Sonnet <noreply@anthropic.com>\n");
  const out = json("--list", repo);
  assert.strictEqual(out.total.agent, 1);
  assert.strictEqual(out.total.verified, 1);
  assert.deepStrictEqual(out.commits[0].kinds, ["machine"]);
}));

test("the last Verified line wins", () => withRepo((repo) => {
  commit(repo, `feat: r\n\nVerified: machine\n\nVerified: live\n${AGENT}\n`);
  const out = json("--list", repo);
  assert.deepStrictEqual(out.commits[0].kinds, ["live"]);
}));

test("--since excludes an older commit; --until excludes a newer one", () => withRepo((repo) => {
  commit(repo, `old\n\nVerified: machine\n${AGENT}\n`, "2026-01-05T12:00:00Z");
  commit(repo, `new\n\nVerified: live\n${AGENT}\n`, "2026-11-10T12:00:00Z");
  assert.strictEqual(json("--since", "2026-06-01", repo).total.agent, 1);
  assert.strictEqual(json(repo).total.agent, 2);
  const until = json("--until", "2026-06-01", "--list", repo);
  assert.strictEqual(until.total.agent, 1);
  assert.strictEqual(until.commits[0].subject, "old");
  assert.strictEqual(until.since, null);
  assert.strictEqual(until.until, "2026-06-01");
}));

test("the same repo given twice does not double count", () => withRepo((repo) => {
  commit(repo, `feat: d\n\nVerified: machine\n${AGENT}\n`);
  const out = json(repo, repo);
  assert.strictEqual(out.total.agent, 1);
  assert.strictEqual(out.repos.length, 2);
}));

test("--json shape; commits only with --list", () => withRepo((repo) => {
  commit(repo, `feat: s\n\nVerified: visual\n${AGENT}\n`);
  const out = json(repo);
  assert.deepStrictEqual(Object.keys(out), ["since", "until", "repos", "total"]);
  assert.strictEqual(out.repos[0].agent, 1);
  assert.ok(out.repos[0].repo);
  for (const k of ["agent", "human", "verified", "none", "noneReason", "missing", "malformed", "kinds"]) assert.ok(k in out.total, k);
  const listed = json("--list", repo);
  assert.deepStrictEqual(Object.keys(listed.commits[0]), ["hash", "repo", "state", "kinds", "reason", "subject"]);
}));

test("text output: total line and --list lines; never prints the body", () => withRepo((repo) => {
  commit(repo, `feat: t\n\nSECRET-BODY-TEXT\nVerified: machine, review\n${AGENT}\n`);
  const r = run("--list", repo);
  assert.strictEqual(r.status, 0, r.stderr);
  assert.match(r.stdout, /^[0-9a-f]{7} verified +machine,review +feat: t$/m);
  assert.match(r.stdout, /^total {2}agent 1 {2}verified 1 {2}none 0 \(reason 0\) {2}missing 0 {2}malformed 0 {2}human 0 {2}kinds machine=1 live=0 visual=0 review=1 human=0 unknown=0$/m);
  assert.doesNotMatch(r.stdout, /SECRET-BODY-TEXT/);
}));

test("no repo gives exit 2 with a usage line; a non-repo gives exit 1", () => {
  const r = run();
  assert.strictEqual(r.status, 2);
  assert.match(r.stderr, /usage/i);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "provost-countverified-notrepo-"));
  try {
    const bad = run(dir);
    assert.strictEqual(bad.status, 1);
    assert.ok(bad.stderr.length > 0);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
