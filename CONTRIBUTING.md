# Contributing to Provost

Provost is a young project. Focused bug reports, governance proposals,
documentation improvements, tests, packaging work, and cross-platform ports are
welcome.

## Report a bug

Use the bug-report template and include the affected tier, operating system,
PowerShell or Claude Code version where relevant, the smallest reproduction, and
the expected and actual result. Redact credentials, private paths, and private
repository content.

Do not report a suspected vulnerability in a public issue. Use the private
channel described in [`SECURITY.md`](SECURITY.md), which also records what is in
scope and what Provost does not claim to defend against.

## Propose a governance feature

Describe the risk being controlled, the invariant that should hold, where it is
enforced, its failure mode, and how it can be tested. Clearly separate a design
goal from behavior implemented by the current reference runtime.

Changes must not silently weaken a documented guarantee. If a tradeoff requires
weaker enforcement, call it out in the proposal, tests, documentation, and pull
request description.

## Documentation and runtime contributions

- Documentation changes should keep `README.md`, the core status section in
  `README.zh-TW.md`, and the governance capability matrix consistent.
- If you add or remove a methodology skill directory, update the install
  command lists in both `README.md` and `README.zh-TW.md`.
- If you change a role model in `agents/`, update the role table in both
  `README.md` and `README.zh-TW.md`. `Test-RoleDefinitions.ps1` fails on drift
  between the shipped frontmatter and either table.
- Vendored skills stay pinned to the snapshot recorded in
  [`skills/NOTICE.md`](skills/NOTICE.md). Do not auto-follow upstream. Adopt a
  later change only when it is a safety or methodology fix that does not change
  Provost's public contract (tiers, roles, schemas, or install surface). Record
  the source commit and any deliberately skipped upstream edits in NOTICE.
- Windows reference-runtime changes should include a focused failing check first,
  the smallest coherent fix, and relevant regression coverage.
- Cross-platform work should preserve fail-closed behavior and literal path
  semantics unless a proposal explicitly changes the contract.
- Do not remove third-party attribution from `skills/NOTICE.md`.

## Work on Provost with Provost

This repository is itself the plugin, so load the working copy rather than an
installed release:

```bash
claude --plugin-dir .
```

That session gets the six roles, the six skills, and the orchestration policy
from the files you are editing. Confirm what a change ships with:

```bash
claude plugin validate .
claude --plugin-dir . plugin details provost
```

## Run the checks

From the repository root on Windows:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-PowerShellSyntax.ps1
powershell.exe -NoProfile -File .\tests\roles\Test-RoleDefinitions.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-WriteScope.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-RefGuard.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-ManifestPin.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-PathCustody.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-CompletionGate.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-FailureDiagnosis.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-AuditArtifacts.ps1
powershell.exe -NoProfile -File .\tests\governance\Test-SessionLiveness.ps1
powershell.exe -NoProfile -File .\tests\docs\Test-MarkdownLinks.ps1
```

The checks have no package dependencies. `Test-ManifestPin.ps1`,
`Test-PathCustody.ps1`, `Test-CompletionGate.ps1`,
`Test-FailureDiagnosis.ps1`, and `Test-AuditArtifacts.ps1` also require
`git`. Tier 2 is Windows-specific today;
contributors working on other platforms can still review and improve Tier 1 and
documentation, but should state which checks they could not run.

## Pull requests

Keep each pull request narrow. Explain the problem, scope and non-goals, affected
governance guarantees, and current verification evidence. Avoid unrelated
formatting or refactors. A passing check is evidence for the behavior it covers,
not proof of untested invariants.
