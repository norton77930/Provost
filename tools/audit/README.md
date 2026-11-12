# Audit measurement scripts

The scripts behind the numbers in `docs/analysis/2026-11-12-architecture-audit.md`. They are
kept so a later change can be measured the same way as the baseline. All of them are read-only:
they read Claude Code transcripts under `<config dir>/projects` (`CLAUDE_CONFIG_DIR`, else
`~/.claude`) and print counts, token sums and durations. Dollar figures are recorded tokens
multiplied by the price table in `tools/dispatch-report.js`, not a bill.

The output names local project folders and can quote short pieces of hook output. Read it
before sharing it.

| Script | Answers | Run |
|---|---|---|
| `measure.js` + `summarize.js` | usage per role and model, prompt size per request, hook context and durations, compactions, tool, read and runtime CLI counts | `node tools/audit/measure.js 2026-10-27T00:00:00Z out.json` then `node tools/audit/summarize.js out.json` |
| `measure2.js` | main-session requests by class (work, text-only, notification-only, handoff-only and so on), counted once per message id across resumed and forked transcripts; long subagent runs; files read by several agents | `node tools/audit/measure2.js 2026-10-27T00:00:00Z` |
| `hook-timing.js` | hook duration p50/p90 and timeouts per period, for named and anonymous (`command hook N`) records | `node tools/audit/hook-timing.js 2026-10-27T00:00:00Z 2026-11-03 2026-11-09` (the dates are optional period boundaries; without them periods are 7 days) |
| `count_acceptance.js` | acceptance files per repository and worktree: closed, every non-human gate passed but not closed, unmet, unparseable | `node tools/audit/count_acceptance.js --list <dir-of-repos>` |
| `time_gate.py` | time spent in `session_files`, `last_edit` and `tree_digest` for a given transcript size | `python -B tools/audit/time_gate.py` |
| `time_sessionstart.py` | child processes and time per `SessionStart` source, profiled against a copy of the store | see the header of the file |

`time_sessionstart.py` refuses to run against the live store, because `SessionStart` writes to
it. Copy `config.json`, `cache/` and `handoffs/` to a temporary directory and point
`PROVOST_CONFIG` and `PROVOST_HANDOFF_HOME` at the copy.

Known limits: subagent transcripts are not deduplicated across files; hook records written
since Claude Code stopped storing the command string are attributed by registration order and
timeout value; `measure2.js` counts a handoff payload write as handoff work only when the file
name contains `handoff`, `payload`, `record`, `checkpoint` or `milestone`.
