#!/usr/bin/env node
// Builds code-reviewer eval cases (#56): lists case candidates from history, saves a confirmed case.
// Usage: node reviewer-cases.js candidates --repo <path> [--since YYYY-MM-DD] [--fix-days 30] [--tsv]
//        node reviewer-cases.js save --id <id> --repo <path> --base <sha> --head <sha> --claim <text>
//             --origin escape|seeded|clean --confirmed-by <name> [--expected '<json array of {file,lines,summary}>']
// candidates reads ~/.claude/projects/*/<session>.jsonl (code-reviewer dispatches whose cwd is in --repo) and
// `git log --all`; head = the first commit named in the claim that resolves in the repo (headFrom=claim), else
// the first commit within 3h after the dispatch (headFrom=window); base = head^ (first parent). Read-only.
// save writes ~/.claude/provost/evals/reviewer/cases/<id>/case.json and refuses to overwrite.
const fs = require("fs"), path = require("path"), os = require("os"), readline = require("readline");
const { execFileSync } = require("child_process");

const args = process.argv.slice(2);
const cmd = args[0];
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 && i + 1 < args.length ? args[i + 1] : d; };
const die = m => { console.error(m); process.exit(1); };

const redact = s => String(s ?? "").replace(/(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{16,})/g, "[REDACTED]");
const clip = (s, n) => redact(String(s ?? "").replace(/\s+/g, " ").slice(0, n));
const norm = p => String(p).replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
const WINDOW_MS = 3 * 3600 * 1000;

function git(repo, ...a) {
  return execFileSync("git", ["-c", "core.quotepath=off", "-C", repo, ...a], { encoding: "utf8", maxBuffer: 1 << 28, stdio: ["ignore", "pipe", "pipe"] });
}

async function eachLine(file, cb) {
  const rl = readline.createInterface({ input: fs.createReadStream(file), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.includes("code-reviewer")) continue; // transcripts are large; parse only candidate lines
    try { cb(JSON.parse(line)); } catch { /* skip partial lines */ }
  }
}

// code-reviewer dispatches in main-session transcripts whose cwd is the repo or below it.
async function reviews(repo, since) {
  const root = path.join(os.homedir(), ".claude", "projects"), want = norm(repo), out = [];
  if (!fs.existsSync(root)) return out;
  for (const dir of fs.readdirSync(root)) {
    const projectDir = path.join(root, dir);
    if (!fs.statSync(projectDir).isDirectory()) continue;
    for (const fn of fs.readdirSync(projectDir).filter(f => f.endsWith(".jsonl"))) {
      const file = path.join(projectDir, fn);
      if (since && fs.statSync(file).mtime < since) continue;
      await eachLine(file, o => {
        if (o.type !== "assistant" || !o.timestamp || !o.cwd) return;
        const cwd = norm(o.cwd);
        if (cwd !== want && !cwd.startsWith(want + "/")) return;
        for (const c of (o.message && o.message.content) || []) {
          if (c.type !== "tool_use" || (c.name !== "Agent" && c.name !== "Task")) continue;
          if (!c.input || c.input.subagent_type !== "code-reviewer") continue;
          if (since && new Date(o.timestamp) < since) continue;
          out.push({ ts: o.timestamp, session: path.basename(fn, ".jsonl").slice(0, 8), claim: clip(c.input.prompt, 200) });
        }
      });
    }
  }
  return out.sort((a, b) => a.ts.localeCompare(b.ts));
}

// All commits as {sha, ct (ms), parent (first parent), subject}, oldest first.
function commits(repo) {
  const rows = git(repo, "log", "--all", "--format=%H%x09%ct%x09%P%x09%s").split("\n").filter(Boolean).map(l => {
    const [sha, ct, parents, ...s] = l.split("\t");
    return { sha, ct: Number(ct) * 1000, parent: parents.split(" ")[0] || "", subject: s.join("\t") };
  });
  return rows.reverse().sort((a, b) => a.ct - b.ct); // stable: ties keep oldest-first log order
}

