const fs = require("fs");
const R = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const PRICES = {
  "opus-5-5": [4, 5, 8, 0.2, 20], "sonnet-5-5": [2, 2.5, 4, 0.1, 10], "haiku-5-5": [0.1, 0.125, 0.2, 0.01, 0.5],
  "sonnet-5": [2, 2.5, 4, 0.2, 10], "fable-5-1": [10, 12.5, 20, 0.25, 50], "fable-5-2": [8, 10, 16, 0.2, 40],
  "opus-5": [5, 6.25, 10, 0.5, 25], "opus-4-8": [5, 6.25, 10, 0.5, 25],
};
const q = (a, p) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const sum = a => a.reduce((x, y) => x + y, 0);
const M = n => (n / 1e6).toFixed(2) + "M";
const cost = (model, u) => { const p = PRICES[model]; if (!p) return null; return { input: u.input * p[0] / 1e6, w5: u.w5 * p[1] / 1e6, w1: u.w1 * p[2] / 1e6, cr: u.cr * p[3] / 1e6, out: u.out * p[4] / 1e6 }; };
console.log("WINDOW since", R.since, "files", JSON.stringify(R.files), "mainSessions(with records in window)", R.mainSessions, "with 分類 line", R.declarations);
console.log("\n== USAGE by kind|role|model (tokens; est USD by dispatch-report price table)");
const rows = []; const tot = { main: 0, sub: 0 }; const comp = { main: { input: 0, w5: 0, w1: 0, cr: 0, out: 0 }, sub: { input: 0, w5: 0, w1: 0, cr: 0, out: 0 } };
const byRole = {}, byModel = {};
for (const [k, u] of Object.entries(R.usage)) {
  const parts = k.split("|"); const kind = parts[0]; const model = parts[parts.length - 1]; const role = kind === "sub" ? parts[1] : "main";
  const c = cost(model, u); const usd = c ? sum(Object.values(c)) : null;
  if (c) { tot[kind] += usd; for (const f in c) comp[kind][f] += c[f]; }
  const br = byRole[role] = byRole[role] || { req: 0, usd: 0, out: 0, cr: 0 }; br.req += u.req; br.usd += usd || 0; br.out += u.out; br.cr += u.cr;
  const bm = byModel[kind + "|" + model] = byModel[kind + "|" + model] || { req: 0, usd: 0 }; bm.req += u.req; bm.usd += usd || 0;
  rows.push({ k, req: u.req, input: u.input, w5: u.w5, w1: u.w1, cr: u.cr, out: u.out, usd });
}
rows.sort((a, b) => (b.usd || 0) - (a.usd || 0));
for (const r of rows.slice(0, 28)) console.log(`${r.k.padEnd(44)} req=${String(r.req).padStart(6)} in=${M(r.input)} w5=${M(r.w5)} w1=${M(r.w1)} cacheRead=${M(r.cr)} out=${M(r.out)} usd=${r.usd === null ? "n/a" : r.usd.toFixed(2)}`);
console.log("TOTAL est USD main", tot.main.toFixed(2), "sub", tot.sub.toFixed(2));
for (const k of ["main", "sub"]) console.log(" cost components", k, Object.entries(comp[k]).map(([f, v]) => `${f}=${v.toFixed(2)} (${(100 * v / (tot[k] || 1)).toFixed(0)}%)`).join(" "));
console.log("\n== by role (requests, est USD)");
for (const [r, v] of Object.entries(byRole).sort((a, b) => b[1].usd - a[1].usd)) console.log(` ${r.padEnd(26)} req=${String(v.req).padStart(6)} usd=${v.usd.toFixed(2)} out=${M(v.out)} cacheRead=${M(v.cr)}`);
console.log("\n== by kind|model");
for (const [r, v] of Object.entries(byModel).sort((a, b) => b[1].usd - a[1].usd)) console.log(` ${r.padEnd(26)} req=${String(v.req).padStart(6)} usd=${v.usd.toFixed(2)}`);
console.log("\n== cost-state cross-check (sessions started in window):", Object.keys(R.costState).length, "sessions, total USD", sum(Object.values(R.costState).map(c => c.usd || 0)).toFixed(2));
console.log("\n== main per-request prompt tokens: n", R.mainReq.length, "p10", q(R.mainReq, 0.1), "p50", q(R.mainReq, 0.5), "p90", q(R.mainReq, 0.9), "max", q(R.mainReq, 0.999), "share>=200K", (100 * R.mainReq.filter(x => x >= 200000).length / R.mainReq.length).toFixed(1) + "%");
const fr = R.firstReq.map(x => x.prompt).filter(x => x > 0);
console.log("== first request of each main transcript-in-window (tokens): n", fr.length, "p10", q(fr, 0.1), "p50", q(fr, 0.5), "p90", q(fr, 0.9), "(includes resumed sessions whose first in-window request is mid-conversation)");
console.log("\n== subagents by role: n, first-request prompt tokens p10/p50/p90, turns p50/p90, models");
for (const [r, s] of Object.entries(R.subByRole).sort((a, b) => b[1].n - a[1].n)) console.log(` ${r.padEnd(24)} n=${String(s.n).padStart(4)} first p10=${q(s.firstPrompt, 0.1)} p50=${q(s.firstPrompt, 0.5)} p90=${q(s.firstPrompt, 0.9)} turns p50=${q(s.turns, 0.5)} p90=${q(s.turns, 0.9)} models=${JSON.stringify(s.models)}`);
console.log("\n== Agent tool calls by subagent_type");
for (const [r, a] of Object.entries(R.agentCalls).sort((x, y) => y[1].n - x[1].n)) console.log(` ${r.padEnd(24)} n=${String(a.n).padStart(4)} fromMain=${a.main} fromSub=${a.sub} avgBriefChars=${Math.round(a.briefChars / a.n)} modelOverride=${a.modelOverride}`);
console.log("\n== hook additionalContext injected (chars)");
for (const [k, h] of Object.entries(R.hookCtx)) console.log(` ${k.padEnd(28)} n=${h.n} total=${h.chars} p50=${q(h.sizes, 0.5)} p90=${q(h.sizes, 0.9)} max=${q(h.sizes, 0.9999)}`);
console.log("\n== non-SessionStart injected variants");
for (const [k, v] of Object.entries(R.upsVariants).sort((a, b) => b[1].n - a[1].n).slice(0, 12)) console.log(` n=${String(v.n).padStart(4)} chars=${v.chars} :: ${k}`);
console.log("\n== hook durations ms (attachment records)");
for (const [k, h] of Object.entries(R.hookDur).sort((a, b) => b[1].n - a[1].n).slice(0, 40)) console.log(` ${k.padEnd(60)} n=${String(h.n).padStart(6)} withDur=${h.d.length} p50=${q(h.d, 0.5)} p90=${q(h.d, 0.9)} max=${q(h.d, 0.9999)} sumMin=${(sum(h.d) / 60000).toFixed(1)}`);
console.log("\n== stop_hook_summary n", R.stopSummary.n, "preventedContinuation", R.stopSummary.prevented);
for (const [k, h] of Object.entries(R.stopSummary.dur).sort((a, b) => b[1].n - a[1].n)) console.log(` ${k.padEnd(28)} n=${String(h.n).padStart(5)} withDur=${h.d.length} p50=${q(h.d, 0.5)} p90=${q(h.d, 0.9)} p99=${q(h.d, 0.99)} max=${q(h.d, 0.9999)} sumMin=${(sum(h.d) / 60000).toFixed(1)} over5s=${h.d.filter(x => x > 5000).length} over15s=${h.d.filter(x => x > 15000).length}`);
console.log("turns", R.turns.n, "total turn minutes", (R.turns.durMs / 60000).toFixed(0));
console.log("\n== Stop/other blocking errors:", R.stopBlocks.length);
const bySess = {}; for (const b of R.stopBlocks) { const k = b.proj.slice(-28) + "/" + b.sess; bySess[k] = (bySess[k] || 0) + 1; }
console.log(" by session:", JSON.stringify(bySess));
const cat = { pending: 0, fail: 0, stale: 0, malformed: 0, other: 0 };
for (const b of R.stopBlocks) { const t = b.head + ""; if (/Malformed/.test(t)) cat.malformed++; }
console.log(" avg chars", Math.round(sum(R.stopBlocks.map(b => b.chars)) / (R.stopBlocks.length || 1)), "max", Math.max(0, ...R.stopBlocks.map(b => b.chars)));
for (const b of R.stopBlocks.slice(0, 3)) console.log("  sample:", b.ts, b.cmd, b.head.slice(0, 200).replace(/\n/g, " | "));
console.log("\n== compactions:", R.compactions.length);
const cm = R.compactions.filter(c => c.kind === "main"), cs = R.compactions.filter(c => c.kind === "sub");
const trig = {}; for (const c of R.compactions) trig[c.kind + "|" + c.trigger] = (trig[c.kind + "|" + c.trigger] || 0) + 1;
console.log(" by kind|trigger", JSON.stringify(trig), "main pre p50", q(cm.map(c => c.pre || 0), 0.5), "post p50", q(cm.map(c => c.post || 0), 0.5), "dur p50 ms", q(cm.map(c => c.ms || 0), 0.5), "| sub pre p50", q(cs.map(c => c.pre || 0), 0.5));
console.log("\n== instruction files loaded (chars, times seen)");
for (const [k, v] of Object.entries(R.instr).sort((a, b) => b[1].chars - a[1].chars).slice(0, 14)) console.log(` ${String(v.chars).padStart(6)} chars  n=${String(v.n).padStart(4)}  ${k}`);
console.log("\n== listing attachments (avg chars per occurrence)");
for (const [k, v] of Object.entries(R.listings).sort((a, b) => b[1].chars - a[1].chars)) console.log(` ${k.padEnd(34)} n=${String(v.n).padStart(5)} avg=${Math.round(v.chars / v.n)} total=${v.chars}`);
console.log("\n== tools main top"); console.log(" " + Object.entries(R.tools.main).sort((a, b) => b[1] - a[1]).slice(0, 22).map(([k, v]) => k + "=" + v).join(" "));
console.log("== tools sub top"); console.log(" " + Object.entries(R.tools.sub).sort((a, b) => b[1] - a[1]).slice(0, 14).map(([k, v]) => k + "=" + v).join(" "));
console.log("\n== reads", JSON.stringify(R.reads));
console.log("== runtime CLI calls", JSON.stringify(R.runtimeCalls), "guide reads", JSON.stringify(R.guideReads), "PushNotification calls", R.pushNotif);
console.log("\n== projects by est main USD");
for (const [k, v] of Object.entries(R.projects).sort((a, b) => b[1].cost - a[1].cost).slice(0, 12)) console.log(` ${v.cost.toFixed(2).padStart(8)} req=${String(v.req).padStart(6)} ${k.slice(-60)}`);
