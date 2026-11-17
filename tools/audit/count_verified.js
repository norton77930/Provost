// Read-only: count the `Verified:` trailers on agent commits. Runs `git log` per repository and
// reads only commit messages; with --list it prints each agent commit's short hash, state, kinds
// and the first 60 characters of the subject, never the body.
// Usage: node count_verified.js [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--list] [--json] <repo> [<repo> ...]
// Agent commit: a body line starting with `Claude-Session:` or `Co-Authored-By: Claude` (any case).
// Trailer: the last body line starting with `Verified:`. State is `verified` (at least one known
// kind), `none` (with a reason after an em dash, `--` or `-`, or without), `malformed` (no known
// kind) or `missing` (no trailer). Commits are deduplicated by hash across the repositories.
const { spawnSync } = require("child_process");
const path = require("path");

const USAGE = "usage: node count_verified.js [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--list] [--json] <repo> [<repo> ...]";
const KINDS = ["machine", "live", "visual", "review", "human"];
const AGENT_RE = /^\s*(Claude-Session:|Co-Authored-By:\s*Claude\b)/i;
const VERIFIED_RE = /^\s*Verified:\s*(.*)$/i;

let since = null, until = null, list = false, asJson = false;
const repos = [];
const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === "--list") list = true;
  else if (a === "--json") asJson = true;
  else if (a === "--since" || a === "--until") {
    const v = args[++i];
    if (!v || v.startsWith("--")) { console.error(USAGE); process.exit(2); }
    if (a === "--since") since = v; else until = v;
  } else repos.push(a);
}
if (!repos.length) { console.error(USAGE); process.exit(2); }

function parse(body) {
  let value = null;
  for (const line of body.split(/\r?\n/)) {
    const m = VERIFIED_RE.exec(line);
    if (m) value = m[1].trim();
  }
  const r = { state: "missing", kinds: [], reason: null, unknown: 0 };
  if (value === null) return r;
  if (/^none\b/i.test(value)) {
    const rest = value.slice(4).trim();
    const m = /^(?:—|--|-)\s*(.*)$/.exec(rest);
    r.state = "none";
    r.reason = m && m[1].trim() ? m[1].trim() : null;
    return r;
  }
  for (const tok of value.split(",").map(t => t.trim().toLowerCase()).filter(Boolean)) {
    if (KINDS.includes(tok)) { if (!r.kinds.includes(tok)) r.kinds.push(tok); }
    else r.unknown++;
  }
  r.state = r.kinds.length ? "verified" : "malformed";
  return r;
}

const blank = () => ({ agent: 0, human: 0, verified: 0, none: 0, noneReason: 0, missing: 0, malformed: 0, kinds: Object.fromEntries([...KINDS, "unknown"].map(k => [k, 0])) });
function add(c, p) {
  c.agent++;
  c[p.state]++;
  if (p.state === "none" && p.reason) c.noneReason++;
  for (const k of p.kinds) c.kinds[k]++;
  c.kinds.unknown += p.unknown;
}

const seen = new Set(), commits = [], total = blank(), rows = [];
for (const repo of repos) {
  const gitArgs = ["-C", repo, "log", "--all", "--format=%H%x1f%s%x1f%B%x1e"];
  if (since) gitArgs.push(`--since=${since}`);
  if (until) gitArgs.push(`--until=${until}`);
  const r = spawnSync("git", gitArgs, { encoding: "utf8", maxBuffer: 1 << 28 });
  if (r.error || r.status !== 0) {
    console.error(`git log failed for ${repo}: ${r.error ? r.error.message : (r.stderr || "").trim()}`);
    process.exit(1);
  }
  const row = Object.assign({ repo: path.resolve(repo).split(path.sep).join("/") }, blank());
  for (const rec of r.stdout.split("\x1e")) {
    const text = rec.replace(/^\n+/, "");
    if (!text) continue;
    const [hash, subject, body = ""] = text.split("\x1f");
    if (!hash || seen.has(hash)) continue;
    seen.add(hash);
    if (!body.split(/\r?\n/).some(l => AGENT_RE.test(l))) { row.human++; total.human++; continue; }
    const p = parse(body);
    add(row, p);
    add(total, p);
    commits.push({ hash, repo: row.repo, state: p.state, kinds: p.kinds, reason: p.reason, subject });
  }
  rows.push(row);
}

const summary = (label, c) => `${label}  agent ${c.agent}  verified ${c.verified}  none ${c.none} (reason ${c.noneReason})  missing ${c.missing}  malformed ${c.malformed}  human ${c.human}  kinds ${Object.entries(c.kinds).map(([k, n]) => `${k}=${n}`).join(" ")}`;

if (asJson) {
  const out = { since, until, repos: rows, total };
  if (list) out.commits = commits;
  console.log(JSON.stringify(out, null, 2));
} else {
  if (list) for (const c of commits) console.log(`${c.hash.slice(0, 7)} ${c.state.padEnd(9)} ${c.kinds.length ? c.kinds.join(",") : "-"} ${c.subject.slice(0, 60)}`);
  for (const r of rows) console.log(summary(r.repo, r));
  console.log(summary("total", total));
}