// file -> old-side line ranges [a,b] that commit F deleted or modified (pure additions and new files have none).
function oldRanges(repo, f) {
  const diff = git(repo, "diff", "-U0", "--no-color", "--no-renames", f.parent, f.sha);
  const byFile = new Map();
  let file = null, header = false;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) { header = true; file = null; continue; }
    if (header && line.startsWith("--- ")) { // only in the header: a deleted "-- x" line also starts with "--- "
      const m = /^--- a\/(.*?)\t?$/.exec(line);
      file = m ? m[1] : null;
      continue;
    }
    const h = /^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@/.exec(line);
    if (!h) continue;
    header = false;
    const start = Number(h[1]), n = h[2] === undefined ? 1 : Number(h[2]);
    if (file && n > 0) {
      if (!byFile.has(file)) byFile.set(file, []);
      byFile.get(file).push([start, start + n - 1]);
    }
  }
  return byFile;
}

// For fix commit F: blamed commit -> file -> lines (in the blamed commit's own version) that F removed or
// changed. Cached per F, since one fix is checked against every reviewed head.
const blamedCache = new Map();
function blamedLines(repo, f) {
  if (blamedCache.has(f.sha)) return blamedCache.get(f.sha);
  const by = new Map();
  for (const [file, ranges] of oldRanges(repo, f)) {
    let out;
    try { out = git(repo, "blame", "--porcelain", ...ranges.flatMap(([a, b]) => ["-L", `${a},${b}`]), f.parent, "--", file); } catch { continue; }
    // Porcelain header per line: "<sha> <line in that commit's version> <line in F^> [<group size>]".
    for (const m of out.matchAll(/^([0-9a-f]{40}) (\d+) \d+/gm)) {
      if (!by.has(m[1])) by.set(m[1], new Map());
      const perFile = by.get(m[1]);
      if (!perFile.has(file)) perFile.set(file, new Set());
      perFile.get(file).add(Number(m[2]));
    }
  }
  blamedCache.set(f.sha, by);
  return by;
}

// Commit named in the claim: the first 7-40 hex token that resolves to a known commit; null if none does.
const commitCache = new Map();
function resolveCommit(repo, tok) {
  if (commitCache.has(tok)) return commitCache.get(tok);
  let sha = null;
  try { sha = git(repo, "rev-parse", "--verify", "--quiet", `${tok}^{commit}`).trim() || null; } catch { /* unknown token */ }
  commitCache.set(tok, sha);
  return sha;
}
function claimedCommit(repo, claim, bySha) {
  for (const tok of String(claim).match(/\b[0-9a-f]{7,40}\b/gi) || []) {
    const sha = resolveCommit(repo, tok);
    if (sha && bySha.has(sha)) return bySha.get(sha);
  }
  return null;
}

const rangeText = ls => {
  const out = [];
  for (const n of ls) {
    const last = out[out.length - 1];
    if (last && last[1] === n - 1) last[1] = n; else out.push([n, n]);
  }
  return out.map(([a, b]) => a === b ? String(a) : `${a}-${b}`).join(",");
};

