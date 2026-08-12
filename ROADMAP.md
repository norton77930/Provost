# Roadmap

This roadmap describes intended work, not promised dates. Items move to
"Current" only when they are present and verifiable in the repository.

## Current

- Tier 0/1/2 graduated-governance model and decision rule.
- Tier 1 Claude Code collaborator roles and orchestration policy.
- Methodology skills for TDD, diagnosis, review, and completion verification.
- Windows/PowerShell Tier 2 reference helper and hook implementations.
- Hook-level write-scope and ref-guard enforcement demos, helper-level
  v1 manifest-pin and v2 path-custody tests, and basic Windows CI checks.
- Governance capability matrix that separates implemented behavior, design
  intent, and limitations.

## Next

- Package the governed launcher and Claude Code hook registration needed for a
  clean end-to-end setup.
- Add lifecycle fixtures and automated invariant tests for manifest revisions,
  custody handoffs, verifier completion, failure signatures, ledgers, and
  terminal receipts.
- Define and enforce evidence records per acceptance claim.
- Improve installation, configuration, and versioned release packaging.
- Build a cross-platform governed runtime with equivalent fail-closed semantics
  for Windows, Linux, and macOS.
- Add end-to-end examples once the public launcher exists.

## Later / exploration

- Define model-independent runtime interfaces separately from host adapters.
- Explore compatibility with additional coding-agent environments.
- Explore integrations for review, issue triage, and release-maintenance
  workflows without making the governance model dependent on one provider.
- Evaluate external or tamper-evident ledger backends for environments that need
  stronger audit guarantees than local files provide.

This project has no official OpenAI, Anthropic, or other vendor partnership or
integration unless explicitly documented in the future.
