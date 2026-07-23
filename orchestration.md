# Provost orchestration policy (collaborator tier)

This is the orchestration policy for the **collaborator tier** of Provost — a
coordinated crew of read-only and writer subagents running on vanilla Claude
Code. To adopt it, drop the role files in [`.claude/agents/`](.claude/agents/)
into your project (or `~/.claude/agents/`) and merge this policy into your
project's `CLAUDE.md` (or `~/.claude/CLAUDE.md`) so the main agent follows it.

## Roles

The collaborator roles live in `.claude/agents/`:

- `explorer` (read-only) — locate files, trace behavior, gather evidence.
- `implementer` / `implementer-deep` (writers) — bounded TDD implementation.
- `test-analyst` (read-only + test execution) — run existing tests, analyze regressions.
- `code-reviewer` / `architecture-reviewer` (read-only) — evidence-based review.

Each role pins a model to match cost to the job: cheap models (Haiku) for
read-only and grunt work, a capable mid model (Sonnet) for normal
implementation, and the strongest model (Opus) reserved for hard changes and
review. Run the main/orchestrator agent on Opus.

## Flow

Discuss and plan first in Plan mode; after you approve the plan, continue in the
same session in Auto mode. Never use a permission-bypass mode.

## Delegation

- Handle simple, local tasks yourself with a minimal internal completion
  closure; do not add ceremony merely because roles are available.
- In Plan mode, delegate repository exploration to `explorer` and use up to
  three read-only roles concurrently (`explorer`, `test-analyst`,
  `code-reviewer`, `architecture-reviewer`).
- In Auto mode, maintain exactly one active writer at a time (the main agent,
  `implementer`, or `implementer-deep`). This is an orchestration policy, not an
  engine-level lock.
- An implementer owns RED -> GREEN slices: demonstrate a focused failing test
  before implementation, make the smallest change, run the relevant tests, and
  refactor only when that work is in the approved Completion Contract and all
  affected checks stay green.

## Finite completion closure

For every non-simple task, the user-visible Plan must contain a concise
**Completion Contract** before implementation begins: the approved scope and
non-goals, a finite list of required claims, the selected evidence for each
claim, any assurance roles selected for those claims, and one planned final
verification. Keep it finite; never add ceremony.

Roles are capabilities, not mandatory phases:

- Use focused repository evidence for documentation, styling, mechanical, and
  local behavior changes; a reviewer is not required by default.
- Select `test-analyst` only when broader test selection or regression behavior
  is itself an unresolved required claim.
- Select `code-reviewer` for a declared cross-module, authorization,
  data-conversion, error-handling, compatibility, correctness, or security claim.
- Select `architecture-reviewer` only for a declared public API, schema,
  dependency-boundary, lifecycle, concurrency, consistency, security-boundary,
  data-flow, or operational claim.

Maintain each claim as `planned`, `PASS-current`, `FAIL`, or `stale`. Before
every tool, test, or subagent call, be able to state:

    unresolved required claim -> action -> expected evidence

A call is permitted only when it advances a `planned`, `FAIL`, or `stale`
required claim, repairs a recorded failure, or performs the one planned final
verification. Do not call a role or tool merely to gain extra confidence,
discover optional improvements, or repeat already-current evidence.

Use the finite progression `active -> sealing -> done`:

- `active`: at least one required claim is `planned`, `FAIL`, or `stale`.
- `sealing`: every substantive required claim is `PASS-current`; only the one
  planned final verification may run.
- `done`: the final verification passed. Do not add optional review, test,
  analysis, refactor, or documentation work; report the result immediately.

Reopen the closure only when a required check actually fails, new evidence
concretely contradicts an in-scope required claim, or through a user-approved
scope expansion. A suggestion that does not contradict an approved claim is a
non-blocking follow-up and must not become new work in the current run.

Only evidence affected by a later task-scoped change becomes `stale`; perform
targeted revalidation for that evidence. Do not rerun a successful test, review,
or analysis when no relevant change invalidated it.

## Role boundaries

`explorer`, `code-reviewer`, and `architecture-reviewer` must not execute
commands or write files. `test-analyst` may execute existing, non-mutating test
and inspection commands but must not write sources, tests, fixtures, or
configuration; if a test command could mutate the repository, it reports the
limitation instead of running it.

Keep every role report within roughly 4,000 tokens: summarize evidence with
exact paths and lines; never paste whole files, because role reports accumulate
in the main agent's context.

Custom roles do not invoke nested subagents. Do not create commits, branches,
remotes, or worktrees unless the user explicitly asks.

---

For **when** to escalate from this collaborator tier to the full governance tier
(immutable manifests, scope-locking hooks, evidence-per-claim completion), see
[`docs/concepts.md`](docs/concepts.md) — the three-tier graduated-governance
model.
