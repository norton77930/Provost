# The governed tier

Tier 2 applies extra controls to high-blast-radius work: authentication,
secrets, payments, schema or data migrations, public APIs, and changes that need
an audit trail. The design is platform-independent; the implementation in this
repository is a Windows/PowerShell reference extracted from a private system
internally named "Foreman."

## Capability matrix

| Capability | Reference implementation evidence | Current limitation |
|---|---|---|
| Hash-pinned manifest | `Foreman-Manifest.ps1` hashes the approved manifest, records the hash in the active lock, and checks it before lifecycle actions. Changed intent requires a new numbered revision. A public test covers Initialize, Validate, a tampered native Plan, and a tampered approved manifest. | The operating system does not make the file immutable. The helper detects a mismatch and escalates it. |
| Write scope | `PreToolUse-WriteGate.ps1` compares `Edit`, `Write`, and `NotebookEdit` targets with the literal `write_set` of running tasks and returns `deny` on mismatch or unreadable state. | Enforcement requires Claude Code hook registration and launcher-provided `PROVOST_*` environment variables, which `Start-GovernedSession.ps1` supplies for one session. Shell writes inside the workspace require separate command controls. |
| External reference protection | `PreToolUse-RefGuard.ps1` denies write-like commands that mention declared read-only external roots and asks for review when it cannot classify a command. A dependency-free hook test covers silent, deny, allow, and ask decisions. | This is a conservative command-text classifier, not an operating-system sandbox. |
| Workspace anchoring | `SessionStart-WorkspaceCheck.ps1` compares the session cwd with the intended workspace. | Claude Code's `SessionStart` hook can add a warning but cannot block; this check is informational and fail-open. |
| Path custody | Manifest v2 requires dependency ordering for writers sharing a path. The helper records and verifies status/hash evidence between writers. A public test rejects unordered shared writers, records and transfers custody, and escalates when shared content drifts before the next writer starts. | Enforcement is helper-detected, not an operating-system lock. A launcher ships; a packaged runtime does not. |
| Completion gate | A PASS completion requires every declared task to be PASS. The schema requires a code-verifier task, and an optional required architecture verifier must depend on every non-review task. A public test rejects `Complete PASS` until every task is PASS, then confirms the lock is removed. | The shipped launcher does not create fresh agent contexts, which the original did. Context freshness is therefore a workflow/launcher responsibility, not something this helper alone proves. |
| Acceptance evidence | Tasks declare acceptance entries (`id`, `command`, `expect`) and may write one verification summary to the ledger. | Evidence is not yet bound and validated separately for every acceptance claim. Full per-claim evidence enforcement remains planned. |
| Failure diagnosis | Manifest v2 records normalized failure signatures and checks prior terminal receipts; repeated signatures require new diagnosis fields and evidence delta. A public test covers unsigned FAIL, signature persistence, hash normalization, the diagnosis brake, and reused-diagnosis rejection. | This is a helper-level multi-revision check, not an end-to-end governed session. A launcher ships; a packaged runtime does not. |
| Audit artifacts | Lifecycle actions append JSONL events. Terminal handoff receipts pin manifest, ledger, and workspace snapshots by hash, and later flows verify those hashes. A public test checks the initialize ledger event, receipt hash pins, and a later Initialize that rejects a tampered ledger. | The ledger is helper-appended, not protected from arbitrary filesystem modification. Hash checks make some tampering detectable; they do not provide an external append-only store. |

## Reference implementation

[`reference/`](reference/) contains:

- `Foreman-Manifest.ps1` — the governance lifecycle helper. It initializes and
  validates manifests, starts and finishes tasks, manages custody, records
  retries, completes runs, and writes ledger/handoff state.
- `hooks/PreToolUse-WriteGate.ps1` — literal path enforcement for Claude Code
  file-editing tools.
- `hooks/PreToolUse-RefGuard.ps1` — command guard for declared external read
  roots.
- `hooks/SessionStart-WorkspaceCheck.ps1` — workspace-drift warning, and the
  session liveness marker that `Initialize` requires as evidence the hooks ran.
- `Start-GovernedSession.ps1` — opens a governed session: sets the `PROVOST_*`
  environment, registers the three hooks for that session only, and refuses when
  they could not enforce.

The scripts keep their original identifiers (`provost-foreman-*`, `PROVOST_*`).
`Start-GovernedSession.ps1` registers the hooks for one session and sets those
variables; the wider local configuration the original launcher carried is not in
this repository. Treat these scripts as inspectable and executable reference
code rather than a packaged dependency.

## Write-scope enforcement demo

The repository includes a dependency-free Windows test for the write-gate
decision logic:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-WriteScope.ps1
```

The test invokes the hook with synthetic governed state and verifies allowed,
out-of-scope, forbidden, outside-workspace, missing-lock, and invalid-state
cases. See the [walkthrough](../examples/governed-write-scope-demo.md) for
prerequisites, output, and interpretation.

## External-reference guard demo

The repository also includes a dependency-free Windows test for the ref-guard
decision logic:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-RefGuard.ps1
```

The test invokes the hook with a temporary external root and verifies that
non-governed sessions stay silent, write-like commands are denied, a narrow
read is allowed, and an unclassified command asks for review. See the
[walkthrough](../examples/governed-ref-guard-demo.md).

## Manifest-pin demo

A third Windows test exercises the lifecycle helper's hash pin without a
launcher:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-ManifestPin.ps1
```

The test initializes a minimal v1 manifest in an isolated Git workspace,
validates it, then confirms that a tampered native Plan and a tampered
approved manifest are rejected. It requires `git`. See the
[walkthrough](../examples/governed-manifest-pin-demo.md).

## Path-custody demo

A fourth Windows test exercises v2 shared-path custody:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-PathCustody.ps1
```

The test rejects two writers that share a path without a dependency, records
and transfers custody when they are ordered, and escalates if the shared file
changes before the next writer starts. It requires `git`.
See the [walkthrough](../examples/governed-path-custody-demo.md).

## Completion-gate demo

A fifth Windows test exercises the helper's completion gate:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-CompletionGate.ps1
```

The test rejects `Complete PASS` until every declared task is PASS, then
confirms the active lock is removed. It requires `git`. See the
[walkthrough](../examples/governed-completion-gate-demo.md).

## Failure-diagnosis demo

A sixth Windows test exercises v2 failure signatures and the diagnosis brake:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-FailureDiagnosis.ps1
```

The test requires a signature on `Complete FAIL`, checks that equivalent
signatures normalize to one hash, and requires a new diagnosis after the same
failure repeats. It requires `git`. See the
[walkthrough](../examples/governed-failure-diagnosis-demo.md).

## Audit-artifacts demo

A seventh Windows test exercises ledger append and receipt hash pins:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-AuditArtifacts.ps1
```

The test checks that Initialize writes a `run_initialized` ledger event, that
a terminal receipt pins manifest and ledger hashes, and that a later
Initialize rejects a ledger that changed after the receipt. It requires `git`.
See the [walkthrough](../examples/governed-audit-artifacts-demo.md).

## Design direction

The long-term target is a packaged, cross-platform runtime whose governance
interfaces can be adapted to multiple coding-agent environments. The current
Claude Code/PowerShell implementation is evidence for the design, not evidence
that those ports already exist. See the root [`ROADMAP.md`](../../ROADMAP.md).
