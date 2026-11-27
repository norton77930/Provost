#!/usr/bin/env node
// Builds code-reviewer eval cases (#56): lists case candidates from history, saves a confirmed case.
// Usage: node reviewer-cases.js candidates --repo <path> [--since YYYY-MM-DD] [--fix-days 30] [--tsv]
//        node reviewer-cases.js commits --repo <path> [--since YYYY-MM-DD] [--fix-days 30] [--min-lines 250]
//             [--max-lines 1250] [--tsv]
//        node reviewer-cases.js save --id <id> --repo <path> --base <sha> --head <sha> --claim <text>
//             --origin escape|seeded|clean --confirmed-by <name> [--expected '<json array of {file,lines,summary}>']
// candidates reads ~/.claude/projects/*/<session>.jsonl (code-reviewer dispatches whose cwd is in --repo) and
// `git log --all`; head = the first commit named in the claim that resolves in the repo (headFrom=claim), else,
// when the claim names a ticket (`ticket #N`, `#N`, `(N)` or a leading `NNN:`), the first commit within 3h
// after the dispatch whose subject carries the same ticket number (headFrom=ticket), else the first commit
// within 3h after the dispatch (headFrom=window; window? when the claim named a ticket no subject matched, so
// check it before saving); base = head^ (first parent). Read-only.
// A claim token that resolves to a merge commit (e.g. "based on main 2246db3") is skipped as a head.
// commits reads only git (no transcripts, which Claude Code deletes after 30 days): single-parent commits at
// least --fix-days old, with min-lines <= added+deleted <= max-lines, that no later fix/revert commit touched
// (the same rule candidates uses for a defect); each row carries a suggested clean-case claim. Read-only.
// Commits with the same `git patch-id --stable` (a commit and its rebased or cherry-picked copies) count as one:
// a later fix that blamed any copy marks the whole group fixed (fixed=), and the group is listed once, as the
// copy reachable from HEAD when there is one (the other copies are counted in dup=). A commit with no
// equivalent is judged alone. The stderr summary line ends with `...; fixed=N; dup=N`.
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

// All commits as {sha, ct (ms), parent (first parent), parents (count), subject}, oldest first.
function commits(repo) {
  const rows = git(repo, "log", "--all", "--format=%H%x09%ct%x09%P%x09%s").split("\n").filter(Boolean).map(l => {
    const [sha, ct, parents, ...s] = l.split("\t");
    const ps = parents.split(" ").filter(Boolean);
    return { sha, ct: Number(ct) * 1000, parent: ps[0] || "", parents: ps.length, subject: s.join("\t") };
  });
  return rows.reverse().sort((a, b) => a.ct - b.ct); // stable: ties keep oldest-first log order
}

// sha -> group of commits sharing one `git patch-id --stable` (a commit and its rebased or cherry-picked copies),
// computed in one batch over all non-merge history. A commit with no equivalent gets no entry.
function patchGroups(repo) {
  const log = git(repo, "log", "--all", "--no-merges", "-p", "--format=commit %H");
  const out = execFileSync("git", ["-C", repo, "patch-id", "--stable"], { input: log, encoding: "utf8", maxBuffer: 1 << 28, stdio: ["pipe", "pipe", "pipe"] });
  const byId = new Map();
  for (const l of out.split("\n").filter(Boolean)) {
    const [id, sha] = l.split(" ");
    if (!byId.has(id)) byId.set(id, []);
    byId.get(id).push(sha);
  }
  const groups = new Map();
  for (const g of byId.values()) if (g.length > 1) for (const sha of g) groups.set(sha, g);
  return groups;
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

// Commit named in the claim: the first 7-40 hex token that resolves to a known non-merge commit; null if none does.
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
    if (sha && bySha.has(sha) && bySha.get(sha).parents < 2) return bySha.get(sha);
  }
  return null;
}

