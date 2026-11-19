// Read-only: count acceptance files per repository and worktree. Reads only `closed` and the
// gate `kind`/`status` fields; never prints file content.
// Usage: node count_acceptance.js [--list] <dir> [<dir> ...]
// Each <dir> is a repository, or a directory whose children are repositories. Worktrees under
// <repo>/.claude/worktrees are included. --list also prints one line per file with its state.
// States: closed; waitingHuman (every non-human gate passes, at least one human gate is not pass:
// waiting on the owner); allDone (every gate passes, human gates included, still not closed);
// unmet (a non-human gate is not pass); bad (not parseable JSON). A leading UTF-8 BOM is ignored,
// as the Stop gate does.
const fs = require("fs"), path = require("path");
const args = process.argv.slice(2);
const list = args.includes("--list");
const roots = args.filter(a => a !== "--list");
if (!roots.length) { console.error("usage: node count_acceptance.js [--list] <dir> [<dir> ...]"); process.exit(2); }
const STATES = ["closed", "waitingHuman", "allDone", "unmet", "bad"];
const seen = new Set(), rows = [], files = [];
const total = { dirs: 0, files: 0, closed: 0, waitingHuman: 0, allDone: 0, unmet: 0, bad: 0 };
function classify(text) {
  const j = JSON.parse(text.replace(/^﻿/, ""));
  if (typeof j.closed === "string" && j.closed) return "closed";
  if (!Array.isArray(j.gates)) return "unmet";
  const gates = j.gates;
  if (!gates.every(g => g && (g.status === "pass" || g.kind === "human"))) return "unmet";
  return gates.every(g => g.status === "pass") ? "allDone" : "waitingHuman";
}
function look(repo) {
  const d = path.join(repo, ".claude", "acceptance");
  const key = path.resolve(d).toLowerCase();
  if (seen.has(key) || !fs.existsSync(d)) return;
  seen.add(key);
  const r = { files: 0, closed: 0, waitingHuman: 0, allDone: 0, unmet: 0, bad: 0, repo: path.resolve(repo).split(path.sep).join("/") };
  for (const f of fs.readdirSync(d)) {
    if (!f.endsWith(".json")) continue;
    r.files++;
    let state;
    try { state = classify(fs.readFileSync(path.join(d, f), "utf8")); } catch { state = "bad"; }
    r[state]++;
    files.push(`${state.padEnd(12)} ${r.repo}/.claude/acceptance/${f}`);
  }
  total.dirs++;
  for (const k of ["files", ...STATES]) total[k] += r[k];
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
console.log(`acceptance dirs ${total.dirs} | json files ${total.files} | closed ${total.closed} | all non-human gates pass, a human gate pending (waitingHuman) ${total.waitingHuman} | every gate pass, not closed (allDone) ${total.allDone} | unmet ${total.unmet} | unparseable ${total.bad}`);
for (const r of rows.sort((a, b) => b.files - a.files)) console.log(` files=${r.files} closed=${r.closed} waitingHuman=${r.waitingHuman} allDone=${r.allDone} unmet=${r.unmet} bad=${r.bad}  ${r.repo}`);
if (list) { console.log(""); for (const f of files.sort()) console.log(f); }
