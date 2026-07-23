# Provost

**Match oversight to blast radius.** — a graduated governance framework for Claude Code agents.

*[中文說明 →](README.zh-TW.md)*

---

Most setups give an AI coding agent one fixed amount of process — the same
ceremony for a typo as for a payment-system migration. Provost makes **oversight a
dial**. Three tiers let you match the ceremony to the *blast radius* of a change:
a one-line fix runs solo; a normal feature runs through a coordinated crew of
model-tiered subagents under a single-writer discipline; and a high-stakes change
runs under an immutable manifest with engine-enforced write scope, path custody,
and evidence-per-claim completion.

It is **model-agnostic** and runs on **vanilla Claude Code** with native Anthropic
models — cheap models for the grunt work, the strongest model reserved for
orchestration and review.

## The dial

| Tier | What it is | Use it for |
|---|---|---|
| **0 · Bare** | Just Claude Code with guardrails — no crew, no ceremony. | Throwaway edits, questions, exploration. |
| **1 · Collaborator** | A crew of model-tiered subagents, one active writer, a finite Completion Contract. | Normal features and bug fixes. |
| **2 · Governed** | Immutable manifest, engine-enforced write scope, custody, per-claim evidence, fresh verifier. | Auth, migrations, public APIs — anything you'll be audited on. |

The goal is to **stop paying tier-2 costs for tier-0 work** — and to stop running
tier-0 recklessness on tier-2 changes. See [`docs/concepts.md`](docs/concepts.md)
for the full model and the decision rule.

## Quickstart (Tier 1 — the collaborator crew)

Runs on vanilla Claude Code; no proxy, no extra services.

1. Copy the role files into your project (or `~/.claude/`):
   ```bash
   cp -r .claude/agents/ your-project/.claude/agents/
   ```
2. Merge [`orchestration.md`](orchestration.md) into your project's `CLAUDE.md`
   (or `~/.claude/CLAUDE.md`).
3. Run Claude Code on Opus and work as usual — plan first, then let the crew
   execute. The main agent delegates read-only work to `explorer` and the
   reviewers, keeps one active writer, and stops when the Completion Contract is
   met.

The roles pin models to match cost to the job:

| Role | Model | |
|---|---|---|
| `explorer` | haiku | read-only evidence gathering |
| `implementer` / `implementer-deep` | sonnet / opus | bounded TDD writers |
| `test-analyst` | haiku | runs existing tests, never edits |
| `code-reviewer` / `architecture-reviewer` | opus | read-only review |

> _Demo: terminal recording coming — Opus orchestrating Haiku/Sonnet workers on a real task._

## Tier 2 — governance

For high-blast-radius work, the collaborator discipline gets *enforced* rather
than merely agreed to: an immutable manifest, hooks that block out-of-scope writes
at the engine level, path custody, per-claim evidence, and a fresh-context
verifier. Design and a reference implementation: [`docs/governance/`](docs/governance/).

## Running other models

Provost is prompts, roles, and a governance design — nothing is tied to a vendor.
The examples use native Anthropic models so anyone can reproduce them. To run other
models behind Claude Code, point it at any Anthropic-compatible gateway with a
router such as [CC Switch](https://github.com/farion1231/cc-switch) or CLIProxyAPI
— an external choice Provost neither ships nor endorses. Provost is the crew and
the process; the router is just which engine they run on.

## Also here

- [`docs/field-notes.md`](docs/field-notes.md) — hard-won operational notes on
  running Claude Code (the context-window internals; the bundled `claude-api`
  skill that can blow your window in one message).
- [`skills/`](skills/) — the methodology skills the crew uses (TDD, bug diagnosis,
  review, verification-before-completion). Some are vendored from third-party MIT
  projects; see [`skills/NOTICE.md`](skills/NOTICE.md).

## Prior art & positioning

Provost is built entirely on native Claude Code primitives — subagents, hooks,
skills. It is a *proactive* governance layer: it constrains scope and gates
completion on evidence up front. If you want retrospective observability — a
queryable record of every session — projects like
[claude-agent-team](https://github.com/ek33450505/claude-agent-team) approach the
same "governance layer for Claude Code" space from the record-keeping angle.

## Roadmap

- **Phase A (now):** the collaborator tier as a runnable, cross-platform drop-in;
  the governed tier as design + a Windows/PowerShell reference implementation.
- **Phase B:** a clean, cross-platform, model-agnostic port of the governed tier
  as an installable tool; possibly a UI for composing role catalogs.

## License

MIT — see [`LICENSE`](LICENSE). Vendored skills retain their original MIT
attribution in [`skills/NOTICE.md`](skills/NOTICE.md).
