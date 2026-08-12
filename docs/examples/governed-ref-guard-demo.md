# Governed ref-guard demo

This hook-level demo exercises the PowerShell `PreToolUse` ref-guard decision
logic: write-like commands that mention a declared external read root are
denied, a narrow pure-read is allowed, and an unclassified command asks for
review.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-RefGuard.ps1
```

The script creates an isolated temporary directory, treats it as a declared
`--ref` root, invokes
`docs/governance/reference/hooks/PreToolUse-RefGuard.ps1` through the same JSON
stdin shape used by a hook, and removes the temporary directory afterward.

Expected output resembles:

```text
PASS: non-foreman session was silent
PASS: foreman session with no declared refs was silent
PASS: write-like command into a declared ref was denied
PASS: narrow Get-Content against a declared ref was allowed
PASS: unclassified command that mentions a ref asked for review
Ref-guard checks passed: 5
```

## What is enforced

The hook is inert unless `PROVOST_SESSION_PROFILE` is `foreman` and
`PROVOST_FOREMAN_EXTERNAL_READ_ROOTS` lists at least one root. It then classifies
the shell command text: write-like tokens plus a ref path return `deny`; a
narrow allowlisted read returns `allow`; anything else that mentions a ref
returns `ask`.

That differs from prompt-only instructions because the host consumes the
decision before the command runs. The model is not being asked to judge its own
compliance.

## Limitations

This is a **hook-level integration test**, not an end-to-end governed Claude
Code session. The public repository does not include the launcher that declares
`--ref` roots or registers the hook. The classifier inspects command text; it is
not an operating-system sandbox and does not prove that every possible write
into a ref is blocked.
