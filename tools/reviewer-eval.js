#!/usr/bin/env node
// Runs code-reviewer headless on saved eval cases (#56) and scores its Important findings.
// Usage: node reviewer-eval.js run [--cases-dir DIR] [--ids a,b] [--label NAME] [--max-cost USD]
//             [--claude PATH] [--tsv]
// Cases: <cases-dir>/<id>/case.json (default ~/.claude/provost/evals/reviewer/cases), written by
// reviewer-cases.js save. Per case, sorted by id: `git archive <head>` is unpacked into a fresh temp dir
// (the source repo is never checked out or changed), `git diff <base> <head>` goes to .review/diff.patch, and
// `claude -p --agent code-reviewer` reviews it read-only (Read, Glob, Grep) with model and effort from
// ~/.claude/agents/code-reviewer.md. Hit = an Important finding (I<n>) cites an expected file within ±10 lines
// of an expected line; fileHit = it cites the file at all; a clean case (expected []) with any Important is a
// false alarm. Rows go to stdout and ~/.claude/provost/evals/reviewer/runs/<UTC>-<label>.tsv; summary on stderr.
// --max-cost stops after the case that takes the cumulative total_cost_usd above it. --claude PATH ending in
// .js runs under this node (test doubles).
const fs = require("fs"), path = require("path"), os = require("os");
const { execFileSync, spawnSync } = require("child_process");

const args = process.argv.slice(2);
const cmd = args[0];
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 && i + 1 < args.length ? args[i + 1] : d; };
const die = m => { console.error(m); process.exit(1); };

const EVALS = path.join(os.homedir(), ".claude", "provost", "evals", "reviewer");
const TOOLS = "Read,Glob,Grep";
const WINDOW = 10;
const CLI_TIMEOUT_MS = 30 * 60 * 1000;

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
  `Review the change ${c.base}..${c.head} in this directory, a checkout of ${c.head}. The diff under review is in .review/diff.patch; read it first, then the changed files and their callers as needed.`,
  `Claim to review: ${c.claim}`,
  "Tag every finding Important, Nit or Pre-existing. Number Important findings I1, I2, … and cite each as path:line or path:line-line, relative to this directory. Report only; do not edit.",
].join("\n\n");

function runClaude(cli, dir, c, role) {
  const a = ["-p", "--agent", "code-reviewer", "--tools", TOOLS, "--allowedTools", TOOLS,
    "--permission-mode", "dontAsk", "--output-format", "json", "--no-session-persistence"];
  if (role.model) a.push("--model", role.model);
  if (role.effort) a.push("--effort", role.effort);
  const [bin, pre] = /\.[cm]?js$/i.test(cli) ? [process.execPath, [cli]] : [cli, []];
  const r = spawnSync(bin, [...pre, ...a], { cwd: dir, input: prompt(c), encoding: "utf8", maxBuffer: 1 << 28, timeout: CLI_TIMEOUT_MS, windowsHide: true });
  if (r.error) throw new Error(r.error.code === "ETIMEDOUT" ? "cli timeout" : `cli ${r.error.code || r.error.message}`);
  let out = null;
  try { out = JSON.parse(r.stdout); } catch { /* reported below */ }
  const cost = out && Number(out.total_cost_usd) || 0;
  if (r.status !== 0) return { cost, error: `cli exit ${r.status}` };
  if (!out || typeof out.result !== "string") return { cost, error: "cli output not json" };
  if (out.is_error) return { cost, error: "cli is_error" };
  return { cost, text: out.result };
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

function runCase(id, file, cli, role) {
  const row = { id, origin: "", kind: "", important: "", hit: "", fileHit: "", falseAlarm: "", cost: 0, status: "ok" };
  let c;
  try { c = JSON.parse(fs.readFileSync(file, "utf8")); } catch { row.status = "error:bad case.json"; return row; }
  row.origin = c.origin || "";
  const expected = Array.isArray(c.expected) ? c.expected : [];
  row.kind = expected.length ? "defect" : "clean";
  if (!c.repo || !c.base || !c.head) { row.status = "error:case.json lacks repo/base/head"; return row; }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "provost-reval-"));
  try {
    try {
      untar(git(c.repo, ["archive", "--format=tar", c.head], "buffer"), dir);
      fs.mkdirSync(path.join(dir, ".review"), { recursive: true });
      fs.writeFileSync(path.join(dir, ".review", "diff.patch"), git(c.repo, ["diff", "--no-color", c.base, c.head], "buffer"));
    } catch (e) { row.status = "error:git " + String(e.stderr || e.message).trim().split("\n")[0]; return row; }
    let out;
    try { out = runClaude(cli, dir, c, role); } catch (e) { row.status = "error:" + e.message; return row; }
    row.cost = out.cost;
    if (out.error) { row.status = "error:" + out.error; return row; }
    const s = score(findings(out.text, dir), expected, dir);
    const yn = v => v ? "yes" : "no";
    row.important = String(s.important);
    if (row.kind === "defect") { row.hit = yn(s.hit); row.fileHit = yn(s.fileHit); }
    else row.falseAlarm = yn(s.falseAlarm);
    return row;
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
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
  let ids = fs.readdirSync(casesDir).filter(d => fs.existsSync(path.join(casesDir, d, "case.json"))).sort();
  const want = opt("--ids", "");
  if (want) {
    const w = want.split(",").map(s => s.trim()).filter(Boolean);
    const missing = w.filter(x => !ids.includes(x));
    if (missing.length) die(`run: unknown case id(s): ${missing.join(", ")}`);
    ids = ids.filter(x => w.includes(x));
  }
  const role = roleConfig(), rows = [];
  let total = 0, stoppedAfter = "";
  for (const id of ids) {
    const row = runCase(id, path.join(casesDir, id, "case.json"), cli, role);
    rows.push(row);
    total += row.cost;
    console.error(`${id}: ${row.status}`);
    if (total > maxCost) { stoppedAfter = id; break; }
  }

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
    `role: code-reviewer model=${role.model || "default"} effort=${role.effort || "default"}`,
  ];

  const cols = ["id", "origin", "kind", "important", "hit", "fileHit", "falseAlarm", "costUsd", "status"];
  const cell = (r, c) => c === "costUsd" ? r.cost.toFixed(4) : String(r[c] ?? "");
  const tsv = [cols.join("\t"), ...rows.map(r => cols.map(c => cell(r, c).replace(/[\t\r\n]+/g, " ")).join("\t"))];
  const runsDir = path.join(EVALS, "runs");
  fs.mkdirSync(runsDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
  const out = path.join(runsDir, `${stamp}-${label}.tsv`);
  fs.writeFileSync(out, [...tsv, "# " + summary.join("; ")].join("\n") + "\n");

  if (args.includes("--tsv")) console.log(tsv.join("\n"));
  else {
    console.log("| " + cols.join(" | ") + " |"); console.log("|" + cols.map(() => "---").join("|") + "|");
    rows.forEach(r => console.log("| " + cols.map(c => cell(r, c).replace(/\|/g, "\\|").replace(/[\r\n]+/g, " ")).join(" | ") + " |"));
  }
  console.error("\n" + summary.join("\n") + `\nsaved: ${out}`);
}

if (cmd === "run") run();
else die("usage: reviewer-eval.js run [--cases-dir DIR] [--ids a,b] [--label NAME] [--max-cost USD] [--claude PATH] [--tsv]");
