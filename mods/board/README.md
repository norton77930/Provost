# board

A task board for Claude Code. It tracks the subagents the model dispatches, the model's own checklist (TaskCreate, TaskUpdate and TodoWrite) and the acceptance gates the session writes to `.claude/acceptance/*.json`. Everything shows in a collapsible `board` pane and on the status line, with a progress bar, elapsed times, an estimate for running agents once their role has three finished runs, and a mark on any human gate that is waiting for you. Type `/board` to close or reopen the pane.

The expanded pane in a real session:

![The expanded board pane: a progress bar with 3/5 steps, 2/4 gates and "needs you: merge", a Steps section with an implementer-sonnet dispatch under step 3, and a Gates section with tests, validate, live-new-reset and merge](docs/board-pane.png)

The same pane as text:

```
board  ███████░░░ 3/5 · gates 2/4 · needs you: merge   [Collapse]
── Steps ──────────────────────────────────────────────────────
 ✓ 1  Move board mod into Provost repo
 ✓ 2  Write acceptance file for board round 1
 ✓ 3  Implement board round-1 fixes
      ↳ implementer-sonnet · Board round-1 fixes · 19 min
 » 4  Point plugin dir at repo board and verify live
 · 5  Commit, push and open draft PR
── Gates · board round 1 ──────────────────────────────────────
 ✓ tests           plugin test 48 pass
 ✓ validate        plugin validate
 · live-new-reset  real session /new
 ! merge           needs your approval
```

## Install

```
/plugin marketplace add norton77930/Provost
/plugin install board@provost
```

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `language` | `en` | UI language: `en` (English) or `zh-TW` (繁體中文). Any other value shows English. |

To change a setting, run `/config`, or `/plugin` → Installed → board → Configure options. For example, set `language` to `zh-TW` to get the Traditional Chinese UI. If you load the plugin from a directory (`--plugin-dir` or `CLAUDE_CODE_PLUGIN_DIRS`), `/config` stores it in `settings.json` under `pluginConfigs` → `board@inline` → `options`.

## Development

```
claude plugin test mods/board
```
