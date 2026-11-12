// Read-only: hook durations and timeouts per period from Claude Code transcripts, including the
// anonymous "command hook N" records newer versions write. Prints truncated samples only; the
// samples can contain local paths, so read the output before sharing it.
// Usage: node hook-timing.js <sinceISO> [boundary YYYY-MM-DD ...]
// Without boundaries the periods are 7 days long, counted from <sinceISO>.
const fs = require("fs"), path = require("path"), os = require("os"), readline = require("readline");
const SINCE = Date.parse(process.argv[2]);
if (Number.isNaN(SINCE)) { console.error("usage: node hook-timing.js <sinceISO> [boundary YYYY-MM-DD ...]"); process.exit(2); }
const BOUNDS = process.argv.slice(3).sort();
const DAY = 86400000;
const iso = t => new Date(t).toISOString().slice(0, 10);
function period(day) {
  if (BOUNDS.length) { let from = iso(SINCE); for (const b of BOUNDS) { if (day < b) return from + ".." + b + " (excl.)"; from = b; } return from + ".."; }
  const n = Math.floor((Date.parse(day + "T00:00:00Z") - Date.parse(iso(SINCE) + "T00:00:00Z")) / (7 * DAY));
  const start = Date.parse(iso(SINCE) + "T00:00:00Z") + n * 7 * DAY;
  return iso(start) + ".." + iso(start + 6 * DAY);
}
const root = path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"), "projects");
const samples = {}, byDay = {}, stopDay = {}, cancelReason = {};
const q = (a, p) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const trunc = v => { const s = typeof v === "string" ? v : JSON.stringify(v); return s === undefined ? "undefined" : s.slice(0, 160).replace(/\n/g, "\\n"); };
async function scan(fp) {
  const rl = readline.createInterface({ input: fs.createReadStream(fp), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.includes("hook")) continue;
    if (line.startsWith('{"type":"api-request')) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    const ts = o.timestamp ? Date.parse(o.timestamp) : null;
    if (ts === null || ts < SINCE) continue;
    const day = o.timestamp.slice(0, 10);
    if (o.type === "attachment" && o.attachment && /^hook_(success|cancelled)/.test(o.attachment.type)) {
      const a = o.attachment; const cmd = String(a.command || "");
      const name = /hooks\/(native-event|native-facts|acceptance-gate)/.test(cmd) ? RegExp.$1 : /orca/i.test(cmd) ? "orca" : cmd.slice(0, 24);
      const k = a.type + "|" + a.hookEvent + "|" + name;
      const d = byDay[k] = byDay[k] || {}; const e = d[day] = d[day] || []; if (typeof a.durationMs === "number") e.push(a.durationMs); else e.push(-1);
      if (/^command hook/.test(cmd) || a.type === "hook_cancelled") {
        const sk = k; samples[sk] = samples[sk] || [];
        if (samples[sk].length < 2) samples[sk].push(Object.fromEntries(Object.entries(a).map(([kk, v]) => [kk, trunc(v)])));
      }
    } else if (o.type === "system" && o.subtype === "stop_hook_summary") {
      (o.hookInfos || []).forEach((h, i) => {
        const cmd = String(h.command || "");
        const name = /hooks\/(native-event|native-facts|acceptance-gate)/.test(cmd) ? RegExp.$1 : /orca/i.test(cmd) ? "orca" : cmd.slice(0, 24);
        const k = (i + 1) + "of" + o.hookCount + "|" + name;
        const d = stopDay[k] = stopDay[k] || {}; const e = d[day] = d[day] || []; if (typeof h.durationMs === "number") e.push(h.durationMs);
      });
      if (!samples.stopSummary) samples.stopSummary = [];
      if (samples.stopSummary.length < 2 && (o.hookInfos || []).some(h => /^command hook/.test(String(h.command)))) samples.stopSummary.push(Object.fromEntries(Object.entries(o).filter(([k]) => !["uuid", "parentUuid", "sessionId", "cwd"].includes(k)).map(([kk, v]) => [kk, trunc(v).slice(0, 420)])));
    }
  }
}
(async () => {
  for (const proj of fs.readdirSync(root)) {
    const pd = path.join(root, proj);
    let st; try { st = fs.statSync(pd); } catch { continue; }
    if (!st.isDirectory()) continue;
    for (const f of fs.readdirSync(pd)) {
      if (!f.endsWith(".jsonl")) continue;
      const fp = path.join(pd, f);
      if (fs.statSync(fp).mtimeMs < SINCE) continue;
      await scan(fp);
    }
  }
  console.log("== SAMPLES");
  for (const [k, list] of Object.entries(samples)) { if (!/Stop|SessionStart|PreToolUse|stopSummary|UserPromptSubmit/.test(k)) continue; console.log("--", k); for (const s of list) console.log("   ", JSON.stringify(s).slice(0, 900)); }
  const weeks = d => { const w = {}; for (const [day, arr] of Object.entries(d)) { const key = period(day); (w[key] = w[key] || []).push(...arr); } return w; };
  console.log("\n== attachment hook records by period: n / p50 / p90 (ms; -1 = no duration)");
  for (const [k, d] of Object.entries(byDay).sort()) { if (!/Stop|SessionStart|PreToolUse\|(command hook|acceptance|orca)|UserPromptSubmit|PostToolUse\|command hook 2/.test(k)) continue; const w = weeks(d); console.log(" " + k.padEnd(52) + Object.entries(w).sort().map(([p, a]) => `${p}: n=${a.length} p50=${q(a, 0.5)} p90=${q(a, 0.9)}`).join(" | ")); }
  console.log("\n== stop_hook_summary hooks by period: n / p50 / p90 / >=29s");
  for (const [k, d] of Object.entries(stopDay).sort()) { const w = weeks(d); console.log(" " + k.padEnd(34) + Object.entries(w).sort().map(([p, a]) => `${p}: n=${a.length} p50=${q(a, 0.5)} p90=${q(a, 0.9)} ge29s=${a.filter(x => x >= 29000).length}`).join(" | ")); }
})();
