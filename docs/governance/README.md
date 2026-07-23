# The governed tier

Tier 2 of the dial. When a change's blast radius is high — auth, secrets,
payments, schema/data migrations, public APIs, anything you'll be audited on —
agreeing on scope isn't enough. You want it **enforced**, and you want an
evidence trail you can reconstruct afterward.

## What it adds over the collaborator tier

- **Immutable manifest.** After you approve a plan, the run is pinned to an
  immutable manifest (`r001.json`) hashed against that plan. Changing the scope,
  roles, dependencies, or acceptance means producing a new revision and
  re-approving — you can't quietly move the goalposts mid-run.
- **Engine-enforced write scope.** A `PreToolUse` hook blocks any Edit/Write
  outside the running task's literal `write_set` — at the engine level, not by
  asking the agent nicely. A second hook keeps declared external references
  read-only; a third verifies the session is anchored to the intended workspace
  root.
- **Path custody.** When one task hands changed files to a dependent task, the
  helper checks a pinned hash so nothing drifts between writers.
- **Per-claim evidence + a fresh-context verifier.** Every required claim in the
  Completion Contract carries selected evidence, and a fresh-context verifier task
  must pass before the run can be marked complete.
- **Failure-signature diagnosis gate.** A repeated identical failure can't just be
  retried forever — the same normalized failure signature twice forces a written,
  falsifiable diagnosis with new evidence before another attempt.
- **JSONL ledger + immutable handoff receipts.** Every terminal state leaves an
  append-only audit record; missing, orphaned, or tampered receipts are rejected.

## Reference implementation

[`reference/`](reference/) contains the actual engine this design came from:

- `Foreman-Manifest.ps1` — the ~1,700-line governance helper (initialize a run,
  start/finish tasks, enforce custody, complete a run).
- `hooks/` — the three `PreToolUse` / `SessionStart` scripts that do the
  engine-level enforcement.

**Read it as a reference, not a dependency.** It is Windows / PowerShell only and
was extracted from the author's private system (internally named "Foreman"); it
keeps its original internal identifiers (`provost-foreman-*`, `PROVOST_*`) and
assumes a launcher that sets those environment variables. A clean, cross-platform,
model-agnostic port is the Phase B goal — see the root README's roadmap.

The governance *model* — immutable manifest, engine-enforced scope, custody,
evidence-per-claim, diagnosis gate — is platform-independent; only this particular
implementation is Windows-bound.
