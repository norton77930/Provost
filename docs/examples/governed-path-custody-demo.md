# Governed path-custody demo

This helper-level demo exercises Manifest v2 shared-path custody in
`Foreman-Manifest.ps1`: writers that share a path must be dependency-ordered,
the first writer's `FinishTask` records a content hash, and the next writer's
`StartTask` transfers that custody.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- `git` on `PATH`.
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-PathCustody.ps1
```

The script creates isolated temporary Git repositories, writes a minimal
`provost-foreman-manifest/v2` draft under `.claude/provost/foreman/`, and
removes the temporary repositories afterward.

Expected output resembles:

```text
PASS: Initialize rejected unordered writers sharing a path
PASS: FinishTask T01 recorded custody for the shared path
PASS: StartTask T02 transferred custody from T01
PASS: StartTask T02 rejected shared-path content drift
Path-custody checks passed: 4
```

## What is enforced

Two writers that list the same path must be ordered by `depends_on`. After the
first writer passes, the lock stores `path_custody` with that task id and the
file hash. Starting the dependent writer transfers custody and records
`received_from_task_id`. If the shared file changes after that pin and before
the next writer starts, `StartTask` escalates (`SCOPE_ESCALATE`,
`custody_drift`) and writes a terminal handoff receipt.

## Limitations

This is a **helper-level integration test**, not an end-to-end governed Claude
Code session. It does not cover failure signatures or a packaged launcher.
