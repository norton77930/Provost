#!/usr/bin/env node
// Reports stale Claude Code auto memory as suggestions; it never edits anything.
// Usage: node memory-review.js [--config <dir>] [--project <substring>] [--today YYYY-MM-DD] [--json]
//          [--max-file-kb 12] [--max-dated 8] [--max-index-kb 6] [--stale-days 7]
// Reads <config dir>/projects/*/memory/*.md, where <config dir> is --config, else $CLAUDE_CONFIG_DIR, else ~/.claude.
// Every run lists the memory dirs afresh: any projects/<slug>/memory that holds a .md file is scanned, so a project
// created later shows up with no configuration change. --project keeps the slugs that contain the substring.
// Checks per memory dir:
//   C1 overdue follow-up: a follow-up keyword followed within 60 characters by a date on or before today, in a
//      sentence not marked done/completed/superseded/PASS; the date that opens a line is a history stamp, never counted
//   C2 bloated file: over --max-file-kb, or more than --max-dated paragraphs that start with a date
//   C3 index health: MEMORY.md missing or over --max-index-kb, broken links, .md files the index does not link
//   C4 stale status words: CURRENT, PENDING, NOT merged, NOT installed, uncommitted in a file not modified for more than --stale-days
// Read-only: it writes, renames and touches nothing, not even a temp file. Exit 0 on success (findings are not
// errors), 2 on bad arguments or an unreadable config dir.
// The output quotes lines from your memory files, which can hold private text: read it before sharing it.
const fs = require("fs"), path = require("path"), os = require("os");

const VALUE_FLAGS = ["--config", "--project", "--today", "--max-file-kb", "--max-dated", "--max-index-kb", "--stale-days"];
const USAGE = "usage: memory-review.js [--config <dir>] [--project <substring>] [--today YYYY-MM-DD] [--json] " +
  "[--max-file-kb N] [--max-dated N] [--max-index-kb N] [--stale-days N]";
const fail = msg => { process.stderr.write(`memory-review: ${msg}\n${USAGE}\n`); process.exit(2); };

const pad = n => String(n).padStart(2, "0");
const ymdLocal = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const validYmd = (y, m, d) => m >= 1 && m <= 12 && d >= 1 && d <= new Date(Date.UTC(y, m, 0)).getUTCDate();
const dayNum = ymd => { const [y, m, d] = ymd.split("-").map(Number); return Date.UTC(y, m - 1, d) / 864e5; };

function parseArgs(argv) {
  const o = { json: false, config: "", project: "", today: "", maxFileKb: 12, maxDated: 8, maxIndexKb: 6, staleDays: 7 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--json") { o.json = true; continue; }
    if (!VALUE_FLAGS.includes(a)) fail(`unknown argument ${a}`);
    const v = argv[++i];
    if (v === undefined || v.startsWith("--")) fail(`${a} needs a value`);
    if (a === "--config") o.config = v;
    else if (a === "--project") o.project = v;
    else if (a === "--today") {
      const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
      if (!m || !validYmd(+m[1], +m[2], +m[3])) fail(`--today must be a valid YYYY-MM-DD date, got ${v}`);
      o.today = v;
    } else {
      const n = Number(v);
      if (v.trim() === "" || !Number.isFinite(n) || n < 0) fail(`${a} must be a non-negative number, got ${v}`);
      o[{ "--max-file-kb": "maxFileKb", "--max-dated": "maxDated", "--max-index-kb": "maxIndexKb", "--stale-days": "staleDays" }[a]] = n;
    }
  }
  return o;
}

