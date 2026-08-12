# Governed manifest-pin demo

This helper-level demo exercises hash pinning in `Foreman-Manifest.ps1`: an
approved manifest is hashed into the active lock, Validate accepts the
unchanged files, and a tampered Plan or approved manifest is rejected.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- `git` on `PATH` (the helper requires a Git workspace unless a workspace
  declaration is present; this test uses `git init`).
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-ManifestPin.ps1
```

The script creates an isolated temporary Git repository, writes a minimal
`provost-foreman-manifest/v1` draft under `.claude/provost/foreman/`, calls
`Initialize` and `Validate`, tampers the Plan and the approved manifest, and
removes the temporary repository afterward.

Expected output resembles:

```text
PASS: Initialize created a pinned r001 manifest and lock hash
PASS: Validate accepted the unchanged approved manifest
PASS: Validate rejected a tampered native Plan
PASS: StartTask rejected a tampered approved manifest via the lock hash
Manifest-pin checks passed: 4
```

## What is enforced

`Initialize` writes `r001.json`, records `manifest_sha256` on `active-run.lock`,
and hashes the native Plan into the manifest. `Validate` fails closed if the
Plan bytes change. Later lifecycle actions (`StartTask` here) fail closed if the
approved manifest bytes no longer match the lock hash.

That is helper-enforced detection, not an operating-system lock. The files
remain writable; a mismatch escalates instead of being ignored.

## Limitations

This is a **helper-level integration test**, not an end-to-end governed Claude
Code session. It covers v1 pin-and-detect only. It does not exercise v2 intent
hashes, revision supersedes, path custody, or a packaged launcher.
