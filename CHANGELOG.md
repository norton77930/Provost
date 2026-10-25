# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The repository is now the source of truth for the setup in daily use: the
orchestration policy, its roles and the handoff runtime, installed by copying
into the Claude config directory. The graduated-governance design (Tier 0/1/2)
and the plugin distribution are archived at tag `archive/tier2-foreman`.

### Added

- Acceptance gates, first slice (#44): `hooks/acceptance-gate`, registered for
  `Stop` as a synchronous hook, blocks the main turn end while an acceptance file
  the session wrote (`<cwd>/.claude/acceptance/*.json`, modified at or after the
  session's first transcript timestamp) has a non-`human` gate `pending` or `fail`,
  or is malformed; the reason lists every unmet gate id and the file path. Without
  an acceptance directory it exits before starting Python; any internal error ends
  the turn. The schema and settings entry are in the operations guide;
  `install.py setup` registers the hook and `check` reports `not-registered`
  without a synchronous registration.
- Acceptance gates, exemptions and push budget (#45): the acceptance `Stop` check
  ends the turn with no decision for a subagent's `Stop` (`agent_id`), while
  `background_tasks` is non-empty, and when the answer has a `needs input:` or
  `failed:` line (bullet, quote or bold prefix allowed). It blocks at most three
  times per episode (the reason says `push 1/3`); a `Stop` with no unmet gate or
  the person's next prompt (`hooks/native-facts`, `UserPromptSubmit`, also with
  handoff off) starts a new episode. The per-session count lives in
  `<provost dir>/cache/acceptance/<session_id>.pushes`; a counter error never
  blocks.
- Acceptance gates, stale evidence (#46): a `machine`, `live` or `visual` gate that
  passed before the session's last main-session edit (read from the transcript) is
  stale and holds the turn end until it is re-run.
- Acceptance gates, dispatch reminder (#47): `hooks/acceptance-gate` is also
  registered for `PreToolUse` with matcher `Agent` (synchronous). When the main
  session dispatches `implementer`, `implementer-sonnet` or `mech-executor` while
  the session has an acceptance file and the brief does not mention any session
  file's name (so `.claude/acceptance/<name>` and absolute paths count), it returns
  additional context asking to cite the file and, for visual gates, the fixture
  data and screenshot output directory. Advisory only: never a deny; other roles,
  a cited file, subagent dispatches, no session file or any error return nothing.
  `install.py setup` registers it and `check` reports `not-registered` without it.
- Acceptance gates review fixes: the block reason states the current UTC time and
  `ranAt` must be that clock with a zone; a passed gate whose `ranAt` has no time
  zone is stale once the session has edited; an unreadable edit history no longer
  drops the check, so pending and failed gates still hold the turn; the policy
  says a `needs input:` or `failed:` line must be on its own line.
- Acceptance gates clock and guide path: a passed `machine`, `live` or `visual`
  gate whose zoned `ranAt` is more than 5 minutes after the current UTC time is
  stale even without an edit (Windows PowerShell's `date -u` prints local time
  labelled `Z`); the block reason gives both the Bash and the PowerShell clock
  commands and the absolute path of the operations guide, computed from the
  runtime root so it resolves in the installed runtime. The policy and
  `acceptance-verifier` point at `~/.claude/provost/runtime/native-pilot-v1/docs/native-handoff.md`,
  and the policy sends the judging of captured `visual`, `live` and threshold
  evidence, not its capture, to `acceptance-verifier`.
- `python install.py setup`: a one-step install that also appends the
  `claudex-native` markers to `CLAUDE.md` when there are none, adds the missing
  hook registrations to `settings.json` (other values kept, invalid JSON
  refused, nothing printed from it) and writes a default `provost/config.json`,
  all under the same backup and rollback as `install`. The README's manual hook
  list now includes the `Stop` registration it was missing.
- Native handoff runtime (`scripts/provost_runtime/`, `hooks/native-event`,
  `hooks/native-facts`): sealed per-session record chains shared by a checkout
  and its worktrees, compaction recovery, facts capture and reminders, decision
  search, retention and prune.
- `install.py`: installs the runtime, roles, policy section, skill and tool into
  the Claude config directory with backups, a manifest and rollback; `check` is
  a read-only drift report that also says whether the hooks are registered.
- `native/CLAUDE.native.md`: the orchestration policy (difficulty × risk
  classification, evidence-based review, R2 stop, writer budget, Completion
  Contract), installed between markers in `CLAUDE.md`.
- Eight roles in `agents/`: `Explore`, `mech-executor`, `test-analyst`,
  `implementer-sonnet`, `implementer`, `general-purpose`, `code-reviewer`,
  `architecture-reviewer`.
- `tools/dispatch-report.js`: cost and outcome report of past dispatches.
- CI for the runtime and installer on Windows, Ubuntu and macOS, and a Markdown
  link check.
- Policy §11 Compact instructions: compaction summaries also separate verified
  from unverified work, list what is still running or uncommitted, and carry
  open questions with their options. A 2026-10-13 baseline of 44 compactions
  found no context-loss corrections; these were the only gaps against
  ctx-handoff-mod's handoff checklist. Summaries also keep attribution (hook
  output and injected context are never written up as user requests) and copy
  forbidding and secret rules word for word, after ctx-handoff-mod 0.7.0.
- Declaration reminder: an async `Stop` hook (`hooks/native-event`) scans the
  transcript bytes added since its last scan for the policy §1 `分類:` line and
  for main-session repo edits; when files were edited before any declaration,
  the next prompt's `native-facts` context asks for it, once per window (until a
  `record` with phase `done`). `install.py check` now also requires the `Stop`
  registration.
- Policy §6 task list: an approved plan of three or more steps is recorded with
  `TaskCreate` before the first step starts, and each step is set
  `in_progress` and `completed` with `TaskUpdate` as work moves, so the whole
  plan and its progress stay visible (a task-board mod reads this list).

### Changed

- §3 review triggers, after an audit of 86 `code-reviewer` dispatches (92% had a
  real trigger; the misfires were live-verifiable windows, R2 labels on
  behavior-preserving work, and reviews dispatched under a stale declaration):
  a UI change whose failure shows on any screen you can drive (browser, native
  window, popup, CLI) closes with live verification; auth, secrets and crypto are
  R2 only when the diff changes how they decide, persist or protect; every
  reviewer dispatch names its row or R2 trigger; a code deletion that build,
  typecheck and grep can verify joins the negative list.
- `install.py` writes a managed file in place when replacing it is denied (seen
  on Windows for `~/.claude/agents/*.md`); the write is still backed up first and
  rolled back like any other.
- `general-purpose` runs on Sonnet at high effort (was Opus at medium). Its
  work is research, spikes, document comparison and proofreading; output is
  about 7% of its cost, so the saving comes from the model price and high
  effort keeps the checking depth. It matches the `implementer-sonnet` setup
  that passed its trial.
- Policy §10 routing: a plain `implementer` dispatch names the design choice it
  leaves open, otherwise the slice is `implementer-sonnet`'s, and fixed-content
  briefs (exact-spec UI, listed review fixes, selector or harness updates,
  supplied docs) are named as Sonnet work. The `effort: high` triggers are
  narrowed: lifecycle excludes plain error handling, security means changing how
  auth, approval, secrets or crypto behave (not renames nearby), an open root
  cause needs a known defect, and "significant implementation tradeoffs" is
  dropped. The dispatched role must match the declaration. An audit of 135 Opus
  dispatches found 34 of 57 plain `implementer` slices fit Sonnet.
- Prompt audit follow-ups: the handoff skill takes the runtime and operations
  guide paths from the SessionStart `Runtime:` and `Operations guide:` lines
  instead of a plugin-era `<plugin-root>` placeholder; policy §1 cadence lets
  the turn-end `PushNotification` come before the one-sentence plan; the
  implementer roles say "Add tests" where "Commit tests" read like a git commit.
- `code-reviewer` tags each finding `Important`, `Nit` (at most three) or
  `Pre-existing`, opens its report with the count per tag, cites the file and
  line read for each defect, reads a root `REVIEW.md` when present, and on a
  re-review covers only the fix diff without repeating findings. Policy §3: only
  `Important` findings reopen work, and after two fix-and-review rounds with
  `Important` findings left, main reconsiders the approach and reports to the
  user instead of a third round. Policy §10: a defect escaping work that
  `code-reviewer` passed is reported with the reviewed commit, as a future
  reviewer eval case. Added this repository's `REVIEW.md` and a "How to revert"
  section in the PR template. Follows the Claude Code team's code review
  write-up (review convergence, finding tags, `REVIEW.md`).
- Policy §5 risk escalation agrees with §3: R1 found mid-task is finished and
  then reviewed, as when declared up front; only R2 found mid-task stops for the
  user. The old rule stopped on any risk increase, which contradicted §3's R1.
- Policy follow-ups from a prompt audit: the Claudex bridge session scope and the
  `claudex-foreman` suggestion are gone, since the bridge is no longer used; the
  §6 mockup step applies before any spec, including a moderate task's Completion
  Contract; discussion uses `/grill-with-docs` in a repo and `/grill-me` outside
  one; §7 states what `/implement-spec` authorizes (its integration branch,
  worktrees and draft PR, not a merge into the default branch); §10 drops
  "pilot" from its heading.
- Policy §6 covers UI work without a design mockup: each state becomes a `live`
  gate judged against the spec or issue, never a `visual` gate with nothing to
  compare against. When the look matters, a mockup is settled after `grilling`
  and before `/to-spec` with `prototype`, and a screenshot per state is saved as
  the gate `source`.
- Handoff records are state cards per (session, work_id) (#18): `record` checks
  `--expected` against, appends to and trims (newest 5) only the chain of the
  payload's work_id, so recording one work no longer overwrites another's. The
  session's current card is its most recently written one; `status --work
  <work_id>` reads another work's card and `status` lists the session's
  `other_works`. Single-work sessions behave as before, and existing single-chain
  sessions are read without migration (the chain belongs to its head's work_id).
- Decisions and evidence move to an append-only decision log per work_id (#19),
  shared by a checkout and its worktrees at `<repo dir>/logs/`. `record` takes
  `new_decisions` / `new_evidence` (only what is new), appends them under a
  per-work lock with runtime-assigned ids (`D1…`, `E1…`) and returns them as
  `log_entries`; a replayed request appends nothing twice, and a torn last line
  is ignored on read and cut by the next append. **Record schema 2:** cards no
  longer carry `decisions` / `evidence`, and requests using those fields are
  refused (`LEGACY_FIELDS`, with a `detail` naming the new fields). Schema 1
  cards stay readable. `status` adds `log` (the newest 10 entries of the shown
  work) and its template has the new fields.
- The decision log is complete (#20–#29, spec #17). **Schema 2** splits a record
  into a state card per (session, work_id) and the per-work decision log.
  `record` rejects broken entries, returns soft `warnings` and an `echo` of the
  work, and links corrections with `supersedes` / `updates`; `record --join`
  joins another session's work; phase `done` closes a work. Snapshots hold every
  open card, and `resume --work <work_id>` (repeatable) picks works, with
  `WORK_CHOICE_REQUIRED` when several are open. Compact and resume inject the
  card, open evidence and newest decisions; startup, clear and fork list
  joinable works. `search` covers the logs (`--work`, `--kind`, `--show` with the
  chain). New commands: `migrate [--apply]` imports schema 1 decisions and
  evidence, and the maintainer-only `redact` (deny it to the model with a
  permission rule). `prune` lists `idle_logs` and keeps sessions with
  `UNMIGRATED_DECISIONS`. **`prune` no longer writes the archive**; existing
  archive files stay searchable. `install.py` now installs the `/handoff` skill,
  keeping a differing local copy beside it before the first overwrite. Rollout
  order: merge → `install.py install` → `migrate` dry run reviewed by the user
  → `migrate --apply` → only then `prune --apply`. Rollback limits are in the
  guide under [Migrate](docs/native-handoff.md#migrate) and
  [Redact](docs/native-handoff.md#redact).
- Routing for the rarely dispatched roles (1.0% and 0.4% of 838 dispatches,
  2026-09-06..10-17). Policy §2: same-shaped edits across 5 or more files are
  機械, not 簡單, and main doing them needs a named blocker; 機械 also covers
  wording/version changes and docs or tests whose content the brief supplies.
  §3/§4: the planned final verification goes to `test-analyst` when the suite is
  slow or noisy (e2e/Playwright, full multi-language suites), which is not
  double-checking. The `mech-executor` and `test-analyst` descriptions say the
  same. SessionStart warns when `CLAUDE_CODE_SUBAGENT_MODEL` is set, since it
  overrides every role's model frontmatter (idea from pilotfish).

### Fixed

- Hooks run on macOS `/bin/bash` 3.2.
- `facts.json` never exceeds its size cap when the omitted counters grow.
- An interpreter inside the project directory is no longer cached when the
  directory is reached through a symlink, junction or 8.3 short name.
- `python -m` no longer imports modules from the project directory in hooks.
- The interpreter's inside-the-project check restores the working directory,
  `PWD` and `OLDPWD` even when changing back fails, and reads a parent
  directory named `-` literally instead of jumping to `OLDPWD`.
- `install.py` rolls back on an interrupt (not only on write errors) and names the backup dir, says
  nothing changed when the backup step fails, hints at BOM or stray whitespace on policy markers,
  and rejects `check --dry-run`.
- `prune --apply` holds the session lock while removing an expired session, so
  a concurrent record can no longer leave a broken chain; a busy or recreated
  session is skipped or reported instead of deleted.
- `install.py` refuses an empty or whitespace-only policy source instead of
  emptying the installed section, and an interrupt right after a target is
  replaced still rolls that target back; `prune --apply` lists a session kept
  as `skipped` (for example `STORE_BUSY`) only there, no longer also under `expired`.
- `install.py` also refuses a policy source holding only a BOM or Unicode
  whitespace (such as NBSP); `prune --apply` lists a session kept by an OS error
  only under `errors`, no longer also under `expired`.
- `hooks/native-facts` handles a 500 KB Write in about 2 s instead of over
  60 s: it finds the end of `tool_input` by binary search over linear glob
  tests instead of a quadratic bash suffix strip.

### Removed

- Tier 2 governance reference implementation, its launcher, hooks, tests and
  demos (`docs/governance/`, `tests/governance/`, `docs/examples/`).
- Tier 1 orchestration policy (`orchestration.md`), the adaptive plugin policy
  (`policies/`), `docs/concepts.md`, and the plugin manifest and hooks
  (`.claude-plugin/`, `hooks/hooks.json`, `hooks/session-start`,
  `hooks/handoff-event`, `hooks/native-reminder`).
- The runtime's plugin-mode path: the non-native SessionStart policy injection
  and legacy fallback, the `hook` CLI entry (`native-hook` remains), and the
  `native=` switch of `handle_event`. `runtime_hash` no longer lists
  `hooks/native-reminder`, so its value changes.
- The previous seven roles and the `tdd`, `diagnosing-bugs`, `grilling`,
  `grill-me` and `two-axis-review` skills; methodology now comes from the
  `mattpocock-skills` plugin.
- Tier 2 additions previously listed here as unreleased (lifecycle-action
  coverage, governed-session walkthrough, launcher, session liveness and
  enforcement records) were never released; they are preserved at
  `archive/tier2-foreman`.
- `legacy.py`, the read-only bridge to the pre-relocation
  `<workspace>/.provost/handoffs/v1` stores: `status`, compact/resume recovery,
  `record`/`snapshot` (legacy binding), `resume` and `doctor` no longer read
  them; the `list --legacy` flag and the `legacy`, `legacy_path`,
  `legacy_binding` and `legacy_store_present` output fields are gone.

## [0.2.0] - 2026-09-02

### Changed

- **Breaking:** the collaborator role definitions moved from `.claude/agents/`
  to `agents/` so that the repository is installable as a Claude Code plugin.
  A copy step that referenced the old path must be updated, or replaced by
  installing the plugin.
- The PowerShell syntax check now also covers `skills/`, which had left
  `hitl-loop.template.ps1` unchecked.
- CI pins `actions/checkout` to a commit SHA, limits push runs to `main`, and
  cancels superseded runs.
- Temporary-workspace cleanup in the governance test helper retries briefly
  instead of failing the run. An external scanner can still hold a just-written
  ledger file when the whole suite runs back to back, which reported a run
  whose assertions all passed as a failure.

### Added

- A Linux CI job that runs the `SessionStart` hook under Linux bash and parses
  the plugin manifests, so the claim that the collaborator tier runs wherever
  Claude Code runs is backed by a check rather than asserted.
- A Claude Code plugin manifest (`.claude-plugin/plugin.json`) and a marketplace
  entry (`.claude-plugin/marketplace.json`), so the collaborator tier can be
  installed rather than copied directory by directory.
- A `SessionStart` hook that supplies `orchestration.md` to the session, which
  removes the manual merge into the host project's `CLAUDE.md`. Tier 2 hooks are
  deliberately not registered by the plugin; they remain opt-in.
- A role-definition check (`tests/roles/Test-RoleDefinitions.ps1`) that validates
  the shipped `agents/` frontmatter and fails when the role table in
  `README.md` or `README.zh-TW.md` drifts from it, with CI coverage.
- `SECURITY.md` with a private reporting channel, an explicit scope, and a
  statement of what Tier 2 does not claim to defend against.
- A Dependabot configuration for GitHub Actions updates.

### Fixed

- Closing two locks inside the same second overwrote the first lock archive.
  The archive file was named from a second-resolution timestamp alone and
  `Copy-Item` overwrites silently, so the lost archive took the trust record
  for a handoff receipt still on disk with it, and the next `Initialize`
  reported that genuine receipt as tampering. Lock archives now carry a random
  suffix and refuse to overwrite an existing file. This is what had failed CI
  on every push since 0.1.4.

## [0.1.4] - 2026-08-13

Failure-diagnosis and audit-artifacts coverage since 0.1.3.

### Added

- A Windows helper test for v2 failure diagnosis (unsigned FAIL rejected,
  signature persistence, hash normalization, diagnosis brake, reused
  diagnosis rejected), with a walkthrough and CI coverage. Requires `git`.
- A Windows helper test for audit artifacts (`run_initialized` ledger event,
  terminal receipt hash pins, later Initialize rejects a tampered ledger),
  with a walkthrough and CI coverage. Requires `git`.

## [0.1.3] - 2026-08-13

Completion-gate coverage and shared lifecycle test helpers since 0.1.2.

### Added

- A Windows helper test for the completion gate (`Complete PASS` rejected
  until every task is PASS, then the active lock is removed), with a
  walkthrough and CI coverage. Requires `git`.

### Changed

- Shared Foreman lifecycle test helpers live in
  `tests/governance/ForemanTestHelpers.ps1`. Manifest-pin, path-custody, and
  completion-gate tests now dot-source that file. Assertions are unchanged.

## [0.1.2] - 2026-08-13

Public v2 shared-path custody coverage since 0.1.1.

### Added

- A Windows helper test for v2 shared-path custody (unordered writers
  rejected, FinishTask records custody, StartTask transfers it, and
  content drift before the next writer escalates), with a walkthrough
  and CI coverage. Requires `git`.

## [0.1.1] - 2026-08-13

Public tests and maintainer docs since the first release.

### Added

- Install steps for copying methodology skills into a Claude Code
  `.claude/skills/` directory.
- A pin-and-select policy for vendored skills in
  [`CONTRIBUTING.md`](CONTRIBUTING.md), with the recorded snapshot remaining
  in [`skills/NOTICE.md`](skills/NOTICE.md). Adding or removing a skill
  directory also requires updating the install lists in both READMEs.
- A dependency-free Windows test for the Tier 2 ref-guard decision
  logic, with a walkthrough and CI coverage.
- A Windows helper test for v1 manifest hash pinning (Initialize,
  Validate, tampered Plan, tampered approved manifest), with a
  walkthrough and CI coverage. Requires `git`.

## [0.1.0] - 2026-08-11

First public release. There is no earlier published version; the notes below
describe what that tag shipped, including how the extract differed from the
private source tree.

### Added

- Graduated-governance model (tiers 0/1/2), bilingual README, and the
  concepts guide with a mermaid decision rule.
- Six Claude Code collaborator roles and the Tier 1 orchestration policy.
- Methodology skills for TDD, diagnosis, review, grilling, and
  verification-before-completion, with third-party attribution.
- Windows/PowerShell Tier 2 reference helper and hook scripts, plus a
  dependency-free write-scope decision test and walkthrough.
- Governance capability matrix, contributing guide, issue/PR templates,
  and Windows CI for syntax, write-scope, and relative Markdown links.
- Governance-led README framing. Role prompts do not default to a
  GPT-5.6-era 4k-token report cap; that guidance lives in field notes.
- Public tree without the leftover Grok provider adapter from the private
  multi-provider setup.

[Unreleased]: https://github.com/norton77930/Provost/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/norton77930/Provost/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/norton77930/Provost/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/norton77930/Provost/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/norton77930/Provost/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/norton77930/Provost/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/norton77930/Provost/releases/tag/v0.1.0
