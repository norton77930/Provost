// Read-only measurement over Claude Code transcripts: usage per role and model, hook
// context and durations, compactions, tool and read counts.
// Usage: node measure.js <sinceISO> <out.json>, then node summarize.js <out.json>
const fs = require("fs"), path = require("path"), os = require("os"), readline = require("readline");
const SINCE = Date.parse(process.argv[2]);
const OUT = process.argv[3];
if (Number.isNaN(SINCE) || !OUT) { console.error("usage: node measure.js <sinceISO> <out.json>"); process.exit(2); }
const root = path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"), "projects");
// Price table copied from tools/dispatch-report.js (PRICES): [input, write5m, write1h, cacheRead, output] USD/MTok
const PRICES = {
  "opus-5-5": [4, 5, 8, 0.2, 20], "sonnet-5-5": [2, 2.5, 4, 0.1, 10], "haiku-5-5": [0.1, 0.125, 0.2, 0.01, 0.5],
  "sonnet-5": [2, 2.5, 4, 0.2, 10], "fable-5-1": [10, 12.5, 20, 0.25, 50], "fable-5-2": [8, 10, 16, 0.2, 40],
  "opus-5": [5, 6.25, 10, 0.5, 25], "opus-4-8": [5, 6.25, 10, 0.5, 25],
};
const mkey = m => String(m || "?").replace(/^claude-/, "");
const R = {
  since: process.argv[2], files: { main: 0, sub: 0 }, usage: {}, mainReq: [], subByRole: {}, hookCtx: {}, hookDur: {},
  stopSummary: { n: 0, prevented: 0, dur: {} }, stopBlocks: [], compactions: [], instr: {}, listings: {},
  tools: { main: {}, sub: {} }, agentCalls: {}, reads: { main: { total: 0, repeat: 0, repeatNoChange: 0 }, sub: { total: 0, repeat: 0, repeatNoChange: 0 } },
  costState: {}, projects: {}, upsVariants: {}, turns: { n: 0, durMs: 0 }, firstReq: [], runtimeCalls: {}, guideReads: { main: 0, sub: 0 },
  declarations: 0, mainSessions: 0, pushNotif: 0,
};
const add = (o, k, n = 1) => { o[k] = (o[k] || 0) + n; };
function hookName(cmd) {
  const s = String(cmd || "");
  const m = s.match(/hooks\/(native-event|native-facts|acceptance-gate)/);
  if (m) return "provost:" + m[1];
  if (/orca/i.test(s)) return "orca";
  if (/notify-toast/.test(s)) return "notify-toast";
  return s.slice(0, 40) || "?";
}
const norm = p => String(p).toLowerCase().replace(/\\/g, "/");
async function scan(file, kind, role, sessKey, proj) {
  const byId = new Map(), readSeen = new Map(), lastEdit = new Map(), order = [];
  let seq = 0, compSeq = 0, firstTs = null, lastCost = null, declared = false, anyInWindow = false;
  const rl = readline.createInterface({ input: fs.createReadStream(file), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line) continue;
    if (line.startsWith('{"type":"api-request') || line.startsWith('{"type":"file-history')) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    const ts = o.timestamp ? Date.parse(o.timestamp) : null;
    if (o.type === "cost-state") { lastCost = o; continue; }
    if (ts !== null && ts < SINCE) continue;
    if (ts !== null && firstTs === null) firstTs = ts;
    if (ts !== null) anyInWindow = true;
    seq++;
    if (o.type === "assistant" && o.message) {
      const m = o.message, u = m.usage;
      if (u && m.id) {
        const prev = byId.get(m.id);
        if (!prev) order.push(m.id);
        if (!prev || (u.output_tokens || 0) >= (prev.u.output_tokens || 0)) byId.set(m.id, { u, model: mkey(m.model), ts });
      }
      for (const b of (Array.isArray(m.content) ? m.content : [])) {
        if (b.type === "text" && kind === "main" && !declared && /分類[:：]/.test(b.text || "")) declared = true;
        if (b.type !== "tool_use") continue;
        add(R.tools[kind], b.name);
        const inp = b.input || {};
        if (b.name === "PushNotification") R.pushNotif++;
        if (b.name === "Agent" || b.name === "Task") {
          const r = inp.subagent_type || "(default)";
          const a = R.agentCalls[r] = R.agentCalls[r] || { n: 0, briefChars: 0, modelOverride: 0, main: 0, sub: 0 };
          a.n++; a.briefChars += String(inp.prompt || "").length; if (inp.model) a.modelOverride++; a[kind]++;
        }
        if (b.name === "Bash" || b.name === "PowerShell") {
          const c = String(inp.command || "");
          const mm = c.match(/hooks[\/\\]+runtime["']?\s+(doctor|status|record|snapshot|list|resume|changes|search|prune|digest|migrate|redact)\b/);
          if (mm) add(R.runtimeCalls, kind + "|" + mm[1]);
        }
        if (b.name === "Read" && inp.file_path) {
          const p = norm(inp.file_path);
          if (p.endsWith("native-handoff.md")) R.guideReads[kind]++;
          const k = p + "|" + (inp.offset || 0) + "|" + (inp.limit || 0);
          R.reads[kind].total++;
          const prev = readSeen.get(k);
          if (prev !== undefined) {
            R.reads[kind].repeat++;
            const le = lastEdit.get(p) || 0;
            if (le < prev && compSeq < prev) R.reads[kind].repeatNoChange++;
          }
          readSeen.set(k, seq);
        }
        if ((b.name === "Edit" || b.name === "Write" || b.name === "NotebookEdit") && inp.file_path) lastEdit.set(norm(inp.file_path), seq);
      }
    } else if (o.type === "attachment") {
      const a = o.attachment || {};
      if (a.type === "hook_additional_context") {
        const txt = (Array.isArray(a.content) ? a.content : [a.content]).join("\n");
        const k = kind + "|" + a.hookEvent;
        const h = R.hookCtx[k] = R.hookCtx[k] || { n: 0, chars: 0, sizes: [] };
        h.n++; h.chars += txt.length; h.sizes.push(txt.length);
        if (a.hookEvent !== "SessionStart") {
          const v = a.hookEvent + "|" + txt.slice(0, 70);
          const e = R.upsVariants[v] = R.upsVariants[v] || { n: 0, chars: 0 };
          e.n++; e.chars += txt.length;
        }
      } else if (["hook_success", "hook_cancelled", "hook_non_blocking_error", "async_hook_response"].includes(a.type)) {
        const k = a.type + "|" + a.hookEvent + "|" + hookName(a.command);
        const h = R.hookDur[k] = R.hookDur[k] || { n: 0, d: [] };
        h.n++; if (typeof a.durationMs === "number") h.d.push(a.durationMs);
      } else if (a.type === "hook_blocking_error") {
        const be = a.blockingError || {};
        const txt = String(be.blockingError || a.content || JSON.stringify(a));
        R.stopBlocks.push({ ev: a.hookEvent, sess: sessKey.slice(0, 8), proj, ts: o.timestamp, chars: txt.length, head: txt.slice(0, 260), cmd: hookName(be.command || a.command) });
      } else if (a.type === "instructions") {
        for (const f of (a.files || [])) {
          const k = f.type + "|" + f.path;
          const e = R.instr[k] = R.instr[k] || { n: 0, chars: 0 };
          e.n++; e.chars = String(f.content || "").length;
        }
      } else if (["skill_listing", "agent_listing_delta", "deferred_tools_delta", "mcp_instructions_delta", "deferred_tools_record", "task_reminder", "total_tokens_reminder", "nested_memory", "critical_system_reminder"].includes(a.type)) {
        const e = R.listings[kind + "|" + a.type] = R.listings[kind + "|" + a.type] || { n: 0, chars: 0 };
        e.n++; e.chars += JSON.stringify(a).length;
      }
    } else if (o.type === "system") {
      if (o.subtype === "compact_boundary" && o.compactMetadata) {
        compSeq = seq; const c = o.compactMetadata;
        R.compactions.push({ kind, proj, trigger: c.trigger, pre: c.preTokens, post: c.postTokens, ms: c.durationMs });
      } else if (o.subtype === "stop_hook_summary") {
        R.stopSummary.n++; if (o.preventedContinuation) R.stopSummary.prevented++;
        (o.hookInfos || []).forEach((h, i) => {
          const k = /command hook/.test(h.command) ? ("anon#" + (i + 1) + "of" + o.hookCount) : hookName(h.command);
          const e = R.stopSummary.dur[k] = R.stopSummary.dur[k] || { n: 0, d: [] };
          e.n++; if (typeof h.durationMs === "number") e.d.push(h.durationMs);
        });
      } else if (o.subtype === "turn_duration") { R.turns.n++; R.turns.durMs += o.durationMs || 0; }
    }
  }
  let first = true, turns = 0;
  for (const id of order) {
    const { u, model } = byId.get(id);
    if (model === "<synthetic>") continue;
    turns++;
    const cc = u.cache_creation || {};
    const w1 = cc.ephemeral_1h_input_tokens || 0;
    const w5 = (cc.ephemeral_5m_input_tokens !== undefined) ? cc.ephemeral_5m_input_tokens : Math.max(0, (u.cache_creation_input_tokens || 0) - w1);
    const rec = { input: u.input_tokens || 0, w5, w1, cr: u.cache_read_input_tokens || 0, out: u.output_tokens || 0 };
    const k = kind + "|" + (kind === "sub" ? role + "|" : "") + model;
    const U = R.usage[k] = R.usage[k] || { req: 0, input: 0, w5: 0, w1: 0, cr: 0, out: 0 };
    U.req++; for (const f of ["input", "w5", "w1", "cr", "out"]) U[f] += rec[f];
    const prompt = rec.input + rec.w5 + rec.w1 + rec.cr;
    if (kind === "main") {
      R.mainReq.push(prompt);
      const P = R.projects[proj] = R.projects[proj] || { req: 0, cost: 0 };
      P.req++; const p = PRICES[model];
      if (p) P.cost += (rec.input * p[0] + rec.w5 * p[1] + rec.w1 * p[2] + rec.cr * p[3] + rec.out * p[4]) / 1e6;
    }
    if (first) {
      first = false;
      if (kind === "sub") {
        const S = R.subByRole[role] = R.subByRole[role] || { n: 0, firstPrompt: [], turns: [], models: {} };
        S.n++; S.firstPrompt.push(prompt); add(S.models, model);
      } else R.firstReq.push({ prompt, model, proj });
    }
  }
  if (kind === "sub" && turns) R.subByRole[role].turns.push(turns);
  if (kind === "main" && anyInWindow) { R.mainSessions++; if (declared) R.declarations++; }
  if (kind === "main" && lastCost && lastCost.startTime >= SINCE) {
    R.costState[sessKey] = { usd: lastCost.totalCostUSD, models: Object.fromEntries(Object.entries(lastCost.modelUsage || {}).map(([m, v]) => [mkey(m), v.costUSD])) };
  }
}
(async () => {
  for (const proj of fs.readdirSync(root)) {
    const pd = path.join(root, proj);
    let st; try { st = fs.statSync(pd); } catch { continue; }
    if (!st.isDirectory()) continue;
    for (const f of fs.readdirSync(pd)) {
      const fp = path.join(pd, f);
      if (f.endsWith(".jsonl")) {
        if (fs.statSync(fp).mtimeMs < SINCE) continue;
        R.files.main++; await scan(fp, "main", "", f.replace(".jsonl", ""), proj);
      } else {
        const sd = path.join(fp, "subagents");
        if (!fs.existsSync(sd)) continue;
        for (const sf of fs.readdirSync(sd)) {
          if (!sf.endsWith(".jsonl")) continue;
          const sp = path.join(sd, sf);
          if (fs.statSync(sp).mtimeMs < SINCE) continue;
          let role = "?";
          try { const meta = JSON.parse(fs.readFileSync(sp.replace(".jsonl", ".meta.json"), "utf8")); role = meta.agentType || meta.subagent_type || "?"; } catch { }
          R.files.sub++; await scan(sp, "sub", role, f, proj);
        }
      }
    }
  }
  fs.writeFileSync(OUT, JSON.stringify(R));
  console.log("done", JSON.stringify(R.files));
})();
