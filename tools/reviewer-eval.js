#!/usr/bin/env node
// Runs code-reviewer headless on saved eval cases (#56) and scores its Important findings.
// Usage: node reviewer-eval.js run [--cases-dir DIR] [--ids a,b] [--label NAME] [--max-cost USD]
//             [--claude PATH] [--tsv] [--inject DEST=SRC]... [--hypothesis TEXT]
//        node reviewer-eval.js rescore --run NAME[,NAME] [--judge-model MODEL] [--claude PATH] [--max-cost USD]
//             [--cases-dir DIR] [--hypothesis TEXT]
// Cases: <cases-dir>/<id>/case.json (default ~/.claude/provost/evals/reviewer/cases), written by
// reviewer-cases.js save. Per case, sorted by id: `git archive <head>` is unpacked into a fresh temp dir
// (the source repo is never checked out or changed), `git diff <base> <head>` goes to .changes/change.patch, and
// `claude -p --agent code-reviewer` reviews it read-only (Read, Glob, Grep) with model and effort from
// ~/.claude/agents/code-reviewer.md. The temp dir is named wt-* and the prompt does not mention evaluation.
// --inject DEST=SRC (repeatable) copies the local file SRC to <temp dir>/DEST after unpacking (overwriting an
// archived file), so the reviewer sees the same context as in production (e.g. REVIEW.md); DEST must be relative
// and free of `..`. Hit = an Important finding (I<n>) cites an expected file within ±10 lines
// of an expected line; fileHit = it cites the file at all; a clean case (expected []) with any Important is a
// false alarm. Rows go to stdout and ~/.claude/provost/evals/reviewer/runs/<UTC>-<label>.tsv; summary on stderr.
// The reviewer's report per case is saved in runs/<UTC>-<label>/<id>.md (CLI result text verbatim; <id>.err.txt
// for an error without result text) and linked in the report column, so a score can be audited.
// --max-cost stops after the case that takes the cumulative total_cost_usd above it. --claude PATH ending in
// .js runs under this node (test doubles).
// A usage-limit reply (error exit whose result text matches /hit your .*limit/i) is recorded as
// error:usage-limit and saved as <id>.err.txt; the run stops there and stderr lists the ids not run and a
// ready `--ids` rerun list (the limit case first).
// rescore re-judges saved reports (runs/<name>/<id>.md): the line-window score as in run, plus, for a defect
// case with at least one Important finding, a blind judge (CLI, no tools, empty cwd) that sees only the expected
// defect (with the fix diff when the summary says "follow-up <hash>") and the relabelled Important findings
// and answers yes / no / unsure. Output: runs/<first-run>.rescore.tsv and a summary on stderr.
// Both subcommands append one row to ~/.claude/provost/evals/reviewer/decisions.tsv (verdict left empty for a
// person to fill; --hypothesis TEXT is recorded).
const fs = require("fs"), path = require("path"), os = require("os");
const { execFileSync, spawnSync } = require("child_process");

const args = process.argv.slice(2);
const cmd = args[0];
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 && i + 1 < args.length ? args[i + 1] : d; };
const optAll = k => args.flatMap((a, i) => a === k && i + 1 < args.length ? [args[i + 1]] : []);
const die = m => { console.error(m); process.exit(1); };

const EVALS = path.join(os.homedir(), ".claude", "provost", "evals", "reviewer");
const TOOLS = "Read,Glob,Grep";
const WINDOW = 10;
const CLI_TIMEOUT_MS = 30 * 60 * 1000;
const USAGE_LIMIT = /hit your .*limit/i;

function git(repo, a, encoding = "utf8") {
  return execFileSync("git", ["-c", "core.quotepath=off", "-C", repo, ...a],
    { encoding, maxBuffer: 1 << 30, stdio: ["ignore", "pipe", "pipe"] });
}

