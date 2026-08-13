# Governed audit-artifacts demo

This helper-level demo exercises ledger append and terminal handoff hashing in
`Foreman-Manifest.ps1`: Initialize writes JSONL, a non-PASS completion pins
manifest and ledger hashes on a receipt, and a later Initialize fails closed
if the ledger bytes no longer match that pin.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- `git` on `PATH`.
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-AuditArtifacts.ps1
```

The script creates an isolated temporary Git repository, writes a minimal
`provost-foreman-manifest/v2` draft, and removes the temporary repository
afterward.

Expected output resembles:

```text
PASS: Initialize appended a parseable run_initialized ledger event
PASS: Terminal receipt pins manifest, ledger, and receipt hashes
PASS: Later Initialize rejects a ledger that changed after the receipt
Audit-artifacts checks passed: 3
```

## What is enforced

`Initialize` appends a `run_initialized` JSONL event. `Complete BLOCKED`
writes a `provost-foreman-handoff/v1` receipt whose `manifest_sha256` and
`ledger_sha256` match the files at that moment, and records `handoff_sha256`
on the lock. After `CloseBlocked`, a later `Initialize` of r002 walks prior
receipts and throws `[IMMUTABLE]` if the ledger no longer matches.

## Limitations

This is a **helper-level integration test**, not an end-to-end governed
Claude Code session. Hash checks detect some tampering; they do not provide
an external append-only store. A packaged launcher is not included.
