#!/usr/bin/env node
// Lists implementer-family subagent dispatches from Claude Code transcripts.
// Usage: node dispatch-report.js [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--project substr] [--roles a,b] [--tsv] [--reviews] [--reports <dir>]
// Reads ~/.claude/projects/*/<session>.jsonl and <session>/subagents/*. Read-only.
// --reviews lists code-reviewer dispatches instead: Important findings (I<n> ids in the report) and the
// "Dispositions: I1 fixed; I2 rejected — why" line main writes after it; per-model precision on stderr.
// A code-reviewer that ran in the background returns only "Async agent launched"; its report is then the last
// assistant text of the subagent transcript. --reviews --reports <dir> saves each report (redacted) as
// <YYYYMMDD-HHMM>-<session8>-<agentId or toolUseId8>.md, skipping files that exist, and names it in the report column.
// Quality columns (review findings, takeovers) are not derivable here; fill them in the trial log.
const fs = require("fs"), path = require("path"), os = require("os"), readline = require("readline");

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const since = new Date(opt("--since", "2026-09-23") + "T00:00:00Z");
const untilArg = opt("--until", ""); // inclusive UTC date; omitted = no upper bound
const until = untilArg ? new Date(Date.parse(untilArg + "T00:00:00Z") + 86400000) : null;
const projectFilter = opt("--project", "");
const roles = new Set(opt("--roles", "implementer,implementer-high,implementer-sonnet").split(","));
const tsv = args.includes("--tsv");
const reviews = args.includes("--reviews");
const reportsDir = reviews ? opt("--reports", "") : "";
const root = path.join(os.homedir(), ".claude", "projects");

// USD per million tokens from the official pricing page (checked 2026-10-11):
// [input, 5m cache write, 1h cache write, cache read, output]. Unknown models get no estCost.
// Sonnet 5.5 cache reads dropped from 0.2 to 0.1 on 2026-10-07; earlier rows are priced at 0.1 too.
// Haiku 5.5 uses its <=100K-prompt tier; requests over 100K cost 5x, so its estCost is a lower bound.
const PRICES = {
  "opus-5-5": [4, 5, 8, 0.2, 20],
  "sonnet-5-5": [2, 2.5, 4, 0.1, 10],
  "haiku-5-5": [0.1, 0.125, 0.2, 0.01, 0.5],
  "sonnet-5": [2, 2.5, 4, 0.2, 10],
  "fable-5-1": [10, 12.5, 20, 0.25, 50],
};
const estCost = (model, s) => {
  const p = PRICES[model];
  if (!p) return "";
  return ((s.input * p[0] + s.write5m * p[1] + s.write1h * p[2] + s.cacheRead * p[3] + s.out * p[4]) / 1e6).toFixed(2);
};

const redact = s => String(s ?? "").replace(/(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{16,})/g, "[REDACTED]");
const clip = (s, n) => redact(String(s ?? "").replace(/\s+/g, " ").slice(0, n));

