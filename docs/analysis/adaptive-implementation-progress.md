# Implementation ledger

Plan: docs/superpowers/plans/2026-09-21-adaptive-orchestration-handoff.md.
User authorized implementation. Existing design/plan files preserved.

## Rulings and boundaries

- Current checkout, one writer, no commits/worktrees. Keep this ledger instead of
  skill scripts that assume commits or delete managed scratch state. Cost: changes
  remain uncommitted and must be reviewed/applied by the user.
- CLI cannot infer transcript identity: SessionStart binds workspace/session and
  optional cursor metadata. All session-specific manual writes require that binding.
  Missing transcript metadata is explicitly unknown, never proof of freshness.
- Indexes are hints, complete immutable objects are authoritative. Reconcile the
  full chain even when an older index remains valid after interrupted replacement.
  Cost: read time grows with local history; there is no automatic retention cleanup.
- Live paid Claude probes and real project activation remain pending separate
  approval as the plan specifies. Fixture tests do not certify model judgment.
- Doctor reports runtime/storage readiness, not policy conflict analysis or host
  certification. Existing global/project conflicts were inspected manually.

## Task evidence

- Task 0: local baseline Windows, Python 3.12.9, Claude Code 2.1.278.
  Hooks/subagent interface reviewed during design. Actual interactive auto-compaction,
  notification visibility, platform host behavior and model routing remain pending.
- Task 1: config tests RED (missing module) ? GREEN, 5 tests. Exact root allowlisting,
  strict config/schema/types, disabled default, no settings writes.
- Task 2: adaptive role/policy contract tests GREEN, 2 tests; legacy roles unchanged.
  Role documentation check initially RED (new role absent), then GREEN: 7 roles,
  14 documented model cells.
- Task 3: storage tests RED (missing module) ? GREEN; original 10 tests with Windows
  symlink creation skipped. CAS, idempotency, immutable snapshots, corruption,
  workspace identity, Git exclusion, resume observations. Added recovery/lock tests:
  stale valid index initially hid completed new object (RED), chain reconciliation
  fixes it (GREEN); busy store refuses without replacing history.
- Task 4: notifications RED (missing module) ? GREEN, 4 tests: first, abbreviated
  repeat, recovery once, corrupt/unwritable cache. No guaranteed visibility claim.
- Task 5: hooks/CLI initial RED ? GREEN (10 hook, 4 CLI tests). Shell wrapper RED
  (missing routing/entrypoint, legacy resume context) ? GREEN (5 original tests).
  Subdirectory failure notification lost root cache association: RED ? GREEN.
  Explicit skill operational evaluation: baseline agent already chose safe actions
  but lacked commands; with skill it selected documented doctor/list/resume and
  avoided uncertain worker redispatch. This is skill usability evidence, not Claude
  routing/behavior proof. Worker ownership uses available host evidence; when
  unavailable it remains an explicit clarification, not a fabricated runtime API.
- Task 6: workflow fixture contract RED (missing files) ? GREEN. 20 synthetic cases
  across spec-kit/OpenSpec/Matt/shared scheduling. Guides preserve sources/gates,
  provider identity and project restrictions. Live Claude scenario evaluation pending.
- Task 7: packaging RED (missing docs/README contract), then GREEN. A Windows shell
  encoding mismatch prevented the Chinese README insertion; corrected with escaped
  Unicode and packaging check passed. Added three-platform Python 3.11 CI, preserving
  old jobs. setup-python v6.0.0 SHA resolved from official Git tag:
  e797f83bcb11b83ae66e0230d6156d7c80228e7c.
  Marketplace and plugin manifests each passed Claude CLI validation.
- Task 8: ignored local activation proposal, config and doctor result prepared under
  tmp/. No personal settings or target project writes. Doctor: adaptive selected,
  Python supported, storage_ready=false / HANDOFF_NOT_IGNORED, host_verified=false.
  Target checkout has no tracked handoff files; existing single-writer rules remain.
  Proposed pilot limits 1/1, exact selected root; existing worktrees excluded.
  Global native orchestration conflicts identified; minimal scoped exception drafted,
  no wholesale replacement. Actual enablement and pilot remain pending approval.

## Final independent review

Read-only reviewer checked tracked and untracked changes against design/plan.
Three Important behavior findings, all reproduced RED and fixed GREEN:

1. Unbound/wrong-workspace manual CLI writes: require matching SessionStart binding.
2. Corrupt unrelated snapshot blocked healthy explicit resume: select requested ID
   independently; listing retains healthy entries and labels damaged ones with errors.
3. Adaptive config outside allowlist silently fell back: SessionStart now explains
   workspace is not enabled, without workspace writes.

No deferred minor findings. No second review loop. Focused review-fix suite: 3 passed.
Full final regression results are recorded below after execution.

## Final verification (2026-09-22)

- python -m unittest discover -s tests/adaptive -p "test_*.py" -v:
  50 tests, 49 passed, 1 skipped, 119.156s. Skip: this Windows process cannot create
  symlinks. It is not recorded as a passed path-redirection test.
- All 11 tests/governance/Test-*.ps1 scripts passed in separate powershell.exe
  -NoProfile processes. Existing Tier 2 coverage retained.
- tests/roles/Test-RoleDefinitions.ps1: 7 roles / 14 documented model cells passed.
- tests/docs/Test-MarkdownLinks.ps1: 100 relative links passed.
- tests/governance/Test-PowerShellSyntax.ps1: 20 scripts passed.
- claude plugin validate .: marketplace validation passed.
- claude plugin validate .claude-plugin/plugin.json: plugin validation passed.
- Legacy SessionStart with EOF/no config: valid JSON, 5657 context characters;
  original newline/truncation assertions passed under Git Bash.
- git diff --check passed; untracked UTF-8/trailing-whitespace check: 0 issues.
- Extra wrapper test initially failed because the fixture prepended a native
  Windows drive path to Bash PATH. Converting that fixture path with cygpath fixed
  the simulation. Focused wrappers 6/6, then final full suite above passed.
- Logs are ignored local artifacts under tmp/adaptive-validation. No commits,
  settings writes, project activation, worktrees or paid Claude probes performed.

Delivery boundary: repository runtime plus synthetic checks and pilot preparation.
Real interactive compaction/notifications, actual routing, live workflow judgment
and hosted Linux/macOS CI results remain unverified. Do not call them accepted.