// Unpacks a `git archive --format=tar` buffer into dir: regular files and directories only (pax path
// records honoured, symlinks and global headers skipped, entries escaping dir refused).
function untar(buf, dir) {
  const str = (o, n) => buf.toString("utf8", o, o + n).replace(/\0.*$/s, "");
  let off = 0, paxPath = null;
  while (off + 512 <= buf.length) {
    if (buf.readUInt32LE(off) === 0 && str(off, 100) === "") break;
    const size = parseInt(str(off + 124, 12).trim() || "0", 8);
    const type = str(off + 156, 1) || "0";
    const prefix = str(off + 345, 155);
    let name = paxPath ?? (prefix ? prefix + "/" : "") + str(off, 100);
    const body = buf.subarray(off + 512, off + 512 + size);
    off += 512 + Math.ceil(size / 512) * 512;
    if (type === "x") {
      const m = /(?:^|\n)\d+ path=([^\n]*)\n/.exec(body.toString("utf8"));
      paxPath = m ? m[1] : null;
      continue;
    }
    paxPath = null;
    if (type !== "0" && type !== "5") continue;
    const target = path.resolve(dir, name);
    if (target !== path.resolve(dir) && !target.startsWith(path.resolve(dir) + path.sep)) throw new Error(`unsafe path in archive: ${name}`);
    if (type === "5") fs.mkdirSync(target, { recursive: true });
    else { fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, body); }
  }
}

// model/effort from the installed role's frontmatter, so `--agent` runs with the configured ones.
function roleConfig() {
  const file = path.join(os.homedir(), ".claude", "agents", "code-reviewer.md");
  const out = {};
  if (!fs.existsSync(file)) return out;
  const fm = /^---\r?\n([\s\S]*?)\r?\n---/.exec(fs.readFileSync(file, "utf8"));
  for (const k of ["model", "effort"]) {
    const m = fm && new RegExp(`^${k}:\\s*(\\S+)\\s*$`, "m").exec(fm[1]);
    if (m) out[k] = m[1];
  }
  return out;
}

const prompt = c => [
  `Review the change ${c.base}..${c.head} in this directory, a checkout of ${c.head}. The diff under review is in .changes/change.patch; read it first, then the changed files and their callers as needed.`,
  `Claim to review: ${c.claim}`,
  "Tag every finding Important, Nit or Pre-existing. Number Important findings I1, I2, … and cite each as path:line or path:line-line, relative to this directory. Report only; do not edit.",
].join("\n\n");

function spawnCli(cli, cwd, a, input) {
  const [bin, pre] = /\.[cm]?js$/i.test(cli) ? [process.execPath, [cli]] : [cli, []];
  const r = spawnSync(bin, [...pre, ...a], { cwd, input, encoding: "utf8", maxBuffer: 1 << 28, timeout: CLI_TIMEOUT_MS, windowsHide: true });
  if (r.error) throw new Error(r.error.code === "ETIMEDOUT" ? "cli timeout" : `cli ${r.error.code || r.error.message}`);
  let out = null;
  try { out = JSON.parse(r.stdout); } catch { /* reported below */ }
  const cost = out && Number(out.total_cost_usd) || 0;
  const text = out && typeof out.result === "string" ? out.result : undefined;
  const raw = { stdout: String(r.stdout || ""), stderr: String(r.stderr || "") };
  // A usage-limit reply ("You've hit your 5-hour limit · resets …") arrives as an error exit with the notice as
  // result text; the CLI has no separate structured error for it.
  if ((r.status !== 0 || (out && out.is_error)) && text !== undefined && USAGE_LIMIT.test(text)) return { cost, text, raw, error: "usage-limit" };
  if (r.status !== 0) return { cost, text, raw, error: `cli exit ${r.status}` };
  if (text === undefined) return { cost, raw, error: "cli output not json" };
  if (out.is_error) return { cost, text, raw, error: "cli is_error" };
  return { cost, text, raw };
}

function runClaude(cli, dir, c, role) {
  const a = ["-p", "--agent", "code-reviewer", "--tools", TOOLS, "--allowedTools", TOOLS,
    "--permission-mode", "dontAsk", "--output-format", "json", "--no-session-persistence"];
  if (role.model) a.push("--model", role.model);
  if (role.effort) a.push("--effort", role.effort);
  return spawnCli(cli, dir, a, prompt(c));
}

