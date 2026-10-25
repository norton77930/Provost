<p align="center">
  <img src="docs/logo.svg" width="64" height="64" alt="Provost logo">
</p>

<h1 align="center">Provost</h1>

<p align="center">
  <b>Classify every request&nbsp;&nbsp;|&nbsp;&nbsp;Cheaper models do the typing&nbsp;&nbsp;|&nbsp;&nbsp;Review only on risk&nbsp;&nbsp;|&nbsp;&nbsp;Decisions survive compaction&nbsp;&nbsp;|&nbsp;&nbsp;Measured on real work</b>
</p>

<p align="center">
  <img src="docs/demo/hero-en.png" width="100%"
    alt="Same work, different price. Left: plain Claude Code runs every step (edits, tests, self-review, browser, a context at 92%) on Opus, cost 100%. Right: with Provost a 12-file rename goes to mech-executor on Haiku, a button label stays in main, a UI form goes to implementer-sonnet with a live browser check, expired-token errors go to implementer-sonnet and code-reviewer, auth or a migration stops for you, and PreCompact/PostCompact keep decisions near the context limit; build cost −53–66%">
</p>

<p align="center">
  <b>Stop paying top-model prices for every step your agent takes.</b><br>
  An orchestration policy, eight subagent roles and a handoff runtime for Claude Code. The main session classifies each request, hands the typing to cheaper models, and keeps its decisions through compaction.
</p>

<p align="center">
  <a href="https://github.com/norton77930/Provost/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/norton77930/Provost/ci.yml?branch=main&style=flat-square&labelColor=16181d&label=CI" alt="CI"></a>
  <img src="https://img.shields.io/badge/works%20with-Claude%20Code-2ea44f?style=flat-square&labelColor=16181d" alt="Works with Claude Code">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f?style=flat-square&labelColor=16181d" alt="MIT license"></a>
</p>

<p align="center">
  <a href="#install"><b>Install</b></a> · <a href="#why-not-plain-claude-code">Why</a> · <a href="#how-it-works">How it works</a> · <a href="#evidence-so-far">Evidence</a> · <a href="README.zh-TW.md">繁體中文</a>
</p>

Once installed, work the way you always do. Before it acts, the main session declares how it will handle each request:

```
> Rename getUser to fetchUser in all 12 files
分類:機械/無風險/證據:測試 → mech-executor
> Add validation to the signup form
分類:中等/無風險/證據:實機(瀏覽器) → implementer-sonnet, then a live browser check
> Handle expired tokens in the API client
分類:中等/R1(錯誤處理)/證據:審查 → implementer-sonnet, then code-reviewer
```

