// Read-only: count acceptance files per repository and worktree. Reads only `closed` and the
// gate `kind`/`status` fields; never prints file content.
// Usage: node count_acceptance.js [--list] <dir> [<dir> ...]
// Each <dir> is a repository, or a directory whose children are repositories. Worktrees under
// <repo>/.claude/worktrees are included. --list also prints one line per file with its state.
const fs = require("fs"), path = require("path");
const args = process.argv.slice(2);
const list = args.includes("--list");
const roots = args.filter(a => a !== "--list");
if (!roots.length) { console.error("usage: node count_acceptance.js [--list] <dir> [<dir> ...]"); process.exit(2); }
const seen = new Set(), rows = [], files = [];
const total = { dirs: 0, files: 0, closed: 0, allPass: 0, unmet: 0, bad: 0 };
function look(repo) {
  const d = path.join(repo, ".claude", "acceptance");
  const key = path.resolve(d).toLowerCase();
  if (seen.has(key) || !fs.existsSync(d)) return;
  seen.add(key);
  const r = { files: 0, closed: 0, allPass: 0, unmet: 0, bad: 0, repo: path.resolve(repo).split(path.sep).join("/") };
  for (const f of fs.readdirSync(d)) {
    if (!f.endsWith(".json")) continue;
    r.files++;
    let state;
    try {
      const j = JSON.parse(fs.readFileSync(path.join(d, f), "utf8"));
      if (typeof j.closed === "string" && j.closed) state = "closed";
      else if (Array.isArray(j.gates) && j.gates.every(g => g && (g.status === "pass" || g.kind === "human"))) state = "allPass";
      else state = "unmet";
    } catch { state = "bad"; }
    r[state]++;
    files.push(`${state.padEnd(8)} ${r.repo}/.claude/acceptance/${f}`);
  }
  total.dirs++;
  for (const k of ["files", "closed", "allPass", "unmet", "bad"]) total[k] += r[k];
  rows.push(r);
}
function visit(repo) {
  look(repo);
  const wt = path.join(repo, ".claude", "worktrees");
  if (fs.existsSync(wt)) for (const w of fs.readdirSync(wt)) look(path.join(wt, w));
}
for (const root of roots) {
  if (!fs.existsSync(root)) continue;
  visit(root);
  for (const e of fs.readdirSync(root)) {
    const repo = path.join(root, e);
    try { if (!fs.statSync(repo).isDirectory()) continue; } catch { continue; }
    visit(repo);
  }
}
console.log(`acceptance dirs ${total.dirs} | json files ${total.files} | closed ${total.closed} | every non-human gate pass, not closed ${total.allPass} | unmet ${total.unmet} | unparseable ${total.bad}`);
for (const r of rows.sort((a, b) => b.files - a.files)) console.log(` files=${r.files} closed=${r.closed} allPass=${r.allPass} unmet=${r.unmet} bad=${r.bad}  ${r.repo}`);
if (list) { console.log(""); for (const f of files.sort()) console.log(f); }