const normPath = (p, dir) => {
  let s = String(p).replace(/\\/g, "/");
  const root = dir.replace(/\\/g, "/") + "/";
  if (s.toLowerCase().startsWith(root.toLowerCase())) s = s.slice(root.length);
  return s.replace(/^(\.\/)+/, "").toLowerCase();
};

// Important findings: a line whose first word (after list, heading, bold or bracket marks) is I<n> opens a
// block; it runs until the next such line, a heading, or a Nit / Pre-existing line. Each block yields the
// path:line[-line] references it cites (also path:Lx-Ly).
const START = /^[\s>*#_\-[(]*(?:\d+[.)]\s*)?[*_[(]*I(\d+)\b/;
const STOP = /^\s*(#|[>*_\-[(\s]*(Nit|Pre-existing)\b)/i;
const REF = /((?:[A-Za-z]:)?[\w.\-\\/]*[\w\-]\.[\w]+|[\w.\-\\/]*[\\/][\w.\-]+):L?(\d+)(?:\s*[-–]\s*L?(\d+))?/g;
function findings(text, dir) {
  const by = new Map();
  let cur = null;
  for (const line of String(text).split(/\r?\n/)) {
    const s = START.exec(line);
    if (s) { cur = "I" + s[1]; if (!by.has(cur)) by.set(cur, { refs: [], text: "" }); }
    else if (STOP.test(line)) cur = null;
    if (!cur) continue;
    const f = by.get(cur);
    f.text += line + "\n";
    for (const m of line.matchAll(REF)) {
      const a = Number(m[2]), b = m[3] ? Number(m[3]) : a;
      f.refs.push({ file: normPath(m[1], dir), a: Math.min(a, b), b: Math.max(a, b) });
    }
  }
  return [...by.values()];
}

const ranges = lines => String(lines ?? "").split(",").map(x => x.trim()).filter(Boolean).map(x => {
  const [a, b] = x.split("-").map(Number);
  return [a, Number.isFinite(b) ? b : a];
}).filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b));

function score(found, expected, dir) {
  const r = { important: found.length, hit: false, fileHit: false, falseAlarm: false };
  if (!expected.length) { r.falseAlarm = found.length > 0; return r; }
  for (const e of expected) {
    const file = normPath(e.file, dir), want = ranges(e.lines);
    for (const f of found) {
      const refs = f.refs.filter(x => x.file === file);
      if (refs.length || normPath(f.text, dir).includes(file)) r.fileHit = true;
      if (refs.some(x => want.some(([a, b]) => x.a <= b + WINDOW && x.b >= a - WINDOW))) r.hit = true;
    }
  }
  return r;
}

// Saves what the CLI said so a score can be audited: the result text verbatim as <id>.md, or for an error
// without result text <id>.err.txt (reason plus the first 4000 chars of stdout and stderr). Nothing when the
// CLI never produced output.
function saveReport(rep, id, out) {
  let name;
  if (typeof out.text === "string" && out.error !== "usage-limit") { name = `${id}.md`; fs.writeFileSync(path.join(rep.dir, name), out.text); }
  else if (out.error && (out.text || out.raw.stdout || out.raw.stderr)) {
    name = `${id}.err.txt`;
    const clip = t => t.slice(0, 4000);
    fs.writeFileSync(path.join(rep.dir, name), [out.error, ...(out.error === "usage-limit" ? ["", clip(out.text)] : []), "", "--- stdout ---", clip(out.raw.stdout), "", "--- stderr ---", clip(out.raw.stderr), ""].join("\n"));
  }
  return name ? `${rep.name}/${name}` : "";
}