async function candidates() {
  const repoArg = opt("--repo", ""); if (!repoArg) die("candidates: --repo is required");
  const repo = path.resolve(repoArg);
  const sinceArg = opt("--since", ""), since = sinceArg ? new Date(sinceArg + "T00:00:00Z") : null;
  const fixDays = Number(opt("--fix-days", "30"));
  if (!Number.isFinite(fixDays) || fixDays < 0) die("candidates: --fix-days must be a number >= 0");
  try { git(repo, "rev-parse", "--git-dir"); } catch { die(`candidates: not a git repository: ${repo}`); }
  const all = commits(repo), bySha = new Map(all.map(c => [c.sha, c])), now = Date.now(), rows = [];
  for (const r of await reviews(repo, since)) {
    const t = Date.parse(r.ts);
    const base = { repo, ts: r.ts.slice(0, 16).replace("T", " "), session: r.session, claim: r.claim, base: "", head: "", headFrom: "", fix: "", file: "", lines: "", note: "" };
    let head = claimedCommit(repo, r.claim, bySha), headFrom = "claim";
    if (!head) { head = all.find(c => c.ct > t && c.ct <= t + WINDOW_MS); headFrom = "window"; }
    if (!head) { rows.push({ ...base, kind: "skip", note: "no commit within 3h" }); continue; }
    Object.assign(base, { base: head.parent.slice(0, 12), head: head.sha.slice(0, 12), headFrom });
    let found = false;
    for (const f of all) {
      if (f.ct <= head.ct || !f.parent || !/\b(fix|fixes|fixed|revert)\b/i.test(f.subject)) continue;
      for (const [file, ls] of blamedLines(repo, f).get(head.sha) || []) {
        found = true;
        rows.push({ ...base, kind: "defect", fix: f.sha.slice(0, 12), file, lines: rangeText([...ls].sort((x, y) => x - y)) });
      }
    }
    if (!found) rows.push({ ...base, kind: now - head.ct >= fixDays * 86400000 ? "clean" : "pending-age" });
  }
  const cols = ["kind", "repo", "ts", "session", "base", "head", "headFrom", "fix", "file", "lines", "note", "claim"];
  const cell = c => String(c ?? "");
  if (args.includes("--tsv")) {
    console.log(cols.join("\t"));
    rows.forEach(r => console.log(cols.map(c => cell(r[c]).replace(/[\t\r\n]+/g, " ")).join("\t")));
  } else {
    console.log("| " + cols.join(" | ") + " |"); console.log("|" + cols.map(() => "---").join("|") + "|");
    rows.forEach(r => console.log("| " + cols.map(c => cell(r[c]).replace(/\|/g, "\\|").replace(/[\r\n]+/g, " ")).join(" | ") + " |"));
  }
  const n = {};
  for (const r of rows) n[r.kind] = (n[r.kind] || 0) + 1;
  console.error(`\n${rows.length} rows` + Object.entries(n).map(([k, v]) => `; ${k}=${v}`).join(""));
}

function save() {
  const need = k => { const v = opt("--" + k, ""); if (!v) die(`save: --${k} is required`); return v; };
  const id = need("id"), repo = need("repo"), base = need("base"), head = need("head"), claim = need("claim");
  const origin = need("origin"), confirmedBy = need("confirmed-by");
  if (!/^[A-Za-z0-9._-]+$/.test(id) || /^\.+$/.test(id)) die(`save: invalid id: ${id}`);
  if (!["escape", "seeded", "clean"].includes(origin)) die(`save: --origin must be escape, seeded or clean`);
  let expected = [];
  const raw = opt("--expected", undefined);
  if (raw !== undefined) {
    try { expected = JSON.parse(raw); } catch { die("save: --expected is not valid JSON"); }
    if (!Array.isArray(expected)) die("save: --expected must be a JSON array");
  }
  if (origin === "clean" && expected.length) die("save: a clean case must have no expected findings");
  const dir = path.join(os.homedir(), ".claude", "provost", "evals", "reviewer", "cases", id);
  if (fs.existsSync(dir)) die(`save: case already exists: ${dir}`);
  fs.mkdirSync(dir, { recursive: true });
  const savedAt = new Date().toISOString();
  fs.writeFileSync(path.join(dir, "case.json"), JSON.stringify({ repo, base, head, claim, expected, origin, confirmedBy, savedAt }, null, 2) + "\n", { flag: "wx" });
  console.log(path.join(dir, "case.json"));
}

if (cmd === "candidates") candidates().catch(e => die(e.message));
else if (cmd === "save") save();
else die("usage: reviewer-cases.js candidates --repo <path> [--since D] [--fix-days N] [--tsv] | save --id ... (see header)");
