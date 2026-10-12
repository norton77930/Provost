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

- Native handoff runtime (`scripts/provost_runtime/`, `hooks/native-event`,
  `hooks/native-facts`): sealed per-session record chains shared by a checkout
  and its worktrees, compaction recovery, facts capture and reminders, decision
  search, retention, and prune that archives the newest record per work item.
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
  ctx-handoff-mod's handoff checklist.

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
