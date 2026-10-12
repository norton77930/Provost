# Security Policy

## Supported versions

Provost is pre-1.0. Fixes land on `main`; earlier tags do not receive backports.
The archived Tier 2 governance code (tag `archive/tier2-foreman`) is not
maintained.

## Reporting a vulnerability

Use the **Report a vulnerability** button on this repository's Security tab.
That channel is private to the maintainer, so reporting there does not disclose
the issue publicly.

Do not open a public issue for a suspected vulnerability, and do not include
credentials, private paths, or private repository content in a report.

A useful report names the affected component, the behaviour you believe is
unsafe, the smallest reproduction, and the expected and actual behaviour.

This project is maintained in spare time. Expect an acknowledgement rather than a
fix on a fixed schedule. There is no bounty.

## Scope

In scope:

- The handoff runtime and hooks (`scripts/provost_runtime/`, `hooks/`): code
  execution from the project directory (interpreter discovery, module
  shadowing), writes or deletions outside the handoff store, records that can
  be forged or silently corrupted, hook input that can break or block a Claude
  Code session, and facts that capture command text or file contents.
- `install.py`: writes outside the Claude config directory, loss of user content
  outside the policy markers in `CLAUDE.md`, missing backups or incomplete
  rollback, and any path by which a value from `settings.json` reaches output.

Out of scope: behaviour already listed under Known limitations in
[`README.md`](README.md); anything that presumes an attacker who can already
write to `~/.claude` or the repository; model behaviour that ignores the
orchestration policy (the policy is guidance, not enforcement); and third-party
plugins or skills.

## What Provost does not claim

The orchestration policy and role definitions are instructions to a model, not a
security boundary. The runtime protects its own store and refuses unsafe paths,
but it does not sandbox the agent, restrict what tools may write, or make any
file immutable.
