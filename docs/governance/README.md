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
| Write scope | `PreToolUse-WriteGate.ps1` compares `Edit`, `Write`, and `NotebookEdit` targets with the literal `write_set` of running tasks and returns `deny` on mismatch or unreadable state. | Enforcement requires Claude Code hook registration and launcher-provided `PROVOST_*` environment variables, which this repository does not yet package. Shell writes inside the workspace require separate command controls. |
| External reference protection | `PreToolUse-RefGuard.ps1` denies write-like commands that mention declared read-only external roots and asks for review when it cannot classify a command. A dependency-free hook test covers silent, deny, allow, and ask decisions. | This is a conservative command-text classifier, not an operating-system sandbox. |
| Workspace anchoring | `SessionStart-WorkspaceCheck.ps1` compares the session cwd with the intended workspace. | Claude Code's `SessionStart` hook can add a warning but cannot block; this check is informational and fail-open. |
| Path custody | Manifest v2 requires dependency ordering for writers sharing a path. The helper records and verifies status/hash evidence between writers. | Covered by implementation code but not yet by a committed automated test suite. |
| Completion gate | A PASS completion requires every declared task to be PASS. The schema requires a code-verifier task, and an optional required architecture verifier must depend on every non-review task. | The repository does not include the original launcher that creates fresh agent contexts. Context freshness is therefore a workflow/launcher responsibility, not something this helper alone proves. |
| Acceptance evidence | Tasks declare acceptance entries (`id`, `command`, `expect`) and may write one verification summary to the ledger. | Evidence is not yet bound and validated separately for every acceptance claim. Full per-claim evidence enforcement remains planned. |
| Failure diagnosis | Manifest v2 records normalized failure signatures and checks prior terminal receipts; repeated signatures require new diagnosis fields and evidence delta. | The complete multi-revision flow lacks a public end-to-end demo and broad automated coverage. |
| Audit artifacts | Lifecycle actions append JSONL events. Terminal handoff receipts pin manifest, ledger, and workspace snapshots by hash, and later flows verify those hashes. | The ledger is helper-appended, not protected from arbitrary filesystem modification. Hash checks make some tampering detectable; they do not provide an external append-only store. |

## Reference implementation

[`reference/`](reference/) contains:

- `Foreman-Manifest.ps1` — the governance lifecycle helper. It initializes and
  validates manifests, starts and finishes tasks, manages custody, records
  retries, completes runs, and writes ledger/handoff state.
- `hooks/PreToolUse-WriteGate.ps1` — literal path enforcement for Claude Code
  file-editing tools.
- `hooks/PreToolUse-RefGuard.ps1` — command guard for declared external read
  roots.
- `hooks/SessionStart-WorkspaceCheck.ps1` — workspace-drift warning.

The scripts keep their original identifiers (`provost-foreman-*`, `PROVOST_*`)
and assume a launcher that registers the hooks and sets those variables. That
launcher and its local configuration are not in this public repository. Treat
these scripts as inspectable and executable reference code, not a ready-to-use
dependency.

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

## Design direction

The long-term target is a packaged, cross-platform runtime whose governance
interfaces can be adapted to multiple coding-agent environments. The current
Claude Code/PowerShell implementation is evidence for the design, not evidence
that those ports already exist. See the root [`ROADMAP.md`](../../ROADMAP.md).
