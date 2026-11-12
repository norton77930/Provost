// Read-only second pass over Claude Code transcripts: main requests by class (work, text-only,
// push-only, handoff-only, ...) deduplicated across files by message id, subagent run lengths,
// and read overlap between agents of one session.
// Usage: node measure2.js <sinceISO>
const fs = require("fs"), path = require("path"), os = require("os"), readline = require("readline");
const SINCE = Date.parse(process.argv[2]);
if (Number.isNaN(SINCE)) { console.error("usage: node measure2.js <sinceISO>"); process.exit(2); }
const root = path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"), "projects");
const PRICES = {
  "opus-5-5": [4, 5, 8, 0.2, 20], "sonnet-5-5": [2, 2.5, 4, 0.1, 10], "haiku-5-5": [0.1, 0.125, 0.2, 0.01, 0.5],
  "sonnet-5": [2, 2.5, 4, 0.2, 10], "fable-5-1": [10, 12.5, 20, 0.25, 50], "fable-5-2": [8, 10, 16, 0.2, 40],
  "opus-5": [5, 6.25, 10, 0.5, 25], "opus-4-8": [5, 6.25, 10, 0.5, 25],
};
const mkey = m => String(m || "?").replace(/^claude-/, "");
const norm = p => String(p).toLowerCase().replace(/\\/g, "/");
const RT = /hooks[\/\\]+runtime["']?\s+(doctor|status|record|snapshot|list|resume|changes|search|prune|digest|migrate|redact)\b/;
const seenIds = new Map(); // message id -> first file index (main only)
const R = { dupIds: 0, dupUsd: 0, classes: {}, writeShapes: {}, joins: 0, recordCalls: 0, subCompactByRole: {}, subDispatchByRole: {}, overlap: { sessions: 0, pathsReadBy3plusSubs: 0, readsOnThose: 0, mainAlsoSub: 0, mainPaths: 0 }, outByClass: {}, pushOnlyNoOtherToolInTurn: 0, subTurnsOver: {}, gpDesc: [] };
function cat(b) {
  const inp = b.input || {};
  if (b.name === "PushNotification") return "push";
  if (/^Task(Create|Update|List|Get)$/.test(b.name)) return "tasks";
  if (b.name === "Bash" || b.name === "PowerShell") {
    const m = String(inp.command || "").match(RT);
    if (m) return m[1] === "digest" ? "digest" : "handoff";
    return "work";
  }
  if ((b.name === "Write" || b.name === "Edit") && inp.file_path) {
    const p = norm(inp.file_path);
    if (p.includes("/.claude/acceptance/")) return "acceptfile";
    if (b.name === "Write" && /(handoff|payload|record|checkpoint|milestone)[^\/]*\.json$/.test(p)) return "handoff";
  }
  return "work";
}
async function scan(file, kind, role, sess, fileIdx, sessReads) {
  const byId = new Map(), order = [];
  let compacted = 0;
  const myReads = new Set();
  const rl = readline.createInterface({ input: fs.createReadStream(file), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line) continue;
    if (line.startsWith('{"type":"api-request') || line.startsWith('{"type":"file-history')) continue;
    if (!line.includes('"assistant"') && !line.includes("compact_boundary")) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    const ts = o.timestamp ? Date.parse(o.timestamp) : null;
    if (ts !== null && ts < SINCE) continue;
    if (o.type === "system" && o.subtype === "compact_boundary") { compacted++; continue; }
    if (o.type !== "assistant" || !o.message) continue;
    const m = o.message, u = m.usage;
    if (!u || !m.id) continue;
    let e = byId.get(m.id);
    if (!e) { e = { u, model: mkey(m.model), cats: new Set(), text: false }; byId.set(m.id, e); order.push(m.id); }
    if ((u.output_tokens || 0) >= (e.u.output_tokens || 0)) e.u = u;
    for (const b of (Array.isArray(m.content) ? m.content : [])) {
      if (b.type === "text" && (b.text || "").trim()) e.text = true;
      if (b.type !== "tool_use") continue;
      e.cats.add(cat(b));
      const inp = b.input || {};
      if (kind === "main" && b.name === "Write" && inp.file_path) {
        const shape = norm(inp.file_path).replace(/[0-9a-f]{8,}/g, "#").replace(/\d+/g, "#").split("/").slice(-3).join("/");
        R.writeShapes[shape] = (R.writeShapes[shape] || 0) + 1;
      }
      if (kind === "main" && (b.name === "Bash" || b.name === "PowerShell")) {
        const c = String(inp.command || ""); const mm = c.match(RT);
        if (mm && mm[1] === "record") { R.recordCalls++; if (/--join\b/.test(c)) R.joins++; }
      }
      if (b.name === "Read" && inp.file_path) myReads.add(norm(inp.file_path));
      if (kind === "main" && (b.name === "Agent") && inp.subagent_type === "general-purpose" && R.gpDesc.length < 45) R.gpDesc.push(String(inp.description || "").slice(0, 50));
    }
  }
  for (const id of order) {
    const e = byId.get(id); if (e.model === "<synthetic>") continue;
    const p = PRICES[e.model]; const u = e.u; const cc = u.cache_creation || {};
    const w1 = cc.ephemeral_1h_input_tokens || 0;
    const w5 = (cc.ephemeral_5m_input_tokens !== undefined) ? cc.ephemeral_5m_input_tokens : Math.max(0, (u.cache_creation_input_tokens || 0) - w1);
    const usd = p ? ((u.input_tokens || 0) * p[0] + w5 * p[1] + w1 * p[2] + (u.cache_read_input_tokens || 0) * p[3] + (u.output_tokens || 0) * p[4]) / 1e6 : 0;
    if (kind === "main") {
      if (seenIds.has(id) && seenIds.get(id) !== fileIdx) { R.dupIds++; R.dupUsd += usd; continue; }
      seenIds.set(id, fileIdx);
      const cats = [...e.cats];
      let cls;
      if (!cats.length) cls = "text-only";
      else if (cats.includes("work")) cls = "work";
      else if (cats.length === 1) cls = cats[0] + "-only";
      else cls = "ceremony-mixed(" + cats.sort().join("+") + ")";
      const C = R.classes[cls] = R.classes[cls] || { n: 0, usd: 0, out: 0, cr: 0 };
      C.n++; C.usd += usd; C.out += u.output_tokens || 0; C.cr += u.cache_read_input_tokens || 0;
    }
  }
  if (kind === "sub") {
    R.subDispatchByRole[role] = (R.subDispatchByRole[role] || 0) + 1;
    if (compacted) R.subCompactByRole[role] = (R.subCompactByRole[role] || 0) + 1;
    const t = order.length; const k = role;
    const S = R.subTurnsOver[k] = R.subTurnsOver[k] || { n: 0, over60: 0, over100: 0 };
    S.n++; if (t > 60) S.over60++; if (t > 100) S.over100++;
  }
  sessReads.push({ kind, reads: myReads });
}
(async () => {
  let fileIdx = 0;
  for (const proj of fs.readdirSync(root)) {
    const pd = path.join(root, proj);
    let st; try { st = fs.statSync(pd); } catch { continue; }
    if (!st.isDirectory()) continue;
    const sessions = {};
    for (const f of fs.readdirSync(pd)) {
      const fp = path.join(pd, f);
      if (f.endsWith(".jsonl")) {
        if (fs.statSync(fp).mtimeMs < SINCE) continue;
        const key = f.replace(".jsonl", ""); sessions[key] = sessions[key] || [];
        await scan(fp, "main", "", key, ++fileIdx, sessions[key]);
      }
    }
    for (const f of fs.readdirSync(pd)) {
      const sd = path.join(pd, f, "subagents");
      if (f.endsWith(".jsonl") || !fs.existsSync(sd)) continue;
      sessions[f] = sessions[f] || [];
      for (const sf of fs.readdirSync(sd)) {
        if (!sf.endsWith(".jsonl")) continue;
        const sp = path.join(sd, sf);
        if (fs.statSync(sp).mtimeMs < SINCE) continue;
        let role = "?";
        try { const meta = JSON.parse(fs.readFileSync(sp.replace(".jsonl", ".meta.json"), "utf8")); role = meta.agentType || "?"; } catch { }
        await scan(sp, "sub", role, f, ++fileIdx, sessions[f]);
      }
    }
    for (const list of Object.values(sessions)) {
      const subs = list.filter(x => x.kind === "sub"), mains = list.filter(x => x.kind === "main");
      if (!subs.length) continue;
      R.overlap.sessions++;
      const count = new Map();
      for (const s of subs) for (const p of s.reads) count.set(p, (count.get(p) || 0) + 1);
      for (const [p, n] of count) if (n >= 3) { R.overlap.pathsReadBy3plusSubs++; R.overlap.readsOnThose += n; }
      for (const mm of mains) for (const p of mm.reads) { R.overlap.mainPaths++; if (count.has(p)) R.overlap.mainAlsoSub++; }
    }
  }
  const top = o => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, 22);
  console.log("duplicate message ids across main files:", R.dupIds, "est USD that the first pass double counted:", R.dupUsd.toFixed(2));
  console.log("\nMAIN requests by class (deduped across files): n, est USD, output tokens, cache-read tokens");
  let tot = 0, totN = 0; for (const c of Object.values(R.classes)) { tot += c.usd; totN += c.n; }
  for (const [k, c] of Object.entries(R.classes).sort((a, b) => b[1].usd - a[1].usd)) console.log(` ${k.padEnd(40)} n=${String(c.n).padStart(6)} (${(100 * c.n / totN).toFixed(1)}%) usd=${c.usd.toFixed(2).padStart(8)} (${(100 * c.usd / tot).toFixed(1)}%) out=${c.out} avgCacheRead=${Math.round(c.cr / c.n)}`);
  console.log(" TOTAL n", totN, "usd", tot.toFixed(2));
  console.log("\nrecord calls", R.recordCalls, "with --join", R.joins);
  console.log("\nmain Write path shapes top:"); for (const [k, v] of top(R.writeShapes)) console.log(` ${String(v).padStart(5)} ${k}`);
  console.log("\nsub dispatch files by role / with >=1 compaction / turns>60 / turns>100");
  for (const [r, n] of Object.entries(R.subDispatchByRole).sort((a, b) => b[1] - a[1])) { const s = R.subTurnsOver[r] || {}; console.log(` ${r.padEnd(24)} n=${n} compacted=${R.subCompactByRole[r] || 0} over60=${s.over60 || 0} over100=${s.over100 || 0}`); }
  console.log("\nread overlap:", JSON.stringify(R.overlap));
  console.log("\ngeneral-purpose descriptions:", JSON.stringify(R.gpDesc));
})();