function runCase(id, file, cli, role, rep, injects) {
  const row = { id, origin: "", kind: "", important: "", hit: "", fileHit: "", falseAlarm: "", cost: 0, status: "ok", report: "" };
  let c;
  try { c = JSON.parse(fs.readFileSync(file, "utf8")); } catch { row.status = "error:bad case.json"; return row; }
  row.origin = c.origin || "";
  const expected = Array.isArray(c.expected) ? c.expected : [];
  row.kind = expected.length ? "defect" : "clean";
  if (!c.repo || !c.base || !c.head) { row.status = "error:case.json lacks repo/base/head"; return row; }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-"));
  try {
    try {
      untar(git(c.repo, ["archive", "--format=tar", c.head], "buffer"), dir);
      for (const [dest, src] of injects) {
        const t = path.join(dir, dest);
        fs.mkdirSync(path.dirname(t), { recursive: true });
        fs.copyFileSync(src, t);
      }
      fs.mkdirSync(path.join(dir, ".changes"), { recursive: true });
      fs.writeFileSync(path.join(dir, ".changes", "change.patch"), git(c.repo, ["diff", "--no-color", c.base, c.head], "buffer"));
    } catch (e) { row.status = "error:git " + String(e.stderr || e.message).trim().split("\n")[0]; return row; }
    let out;
    try { out = runClaude(cli, dir, c, role); } catch (e) { row.status = "error:" + e.message; return row; }
    row.cost = out.cost;
    try { row.report = saveReport(rep, id, out); } catch (e) { console.error(`${id}: report not saved: ${e.message}`); }
    if (out.error) { row.status = "error:" + out.error; return row; }
    const s = score(findings(out.text, dir), expected, dir);
    const yn = v => v ? "yes" : "no";
    row.important = String(s.important);
    if (row.kind === "defect") { row.hit = yn(s.hit); row.fileHit = yn(s.fileHit); }
    else row.falseAlarm = yn(s.falseAlarm);
    return row;
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 }); }
    catch (e) { console.error(`temp dir not removed: ${dir}: ${e.code || e.message}`); }
  }
}

