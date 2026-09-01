# Security Policy

## Supported versions

Provost is pre-1.0. Fixes land on the latest tag; earlier `0.1.x` tags do not
receive backports.

| Version | Supported |
|---|---|
| Latest `0.1.x` tag | Yes |
| Earlier tags | No |

## Reporting a vulnerability

Use the **Report a vulnerability** button on this repository's Security tab.
That channel is private to the maintainer, so reporting there does not disclose
the issue publicly.

Do not open a public issue for a suspected vulnerability, and do not include
credentials, private paths, or private repository content in a report.

A useful report names the affected tier, the documented guarantee you believe is
broken, the smallest reproduction, and the expected and actual behavior.

This project is maintained in spare time. Expect an acknowledgement rather than a
fix on a fixed schedule. There is no bounty.

## Scope

In scope: a defect that lets a governed run bypass a guarantee the documentation
claims — write-scope enforcement, ref-guard classification, manifest pinning,
path custody, completion gating, or ledger and receipt integrity — in the
reference helper and hooks under
[`docs/governance/reference/`](docs/governance/reference/).

Out of scope: behavior already recorded as a limitation in
[`README.md`](README.md) or the
[governance capability matrix](docs/governance/README.md); anything that
presumes the operator has already removed the hooks or the required environment;
and the security of third-party gateways, which Provost neither ships nor
certifies.

## What Provost does not claim

Tier 2 is a reference implementation, not a hardened security boundary. It raises
the cost of an unnoticed out-of-scope change. It does not defend against an
operator who disables the hooks, an agent host that never invokes them, or anyone
with write access to the workspace, the manifest, or the ledger. Hash checks
detect modification after the fact; they do not prevent it, and the files are not
made immutable by the operating system.
