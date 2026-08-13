# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/norton77930/Provost/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/norton77930/Provost/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/norton77930/Provost/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/norton77930/Provost/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/norton77930/Provost/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/norton77930/Provost/releases/tag/v0.1.0