const opts = parseArgs(process.argv.slice(2));
const config = path.resolve(opts.config || process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"));
const today = opts.today || ymdLocal(new Date());
const todayNum = dayNum(today);
try { if (!fs.statSync(config).isDirectory()) throw new Error("not a directory"); }
catch (e) { fail(`cannot read config dir ${config}: ${e.message}`); }

// Every projects/<slug>/memory dir that holds at least one .md file, found fresh on each run.
function discover() {
  const root = path.join(config, "projects");
  if (!fs.existsSync(root)) return [];
  let slugs;
  try { slugs = fs.readdirSync(root); } catch (e) { fail(`cannot read ${root}: ${e.message}`); }
  const dirs = [];
  for (const slug of slugs.sort()) {
    if (opts.project && !slug.toLowerCase().includes(opts.project.toLowerCase())) continue;
    const dir = path.join(root, slug, "memory");
    try {
      if (!fs.statSync(dir).isDirectory()) continue;
      const files = fs.readdirSync(dir).filter(n => /\.md$/i.test(n) && fs.statSync(path.join(dir, n)).isFile()).sort();
      if (files.length) dirs.push({ slug, dir, files });
    } catch { /* no memory dir, or unreadable: not a memory dir */ }
  }
  return dirs;
}

// Reads one memory file: its lines, the frontmatter end, and the date it was last modified (YYYY-MM-DD).
function load(dir, name) {
  const p = path.join(dir, name);
  const st = fs.statSync(p);
  const text = fs.readFileSync(p, "utf8").replace(/^﻿/, "");
  const lines = text.split(/\r?\n/);
  let fmEnd = 0; // number of lines the frontmatter takes, 0 when there is none
  if (lines[0] === "---") {
    const end = lines.indexOf("---", 1);
    if (end > 0) fmEnd = end + 1;
  }
  const fm = lines.slice(0, fmEnd).join("\n");
  const m = /^\s*modified:\s*["']?(\d{4}-\d{2}-\d{2})/m.exec(fm);
  const modified = m && validYmd(+m[1].slice(0, 4), +m[1].slice(5, 7), +m[1].slice(8, 10)) ? m[1] : ymdLocal(new Date(st.mtimeMs));
  return { name, size: st.size, lines, fmEnd, modified };
}

const excerpt = s => { s = s.replace(/\s+/g, " ").trim(); return s.length > 120 ? s.slice(0, 117) + "..." : s; };
const kb = n => (n / 1024).toFixed(1);
const ageDays = f => todayNum - dayNum(f.modified);

const FOLLOW_UP = /\b(re-measure|remeasure|re-check|recheck|re-run|rerun|review|due|follow-up|auto-runs|scheduled|until|ends|expires)\b/i;
const DONE = /\b(done|completed|superseded)\b/i;
const PASS = /\bPASS\b/;
const MAX_GAP = 60; // characters between the end of the keyword and the start of the date

// Valid dates in a sentence, with their offset in it: YYYY-MM-DD, ~YYYY-MM-DD and ~MM-DD (year of the file's modified date).
function datesIn(sentence, defaultYear) {
  const out = [];
  for (const m of sentence.matchAll(/(?<![\d-])~?(\d{4})-(\d{2})-(\d{2})(?!\d)/g))
    if (validYmd(+m[1], +m[2], +m[3])) out.push({ ymd: `${m[1]}-${m[2]}-${m[3]}`, idx: m.index });
  for (const m of sentence.matchAll(/~(\d{2})-(\d{2})(?![\d-])/g))
    if (validYmd(defaultYear, +m[1], +m[2])) out.push({ ymd: `${defaultYear}-${m[1]}-${m[2]}`, idx: m.index });
  return out;
}

// Offset of the date that opens a line (after a list marker, heading marks, bold marks, "(" or "PREVIOUS ("), else -1.
// Such a date stamps a history entry; it is never a follow-up date.
const STAMP = /^\s*(?:[-*+]\s+|#{1,6}\s+)?(?:\*\*|__|\*|_)?\s*(?:[A-Za-z]+\s*\(|\()?\s*(~?)\d{4}-\d{2}-\d{2}/;
const stampIdx = line => { const m = STAMP.exec(line); return m ? m[0].length - 10 - m[1].length : -1; };

function checkC1(f, add) {
  const year = +f.modified.slice(0, 4);
  f.lines.forEach((line, i) => {
    const stamp = stampIdx(line);
    let start = 0;
    for (const part of line.split(/(\. |; )/)) {
      const sentenceStart = start, sentence = part;
      start += part.length;
      if (part === ". " || part === "; ") continue;
      if (DONE.test(sentence) || PASS.test(sentence)) continue;
      // The keyword must come before the date, at most MAX_GAP characters away; the line's stamp date never counts.
      const keywordEnds = [...sentence.matchAll(new RegExp(FOLLOW_UP.source, "gi"))].map(k => k.index + k[0].length);
      const dates = datesIn(sentence, year)
        .filter(d => sentenceStart + d.idx !== stamp && keywordEnds.some(end => end <= d.idx && d.idx - end <= MAX_GAP))
        .map(d => d.ymd).sort();
      if (!dates.length) continue;
      const latest = dates[dates.length - 1];
      if (dayNum(latest) > todayNum) continue;
      add("C1", f.name, i + 1, `follow-up date ${latest} passed ${todayNum - dayNum(latest)} days ago and is not marked done`, sentence);
    }
  });
}

function checkC2(f, add) {
  let dated = 0, first = 0, startsPara = true;
  f.lines.forEach((line, i) => {
    if (i < f.fmEnd) return;
    if (!line.trim()) { startsPara = true; return; }
    if (/^\s*[-*+]\s/.test(line)) startsPara = true;
    if (startsPara && /^\d{4}-\d{2}-\d{2}/.test(line.replace(/^[\s>#*_\-+`[~]+/, ""))) { dated++; if (!first) first = i + 1; }
    startsPara = false;
  });
  const big = f.size > opts.maxFileKb * 1024, many = dated > opts.maxDated;
  if (!big && !many) return;
  add("C2", f.name, first || 1,
    `file is ${kb(f.size)} KB (limit ${opts.maxFileKb}) with ${dated} dated paragraphs (limit ${opts.maxDated}); ` +
    "consider folding it into current state + history", "");
}

const STATUS_CS = /\b(CURRENT|PENDING|NOT merged|NOT installed)\b/;
const STATUS_CI = /\buncommitted\b/i;
const linkOf = line => { const m = /\]\(([^)\s#]+\.md)(?:#[^)\s]*)?\)/.exec(line); return m ? m[1] : ""; };

function checkC4(f, byName, add) {
  const isIndex = f.name === "MEMORY.md";
  f.lines.forEach((line, i) => {
    const m = STATUS_CS.exec(line) || STATUS_CI.exec(line);
    if (!m) return;
    const owner = isIndex ? byName.get(linkOf(line)) : f; // an index line takes its linked file's date
    if (!owner) return;
    const age = ageDays(owner);
    if (age > opts.staleDays)
      add("C4", f.name, i + 1, `status word "${m[0]}" but ${owner.name} was last modified ${owner.modified} (${age} days ago)`, line);
  });
}

function checkC3(d, byName, add) {
  const index = byName.get("MEMORY.md");
  if (!index) { add("C3", "MEMORY.md", 1, "MEMORY.md is missing", ""); }
  else {
    if (index.size > opts.maxIndexKb * 1024) add("C3", "MEMORY.md", 1, `index is ${kb(index.size)} KB (limit ${opts.maxIndexKb} KB)`, "");
    const linked = new Set();
    index.lines.forEach((line, i) => {
      for (const m of line.matchAll(/\]\(([^)\s]+)\)/g)) {
        const target = m[1].split("#")[0];
        if (!/\.md$/i.test(target) || /^[a-z][a-z0-9+.-]*:/i.test(target)) continue;
        linked.add(target);
        if (!fs.existsSync(path.join(d.dir, target))) add("C3", "MEMORY.md", i + 1, `links to ${target}, which does not exist`, line);
      }
    });
    for (const f of d.files) if (f !== "MEMORY.md" && !linked.has(f)) add("C3", f, 1, `${f} is not linked from MEMORY.md`, "");
  }
}

const CHECKS = { C1: "overdue follow-ups", C2: "bloated files", C3: "index health", C4: "stale status words" };

const scanned = discover().map(d => {
  const found = [];
  const add = (check, file, line, reason, text) => found.push({ check, file, line, reason, excerpt: excerpt(text) });
  const loaded = d.files.map(n => load(d.dir, n));
  const byName = new Map(loaded.map(f => [f.name, f]));
  for (const f of loaded) { checkC1(f, add); checkC2(f, add); checkC4(f, byName, add); }
  checkC3(d, byName, add);
  found.sort((a, b) => a.check.localeCompare(b.check) || (a.file < b.file ? -1 : a.file > b.file ? 1 : 0) || a.line - b.line);
  return { slug: d.slug, path: d.dir, findings: found };
});

if (opts.json) {
  process.stdout.write(JSON.stringify({ config, today, dirs: scanned }, null, 2) + "\n");
} else {
  const out = ["# Memory review", "", `Config: ${config}`, `Today: ${today}`, `Memory dirs scanned: ${scanned.length}`, ""];
  for (const d of scanned.filter(x => x.findings.length)) {
    out.push(`## ${d.slug}`, "");
    for (const check of Object.keys(CHECKS)) {
      const fs_ = d.findings.filter(f => f.check === check);
      if (!fs_.length) continue;
      out.push(`### ${check} ${CHECKS[check]}`, "");
      for (const f of fs_) out.push(`- ${f.file}:${f.line} — ${f.reason}${f.excerpt ? ` — "${f.excerpt}"` : ""}`);
      out.push("");
    }
  }
  const clean = scanned.filter(x => !x.findings.length).map(x => x.slug);
  if (clean.length) out.push(`No findings: ${clean.join(", ")}`, "");
  process.stdout.write(out.join("\n"));
}
