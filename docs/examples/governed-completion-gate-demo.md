# Governed completion-gate demo

This helper-level demo exercises `Complete PASS` in `Foreman-Manifest.ps1`:
every declared task must already be `PASS`, including the required code
verifier. Context freshness is not claimed; the shipped launcher does not create fresh
per-task contexts.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- `git` on `PATH`.
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-CompletionGate.ps1
```

The script creates an isolated temporary Git repository, writes a minimal
`provost-foreman-manifest/v1` draft (writer + verifier), and removes the
temporary repository afterward.

Expected output resembles:

```text
PASS: Complete PASS rejected before any task passed
PASS: Complete PASS rejected while the verifier task is unfinished
PASS: Complete PASS succeeded after every task was PASS
Completion-gate checks passed: 3
```

## What is enforced

`Complete -Outcome PASS` walks every task in the approved manifest. If any
task is missing or not `PASS`, the helper throws `[VERIFY]` and leaves the
run lock in place. After every task is `PASS`, `Complete PASS` succeeds and
deletes `active-run.lock`.

## Limitations

This is a **helper-level integration test**, not an end-to-end governed Claude
Code session. It does not prove that the verifier received a fresh agent
context. It does not cover `Complete FAIL`, failure signatures, or a packaged
launcher.
