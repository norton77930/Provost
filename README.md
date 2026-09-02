# Provost

**Match oversight to blast radius.** — graduated governance for AI coding agents.

*[中文說明 →](README.zh-TW.md)*

Provost is a governance framework for deciding how much oversight an AI coding
task needs and making high-risk work more accountable. A typo does not need the
same process as an authentication change, a migration, or a public API change.
Provost provides three tiers so the process can rise with the potential damage.

The governance model itself is host-agnostic; the current reference host is
**Claude Code** and the shipped role files use Claude model identifiers, with
support for other agent hosts planned rather than available today.

Tier 2 is a reference implementation in code, not prose: it contains lifecycle
state, manifest hashing, write-gate decisions, workspace snapshots, custody
records, failure signatures, and an audit ledger. That is what separates Provost
from a prompt collection.

## What works today

- Six Claude Code role definitions under [`agents/`](agents/), installable as a
  plugin — see [Install](#install).
- The Tier 1 [orchestration policy](orchestration.md), including plan-first work,
  one active writer, and evidence-based completion discipline, supplied to each
  session by the plugin rather than merged in by hand.
- Reusable methodology [skills](skills/) for TDD, diagnosis, review, and
  verification, with third-party attribution preserved in
  [`skills/NOTICE.md`](skills/NOTICE.md).
- A Windows launcher that opens a governed session, registers the Tier 2 hooks
  for it, and refuses when they could not enforce — with `Initialize` requiring
  evidence the hooks actually ran.
- Windows checks covering the decision logic of all seven Tier 2 governance
  decisions, each with a demo walkthrough, plus a session-liveness check — see
  [Tier 2 reference checks](#tier-2-reference-checks-windows).

## Why this exists

Prompt instructions are useful, but an agent can misunderstand or ignore them.
For high-blast-radius work, Provost explores stronger controls: a plan is pinned
to a manifest, file writes are checked against an approved set, handoffs retain
path custody, and completion passes through declared verifier tasks.

The current implementation is still described as a reference implementation. A
launcher now ships and a governed session was verified enforcing end to end on
Windows, but
there is no turnkey installer, and Linux and macOS remain unenforced.

## The dial

| Tier | What it is | Use it for |
|---|---|---|
| **0 · Bare** | Claude Code with local guardrails; no crew or governance runtime. | Throwaway edits, questions, and exploration. |
| **1 · Collaborator** | A coordinated role pack, one-active-writer policy, and a finite Completion Contract. | Everyday features and bug fixes. |
| **2 · Governed** | A hash-pinned manifest, hook-enforced write scope, path custody, verifier tasks, and an audit trail. | Auth, secrets, payments, migrations, public APIs, and other high-risk work. |

```mermaid
flowchart TD
    C(["A change to make"]) --> Q1{"Throwaway edit,<br/>question, or exploration?"}
    Q1 -->|yes| T0["Tier 0 · Bare<br/>Claude Code + guardrails"]
    Q1 -->|no| Q2{"Auth · secrets · payments ·<br/>migration · public API ·<br/>audit trail needed?"}
    Q2 -->|yes| T2["Tier 2 · Governed<br/>hash-pinned manifest ·<br/>enforced scope · verifier gate"]
    Q2 -->|no| T1["Tier 1 · Collaborator<br/>model-tiered crew · single writer"]
```

See [the concepts guide](docs/concepts.md) for the decision rule and the intended
boundaries between tiers.

## Project status

What already works is listed under [What works today](#what-works-today) above.
The rest of the Tier 2 surface is a reference implementation, and the known gaps
are collected under [Roadmap / Known limitations](#roadmap--known-limitations)
below.

### Experimental / reference implementation

- The Windows/PowerShell Tier 2 lifecycle helper and Claude Code hook scripts in
  [`docs/governance/reference/`](docs/governance/reference/).
- Hash checks detect changes to an approved manifest, and the helper requires a
  new revision for changed intent; the file is not made immutable by the
  operating system.
- The `PreToolUse` write gate returns a machine-readable deny decision for an
  `Edit`, `Write`, or `NotebookEdit` target outside a running task's literal
  `write_set` when the hook is installed and the governed environment is active.
- The helper implements workspace snapshots, path-custody hashes, task state,
  failure-signature handling, verifier-task completion gates, helper-appended
  JSONL events, and hashed terminal handoff receipts.

## Install

Provost ships as a Claude Code plugin:

```bash
claude plugin marketplace add norton77930/Provost
claude plugin install provost@provost
```

The same two steps are available in a session as `/plugin marketplace add` and
`/plugin install`.

That installs the six collaborator roles, the six methodology skills, and the
orchestration policy. The policy reaches each session through a `SessionStart`
hook, so there is nothing to merge into a `CLAUDE.md` by hand.

The collaborator tier is plain Claude Code configuration. It runs wherever
Claude Code runs and needs no proxy, gateway, or extra service. The Tier 2
reference runtime described further down is Windows-only today.

To run against a local checkout instead of a release:

```bash
claude --plugin-dir .
```

### What the plugin ships

| Role | Model | Responsibility |
|---|---|---|
| `explorer` | haiku | Read-only evidence gathering |
| `implementer` / `implementer-deep` | sonnet / opus | Bounded TDD implementation |
| `test-analyst` | haiku | Existing test execution without source edits |
| `code-reviewer` / `architecture-reviewer` | opus | Read-only assurance |

These are configuration choices, not portable model abstractions. You can edit
the role mapping for another model available to your Claude Code environment;
Provost does not ship or certify a third-party gateway.

Alongside the roles are six methodology skills — TDD, bug diagnosis, design
grilling, two-axis review, and completion verification. Attribution for the
vendored skills is in [`skills/NOTICE.md`](skills/NOTICE.md).

## Try it

Start with a plan and a finite Completion Contract. Delegate read-only
exploration and review as needed, and keep exactly one active writer. The
[orchestration policy](orchestration.md) is what the crew follows, and the
[concepts guide](docs/concepts.md) explains when to move between tiers.

## Open a governed session (Windows)

```powershell
.\docs\governance\reference\Start-GovernedSession.ps1 -WorkspaceRoot D:\path\to\repo
```

The launcher sets the `PROVOST_*` environment the hooks read and registers the
Tier 2 hooks for that session only, through `claude --settings`. They are not
registered by the plugin on purpose: a hook costs a PowerShell process on every
matching tool call, and the plugin installs for everyone, including the majority
who never open a governed session. A project's own `PreToolUse` hooks still
run: hooks supplied this way are added to them, not swapped for them.

It refuses rather than proceeding when the workspace is not a Git repository,
when a governance hook is missing or does not parse, or when no PowerShell
interpreter is available. The last case is the one that matters: a hook whose
interpreter cannot be found blocks nothing and reports nothing, so a session
would call itself governed while enforcing nothing.

`Initialize` makes the remaining check. It refuses to open a run unless the
`SessionStart` hook has written a liveness marker naming that session. The
marker is evidence that the hooks loaded and ran, not a credential: it is a
plain file in the workspace, so anything that can write the workspace can write
it. It catches a misconfigured session, not one that is lying on purpose.
`StartTask`, `FinishTask`, `RecordRetry`, and `Complete` of an enforced lock
then require the same Claude Code session that opened it; supplying a different
helper `-SessionId` does not recover the refusal.

## Tier 2 reference checks (Windows)

Each governance decision also has a check that exercises it on its own, which is
the quickest way to see one without opening a session. Every check builds an
isolated workspace, drives the shipped hook or helper through its real
interface, and leaves the repository unmodified.

From the repository root on Windows:

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-WriteScope.ps1
```

| Decision | Check | Needs `git` | Walkthrough |
|---|---|---|---|
| Write scope | `Test-WriteScope.ps1` | no | [demo](docs/examples/governed-write-scope-demo.md) |
| Ref guard | `Test-RefGuard.ps1` | no | [demo](docs/examples/governed-ref-guard-demo.md) |
| Manifest pin | `Test-ManifestPin.ps1` | yes | [demo](docs/examples/governed-manifest-pin-demo.md) |
| Path custody | `Test-PathCustody.ps1` | yes | [demo](docs/examples/governed-path-custody-demo.md) |
| Completion gate | `Test-CompletionGate.ps1` | yes | [demo](docs/examples/governed-completion-gate-demo.md) |
| Failure diagnosis | `Test-FailureDiagnosis.ps1` | yes | [demo](docs/examples/governed-failure-diagnosis-demo.md) |
| Audit artifacts | `Test-AuditArtifacts.ps1` | yes | [demo](docs/examples/governed-audit-artifacts-demo.md) |
| Session liveness | `Test-SessionLiveness.ps1` | yes | — |
| Continuation enforcement | `Test-ContinuationEnforcement.ps1` | yes | — |

Each walkthrough records the prerequisites, the expected output, and the
limitations of what that check proves.

## Tier 2 governance details

The governed tier's reference design covers:

- **Hash-pinned manifests:** approved content is hashed and checked through the
  lifecycle; changed scope requires a new revision.
- **Write-scope decisions:** the write hook fails closed and denies paths outside
  a running task's literal `write_set`.
- **Path custody:** shared writer paths require dependency ordering and recorded
  content evidence at handoff.
- **Completion gating:** a successful run requires every declared task, including
  required verifier tasks, to have reached `PASS`.
- **Failure control:** repeated failure signatures can require new diagnosis
  evidence before another revision proceeds.
- **Audit artifacts:** lifecycle actions append JSONL events; terminal handoff
  receipts pin the manifest, ledger, and workspace snapshot by hash.

These controls become engine-enforced when the hooks are registered with Claude
Code and the required `PROVOST_*` environment is set. `Start-GovernedSession.ps1`
does both for one session, and `Initialize` refuses to open a run that cannot
show the hooks are live. Read the
[governance documentation](docs/governance/README.md) before evaluating or
adapting the reference implementation.

## Running other models

The governance concepts do not depend on a model's reasoning style. The shipped
integration does depend on Claude Code interfaces. If you choose to place another
model behind an Anthropic-compatible gateway, that gateway is an external part of
your setup; Provost neither ships nor endorses one. Operational observations from
such setups are recorded separately in [field notes](docs/field-notes.md).

## Prior art and positioning

Provost builds on native Claude Code primitives—subagents, hooks, and skills. The
collaborator pattern is well explored elsewhere:

- [pilotfish](https://github.com/Nanako0129/pilotfish) focuses on the cost split
  between a frontier orchestrator and cheaper workers.
- [claude-agent-team](https://github.com/ek33450505/claude-agent-team) focuses on
  local session observability.

Provost's focus is graduated governance: increasing oversight with blast radius
and exploring enforceable scope, custody, verification, and auditability for the
highest tier.

## Roadmap / Known limitations

- A packaged installer. The launcher registers hooks per session; it does not
  write a persistent Claude Code configuration.
- Fresh agent contexts per task, which the original launcher created and this one
  does not.
- A packaged runtime. Opening a governed session is documented and works, but
  everything around it is still assembled by hand.
- Complete per-claim evidence binding. Acceptance entries are validated, and a
  task may record a verification summary, but the helper does not yet require a
  separate evidence record for every acceptance claim.
- Linux/macOS support and adapters for additional coding-agent environments.

See the [governance capability matrix](docs/governance/README.md) and
[`ROADMAP.md`](ROADMAP.md) for the exact implementation boundary and next steps.

## Contributing

Bug reports, governance proposals, documentation improvements, tests, packaging,
and cross-platform work are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md)
and keep pull requests narrowly scoped. Changes must not silently weaken a stated
governance guarantee. Notable changes are listed in [`CHANGELOG.md`](CHANGELOG.md).

Report a suspected vulnerability through the private channel in
[`SECURITY.md`](SECURITY.md) rather than a public issue.

## License

MIT — see [`LICENSE`](LICENSE). Vendored skills retain their original MIT
attribution in [`skills/NOTICE.md`](skills/NOTICE.md).
