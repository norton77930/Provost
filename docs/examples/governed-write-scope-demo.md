# Governed write-scope demo

This hook-level demo exercises the PowerShell `PreToolUse` write-gate decision
logic: it allows a path in a running task's literal `write_set` and returns a
structured deny decision for a path outside it.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-WriteScope.ps1
```

The script creates an isolated temporary workspace, writes a minimal synthetic
manifest and active-run lock, invokes
`docs/governance/reference/hooks/PreToolUse-WriteGate.ps1` through the same JSON
stdin shape used by a hook, and removes the temporary workspace afterward.

Expected output resembles:

```text
PASS: approved write_set path was allowed
PASS: path outside write_set was denied
PASS: must_not_modify took precedence over write_set
PASS: path outside the governed workspace was denied
PASS: missing active-run lock failed closed
PASS: invalid run state failed closed
Write-scope checks passed: 6
```

For an out-of-scope file, the hook response contains a machine-readable decision
like this (temporary paths and formatting are omitted):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Foreman write-gate: \"outside.txt\" is not in any RUNNING task write_set ..."
  }
}
```

## What is enforced

The hook reads the governed workspace root, active lock, manifest, and running
task states. It permits a file-tool target only when the target exactly matches a
literal path in a running task's `write_set`. Explicit `must_not_modify` entries
win over the allowlist. Missing or unreadable governed state returns `deny`.

That differs from prompt-only instructions because the result is a host-consumed
tool decision produced before the file-editing tool executes. The model is not
being asked to judge its own compliance.

## Limitations

This is a **hook-level integration test**, not an end-to-end governed Claude Code
session. `Start-GovernedSession.ps1` supplies the environment and the hook
registration that activate these scripts in Claude Code. The test has been run
in the repository's current Windows/PowerShell environment, but has not yet been
independently verified on a fresh Windows VM. It also does not claim
operating-system sandboxing or coverage of arbitrary shell writes. The related
reference guard classifies commands that touch external read-only roots, but a
packaged command-execution policy remains future work.
