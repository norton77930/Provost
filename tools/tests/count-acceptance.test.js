// Tests for tools/audit/count_acceptance.js. Run: node --test tools/tests/
// Every fixture is a fresh temp directory; the real repositories are never read.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs"), path = require("path"), os = require("os");
const { spawnSync } = require("child_process");

const TOOL = path.join(__dirname, "..", "audit", "count_acceptance.js");

function run(...args) {
  const r = spawnSync(process.execPath, [TOOL, ...args], { encoding: "utf8" });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr };
}
// Builds <tmp>/repo/.claude/acceptance/<name>.json from `files` ({ name: string | Buffer }).
function withFixture(files, fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provost-countacceptance-"));
  try {
    const dir = path.join(root, "repo", ".claude", "acceptance");
    fs.mkdirSync(dir, { recursive: true });
    for (const [name, body] of Object.entries(files)) fs.writeFileSync(path.join(dir, name + ".json"), body);
    return fn(root);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}
const doc = (obj) => JSON.stringify(obj);
const gates = (...g) => doc({ task: "t", gates: g.map(([kind, status]) => ({ id: kind + status, kind, check: "c", status })) });
// The first row after the summary line is the per-repo row; read its counts.
function counts(stdout) {
  const row = stdout.split("\n").find((l) => l.startsWith(" files="));
  return Object.fromEntries([...row.matchAll(/(\w+)=(\d+)/g)].map((m) => [m[1], Number(m[2])]));
}

test("a file with a UTF-8 BOM parses instead of counting as unparseable", () => {
  const bom = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(gates(["machine", "pass"], ["human", "pending"]))]);
  withFixture({ withbom: bom }, (root) => {
    const r = run(root);
    assert.strictEqual(r.status, 0, r.stderr);
    const c = counts(r.stdout);
    assert.strictEqual(c.bad, 0);
    assert.strictEqual(c.waitingHuman, 1);
  });
});

test("invalid JSON is counted as unparseable", () => {
  withFixture({ broken: "{not json" }, (root) => {
    assert.strictEqual(counts(run(root).stdout).bad, 1);
  });
});

test("all-pass files split into waitingHuman and allDone; unmet and closed stay apart", () => {
  withFixture({
    waiting: gates(["machine", "pass"], ["human", "pending"]),
    done: gates(["machine", "pass"], ["human", "pass"]),
    nohuman: gates(["machine", "pass"], ["live", "pass"]),
    unmet: gates(["machine", "pending"], ["human", "pending"]),
    failed: gates(["machine", "fail"]),
    closed: doc({ closed: "merged in #1", gates: [{ id: "a", kind: "machine", status: "pending" }] }),
  }, (root) => {
    const r = run(root);
    assert.strictEqual(r.status, 0, r.stderr);
    const c = counts(r.stdout);
    assert.deepStrictEqual(
      { files: c.files, closed: c.closed, waitingHuman: c.waitingHuman, allDone: c.allDone, unmet: c.unmet, bad: c.bad },
      { files: 6, closed: 1, waitingHuman: 1, allDone: 2, unmet: 2, bad: 0 });
    assert.match(r.stdout.split("\n")[0], /waitingHuman\) 1 .*allDone\) 2 .*unmet 2 \| unparseable 0/);
  });
});

test("--list prints one line per file with the new state names", () => {
  withFixture({
    a: gates(["machine", "pass"], ["human", "pending"]),
    b: gates(["machine", "pass"], ["human", "pass"]),
  }, (root) => {
    const lines = run("--list", root).stdout.split("\n");
    assert.ok(lines.some((l) => /^waitingHuman +.*acceptance\/a\.json$/.test(l)), lines.join("\n"));
    assert.ok(lines.some((l) => /^allDone +.*acceptance\/b\.json$/.test(l)), lines.join("\n"));
  });
});
