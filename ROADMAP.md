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
- A published security policy with an explicit scope and a statement of what
  Tier 2 does not claim to defend against.

## Next

- A short demo of a governed decision being made — a write outside an approved
  scope being denied — rather than a clean run finishing.
- A listing in the Claude Code plugin marketplace, once the demo exists.
- Ship the Tier 2 hooks with the plugin instead of registering them by hand.
  Each already stands down outside a governed session — every one exits early
  unless `PROVOST_SESSION_PROFILE` is `foreman` — and registering them is safe
  where PowerShell is absent, because a hook whose interpreter cannot be found
  does not block the tool call. That tolerance is also the hazard: a governed
  session on such a platform would run with no write gate and no warning, so
  the launcher must refuse to open one where the hooks cannot execute.
- The governed launcher and the `PROVOST_*` environment the Tier 2 hooks
  require. Plugin hook registration now covers the part that used to need a
  launcher, so what remains is environment setup and lifecycle entry.
- End-to-end examples, once the two items above make a governed session
  reachable without hand-assembly.
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
