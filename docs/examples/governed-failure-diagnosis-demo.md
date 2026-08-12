# Governed failure-diagnosis demo

This helper-level demo exercises Manifest v2 failure signatures in
`Foreman-Manifest.ps1`: a `FAIL` completion must carry signature evidence,
equivalent signatures normalize to one hash, and a repeated signature requires
new diagnosis fields before another revision can initialize.

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- `git` on `PATH`.
- A checkout of this repository. No packages or Claude Code session are needed.

## Run it

From the repository root:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-FailureDiagnosis.ps1
```

The script creates an isolated temporary Git repository, writes successive
`provost-foreman-manifest/v2` revisions under `.claude/provost/foreman/`, and
removes the temporary repository afterward.

Expected output resembles:

```text
PASS: Complete FAIL without a signature is rejected on v2
PASS: Complete FAIL recorded a normalized failure signature
PASS: Equivalent failure signatures normalize to the same hash
PASS: A third same-signature attempt requires diagnosis
PASS: A same-signature retry with diagnosis can initialize
PASS: Reusing a normalized diagnosis is rejected
Failure-diagnosis checks passed: 6
```

## What is enforced

`Complete FAIL` on a v2 manifest requires `FailureSignatureJson` (`command`,
`status`, `error_summary`). The helper stores a normalized hash on the terminal
receipt. After the same hash appears twice, `Initialize` of the next revision
fails closed unless `approval.diagnosis` names that hash and supplies
hypothesis, measurement, and evidence_delta. Reusing a diagnosis that
normalizes to the same evidence is also rejected.

## Limitations

This is a **helper-level multi-revision test**, not an end-to-end governed
Claude Code session. It does not cover a packaged launcher or a long retry
history beyond the diagnosis brake.