function run() {
  const casesDir = path.resolve(opt("--cases-dir", path.join(EVALS, "cases")));
  const label = opt("--label", "run");
  if (!/^[A-Za-z0-9._-]+$/.test(label)) die(`run: invalid --label: ${label}`);
  const maxArg = opt("--max-cost", "");
  const maxCost = maxArg === "" ? Infinity : Number(maxArg);
  if (maxArg !== "" && !(Number.isFinite(maxCost) && maxCost >= 0)) die("run: --max-cost must be a number >= 0");
  const cli = opt("--claude", "claude");
  if (!fs.existsSync(casesDir)) die(`run: no cases directory: ${casesDir}`);
  const injects = optAll("--inject").map(spec => {
    const eq = spec.indexOf("=");
    const dest = eq > 0 ? spec.slice(0, eq) : "", src = eq > 0 ? spec.slice(eq + 1) : "";
    if (!dest || !src) die(`run: --inject needs DEST=SRC: ${spec}`);
    if (path.isAbsolute(dest) || /^([A-Za-z]:|[\\/])/.test(dest)) die(`run: --inject DEST must be relative: ${dest}`);
    if (dest.split(/[\\/]+/).includes("..")) die(`run: --inject DEST must not contain "..": ${dest}`);
    if (!fs.existsSync(src) || !fs.statSync(src).isFile()) die(`run: --inject SRC does not exist: ${src}`);
    return [dest, path.resolve(src)];
  });
  let ids = fs.readdirSync(casesDir).filter(d => fs.existsSync(path.join(casesDir, d, "case.json"))).sort();
  const want = opt("--ids", "");
  if (want) {
    const w = want.split(",").map(s => s.trim()).filter(Boolean);
    const missing = w.filter(x => !ids.includes(x));
    if (missing.length) die(`run: unknown case id(s): ${missing.join(", ")}`);
    ids = ids.filter(x => w.includes(x));
  }
  const role = roleConfig(), rows = [];
  const runsDir = path.join(EVALS, "runs");
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
  const rep = { name: `${stamp}-${label}`, dir: path.join(runsDir, `${stamp}-${label}`) };
  fs.mkdirSync(rep.dir, { recursive: true });
  let total = 0, stoppedAfter = "", limitAt = "";
  for (const id of ids) {
    const row = runCase(id, path.join(casesDir, id, "case.json"), cli, role, rep, injects);
    rows.push(row);
    total += row.cost;
    console.error(`${id}: ${row.status}`);
    if (row.status === "error:usage-limit") { limitAt = id; break; }
    if (total > maxCost) { stoppedAfter = id; break; }
  }
  const notRun = limitAt ? ids.slice(ids.indexOf(limitAt) + 1) : [];

  const ok = rows.filter(r => r.status === "ok");
  const defects = ok.filter(r => r.kind === "defect"), cleans = ok.filter(r => r.kind === "clean");
  const frac = (n, d) => `${n}/${d}` + (d ? ` (${(100 * n / d).toFixed(0)}%)` : "");
  const meanImp = ok.length ? ok.reduce((s, r) => s + Number(r.important), 0) / ok.length : 0;
  const summary = [
    `cases: ${rows.length} (ok ${ok.length}, error ${rows.length - ok.length})`,
    `recall: ${frac(defects.filter(r => r.hit === "yes").length, defects.length)}`,
    `file-only recall: ${frac(defects.filter(r => r.fileHit === "yes").length, defects.length)}`,
    `false-alarm rate: ${frac(cleans.filter(r => r.falseAlarm === "yes").length, cleans.length)}`,
    `mean Important: ${meanImp.toFixed(2)}`,
    `total cost: $${total.toFixed(4)}`,
    `stopped by ceiling: ${stoppedAfter ? `yes (after ${stoppedAfter}, ceiling $${maxCost})` : "no"}`,
    `stopped by usage limit: ${limitAt ? `yes (at ${limitAt})` : "no"}`,
    `role: code-reviewer model=${role.model || "default"} effort=${role.effort || "default"}`,
    `injected: ${injects.length ? injects.map(i => i[0]).join(", ") : "none"}`,
  ];

  const cols = ["id", "origin", "kind", "important", "hit", "fileHit", "falseAlarm", "costUsd", "status", "report"];
  const cell = (r, c) => c === "costUsd" ? r.cost.toFixed(4) : String(r[c] ?? "");
  const tsv = [cols.join("\t"), ...rows.map(r => cols.map(c => cell(r, c).replace(/[\t\r\n]+/g, " ")).join("\t"))];
  const out = path.join(runsDir, `${rep.name}.tsv`);
  fs.writeFileSync(out, [...tsv, "# " + summary.join("; ")].join("\n") + "\n");

  if (args.includes("--tsv")) console.log(tsv.join("\n"));
  else {
    console.log("| " + cols.join(" | ") + " |"); console.log("|" + cols.map(() => "---").join("|") + "|");
    rows.forEach(r => console.log("| " + cols.map(c => cell(r, c).replace(/\|/g, "\\|").replace(/[\r\n]+/g, " ")).join(" | ") + " |"));
  }
  console.error("\n" + summary.join("\n") + `\nsaved: ${out}`);
  const nd = (n, d) => `${n}/${d}`;
  logDecision({ command: "run", label, cases: rows.length, ok: ok.length,
    recall: nd(defects.filter(r => r.hit === "yes").length, defects.length),
    falseAlarm: nd(cleans.filter(r => r.falseAlarm === "yes").length, cleans.length), judgeYes: "",
    cost: total, injected: injects.length ? injects.map(i => i[0]).join(",") : "none" });
  if (limitAt) console.error(`\nusage limit hit at ${limitAt}; not run: ${notRun.join(", ") || "(none)"}\nrerun after the limit resets: --ids ${[limitAt, ...notRun].join(",")}`);
}

// One row per finished run / rescore, so experiments can be compared later. verdict is for a person to fill in.
function logDecision(d) {
  const file = path.join(EVALS, "decisions.tsv");
  const cols = ["utc", "command", "label", "hypothesis", "cases", "ok", "recall", "falseAlarm", "judgeYes", "costUsd", "injected", "verdict"];
  const row = { utc: new Date().toISOString().replace(/\.\d+Z$/, "Z"), hypothesis: opt("--hypothesis", ""), verdict: "", ...d };
  row.costUsd = Number(d.cost || 0).toFixed(4);
  fs.mkdirSync(EVALS, { recursive: true });
  const lines = [];
  if (!fs.existsSync(file) || fs.statSync(file).size === 0) lines.push(cols.join("\t"));
  lines.push(cols.map(c => String(row[c] ?? "").replace(/[\t\r\n]+/g, " ")).join("\t"));
  fs.appendFileSync(file, lines.join("\n") + "\n");
}

