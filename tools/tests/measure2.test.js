// Tests for tools/audit/measure2.js. Run: node --test tools/tests/
// Fixtures are a temp CLAUDE_CONFIG_DIR with projects/<proj>/<session>.jsonl; real transcripts are never read.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs"), path = require("path"), os = require("os");
const { spawnSync } = require("child_process");

const TOOL = path.join(__dirname, "..", "audit", "measure2.js");
const SINCE = "2026-11-01T00:00:00Z";

// opus-5-5 prices: 10M cache-read tokens = 2.00 USD, 100K output tokens = 2.00 USD.
const usage = (out) => ({ input_tokens: 0, output_tokens: out, cache_read_input_tokens: 10000000 });
const asst = (id, ts, content, out = 100000) => JSON.stringify({
  type: "assistant", timestamp: ts, uuid: "a-" + id,
  message: { id, model: "claude-opus-5-5", usage: usage(out), content },
});
const turn = (uuid, ts) => JSON.stringify({ type: "system", subtype: "turn_duration", uuid, timestamp: ts, durationMs: 1000 });
const PUSH = [{ type: "tool_use", name: "PushNotification", input: { message: "m" } }];
const WORK = [{ type: "tool_use", name: "Bash", input: { command: "ls" } }];
const TEXT = [{ type: "text", text: "done" }];

// Session A starts 11-10: push-only, work, text-only, turns u1+u2; then (11-12) push-only and turn u3.
// Session B (a resumed transcript) starts 11-14: push-only, turns u2 (repeated from A) and u4.
// Subagent transcript of A: one work request.
function withFixture(fn) {
  const cfg = fs.mkdtempSync(path.join(os.tmpdir(), "provost-measure2-"));
  try {
    const proj = path.join(cfg, "projects", "proj");
    fs.mkdirSync(path.join(proj, "sessA", "subagents"), { recursive: true });
    const lines = (...l) => l.join("\n") + "\n";
    fs.writeFileSync(path.join(proj, "sessA.jsonl"), lines(
      asst("m-a1", "2026-11-10T00:01:00Z", PUSH),
      asst("m-a2", "2026-11-10T00:02:00Z", WORK, 200000),
      asst("m-a3", "2026-11-10T00:03:00Z", TEXT),
      turn("u1", "2026-11-10T00:04:00Z"),
      turn("u2", "2026-11-10T00:05:00Z"),
      asst("m-a4", "2026-11-12T00:00:00Z", PUSH),
      turn("u3", "2026-11-12T00:01:00Z")));
    fs.writeFileSync(path.join(proj, "sessB.jsonl"), lines(
      asst("m-b1", "2026-11-14T00:01:00Z", PUSH),
      turn("u2", "2026-11-14T00:02:00Z"),
      turn("u4", "2026-11-14T00:03:00Z")));
    fs.writeFileSync(path.join(proj, "sessA", "subagents", "agent-x.jsonl"), lines(
      asst("m-s1", "2026-11-10T00:10:00Z", WORK)));
    return fn(cfg);
  } finally { fs.rmSync(cfg, { recursive: true, force: true }); }
}
function run(cfg, env = {}) {
  const clean = { ...process.env, CLAUDE_CONFIG_DIR: cfg };
  for (const k of ["UNTIL", "SFROM", "SUNTIL", "NOSUB"]) delete clean[k];
  const r = spawnSync(process.execPath, [TOOL, SINCE], { encoding: "utf8", env: { ...clean, ...env } });
  assert.strictEqual(r.status, 0, r.stderr);
  return r.stdout;
}
// " push-only  n=3 ..." -> { "push-only": 3 }
function classes(out) {
  const res = {};
  for (const l of out.split("\n")) { const m = l.match(/^ (\S+)\s+n=\s*(\d+) \(/); if (m) res[m[1]] = Number(m[2]); }
  return res;
}
const summary = (out) => out.split("\n").find((l) => l.startsWith(" SESSIONS"));

// The class lines the script printed before the turn summary existed, for this fixture.
const PRE_CHANGE_CLASS_LINES = [
  " push-only                                n=     3 (60.0%) usd=   12.00 (54.5%) out=300000 avgCacheRead=10000000",
  " work                                     n=     1 (20.0%) usd=    6.00 (27.3%) out=200000 avgCacheRead=10000000",
  " text-only                                n=     1 (20.0%) usd=    4.00 (18.2%) out=100000 avgCacheRead=10000000",
];

test("without env vars the class lines are unchanged and the summary line is added", () => withFixture((cfg) => {
  const out = run(cfg);
  const classLines = out.split("\n").filter((l) => /^ \S.* n= *\d+ \(/.test(l));
  assert.deepStrictEqual(classLines, PRE_CHANGE_CLASS_LINES);
  assert.ok(out.includes(" TOTAL n 5 usd 22.00\n"), out);
  assert.ok(summary(out), out);
}));

test("summary counts sessions, deduplicated turns, push-only ratio and ceremony share", () => withFixture((cfg) => {
  const out = run(cfg);
  assert.deepStrictEqual(classes(out), { "push-only": 3, work: 1, "text-only": 1 });
  // u2 appears in both files: 4 distinct turns. 3 push-only / 4 turns = 0.750. Ceremony = push-only = 3 of 5.
  assert.strictEqual(summary(out),
    " SESSIONS kept 2 skipped 0 | TURNS (turn_duration, dedup) 4 | push-only 3 ratio push-only/turn 0.750 | ceremony 3 60.0%");
}));

test("UNTIL drops records after it, turn_duration included", () => withFixture((cfg) => {
  const out = run(cfg, { UNTIL: "2026-11-11T00:00:00Z" });
  assert.deepStrictEqual(classes(out), { "push-only": 1, work: 1, "text-only": 1 });
  assert.strictEqual(summary(out),
    " SESSIONS kept 2 skipped 0 | TURNS (turn_duration, dedup) 2 | push-only 1 ratio push-only/turn 0.500 | ceremony 1 33.3%");
}));

test("SFROM and SUNTIL keep only sessions whose first timestamp is in [SFROM, SUNTIL)", () => withFixture((cfg) => {
  const late = run(cfg, { SFROM: "2026-11-13T00:00:00Z" });
  assert.deepStrictEqual(classes(late), { "push-only": 1 });
  assert.strictEqual(summary(late),
    " SESSIONS kept 1 skipped 1 | TURNS (turn_duration, dedup) 2 | push-only 1 ratio push-only/turn 0.500 | ceremony 1 100.0%");
  const early = run(cfg, { SUNTIL: "2026-11-13T00:00:00Z" });
  assert.deepStrictEqual(classes(early), { "push-only": 2, work: 1, "text-only": 1 });
  assert.match(summary(early), /^ SESSIONS kept 1 skipped 1 \| TURNS \(turn_duration, dedup\) 3 /);
  // The first timestamp is inclusive at SFROM and exclusive at SUNTIL.
  assert.match(summary(run(cfg, { SFROM: "2026-11-10T00:01:00Z", SUNTIL: "2026-11-14T00:01:00Z" })), /kept 1 skipped 1/);
}));

test("NOSUB skips subagent transcripts", () => withFixture((cfg) => {
  assert.match(run(cfg), /^ \?\s+n=1 /m);
  assert.doesNotMatch(run(cfg, { NOSUB: "1" }), /^ \?\s+n=1 /m);
}));
