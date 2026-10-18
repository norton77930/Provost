# Provost

**Scale the ceremony to the task.** — an orchestration policy, role pack and
handoff runtime for Claude Code.

*[中文說明 →](README.zh-TW.md)*

<!-- promo video (en, 46 s): paste the GitHub user-attachments URL on the next line -->

Provost is the source of truth for one Claude Code setup that is used every day.
The main session classifies each request by difficulty and risk, keeps simple
work for itself, hands bounded slices to cheaper implementer roles, and calls a
fresh-context reviewer only when a failure would be invisible to tests and to a
live run. A small runtime records decisions and dispatches so that work survives
context compaction and session restarts.

It is published as it is used: tuned for one person's work, Windows first, and
installed by copying files into the Claude config directory with a drift check.

## Why

- **The problem:** a main agent that does everything itself — edits, test runs,
  self-review, browser checks — pays the top model's price for every step.
- **① Classify, then route and verify.** Simple work stays in the main session;
  mechanical batches go to `mech-executor` on Haiku; a UI change closes with a
  live browser check; a reviewer is dispatched only when the risk calls for one
  (e.g. error-handling paths go to `code-reviewer`).
- **② Hooks: compact early, keep decisions.** Cost per call grows about ×2.3
  between a ≤200K and a 400–600K context. PreCompact saves the decisions,
  PostCompact restores them, so compacting early is safe.
- **Result (one user, real work):** build cost −53–66% with
  `implementer-sonnet` at equal quality (34-slice trial); main-session weekly
  cost −37% before/after and the hooks themselves at 3.6% of total spend
  (7 days measured).

## Quick start

```bash
git clone https://github.com/norton77930/Provost
python Provost/install.py setup
```

Requirements, what `setup` changes and how to do it by hand are under
[Install](#install).

## What is in the repository

| Path | What it is | Installed to (in `~/.claude`) |
|---|---|---|
| [`native/CLAUDE.native.md`](native/CLAUDE.native.md) | The orchestration policy | between the `claudex-native` markers in `CLAUDE.md` |
| [`agents/`](agents/) | Eight subagent roles | `agents/` |
| [`hooks/`](hooks/), [`scripts/provost_runtime/`](scripts/provost_runtime/) | Handoff runtime and its hooks | `provost/runtime/native-pilot-v1/` |
| [`skills/verification-before-completion/`](skills/verification-before-completion/) | Completion-claim discipline | `skills/` |
| [`skills/handoff/`](skills/handoff/) | The `/handoff` skill (save, resume, search) | `skills/handoff/` |
| [`tools/dispatch-report.js`](tools/dispatch-report.js) | Cost and outcome report of past dispatches, from transcripts | `experiments/` |
| [`install.py`](install.py) | Installer and read-only drift check | — |

Methodology skills (TDD, diagnosis, grilling, spec and tickets) come from the
[`mattpocock-skills`](https://github.com/mattpocock/skills) plugin, which the
policy refers to by name.

## How the policy works

Every non-trivial request starts with a one-line declaration:

```text
分類:<simple|moderate|mechanical|hard> / <no-risk|R1|R2> / evidence:<test|live|review|architecture> → <route>
```

- **Difficulty decides who implements.** Simple work stays in the main session.
  Moderate slices go to `implementer-sonnet` (settled method, clear acceptance)
  or `implementer` (design choices left to the worker; `effort: high` for
  concurrency, lifecycle or security-adjacent code). Fully specified repetition
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

## Roles

| Role | Model | Effort | Tools | Used for |
|---|---|---|---|---|
| `Explore` | haiku | high | Read, Glob, Grep | Read-only reconnaissance |
| `mech-executor` | haiku | high | + Write, Edit, Bash | Fully specified mechanical batches |
| `test-analyst` | haiku | medium | Read, Glob, Grep, Bash | Running existing tests, regression analysis |
| `implementer-sonnet` | sonnet | high | + Write, Edit, Bash | Default implementer for settled, bounded slices (TDD) |
| `implementer` | opus | medium | + Write, Edit, Bash | Slices with real design choices; dispatched at `high` for hard ones |
| `general-purpose` | opus | medium | all but Agent | Bounded non-code work no other role fits |
| `code-reviewer` | fable | high | Read, Glob, Grep | Risk-triggered fresh-context review of a declared claim |
| `architecture-reviewer` | fable | high | Read, Glob, Grep | Review when an exposed interface or boundary changes |

Reviewers fall back to `opus` only when a run fails with an explicit Fable quota
error.

## Handoff runtime

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

## Install

Requirements: Claude Code, Python 3.11+, and on Windows Git Bash (the hooks are
bash scripts).

```bash
git clone https://github.com/norton77930/Provost
python Provost/install.py setup
```

Then restart Claude Code and confirm with
`bash ~/.claude/provost/runtime/native-pilot-v1/hooks/runtime doctor`.

`setup` does everything an install needs, and `--dry-run` shows it first:

- copies the policy, roles, skills and runtime into `~/.claude` (the same as
  `install`);
- appends the `<!-- claudex-native:begin -->` / `<!-- claudex-native:end -->`
  markers to `~/.claude/CLAUDE.md` when it has none (the policy only ever goes
  between them, and a malformed pair is refused);
- adds the six hook registrations below to `~/.claude/settings.json` when they
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
  "PostToolUse": [{"matcher": "Edit|Write|NotebookEdit|Agent|Bash|PowerShell|EnterWorktree|ExitWorktree", "hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "async": true, "timeout": 20}]}],
  "UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "timeout": 10}]}],
  "Stop": [{"hooks": [
    {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "async": true, "timeout": 20}]}]
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

## License

[MIT](LICENSE). Third-party skill attribution is in
[`skills/NOTICE.md`](skills/NOTICE.md).