const JUDGE_DIFF_MAX = 12000;
const FOLLOW_UP = /follow-up ([0-9a-f]{7,40})/;

function judgePrompt(c, expected, important) {
  const defects = expected.map((e, i) => {
    const parts = [`D${i + 1}: file ${e.file}, lines ${e.lines ?? "?"}: ${e.summary ?? ""}`];
    const h = FOLLOW_UP.exec(String(e.summary ?? ""));
    if (h && c.repo) {
      try {
        const d = git(c.repo, ["show", "--no-color", h[1], "--", e.file]);
        parts.push(`Fix diff for ${e.file} (commit ${h[1]}):`, d.length > JUDGE_DIFF_MAX ? d.slice(0, JUDGE_DIFF_MAX) + "\n[truncated]" : d);
      } catch { /* no diff available; the summary alone is shown */ }
    }
    return parts.join("\n");
  });
  const found = important.map((f, i) => `F${i + 1}:\n` + f.text.replace(/\bI\d+\b/, `F${i + 1}`).trim());
  return [
    "A code change contains the known defect(s) below. Further down are findings that someone wrote while examining the change.",
    "Known defect(s):\n\n" + defects.join("\n\n"),
    "Findings:\n\n" + found.join("\n\n"),
    "Question: does any finding describe the same defect as a known defect above (the same underlying problem, even if worded differently or citing slightly different lines)? Answer unsure if you cannot tell.",
    'Reply with a single line of JSON and nothing else: {"verdict":"yes|no|unsure","finding":"F<n>|none"}',
  ].join("\n\n");
}

function parseVerdict(text) {
  const objs = String(text).match(/\{[^{}]*\}/g) || [];
  for (const o of objs.reverse()) {
    try {
      const j = JSON.parse(o);
      const v = typeof j.verdict === "string" ? j.verdict.toLowerCase() : "";
      if (["yes", "no", "unsure"].includes(v)) return { verdict: v, finding: typeof j.finding === "string" ? j.finding : "none" };
    } catch { /* try the previous object */ }
  }
  return null;
}

function runJudge(cli, model, p) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-"));
  try {
    return spawnCli(cli, dir, ["-p", "--model", model, "--tools", "", "--output-format", "json", "--no-session-persistence", "--permission-mode", "dontAsk"], p);
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 }); }
    catch (e) { console.error(`temp dir not removed: ${dir}: ${e.code || e.message}`); }
  }
}

