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
| `measure2.js` | main-session requests by class (work, text-only, notification-only, handoff-only and so on), counted once per message id across resumed and forked transcripts; main-session turns (`turn_duration` records, counted once per `uuid`); long subagent runs; files read by several agents | `node tools/audit/measure2.js 2026-10-27T00:00:00Z` (optional environment variables below) |
| `hook-timing.js` | hook duration p50/p90 and timeouts per period, for named and anonymous (`command hook N`) records | `node tools/audit/hook-timing.js 2026-10-27T00:00:00Z 2026-11-03 2026-11-09` (the dates are optional period boundaries; without them periods are 7 days) |
| `count_acceptance.js` | acceptance files per repository and worktree: `closed`; `waitingHuman` (every non-human gate passes, a human gate is not `pass`: waiting on the owner); `allDone` (every gate passes, human gates included, not closed); `unmet`; unparseable. A leading UTF-8 BOM is ignored, as the Stop gate does | `node tools/audit/count_acceptance.js --list <dir-of-repos>` |
| `count_verified.js` | agent commits (a `Claude-Session:` or `Co-Authored-By: Claude` line) with a `Verified:` trailer per kind, `none` with or without a reason, malformed and missing; it reads `git log --all`, so a squash-merged PR counts once for its branch commits and once for the merge commit; it reads commit messages and prints subjects with `--list`, so read the output before sharing | `node tools/audit/count_verified.js --since 2026-11-18 --list <repo>` |
| `time_gate.py` | time spent in `session_files`, `last_edit` and `tree_digest` for a given transcript size | `python -B tools/audit/time_gate.py` |
| `time_sessionstart.py` | child processes and time per `SessionStart` source, profiled against a copy of the store | see the header of the file |

`measure2.js` environment variables, all off by default (without them the class lines are what
the script always printed, plus one summary line):

| Variable | Effect |
|---|---|
| `UNTIL=<ISO>` | skip records (requests, compactions and turns) with a timestamp after it; the first argument is the lower bound |
| `SFROM=<ISO>` `SUNTIL=<ISO>` | count only main transcripts whose first timestamp is in `[SFROM, SUNTIL)`; the others are skipped and shown as skipped sessions |
| `NOSUB=1` | skip subagent transcripts (faster; the subagent sections then print nothing) |

After the `TOTAL` line it prints one line: sessions kept and skipped, turns, push-only
requests, push-only divided by turns (3 decimals), and the ceremony count and share (every
class except `work` and `text-only`). Example, the sessions started 2026-11-13T01:45Z to
2026-11-16T03:02:50Z with records cut at the end of that window:

```
NOSUB=1 SFROM=2026-11-13T01:45:00Z SUNTIL=2026-11-16T03:02:50Z UNTIL=2026-11-16T03:02:50Z node tools/audit/measure2.js 2026-11-13T01:45:00Z
```

Compare the push-only / turn ratio only with numbers computed by the same method. The audit's
0.53 divided by turn counts from `measure.js`, which counts a resumed transcript's turns twice
(3,154 against 2,900 deduplicated). On this method the baseline is 0.587 (1,702 / 2,900:
`NOSUB=1 UNTIL=2026-11-12T12:00:00Z node tools/audit/measure2.js 2026-10-27T00:00:00Z`).

`time_sessionstart.py` refuses to run against the live store, because `SessionStart` writes to
it. Copy `config.json`, `cache/` and `handoffs/` to a temporary directory and point
`PROVOST_CONFIG` and `PROVOST_HANDOFF_HOME` at the copy.

Known limits: subagent transcripts are not deduplicated across files; hook records written
since Claude Code stopped storing the command string are attributed by registration order and
timeout value; `measure2.js` counts a handoff payload write as handoff work only when the file
name contains `handoff`, `payload`, `record`, `checkpoint` or `milestone`.
