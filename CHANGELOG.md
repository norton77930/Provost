# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Coverage for `RecordRetry` and `RecoverLock`, the two lifecycle actions that
  no test had ever called. `RecordRetry` is checked for its running-task
  precondition, the ledger event it appends, the one-retry-per-task rule, and
  its session binding; `RecoverLock` for the `-Acknowledge` requirement, the
  collision-resistant archive name, and — the reason the check exists — that it
  still succeeds from a session other than the one that opened an enforced
  lock. That exemption is deliberate and nothing was holding it in place.

- An end-to-end governed session walkthrough. It opens a launcher session on a
  throwaway Git workspace, shows a `Write` outside the running task's
  `write_set` denied by the hook, and completes `PASS`. It is documentation of
  a path the existing twelve checks already cover in pieces; it does not add a
  thirteenth CI step.

- A running lock opened under enforcement can only be advanced by the session
  that opened it. `StartTask`, `FinishTask`, `RecordRetry`, and `Complete` now
  refuse when `CLAUDE_CODE_SESSION_ID` is not the `enforcement.session_id`
  recorded at `Initialize`. The helper `-SessionId` is agent-supplied and does
  not recover the refusal. A lock opened with `mode = 'none'` is unchanged.
  `RecoverLock` stays operator-driven and does not go through this check.

- An enforcement record on every run. `Initialize` writes whether the run
  opened under hook enforcement — and the session that proved it — into the
  lock, the `run_initialized` ledger event, and the terminal handoff receipt.
  An enforced run then refuses to adopt continuation work from a run recorded
  as unenforced, or from a receipt carrying no record at all. The adoption path
  had no test of any kind before this; it now has both directions. The record
  is open-time truth: it says the hooks were live when the run began, not that
  every write in it was policed.

- Coverage for the producer half of the liveness contract. The `SessionStart`
  hook is now driven for real rather than the marker being hand-written, so a
  change to its path or payload handling fails the suite instead of silently
  refusing every session. A companion check asserts the launcher sets every
  `PROVOST_*` variable the hooks read.

- `Start-GovernedSession.ps1`, a launcher that opens a governed session. It sets
  the `PROVOST_*` environment the hooks read, registers the Tier 2 hooks for
  that session only through `claude --settings`, and refuses when they could not
  enforce — a workspace that is not a Git repository, a hook that is missing or
  does not parse, or no PowerShell interpreter at all. It restores the caller's
  environment and location when the session ends, so a shell is not left marked
  governed.
- A session liveness marker. The `SessionStart` hook records the session it ran
  in, and `Initialize` refuses to open a governed run unless a marker names that
  same session. A hook whose interpreter cannot be found blocks nothing and
  reports nothing, so without this a governed run could proceed with no write
  gate and no warning. The marker is evidence that the hooks loaded, not a
  credential: it is a plain file in the workspace, so anything that can write
  the workspace can write it.
- `Test-SessionLiveness.ps1`, covering the refusal, a marker left by another
  session, and the accepting case, with CI coverage.
- Ref-guard coverage for several declared read roots, and a check tying the
  separator the launcher joins them with to the one the hook splits on. A
  mismatch there left the guard silent on every command.

### Changed

- The launcher records what `claude --settings` actually does with hooks,
  because the liveness proof depends on it. Measured: hooks supplied that way
  are added to a project's own rather than replacing them, and
  `disableAllHooks` suppresses everything including `SessionStart`, so the
  marker is absent and `Initialize` refuses. No combination leaves the marker
  present while the write gate is gone.

- The Tier 2 hooks are registered per launch rather than by the plugin. Each
  costs a PowerShell process on every matching tool call — around two seconds on
  a normal Windows machine — and the plugin installs for everyone, including the
  majority who never open a governed session.
- Documentation no longer says the launcher is absent. What remains missing is
  narrower: a packaged installer, and the fresh per-task agent contexts the
  original launcher created.

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
