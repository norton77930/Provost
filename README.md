# Provost

**Scale the ceremony to the task.** — an orchestration policy, role pack and
handoff runtime for Claude Code.

*[中文說明 →](README.zh-TW.md)*

Provost is the source of truth for one Claude Code setup that is used every day.
The main session classifies each request by difficulty and risk, keeps simple
work for itself, hands bounded slices to cheaper implementer roles, and calls a
fresh-context reviewer only when a failure would be invisible to tests and to a
live run. A small runtime records decisions and dispatches so that work survives
context compaction and session restarts.

It is published as it is used: tuned for one person's work, Windows first, and
installed by copying files into the Claude config directory with a drift check.

## What is in the repository

| Path | What it is | Installed to (in `~/.claude`) |
|---|---|---|
| [`native/CLAUDE.native.md`](native/CLAUDE.native.md) | The orchestration policy | between the `claudex-native` markers in `CLAUDE.md` |
| [`agents/`](agents/) | Eight subagent roles | `agents/` |
| [`hooks/`](hooks/), [`scripts/provost_runtime/`](scripts/provost_runtime/) | Handoff runtime and its hooks | `provost/runtime/native-pilot-v1/` |
| [`skills/verification-before-completion/`](skills/verification-before-completion/) | Completion-claim discipline | `skills/` |
| [`skills/handoff/`](skills/handoff/) | Reference `/handoff` skill (not installed; keep your own) | — |
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
main session when a dispatch or edit has no record yet. Commands: `doctor`,
`status`, `record`, `snapshot`, `list`, `resume`, `changes`, `search`, `prune`.
See [`docs/native-handoff.md`](docs/native-handoff.md).

## Install

Requirements: Claude Code, Python 3.11+, and on Windows Git Bash (the hooks are
bash scripts).

1. Add the markers to your `~/.claude/CLAUDE.md` where the policy should go. The
   installer only ever replaces the text between them:

   ```markdown
   <!-- claudex-native:begin -->
   <!-- claudex-native:end -->
   ```

2. Preview, install, and check:

   ```bash
   python install.py check              # read-only; exit 0 = no drift, 1 = drift
   python install.py install --dry-run  # what would change
   python install.py install            # backs up every changed file first
   ```

   Backups go to `~/.claude/provost/backups/install-<UTC time>/` with a
   `manifest.json`; a failed install rolls back. `CLAUDE_CONFIG_DIR` and
   `--config-dir` are honoured.

3. Register the hooks in `~/.claude/settings.json` (the installer never edits
   it; `check` reports whether they are registered). Replace `<runtime>` with
   the absolute path of `~/.claude/provost/runtime/native-pilot-v1`:

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
       {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "timeout": 10}]}]
   }
   ```

4. Enable the runtime in `~/.claude/provost/config.json`, then confirm with
   `bash <runtime>/hooks/runtime doctor`:

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

After changing anything in the repository, run `python install.py check` to see
what differs from the live setup, and `install` to apply it.

## Evidence so far

Single user, real work, not a controlled experiment:

- `implementer-sonnet` was trialled against `implementer` on 34 slices across 5
  repositories, including paired runs on the same brief, before becoming the
  default implementer (2026-10-04).
- Over two weeks (2026-09-28 to 10-12, 84 sessions) the main session wrote about
  3% of changed code lines; implementer roles wrote the rest across 270
  dispatches.

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
