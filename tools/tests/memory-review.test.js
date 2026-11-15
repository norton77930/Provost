// Tests for tools/memory-review.js. Run: node --test tools/tests/
// Every fixture lives in a fresh temp config dir; the real ~/.claude is never read or written.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs"), path = require("path"), os = require("os"), crypto = require("crypto");
const { spawnSync } = require("child_process");

const TOOL = path.join(__dirname, "..", "memory-review.js");
const TODAY = "2026-11-16";

const mkConfig = () => fs.mkdtempSync(path.join(os.tmpdir(), "provost-memreview-"));

// Writes <config>/projects/<slug>/memory/<name>; `mtime` is an ISO date for fs.utimesSync.
function put(config, slug, name, content, mtime) {
  const dir = path.join(config, "projects", slug, "memory");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, name);
  fs.writeFileSync(file, content);
  if (mtime) { const t = new Date(mtime + "T12:00:00Z"); fs.utimesSync(file, t, t); }
  return file;
}
// A memory file; the body starts on line 8 when `modified` is given.
const mem = (modified, body) =>
  `---\nname: m\ndescription: d\nmetadata:\n  type: project\n${modified ? `  modified: ${modified}T07:00:00Z\n` : ""}---\n${body}\n`;

function run(config, ...args) {
  const env = { ...process.env };
  delete env.CLAUDE_CONFIG_DIR;
  const r = spawnSync(process.execPath, [TOOL, "--config", config, "--today", TODAY, ...args], { encoding: "utf8", env });
  return { status: r.status, stdout: r.stdout, stderr: r.stderr };
}
function json(config, ...args) {
  const r = run(config, "--json", ...args);
  assert.strictEqual(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}
const findings = (out, check, slug = "p") => {
  const d = out.dirs.find(x => x.slug === slug);
  return d ? d.findings.filter(f => !check || f.check === check) : [];
};
// One-project config with a MEMORY.md that links the given files, so C3 stays quiet.
function oneProject(files, mtime) {
  const config = mkConfig();
  put(config, "p", "MEMORY.md", Object.keys(files).map(n => `- [${n}](${n}) — hook`).join("\n") + "\n", mtime);
  for (const [n, c] of Object.entries(files)) put(config, "p", n, c, mtime);
  return config;
}

test("discovery: a memory dir added after the first run appears in the next run; dirs without .md are ignored", () => {
  const config = mkConfig();
  put(config, "old-proj", "a.md", mem("2026-11-15", "hello"));
  fs.mkdirSync(path.join(config, "projects", "empty-proj", "memory"), { recursive: true });
  fs.writeFileSync(path.join(config, "projects", "empty-proj", "memory", "notes.txt"), "x");
  fs.mkdirSync(path.join(config, "projects", "no-memory-proj"), { recursive: true });
  const first = json(config).dirs.map(d => d.slug);
  assert.deepStrictEqual(first, ["old-proj"]);
  put(config, "new-proj", "x.md", mem("2026-11-15", "hello"));
  const second = json(config).dirs.map(d => d.slug);
  assert.deepStrictEqual(second, ["new-proj", "old-proj"]);
});

test("--project keeps only slugs containing the substring, case-insensitively", () => {
  const config = mkConfig();
  put(config, "D--OSS-Provost", "a.md", "x");
  put(config, "D--OSS-Other", "a.md", "x");
  assert.deepStrictEqual(json(config, "--project", "provost").dirs.map(d => d.slug), ["D--OSS-Provost"]);
  assert.deepStrictEqual(json(config, "--project", "nomatch").dirs, []);
});

test("CLAUDE_CONFIG_DIR is used when --config is absent", () => {
  const config = mkConfig();
  put(config, "env-proj", "a.md", "x");
  const r = spawnSync(process.execPath, [TOOL, "--json", "--today", TODAY], { encoding: "utf8", env: { ...process.env, CLAUDE_CONFIG_DIR: config } });
  assert.strictEqual(r.status, 0, r.stderr);
  assert.deepStrictEqual(JSON.parse(r.stdout).dirs.map(d => d.slug), ["env-proj"]);
});

test("C1 flags a follow-up whose date has passed, with the date and days overdue", () => {
  const config = oneProject({ "a.md": mem("2026-11-01", "Intro line.\nRe-measure ~2026-11-10 with the audit tool.") });
  const f = findings(json(config), "C1");
  assert.strictEqual(f.length, 1);
  assert.strictEqual(f[0].file, "a.md");
  assert.strictEqual(f[0].line, 9);
  assert.match(f[0].reason, /2026-11-10/);
  assert.match(f[0].reason, /6 days/);
  assert.match(f[0].excerpt, /Re-measure/);
});

test("C1 flags a date equal to today and uses the latest of several dates", () => {
  const config = oneProject({
    "a.md": mem("2026-11-01", "Review 2026-11-16 please."),
    "b.md": mem("2026-11-01", "Review 2026-11-01 and again 2026-12-01."),
  });
  const f = findings(json(config), "C1");
  assert.deepStrictEqual(f.map(x => x.file), ["a.md"]);
  assert.match(f[0].reason, /0 days/);
});

test("C1 negatives: future date, DONE, completed, superseded, no keyword, non-dates", () => {
  const body = [
    "Re-check ~2026-12-01 again.",
    "Re-measure 2026-11-01 DONE.",
    "Re-measure 2026-11-01 done.",
    "Review 2026-11-01 completed.",
    "Review 2026-11-01 superseded by the new plan.",
    "Merged on 2026-11-01 and it works.",
    "Re-measure ~52-58 of the lines.",
    "Review ~26-34 of them.",
    "Review ~13-01 someday.",
    "Review 2026-13-01 someday.",
    "Review 2026-02-30 someday.",
  ].join("\n");
  const config = oneProject({ "a.md": mem("2026-11-01", body) });
  assert.deepStrictEqual(findings(json(config), "C1"), []);
});

test("C1 splits sentences, so DONE in another sentence does not hide the follow-up", () => {
  const body = "The first run is DONE. Re-measure 2026-11-01 next; the rest is fine.\nRe-run 2026-11-02 done; Review 2026-11-03 left.";
  const config = oneProject({ "a.md": mem("2026-11-01", body) });
  const f = findings(json(config), "C1");
  assert.deepStrictEqual(f.map(x => x.line), [8, 9]);
  assert.match(f[1].reason, /2026-11-03/);
});

test("C1 reads ~MM-DD with the year of metadata.modified, else of the file mtime", () => {
  const config = oneProject({
    "a.md": mem("2025-12-01", "Re-check ~11-20 here."),
    "b.md": mem("2026-10-01", "Re-check ~11-20 here. Re-check ~11-10 here."),
    "c.md": "Re-run ~03-01 here.\n",
  }, "2024-05-05");
  const f = findings(json(config), "C1");
  const by = file => f.filter(x => x.file === file).map(x => x.reason.match(/\d{4}-\d{2}-\d{2}/)[0]);
  assert.deepStrictEqual(by("a.md"), ["2025-11-20"]);
  assert.deepStrictEqual(by("b.md"), ["2026-11-10"]);
  assert.deepStrictEqual(by("c.md"), ["2024-03-01"]);
});

test("C1 ignores dated history lines (real false positives from a live memory dir)", () => {
  const body = [
    "2026-10-26 recheck (window 10-14..10-23, 300K): compaction settings main -45.6%",
    "**PREVIOUS (2026-11-13 07:05 UTC):** installed from main cde927e, review follow-ups",
    "2026-10-17 measurement (job 4c652719): undoing 26 auto compactions, kept until the window ends",
    "- `a01b480` was reviewed as part of the full-line PR1 review (2026-10-12, PASS)",
    "Review (2026-10-01 07:05 UTC): notes only",
    "On 2026-11-01 a review happened.",
  ].join("\n");
  const config = oneProject({ "a.md": mem("2026-10-01", body) });
  assert.deepStrictEqual(findings(json(config), "C1"), []);
});

test("C1 keeps real follow-ups: keyword before the date, within 60 characters", () => {
  const body = [
    "- review ~2026-10-25 for noise and cost",
    "re-run Windows task 2026-11-08 → experiments/routing-audit/out-20261108",
    "full re-check due 2026-10-24",
    "re-measure auto-runs 2026-11-16",
    "(9) selfwork re-measure is separately scheduled 2026-11-16",
  ].join("\n");
  const config = oneProject({ "a.md": mem("2026-10-01", body) });
  const f = findings(json(config), "C1");
  assert.deepStrictEqual(f.map(x => x.line), [8, 9, 10, 11, 12]);
  assert.deepStrictEqual(f.map(x => x.reason.match(/\d{4}-\d{2}-\d{2}/)[0]),
    ["2026-10-25", "2026-11-08", "2026-10-24", "2026-11-16", "2026-11-16"]);
});

test("C1 excludes only the history stamp, and limits the keyword-to-date gap to 60 characters", () => {
  const body = [
    "Review (2026-10-01): notes, due 2026-11-01.",
    "Review the notes" + " x".repeat(30) + " 2026-11-01",
    "Review the notes" + " x".repeat(20) + " 2026-11-01",
    "**2026-10-05** review, then follow-up ~2026-11-02",
  ].join("\n");
  const config = oneProject({ "a.md": mem("2026-10-01", body) });
  const f = findings(json(config), "C1");
  assert.deepStrictEqual(f.map(x => `${x.line}:${x.reason.match(/\d{4}-\d{2}-\d{2}/)[0]}`), ["8:2026-11-01", "10:2026-11-01", "11:2026-11-02"]);
});

test("C1 treats PASS as a done word (case-sensitive)", () => {
  const config = oneProject({ "a.md": mem("2026-10-01", "Review 2026-11-01 PASS.\nReview 2026-11-02 pass.") });
  assert.deepStrictEqual(findings(json(config), "C1").map(x => x.line), [9]);
});

test("C2 flags a file over --max-file-kb and not a small one", () => {
  const config = oneProject({ "big.md": mem("2026-11-15", "x".repeat(13 * 1024)), "small.md": mem("2026-11-15", "short") });
  const f = findings(json(config), "C2");
  assert.deepStrictEqual(f.map(x => x.file), ["big.md"]);
  assert.match(f[0].reason, /13\.\d KB/);
  assert.deepStrictEqual(findings(json(config, "--max-file-kb", "20"), "C2"), []);
});

test("C2 flags more than --max-dated dated paragraphs; exactly the limit is fine", () => {
  const para = i => i % 2 ? `**2026-10-${10 + i}**\nlog ${i}` : `2026-10-${10 + i} (07:42 UTC): log ${i}`;
  const nine = Array.from({ length: 9 }, (_, i) => para(i)).join("\n\n");
  const eight = Array.from({ length: 8 }, (_, i) => para(i)).join("\n\n");
  const config = oneProject({ "nine.md": mem("2026-11-15", nine), "eight.md": mem("2026-11-15", eight) });
  const f = findings(json(config), "C2");
  assert.deepStrictEqual(f.map(x => x.file), ["nine.md"]);
  assert.match(f[0].reason, /9 dated paragraphs/);
  assert.deepStrictEqual(findings(json(config, "--max-dated", "9"), "C2"), []);
});

test("C3 flags a missing index, a broken link, an unindexed file and an oversize index", () => {
  const config = mkConfig();
  put(config, "noindex", "a.md", mem("2026-11-15", "x"));
  put(config, "p", "MEMORY.md", "- [A](a.md) — ok\n- [Gone](gone.md) — broken\n- [Web](https://example.com/x.md) — url\n", "2026-11-15");
  put(config, "p", "a.md", mem("2026-11-15", "x"));
  put(config, "p", "orphan.md", mem("2026-11-15", "x"));
  put(config, "bigindex", "MEMORY.md", "- [A](a.md) — " + "y".repeat(7 * 1024) + "\n", "2026-11-15");
  put(config, "bigindex", "a.md", mem("2026-11-15", "x"));
  const out = json(config);
  const none = findings(out, "C3", "noindex");
  assert.strictEqual(none.length, 1);
  assert.match(none[0].reason, /missing/);
  const p = findings(out, "C3", "p");
  assert.strictEqual(p.length, 2, JSON.stringify(p));
  assert.ok(p.some(f => f.file === "MEMORY.md" && f.line === 2 && /gone\.md/.test(f.reason)));
  assert.ok(p.some(f => f.file === "orphan.md" && /not linked/.test(f.reason)));
  const big = findings(out, "C3", "bigindex");
  assert.strictEqual(big.length, 1);
  assert.match(big[0].reason, /index is 7\.\d KB/);
});

test("C3 negative: a healthy index has no findings", () => {
  const config = oneProject({ "a.md": mem("2026-11-15", "x"), "b.md": mem("2026-11-15", "y") }, "2026-11-15");
  assert.deepStrictEqual(findings(json(config), "C3"), []);
});

test("C4 flags status words in a file older than --stale-days", () => {
  const body = ["State: CURRENT build.", "PR is NOT merged yet.", "Fix NOT installed.", "Review PENDING.", "uncommitted edits", "Still pending review.", "Waiting for CI.", "currently fine", "not merged", "Spending", "Why: the user forgets pending measurements.", "first answer/close pending approvals in Grok"].join("\n");
  const config = oneProject({ "old.md": mem("2026-11-01", body) });
  const f = findings(json(config), "C4");
  assert.deepStrictEqual(f.map(x => x.line), [8, 9, 10, 11, 12]);
  assert.match(f[0].reason, /15 days/);
  assert.match(f[0].excerpt, /CURRENT/);
});

test("C4 negatives: recent file, or exactly --stale-days old", () => {
  const body = "State: CURRENT build.\nReview PENDING.";
  const config = oneProject({ "new.md": mem("2026-11-14", body), "edge.md": mem("2026-11-09", body) });
  assert.deepStrictEqual(findings(json(config), "C4"), []);
  assert.strictEqual(findings(json(config, "--stale-days", "1"), "C4").length, 4);
});

test("C4 falls back to the file mtime without metadata.modified, and uses the linked file's date for index lines", () => {
  const config = mkConfig();
  put(config, "p", "MEMORY.md", "- [Old](old.md) — CURRENT state\n- [New](new.md) — CURRENT state\n- [Loose] — PENDING, no link\n", "2026-11-15");
  put(config, "p", "old.md", "Plain file, PENDING.\n", "2026-10-01");
  put(config, "p", "new.md", mem("2026-11-15", "ok"), "2026-10-01");
  const f = findings(json(config), "C4");
  assert.deepStrictEqual(f.map(x => `${x.file}:${x.line}`), ["MEMORY.md:1", "old.md:1"]);
});

test("--json has the documented shape", () => {
  const config = oneProject({ "a.md": mem("2026-11-01", "Review 2026-11-02.") });
  put(config, "clean", "MEMORY.md", "- [A](a.md) — h\n", "2026-11-15");
  put(config, "clean", "a.md", mem("2026-11-15", "x"));
  const out = json(config);
  assert.deepStrictEqual(Object.keys(out), ["config", "today", "dirs"]);
  assert.strictEqual(out.config, config);
  assert.strictEqual(out.today, TODAY);
  assert.deepStrictEqual(out.dirs.map(d => d.slug), ["clean", "p"]);
  assert.deepStrictEqual(Object.keys(out.dirs[1]), ["slug", "path", "findings"]);
  assert.strictEqual(out.dirs[1].path, path.join(config, "projects", "p", "memory"));
  assert.deepStrictEqual(Object.keys(out.dirs[1].findings[0]), ["check", "file", "line", "reason", "excerpt"]);
  assert.deepStrictEqual(out.dirs[0].findings, []);
});

test("Markdown output has a header, findings grouped by check, and a no-findings line", () => {
  const config = oneProject({ "a.md": mem("2026-11-01", "Review 2026-11-02.") });
  put(config, "clean", "MEMORY.md", "- [A](a.md) — h\n", "2026-11-15");
  put(config, "clean", "a.md", mem("2026-11-15", "x"));
  const r = run(config);
  assert.strictEqual(r.status, 0, r.stderr);
  assert.ok(r.stdout.includes(`Config: ${config}`));
  assert.match(r.stdout, /Today: 2026-11-16/);
  assert.match(r.stdout, /Memory dirs scanned: 2/);
  assert.match(r.stdout, /## p\n/);
  assert.match(r.stdout, /### C1 /);
  assert.match(r.stdout, /^- a\.md:8 — .*14 days.* — "Review 2026-11-02\."$/m);
  assert.match(r.stdout, /No findings: clean/);
});

test("excerpts are at most 120 characters", () => {
  const config = oneProject({ "a.md": mem("2026-11-01", "Review 2026-11-02 " + "word ".repeat(60)) });
  const f = findings(json(config), "C1");
  assert.ok(f[0].excerpt.length <= 120 && f[0].excerpt.length > 100);
});

test("exit code 2 on a bad flag, a bad value or an unreadable config dir", () => {
  const config = mkConfig();
  assert.strictEqual(run(config, "--bogus").status, 2);
  assert.strictEqual(run(config, "--max-file-kb", "abc").status, 2);
  assert.strictEqual(run(config, "--max-file-kb").status, 2);
  assert.strictEqual(spawnSync(process.execPath, [TOOL, "--config", config, "--today", "2026-02-30"]).status, 2);
  assert.strictEqual(run(path.join(config, "does-not-exist")).status, 2);
  assert.strictEqual(run(config).status, 0);
});

test("read-only: every file's content and mtime is unchanged after a run", () => {
  const config = oneProject({
    "a.md": mem("2026-11-01", "Review 2026-11-02. CURRENT.\n" + "x".repeat(13 * 1024)),
    "b.md": "no frontmatter pending\n",
  }, "2026-10-01");
  put(config, "other", "z.md", "x");
  fs.mkdirSync(path.join(config, "projects", "empty", "memory"), { recursive: true });
  const snap = () => {
    const h = crypto.createHash("sha256");
    const walk = d => fs.readdirSync(d, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name)).forEach(e => {
      const p = path.join(d, e.name), st = fs.statSync(p);
      h.update(`${path.relative(config, p)}|${st.mtimeMs}|${e.isDirectory() ? "dir" : st.size}\n`);
      if (e.isDirectory()) walk(p); else h.update(fs.readFileSync(p));
    });
    walk(config);
    return h.digest("hex");
  };
  const before = snap();
  assert.strictEqual(run(config).status, 0);
  assert.strictEqual(run(config, "--json").status, 0);
  assert.strictEqual(snap(), before);
});
