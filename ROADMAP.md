# Roadmap

This roadmap describes intended work, not promised dates. Items move to
"Current" only when they are present and verifiable in the repository.

## Current

- Tier 0/1/2 graduated-governance model and decision rule.
- Tier 1 Claude Code collaborator roles and orchestration policy, installable
  as a Claude Code plugin. The orchestration policy reaches a session through a
  `SessionStart` hook rather than a manual merge into `CLAUDE.md`.
- Methodology skills for TDD, diagnosis, review, and completion verification.
- Windows/PowerShell Tier 2 reference helper and hook implementations.
- Hook-level write-scope and ref-guard enforcement demos, helper-level
  v1 manifest-pin, v2 path-custody, completion-gate, failure-diagnosis,
  and audit-artifacts tests.
- CI on Windows for the Tier 2 reference checks and on Linux for the
  collaborator tier, so the claim that the collaborator tier runs wherever
  Claude Code runs is checked rather than asserted.
- A role-definition check that fails when the README role tables drift from the
  shipped frontmatter.
- Governance capability matrix that separates implemented behavior, design
  intent, and limitations.
- A Windows launcher that opens a governed session, registers the Tier 2 hooks
  for that session alone, and refuses when they could not enforce. `Initialize`
  will not open a run whose session shows no marker from the `SessionStart`
  hook.
- An enforcement record on every run — in the lock, the initialize ledger
  event, and the terminal handoff receipt — and a continuation gate that
  refuses to adopt work from a run that opened with nothing enforcing.
- A published security policy with an explicit scope and a statement of what
  Tier 2 does not claim to defend against.

## Next

- A short demo of a governed decision being made — a write outside an approved
  scope being denied — rather than a clean run finishing.
- A listing in the Claude Code plugin marketplace, once the demo exists.
- Tie a running lock to the session that opened it. The enforcement record is
  open-time truth, so a lock opened under enforcement can be driven through
  StartTask, FinishTask, and Complete from a later plain session, and the
  resulting receipt still reads as enforced. With `enforcement.session_id` now
  in the lock this is a check in `Get-ActiveLockForSession`.
- End-to-end examples now that a governed session can be opened directly.
- Lifecycle fixtures and automated invariant tests for manifest revisions,
  custody handoffs, verifier completion, failure signatures, ledgers, and
  terminal receipts.
- Evidence records defined and enforced per acceptance claim.
- A cross-platform Tier 2 runtime with equivalent fail-closed semantics for
  Linux and macOS. The collaborator tier already runs on all three.

## Later / exploration

- Define model-independent runtime interfaces separately from host adapters.
- Explore compatibility with additional coding-agent environments.
- Explore integrations for review, issue triage, and release-maintenance
  workflows without making the governance model dependent on one provider.
- Evaluate external or tamper-evident ledger backends for environments that need
  stronger audit guarantees than local files provide.

This project has no official OpenAI, Anthropic, or other vendor partnership or
integration unless explicitly documented in the future.