// Ticket number the claim names: `ticket #N`, `#N`, `(N)` or a leading `NNN:`; null if none.
function claimedTicket(claim) {
  const m = /\bticket\s*#?(\d+)\b/i.exec(claim) || /(?:^|[^\w&])#(\d+)\b/.exec(claim) || /\((\d+)\)/.exec(claim) || /^\s*(\d+):/.exec(claim);
  return m ? m[1] : null;
}
const FIX_RE = /\b(fix|fixes|fixed|revert)\b/i;
// Does the commit subject carry ticket n (`#n`, `(n)` or a leading `n:`)?
const subjectHasTicket = (subject, n) => new RegExp(`(?:^|[^\\w&])#${n}\\b|\\(${n}\\)|^\\s*${n}:`).test(subject);

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
    const inWindow = c => c.ct > t && c.ct <= t + WINDOW_MS;
    const ticket = head ? null : claimedTicket(r.claim);
    if (!head && ticket) { head = all.find(c => inWindow(c) && subjectHasTicket(c.subject, ticket)); headFrom = "ticket"; }
    if (!head) { head = all.find(inWindow); headFrom = ticket ? "window?" : "window"; }
    if (!head) { rows.push({ ...base, kind: "skip", note: "no commit within 3h" }); continue; }
    Object.assign(base, { base: head.parent.slice(0, 12), head: head.sha.slice(0, 12), headFrom });
    let found = false;
    for (const f of all) {
      if (f.ct <= head.ct || !f.parent || !FIX_RE.test(f.subject)) continue;
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

// Added+deleted lines and file count of commit c against its first parent (binary rows count 0 lines).
function diffSize(repo, c) {
  let lines = 0, files = 0;
  for (const l of git(repo, "diff", "--numstat", "--no-renames", c.parent, c.sha).split("\n").filter(Boolean)) {
    const [a, d] = l.split("\t");
    lines += (Number(a) || 0) + (Number(d) || 0); files++;
  }
  return { lines, files };
}

function commitsMode() {
  const repoArg = opt("--repo", ""); if (!repoArg) die("commits: --repo is required");
  const repo = path.resolve(repoArg);
  const sinceArg = opt("--since", ""), since = sinceArg ? new Date(sinceArg + "T00:00:00Z") : null;
  if (since && Number.isNaN(since.getTime())) die("commits: --since must be YYYY-MM-DD");
  const num = (k, d) => { const v = Number(opt(k, d)); if (!Number.isFinite(v) || v < 0) die(`commits: ${k} must be a number >= 0`); return v; };
  const fixDays = num("--fix-days", "30"), minLines = num("--min-lines", "250"), maxLines = num("--max-lines", "1250");
  try { git(repo, "rev-parse", "--git-dir"); } catch { die(`commits: not a git repository: ${repo}`); }
  const all = commits(repo), now = Date.now(), rows = [], n = { merge: 0, size: 0, young: 0, fixed: 0, dup: 0 };
  const groups = patchGroups(repo), bySha = new Map(all.map(c => [c.sha, c]));
  const onHead = new Set(git(repo, "rev-list", "HEAD").split("\n").filter(Boolean));
  // one representative per group: a commit reachable from HEAD, else the oldest
  const rep = g => g.find(s => onHead.has(s)) || g.slice().sort((a, b) => (bySha.get(a)?.ct ?? 0) - (bySha.get(b)?.ct ?? 0))[0];
  for (const c of all) {
    if (since && c.ct < since.getTime()) continue;
    if (c.parents !== 1) { n.merge++; continue; } // merge commits, and the root commit
    const group = groups.get(c.sha) || [c.sha];
    if (rep(group) !== c.sha) { n.dup++; continue; } // listed once, through its representative
    if (now - c.ct < fixDays * 86400000) { n.young++; continue; }
    const size = diffSize(repo, c);
    if (size.lines < minLines || size.lines > maxLines) { n.size++; continue; }
    // blame is the costly part, so only survivors get the fix check
    // a fix that blamed any equivalent copy counts, since git blame follows only the copy on the fixed line
    const first = Math.min(...group.map(s => bySha.get(s)?.ct ?? c.ct));
    if (all.some(f => f.ct > first && f.parent && FIX_RE.test(f.subject) && group.some(s => blamedLines(repo, f).get(s)?.size))) { n.fixed++; continue; }
    const subject = clip(c.subject, 200);
    rows.push({ repo, sha: c.sha.slice(0, 12), parent: c.parent.slice(0, 12), date: new Date(c.ct).toISOString().slice(0, 10), lines: size.lines, files: size.files, subject,
      claim: `Commit ${c.sha.slice(0, 7)} "${subject}". Review whether this change is correct and complete.` });
  }
  const cols = ["repo", "sha", "parent", "date", "lines", "files", "subject", "claim"];
  const cell = c => String(c ?? "");
  if (args.includes("--tsv")) {
    console.log(cols.join("\t"));
    rows.forEach(r => console.log(cols.map(c => cell(r[c]).replace(/[\t\r\n]+/g, " ")).join("\t")));
  } else {
    console.log("| " + cols.join(" | ") + " |"); console.log("|" + cols.map(() => "---").join("|") + "|");
    rows.forEach(r => console.log("| " + cols.map(c => cell(r[c]).replace(/\|/g, "\|").replace(/[\r\n]+/g, " ")).join(" | ") + " |"));
  }
  console.error(`\n${rows.length} rows; merge=${n.merge}; size=${n.size}; young=${n.young}; fixed=${n.fixed}; dup=${n.dup}`);
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
else if (cmd === "commits") commitsMode();
else if (cmd === "save") save();
else die("usage: reviewer-cases.js candidates --repo <path> [--since D] [--fix-days N] [--tsv] | commits --repo <path> [--since D] [--fix-days N] [--min-lines N] [--max-lines N] [--tsv] | save --id ... (see header)");