function rescore() {
  const casesDir = path.resolve(opt("--cases-dir", path.join(EVALS, "cases")));
  const runNames = opt("--run", "").split(",").map(s => s.trim()).filter(Boolean);
  if (!runNames.length) die("rescore: --run NAME[,NAME] is required");
  const maxArg = opt("--max-cost", "");
  const maxCost = maxArg === "" ? Infinity : Number(maxArg);
  if (maxArg !== "" && !(Number.isFinite(maxCost) && maxCost >= 0)) die("rescore: --max-cost must be a number >= 0");
  const cli = opt("--claude", "claude"), model = opt("--judge-model", "sonnet");
  const runsDir = path.join(EVALS, "runs");
  const work = [];
  for (const name of runNames) {
    if (!/^[A-Za-z0-9._-]+$/.test(name)) die(`rescore: invalid run name: ${name}`);
    const dir = path.join(runsDir, name);
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) die(`rescore: no such run: ${dir}`);
    for (const f of fs.readdirSync(dir).filter(f => f.endsWith(".md")).sort()) {
      const id = f.slice(0, -3);
      if (fs.existsSync(path.join(casesDir, id, "case.json"))) work.push({ run: name, id, file: path.join(dir, f) });
    }
  }
  const rows = [];
  let total = 0, stoppedAfter = "", limitAt = "";
  for (const w of work) {
    const row = { run: w.run, id: w.id, kind: "", important: "", lineHit: "", fileHit: "", judge: "", finding: "", cost: 0, status: "ok" };
    rows.push(row);
    let c;
    try { c = JSON.parse(fs.readFileSync(path.join(casesDir, w.id, "case.json"), "utf8")); } catch { row.status = "error:bad case.json"; continue; }
    const expected = Array.isArray(c.expected) ? c.expected : [];
    row.kind = expected.length ? "defect" : "clean";
    const found = findings(fs.readFileSync(w.file, "utf8"), "");
    const s = score(found, expected, "");
    row.important = String(s.important);
    if (row.kind === "clean") continue;
    row.lineHit = s.hit ? "yes" : "no";
    row.fileHit = s.fileHit ? "yes" : "no";
    if (!found.length) { row.judge = "no"; row.finding = "none"; continue; }
    let out;
    try { out = runJudge(cli, model, judgePrompt(c, expected, found)); } catch (e) { row.judge = "error"; row.status = "error:" + e.message; continue; }
    row.cost = out.cost;
    total += out.cost;
    if (out.error) { row.judge = "error"; row.status = "error:" + out.error; }
    else {
      const v = parseVerdict(out.text);
      if (v) { row.judge = v.verdict; row.finding = v.finding; } else row.judge = "error";
    }
    console.error(`${w.run}/${w.id}: judge ${row.judge}`);
    if (row.status === "error:usage-limit") { limitAt = `${w.run}/${w.id}`; break; }
    if (total > maxCost) { stoppedAfter = `${w.run}/${w.id}`; break; }
  }
  const notRun = (limitAt || stoppedAfter) ? work.slice(rows.length).map(w => `${w.run}/${w.id}`) : [];

  const ok = rows.filter(r => r.status === "ok");
  const defects = rows.filter(r => r.kind === "defect"), cleans = rows.filter(r => r.kind === "clean");
  const table = lh => {
    const sel = defects.filter(r => r.lineHit === lh);
    return `lineHit=${lh} (${sel.length}): ` + ["yes", "no", "unsure", "error"].map(j => `judge ${j} ${sel.filter(r => r.judge === j).length}`).join(", ");
  };
  const summary = [
    `reports: ${rows.length} (ok ${ok.length}, error ${rows.length - ok.length})`,
    table("yes"), table("no"),
    `clean cases with Important: ${cleans.filter(r => Number(r.important) > 0).length}/${cleans.length}`,
    `total cost: $${total.toFixed(4)}`,
    `judge model: ${model}`,
    `stopped by ceiling: ${stoppedAfter ? `yes (after ${stoppedAfter}, ceiling $${maxCost})` : "no"}`,
    `stopped by usage limit: ${limitAt ? `yes (at ${limitAt})` : "no"}`,
  ];
  const cols = ["run", "id", "kind", "important", "lineHit", "fileHit", "judge", "finding", "costUsd", "status"];
  const cell = (r, c) => c === "costUsd" ? r.cost.toFixed(4) : String(r[c] ?? "");
  const out = path.join(runsDir, `${runNames[0]}.rescore.tsv`);
  fs.writeFileSync(out, [cols.join("\t"), ...rows.map(r => cols.map(c => cell(r, c).replace(/[\t\r\n]+/g, " ")).join("\t")), "# " + summary.join("; ")].join("\n") + "\n");
  console.error("\n" + summary.join("\n") + `\nsaved: ${out}`);
  const nd = (n, d) => `${n}/${d}`;
  logDecision({ command: "rescore", label: runNames.join(","), cases: rows.length, ok: ok.length,
    recall: nd(defects.filter(r => r.lineHit === "yes").length, defects.length),
    falseAlarm: nd(cleans.filter(r => Number(r.important) > 0).length, cleans.length),
    judgeYes: nd(defects.filter(r => r.judge === "yes").length, defects.length), cost: total, injected: "" });
  if (limitAt || stoppedAfter) console.error(`\nnot judged: ${notRun.join(", ") || "(none)"}`);
}

if (cmd === "run") run();
else if (cmd === "rescore") rescore();
else die("usage: reviewer-eval.js run [--cases-dir DIR] [--ids a,b] [--label NAME] [--max-cost USD] [--claude PATH] [--tsv] [--inject DEST=SRC]... [--hypothesis TEXT]\n       reviewer-eval.js rescore --run NAME[,NAME] [--judge-model MODEL] [--claude PATH] [--max-cost USD] [--cases-dir DIR] [--hypothesis TEXT]");