The declaration is written in your language; the format is in [How it works](#how-it-works).

Here is the whole idea in 46 seconds:

https://github.com/user-attachments/assets/6e564d31-55b6-4271-a65d-1d8c1cebe413

<p align="center"><sub>46 seconds, English captions, music only. The Chinese version is in <a href="README.zh-TW.md">README.zh-TW.md</a>.</sub></p>

## Why not plain Claude Code?

You can. Plain Claude Code is a strong agent. What it does not do on its own is choose who does each step and what proves it is done, so every step runs on the main model and review happens when you remember to ask.

| When… | Plain Claude Code | With Provost |
| :--- | :--- | :--- |
| a rename touches 12 files | the main model edits each file itself | `mech-executor` on Haiku does the batch; the tests close it |
| a bounded feature is ready to build | the main model writes it at its own price | `implementer-sonnet` builds it: −53–66% build cost at equal review outcomes (34-slice trial) |
| a change is visible in the UI | done when the code looks right | closes with a live check (browser, window, popup or CLI), not a reviewer |
| a change touches error handling, data conversion or concurrency | reviewed only if you ask | `code-reviewer` runs in a fresh context; file count and diff size never trigger review |
| a change alters how auth, secrets or payments behave, or migrates data | proceeds like any other | R2: the session stops and waits for your decision |
| the context grows past 400K | each call costs about ×2.3 more, and compaction can drop decisions | compact early: PreCompact saves the decisions, PostCompact restores them (44 compactions, no re-explaining) |

Measured results and their limits are under [Evidence so far](#evidence-so-far).

## Install

Requirements: Claude Code, Python 3.11+, and on Windows Git Bash (the hooks are
bash scripts).

```bash
git clone https://github.com/norton77930/Provost
python Provost/install.py setup
```

Then restart Claude Code and confirm with
`bash ~/.claude/provost/runtime/native-pilot-v1/hooks/runtime doctor`.

<details>
<summary><b>What setup changes, backups and upgrades</b></summary>

`setup` does everything an install needs, and `--dry-run` shows it first:

- copies the policy, roles, skills and runtime into `~/.claude` (the same as
  `install`);
- appends the `<!-- claudex-native:begin -->` / `<!-- claudex-native:end -->`
  markers to `~/.claude/CLAUDE.md` when it has none (the policy only ever goes
  between them, and a malformed pair is refused);
- adds the eight hook registrations below to `~/.claude/settings.json` when they
  are missing. Everything else in the file is kept, it is re-serialized with a
  2-space indent, and a file that is not valid JSON is refused. No value from it
  is ever printed;
- writes `~/.claude/provost/config.json` with handoff on when it does not exist.

Every changed file is backed up first to
`~/.claude/provost/backups/install-<UTC time>/` with a `manifest.json`, and a
failed run rolls back. Running `setup` again changes nothing. The first time it
replaces a local `skills/handoff/SKILL.md` that differs, it also keeps that copy
beside it as `SKILL.md.local-<UTC time>.bak`. The backup of `settings.json` is a
full copy, credentials included, and backups are never pruned. The installer
honours `CLAUDE_CONFIG_DIR` and `--config-dir`; the hooks themselves read
`~/.claude/provost/config.json` unless `PROVOST_CONFIG` points elsewhere.

After changing anything in the repository, run `python install.py check` to see
what differs from the live setup (exit 0 = no drift, 1 = drift), and
`python install.py install` to apply it; `install` never touches `settings.json`.
After upgrading to the decision log, run `migrate` and review it before
`migrate --apply` and any `prune --apply` (see the guide's
[Install and rollout](docs/native-handoff.md#install-and-rollout)).

</details>

<details>
<summary><b>What setup registers, to do it by hand</b></summary>

Replace `<runtime>` with the absolute path of
`~/.claude/provost/runtime/native-pilot-v1`:

```json
"hooks": {
  "SessionStart": [{"matcher": "startup|clear|compact|resume|fork", "hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "timeout": 30}]}],
  "PreCompact": [{"matcher": "manual|auto", "hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "timeout": 30}]}],
  "PostCompact": [{"matcher": "manual|auto", "hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "timeout": 30}]}],
  "PreToolUse": [{"matcher": "Agent", "hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/acceptance-gate\"", "timeout": 30}]}],
  "PostToolUse": [{"matcher": "Edit|Write|NotebookEdit|Agent|Bash|PowerShell|EnterWorktree|ExitWorktree", "hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "async": true, "timeout": 20}]}],
  "UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "timeout": 10}]}],
  "Stop": [{"hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "async": true, "timeout": 20}]},
    {"hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/acceptance-gate\"", "timeout": 30}]}]
}
```

The `~/.claude/provost/config.json` it writes:

```json
{
  "schema_version": 1,
  "mode": "adaptive",
  "workspace_roots": [],
  "all_workspaces": true,
  "max_writers": 2,
  "burst_writers": 3,
  "handoff": true
}
```

`exclude_roots` (a list of directories) turns handoff off below them. See
[`docs/native-handoff.md`](docs/native-handoff.md) for storage, retention
and recovery.

</details>

## How it works

Provost is the source of truth for one Claude Code setup that is used every day.
The main session classifies each request by difficulty and risk, keeps simple
work for itself, hands bounded slices to cheaper implementer roles, and calls a
fresh-context reviewer only when a failure would be invisible to tests and to a
live run. A small runtime records decisions and dispatches so that work survives
context compaction and session restarts.

It is published as it is used: tuned for one person's work, Windows first, and
installed by copying files into the Claude config directory with a drift check.

Every non-trivial request starts with a one-line declaration:

```text
分類:<simple|moderate|mechanical|hard> / <no-risk|R1|R2> / evidence:<test|live|review|architecture> → <route>
```

- **Difficulty decides who implements.** Simple work stays in the main session.
  Moderate slices go to `implementer-sonnet` (settled method, clear acceptance)
  or `implementer` (design choices left to the worker; `effort: high` for
  concurrency, lifecycle or security logic). Fully specified repetition
  goes to `mech-executor`. Hard work needs an approved spec first.
- **Risk decides which evidence closes a claim.** Pick the cheapest evidence that
  would catch the failure: a focused test, a live run, or — only when the failure
  is invisible to both — a `code-reviewer` (plus `architecture-reviewer` when an
  exposed interface changes). File count, diff size and "looks complex" are
  explicitly not reasons to review.
- **R2 stops.** Auth, secrets, payments, migrations, breaking public APIs,
  cross-repo or irreversible operations wait for the user's decision, and are
  done only after review.
- **Writers are budgeted.** Normally at most two concurrent writers, three when
  the third is provably independent; ownership of files and shared resources is
  checked before every wave.
- **Completion is finite.** A Completion Contract lists the claims and their
  evidence; once every claim passes, only the planned final verification runs.

The full text is [`native/CLAUDE.native.md`](native/CLAUDE.native.md).

### Roles

| Role | Model | Effort | Tools | Used for |
|---|---|---|---|---|
| `Explore` | haiku | high | Read, Glob, Grep | Read-only reconnaissance |
| `mech-executor` | haiku | high | + Write, Edit, Bash | Fully specified mechanical batches |
| `test-analyst` | haiku | medium | Read, Glob, Grep, Bash | Running existing tests, regression analysis |
| `implementer-sonnet` | sonnet | high | + Write, Edit, Bash | Default implementer for settled, bounded slices (TDD) |
| `implementer` | opus | medium | + Write, Edit, Bash | Slices with real design choices; dispatched at `high` for hard ones |
| `general-purpose` | sonnet | high | all but Agent | Bounded non-code work no other role fits |
| `code-reviewer` | fable | high | Read, Glob, Grep | Risk-triggered fresh-context review of a declared claim |
| `acceptance-verifier` | sonnet | high | Read, Glob | Judging visual, live and threshold gates against their source; reports PASS, ISSUES or BLOCKED |
| `architecture-reviewer` | fable | high | Read, Glob, Grep | Review when an exposed interface or boundary changes |

Reviewers fall back to `opus` only when a run fails with an explicit Fable quota
error.

### Handoff runtime

The runtime keeps a sealed, per-session chain of handoff records (sources,
decisions, assignments, evidence, next step) under
`~/.claude/provost/handoffs`, shared by a checkout and its worktrees. Hooks
inject the latest checkpoint after compaction or resume, capture facts (edited
paths, dispatches, commits — never command text or file contents) and remind the
main session when a dispatch or edit has no record yet. Decisions and evidence go
to an append-only log per work, shared by every session of the repository.
Commands: `doctor`, `status`, `record`, `snapshot`, `list`, `resume`, `changes`,
`search`, `prune`, `migrate`, and the maintainer-only `redact`.
See [`docs/native-handoff.md`](docs/native-handoff.md).

### What is in the repository

| Path | What it is | Installed to (in `~/.claude`) |
|---|---|---|
| [`native/CLAUDE.native.md`](native/CLAUDE.native.md) | The orchestration policy | between the `claudex-native` markers in `CLAUDE.md` |
| [`agents/`](agents/) | Nine subagent roles | `agents/` |
| [`hooks/`](hooks/), [`scripts/provost_runtime/`](scripts/provost_runtime/) | Handoff runtime and its hooks | `provost/runtime/native-pilot-v1/` |
| [`skills/verification-before-completion/`](skills/verification-before-completion/) | Completion-claim discipline | `skills/` |
| [`skills/handoff/`](skills/handoff/) | The `/handoff` skill (save, resume, search) | `skills/handoff/` |
| [`tools/dispatch-report.js`](tools/dispatch-report.js) | Cost and outcome report of past dispatches, from transcripts | `experiments/` |
| [`install.py`](install.py) | Installer and read-only drift check | — |

Methodology skills (TDD, diagnosis, grilling, spec and tickets) come from the
[`mattpocock-skills`](https://github.com/mattpocock/skills) plugin, which the
policy refers to by name.

## Evidence so far

Single user, real work, not a controlled experiment:

- `implementer-sonnet` was trialled against `implementer` on 34 slices across 5
  repositories, including paired runs on the same brief, before becoming the
  default implementer (2026-10-04).
- Over two weeks (2026-09-28 to 10-12, 84 sessions) the main session wrote about
  3% of changed code lines; implementer roles wrote the rest across 270
  dispatches.
- `implementer-sonnet` cost 53–66% less than `implementer` on the trialled
  slices, with equal review outcomes.
- Cost per call rose about ×2.3 from a ≤200K-token context to 400–600K.
  Compacting earlier (`autoCompactWindow`) cut the main session's weekly cost by
  37% (7 days measured); the handoff hooks are what make early compaction safe, so the −37% is the
  setting plus the hooks, not the hooks alone.
- Across 44 compactions, no session needed the user to re-explain lost context.
- The hook mechanism itself cost 3.6% of total spend over a measured week.

## Tests

```bash
python -m unittest discover -s tests/adaptive -p "test_*.py"
```

CI runs the suite on Windows, Ubuntu and macOS, and checks Markdown links.

## History

Provost started as a three-tier governance framework (Tier 0 bare, Tier 1
collaborator, Tier 2 governed with hash-pinned manifests and a PowerShell write
gate), shipped as a Claude Code plugin. That design is archived at tag
[`archive/tier2-foreman`](https://github.com/norton77930/Provost/tree/archive/tier2-foreman)
and is no longer maintained.

## Known limitations

- Tuned for one user; model aliases (`fable`, `opus`, `sonnet`, `haiku`) resolve
  to whatever your Claude Code version maps them to.
- Hooks spawn bash and Python on every call; on Windows that costs roughly a
  second per hook.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Release notes are in [CHANGELOG.md](CHANGELOG.md); plans in [ROADMAP.md](ROADMAP.md). Ideas and bug reports go in [issues](https://github.com/norton77930/Provost/issues/new/choose). Security reports: [SECURITY.md](SECURITY.md).

## Star History

<a href="https://star-history.com/#norton77930/Provost&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=norton77930/Provost&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=norton77930/Provost&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=norton77930/Provost&type=Date" />
  </picture>
</a>

## License

[MIT](LICENSE). Copyright (c) 2026 Norton. Third-party skill attribution is in
[`skills/NOTICE.md`](skills/NOTICE.md).