async function eachLine(file, cb) {
  const rl = readline.createInterface({ input: fs.createReadStream(file), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    try { cb(JSON.parse(line)); } catch { /* skip partial lines */ }
  }
}

async function subagentStats(file) {
  const s = { first: null, last: null, turns: 0, out: 0, cacheRead: 0, input: 0, write5m: 0, write1h: 0, models: new Set(), apiErrors: 0 };
  if (!fs.existsSync(file)) return null;
  // Claude Code writes one line per content block and every line repeats the
  // response's usage, so count each response once (by message.id), keeping the
  // line with the largest output. Summing lines overcounted tokens ~2x.
  const byId = new Map();
  await eachLine(file, o => {
    if (o.timestamp) { s.first = s.first || o.timestamp; s.last = o.timestamp; }
    if (o.type !== "assistant") return;
    const m = o.message || {}, u = m.usage || {};
    if (m.model) s.models.add(m.model);
    if (o.isApiErrorMessage) s.apiErrors++;
    const id = m.id || o.uuid, prev = byId.get(id);
    if (!prev || (u.output_tokens || 0) >= (prev.output_tokens || 0)) byId.set(id, u);
  });
  for (const u of byId.values()) {
    s.turns++;
    s.out += u.output_tokens || 0;
    s.cacheRead += u.cache_read_input_tokens || 0;
    s.input += u.input_tokens || 0;
    // Split cache writes by TTL when the breakdown exists; otherwise count them as 5-minute writes.
    const cc = u.cache_creation;
    if (cc) { s.write5m += cc.ephemeral_5m_input_tokens || 0; s.write1h += cc.ephemeral_1h_input_tokens || 0; }
    else s.write5m += u.cache_creation_input_tokens || 0;
  }
  return s;
}

async function scanSession(projectDir, file) {
  const rows = [], byToolUse = {};
  await eachLine(file, o => {
    if (o.type === "assistant") {
      for (const c of (o.message && o.message.content) || []) {
        if (c.type !== "tool_use" || (c.name !== "Agent" && c.name !== "Task")) continue;
        if (!roles.has(c.input.subagent_type)) continue;
        // A per-dispatch effort shows in the role ("implementer@high"); rows from
        // before implementer-high was folded into implementer keep their old name.
        const role = c.input.subagent_type + (c.input.effort ? "@" + c.input.effort : "");
        const row = { ts: o.timestamp, role, override: c.input.model || "", desc: clip(c.input.description, 60), status: "?", agentId: null };
        byToolUse[c.id] = row; rows.push(row);
      }
    }
    if (o.type === "user" && Array.isArray(o.message && o.message.content)) {
      for (const c of o.message.content) {
        const row = c.type === "tool_result" && byToolUse[c.tool_use_id];
        if (!row) continue;
        const r = o.toolUseResult || {};
        row.agentId = r.agentId || row.agentId;
        row.status = c.is_error ? "error" : (r.status || "ok");
      }
    }
  });
  const session = path.basename(file, ".jsonl");
  for (const row of rows) {
    if (!row.ts || new Date(row.ts) < since || (until && new Date(row.ts) >= until)) { row.skip = true; continue; }
    const sub = row.agentId && await subagentStats(path.join(projectDir, session, "subagents", `agent-${row.agentId}.jsonl`));
    Object.assign(row, {
      project: path.basename(projectDir).replace(/^[A-Za-z]--/, ""),
      session: session.slice(0, 8),
      model: sub ? [...sub.models].join("+").replace(/claude-/g, "") : "",
      wallMin: sub && sub.first ? ((new Date(sub.last) - new Date(sub.first)) / 60000).toFixed(1) : "",
      turns: sub ? sub.turns : "", out: sub ? sub.out : "", cacheReadM: sub ? (sub.cacheRead / 1e6).toFixed(1) : "",
      input: sub ? sub.input : "",
      write5mM: sub ? (sub.write5m / 1e6).toFixed(2) : "", write1hM: sub ? (sub.write1h / 1e6).toFixed(2) : "",
      apiErrors: sub ? sub.apiErrors : "",
    });
    row.estCost = sub ? estCost(row.model, sub) : "";
  }
  return rows.filter(r => !r.skip);
}

const OUTCOMES = ["fixed", "rejected", "intentional", "moot"];
const resultText = c => typeof c.content === "string" ? c.content
  : (Array.isArray(c.content) ? c.content.filter(b => b && b.type === "text").map(b => b.text).join("\n") : "");

const isAsyncLaunch = (r, text) => !!(r && r.isAsync) || /^\s*Async agent launched/.test(text);

// Final report of a background subagent: the text of its latest-timestamped assistant line that has any.
async function lastAssistantText(file) {
  if (!fs.existsSync(file)) return "";
  let best = "", bestTs = "";
  await eachLine(file, o => {
    if (o.type !== "assistant") return;
    const text = resultText({ content: (o.message && o.message.content) || [] });
    if (text.trim() && (o.timestamp || "") >= bestTs) { best = text; bestTs = o.timestamp || ""; }
  });
  return best;
}

// Writes one redacted report file; returns its name, or "" when there is nothing to write or the file exists.
function saveReport(row, session, text) {
  if (!text.trim()) return "";
  const stamp = row.ts.slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
  const name = `${stamp}-${session.slice(0, 8)}-${row.agentId || String(row.toolUseId).slice(0, 8)}.md`;
  fs.mkdirSync(reportsDir, { recursive: true });
  try { fs.writeFileSync(path.join(reportsDir, name), redact(text) + "\n", { flag: "wx" }); }
  catch (e) {
    if (e.code !== "EEXIST") throw e;
    console.error(`skipped existing report ${name}`);
    return "";
  }
  return name;
}

async function scanReviews(projectDir, file) {
  const rows = [], byToolUse = {};
  let last = null; // the latest review; a Dispositions line counts only until the next code-reviewer dispatch
  await eachLine(file, o => {
    const content = (o.message && o.message.content) || [];
    if (o.type === "assistant") {
      for (const c of content) {
        if ((c.type === "tool_use" && (c.name === "Agent" || c.name === "Task")) && c.input.subagent_type === "code-reviewer") {
          last = { ts: o.timestamp, override: c.input.model || "", desc: clip(c.input.description, 60), agentId: null, toolUseId: c.id, text: null, out: {}, done: false };
          byToolUse[c.id] = last; rows.push(last);
        } else if (c.type === "text" && last && last.done && !last.disp && /^\s*Dispositions:/m.test(c.text || "")) {
          last.disp = true;
          const line = c.text.split("\n").find(l => /^\s*Dispositions:/.test(l));
          for (const e of line.replace(/^\s*Dispositions:/, "").split(";")) {
            const m = /^\s*I(\d+)\s+(fixed|rejected|intentional|moot)\b/.exec(e);
            if (m) last.out["I" + m[1]] = m[2];
          }
        }
      }
    }
    if (o.type === "user" && Array.isArray(content)) {
      for (const c of content) {
        const row = c.type === "tool_result" && byToolUse[c.tool_use_id];
        if (!row) continue;
        row.agentId = (o.toolUseResult || {}).agentId || row.agentId;
        row.done = true;
        const text = resultText(c);
        row.text = isAsyncLaunch(o.toolUseResult, text) ? null : text;
      }
    }
  });
  const session = path.basename(file, ".jsonl");
  const out = [];
  for (const row of rows) {
    if (!row.ts || new Date(row.ts) < since || (until && new Date(row.ts) >= until)) continue;
    const subFile = row.agentId && path.join(projectDir, session, "subagents", `agent-${row.agentId}.jsonl`);
    const sub = subFile && await subagentStats(subFile);
    const reportText = row.text !== null ? row.text : (subFile ? await lastAssistantText(subFile) : "");
    const ids = new Set([...reportText.matchAll(/\bI(\d+)\b/g)].map(m => "I" + m[1]));
    const n = Object.fromEntries(OUTCOMES.map(k => [k, Object.values(row.out).filter(v => v === k).length]));
    const important = ids.size;
    out.push({
      ts: row.ts, project: path.basename(projectDir).replace(/^[A-Za-z]--/, ""), session: session.slice(0, 8),
      model: (sub && sub.models.size ? [...sub.models].join("+").replace(/claude-/g, "") : row.override) || "?",
      important, ...n, unrecorded: Math.max(0, important - Object.keys(row.out).filter(id => ids.has(id)).length), desc: row.desc,
      report: reportsDir ? saveReport(row, session, reportText) : "",
    });
  }
  return out;
}

(async () => {
  const all = [];
  for (const dir of fs.readdirSync(root)) {
    if (/-tests-acceptance-probe-/.test(dir)) continue; // automated probe runs
    if (projectFilter && !dir.toLowerCase().includes(projectFilter.toLowerCase())) continue;
    const projectDir = path.join(root, dir);
    if (!fs.statSync(projectDir).isDirectory()) continue;
    for (const fn of fs.readdirSync(projectDir).filter(f => f.endsWith(".jsonl"))) {
      const file = path.join(projectDir, fn);
      if (fs.statSync(file).mtime < since) continue;
      all.push(...await (reviews ? scanReviews : scanSession)(projectDir, file));
    }
  }
  all.sort((a, b) => a.ts.localeCompare(b.ts));
  const cols = reviews ? ["ts", "project", "session", "model", "important", ...OUTCOMES, "unrecorded", "desc", "report"]
    : ["ts", "project", "session", "role", "override", "model", "status", "wallMin", "turns", "out", "cacheReadM", "input", "write5mM", "write1hM", "estCost", "apiErrors", "desc"];
  const fmt = r => cols.map(c => c === "ts" ? r.ts.slice(0, 16).replace("T", " ") : r[c]);
  if (tsv) { console.log(cols.join("\t")); all.forEach(r => console.log(fmt(r).join("\t"))); }
  else {
    console.log("| " + cols.join(" | ") + " |"); console.log("|" + cols.map(() => "---").join("|") + "|");
    all.forEach(r => console.log("| " + fmt(r).join(" | ") + " |"));
  }
  if (reviews) {
    const byModel = {};
    for (const r of all) {
      const m = byModel[r.model] = byModel[r.model] || { reviews: 0, important: 0, fixed: 0, rejected: 0, intentional: 0, moot: 0, unrecorded: 0 };
      m.reviews++; for (const k of Object.keys(m)) if (k !== "reviews") m[k] += r[k];
    }
    console.error(`
${all.length} reviews since ${since.toISOString().slice(0, 10)}` + (untilArg ? ` through ${untilArg}` : ""));
    for (const [k, m] of Object.entries(byModel)) {
      const dec = m.fixed + m.rejected + m.intentional + m.moot;
      console.error(`${k}: ` + Object.entries(m).map(([a, v]) => `${a}=${v}`).join(" ") + ` precision=${dec ? Math.round(100 * m.fixed / dec) + "%" : "n/a"}`);
    }
    return;
  }
  const byRole = {};
  for (const r of all) (byRole[r.role] = byRole[r.role] || []).push(r);
  console.error(`\n${all.length} dispatches since ${since.toISOString().slice(0, 10)}` + (untilArg ? ` through ${untilArg}` : "") + Object.entries(byRole).map(([k, v]) => `; ${k}=${v.length}`).join(""));
})();
