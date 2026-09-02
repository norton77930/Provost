# Proposal: cover the two lifecycle actions no test has ever run

Status: proposed, not started. Tracked under "Next" in
[`ROADMAP.md`](../../ROADMAP.md).

A proposal lives here only while it is unbuilt. When the change lands, the
roadmap entry moves to "Current" and this file is deleted — what shipped is
described by the code, the tests, and `CHANGELOG.md`, and a proposal left behind
starts contradicting them.

## Why

The helper exposes eight actions. Counting calls across every test:

| Action | Calls in tests |
|---|---|
| `Initialize` | 18 |
| `StartTask` | 10 |
| `Complete` | 7 |
| `FinishTask` | 6 |
| `CloseBlocked` | 3 |
| `Validate` | 2 |
| `RecordRetry` | **0** |
| `RecoverLock` | **0** |

Two lifecycle actions have never been executed by anything. They are not
untested edge cases inside a covered path; nothing has ever run them at all.

`RecoverLock` matters more than the count suggests. It is the operator's way out
of a stuck lock, and it is deliberately exempt from the session binding added
this week — it does not call `Get-ActiveLockForSession`. An exemption with no
test is a decision nothing is holding in place: a later change that routes it
through the same gate would close the escape hatch, and the suite would stay
green while a stuck workspace became unrecoverable.

The same shape produced this week's three real defects. Each was two pieces that
were individually correct with nothing exercising the seam.

## What changes

One new check, `tests/governance/Test-LifecycleActions.ps1`, wired into
`.github/workflows/ci.yml` and the run list in `CONTRIBUTING.md`. This is the
thirteenth check; unlike the walkthrough, this one is a check and belongs there.

## Requirements

- **R1 Every assertion drives the real helper** through `Invoke-ForemanHelper`,
  against a workspace built by running the real lifecycle. Do not hand-write a
  lock or a ledger to reach a state faster.
- **R2 `RecoverLock`'s exemption is asserted, not assumed.** A test must fail if
  a later change makes `RecoverLock` require the opening session. This is the
  reason the proposal exists; if only one thing here lands, it is this.
- **R3 Every refusal is pinned to the check that produced it.** `RETRY`,
  `ACTIVE_LOCK`, and `SCHEMA` are each raised from several places. Match on
  message text, as the existing checks do.
- **R4 No production change.** If an invariant looks wrong, report it — do not
  fix it here. A test change and a behaviour change in one commit cannot be
  reviewed separately.

## Verification

`RecordRetry`:

- Refuses when the named task is not `RUNNING`.
- Records a retry for a running task: `retries[taskId]` becomes 1 and a `retry`
  event is appended to the ledger carrying the task, agent key, model, effort,
  and classification.
- Refuses a second retry for the same task — one transient retry per task is the
  stated rule.
- Is subject to the session binding: an enforced lock refuses `RecordRetry` from
  a different session, with `SESSION`.

`RecoverLock`:

- Refuses without `-Acknowledge`.
- Archives the lock to `abandoned-lock-<stamp>-<suffix>.json` and removes
  `active-run.lock`.
- The archive name carries the collision-resistant suffix, the same invariant
  `Test-FailureDiagnosis.ps1` asserts for `archived-lock-*`. Two recoveries in
  one second must not overwrite each other.
- **Succeeds from a session other than the one that opened an enforced lock.**
  Assert the success, not merely the absence of an error, so the exemption
  cannot be closed silently.

And: the twelve existing checks still pass, unchanged.

## Non-goals

- No coverage for `Validate` or `Status` beyond what exists. They are read-only
  and not where the risk is.
- No new invariants. Test what the helper does today. If the one-retry-per-task
  rule or the `RecoverLock` exemption looks wrong, that is a finding for the
  report, not a change to make here.
- No fixtures library or shared harness refactor. `ForemanTestHelpers.ps1`
  already carries what is needed.

## What will bite you in this codebase

Each of these cost real time this week.

1. **`RETRY`, `ACTIVE_LOCK`, and `SCHEMA` are each raised from several places,
   and `CONTINUATION` from nineteen.** `Assert-ThrowsCode -Code X` alone does
   not identify which check fired. Match the message.

2. **Reaching `RUNNING` takes a real sequence.** `RecordRetry` needs an
   initialized run with a started task; there is no shortcut that is not a
   hand-written lock, which R1 forbids. `Test-PathCustody.ps1` shows the
   `StartTask` / `FinishTask` call shape.

3. **A governed run needs a liveness marker.** Setting `PROVOST_SESSION_PROFILE`
   by hand without one makes `Initialize` refuse, correctly.
   `Test-ContinuationEnforcement.ps1` has a helper that writes the marker for a
   fixture; the marker's own producer is covered by `Test-SessionLiveness.ps1`.

4. **Save and restore `PROVOST_SESSION_PROFILE` and `CLAUDE_CODE_SESSION_ID`
   in a `finally`.** A test that leaks them makes every later test in the same
   shell fail with `ENFORCEMENT`, and the failure looks like it belongs to the
   test that inherited it.

5. **Archive names collide at one-second resolution.** That was a real defect,
   fixed by adding a random suffix. If you write two recoveries back to back,
   assert two files exist rather than assuming.

6. **Temporary-workspace cleanup can hit a file lock** when the whole suite runs
   back to back. `Remove-IsolatedTempRoot` already retries; use it rather than
   deleting directly.

7. **Line endings are LF and files carry no BOM.** PowerShell's
   `Set-Content -Encoding utf8` writes a BOM on 5.1. Use
   `[System.IO.File]::WriteAllText` with `UTF8Encoding($false)`.

## Ground rules from CONTRIBUTING

A reference-runtime change includes a focused failing check first, the smallest
coherent fix, and relevant regression coverage. Here the check is the whole
change: show each new assertion failing against a deliberately broken helper
before it passes, so it is known to test what it claims.
