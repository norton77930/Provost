# Adaptive orchestration and handoff pilot

Experimental and opt-in. Legacy Tier 1 is the default. Adaptive writer scheduling
is a prompt policy, not an enforced lock. Stricter project rules win.

## Personal setup

Use Bash (Git Bash on Windows), Python 3.11+ and Git for Git workspaces.
Python uses only its standard library; nothing is installed by hooks. The legacy
no-config path does not require Python.

Place personal JSON at ~/.claude/provost/config.json or set PROVOST_CONFIG to an
absolute path. Copy [the example](provost-user-config.json), replacing its placeholder
with the exact checkout root. Keep all_workspaces false for a pilot. Root allowlisting
limits personal rollout; it is not shared project or company configuration.

Handoff records live outside the checkout, under ~/.claude/provost/handoffs (or
PROVOST_HANDOFF_HOME / $PROVOST_HOME/handoffs), keyed by the main checkout so its
worktrees share them. No Git exclude is needed. A store home inside a Git work tree
must be ignored by that repository, otherwise writes are refused. Non-Git
workspaces require an explicitly selected directory.

## Runtime commands

SessionStart supplies the runtime path, workspace root and real session ID.
Do not invent the session ID. Session-specific writes require a matching
SessionStart binding; SESSION_NOT_BOUND means fix the host context first. In Bash:

```bash
bash "<plugin-root>/hooks/runtime" doctor --workspace "<root>"
bash "<plugin-root>/hooks/runtime" list --workspace "<root>"
```

Prefer the hooks/runtime wrapper: it resolves Python 3.11+ and sets PYTHONPATH,
PYTHONSAFEPATH=1 and PYTHONUTF8=1 for the call. Calling python -m provost_runtime.cli
directly also needs those three set for that process, otherwise modules in the
current directory can shadow the runtime and non-ASCII text depends on the locale.
Do not replace persistent environment settings.
Doctor reports selected mode, Python support, storage readiness and host_verified,
which is true once a SessionStart binding exists for this workspace (last_binding_at
shows when). It does not diagnose project policy conflicts.

At meaningful milestones, main writes compact state to a payload file and records
it with one plain command (worktree guards refuse heredocs and line continuations):

```bash
bash "<plugin-root>/hooks/runtime" record --workspace "<root>" --session "<session>" --expected "null" --request-id "plan-1" --input "<payload-file>"
```

Payload file:

```json
{
  "work_id": "example",
  "phase": "approved",
  "sources": [{"kind": "file", "ref": "docs/example-plan.md", "item": "T1"}],
  "assignments": [],
  "decisions": ["One approved bounded slice"],
  "blockers": [],
  "evidence": [],
  "next": "Read actual sources before dispatch",
  "suggested_skills": []
}
```

Replace illustrative sources with real ones. Later records use the returned revision
as --expected and a new request ID. Retry IDs only for identical content. On conflict,
reread the current chain; never force-overwrite. Evidence entries have claim/ref/state
(pass-observed, fail, unverified or stale). Assignments have task/agent_id/observed_state/
write_scope. Maximum payload is 32 KiB; exclude secrets and raw transcripts.

```bash
bash "<plugin-root>/hooks/runtime" snapshot --workspace "<root>" \
  --session "<session>" --request-id "handoff-1"
bash "<plugin-root>/hooks/runtime" resume --workspace "<root>" \
  --session "<new-session>" --from "<snapshot-id>"
```

Users may invoke /provost:handoff or /provost:handoff resume <snapshot-id>.
List entries with error identify damaged candidates; valid entries remain usable.
Resume adopts observations, not worker ownership. Verify sources, affected evidence
and worker state before continuing; do not duplicate potentially live work.

## History and failures

Data lives in <home>/v2/repos/<repo-key>/sessions/<hashed-session>/{records,snapshots}
(see [native-handoff.md](../native-handoff.md)). Complete objects are immutable by
the application; current/latest indexes are replaceable hints. Reads reconcile
complete chains, including completed objects left by interrupted index updates.
Corrupt/branching chains are rejected. Each session keeps its newest 5 records and
3 snapshots; horizon.json marks the cut so the remaining chain still verifies.
History is local and outside the repository, not Git version control or source-code
backup. Expired sessions are removed only by an explicit `prune --apply`; no remote
issue mutation occurs. Older .provost/handoffs/v1 stores are read-only fallbacks.

PreCompact snapshots the last full milestone without model summarization. Missing
records report NO_CHECKPOINT, not an empty successful save. PostCompact stores metadata.
Same-session compact/resume provides observations; startup/clear/fork never
automatically adopt previous work. Decisions main never recorded can be missing.

Save errors never block compaction. Interactive terminal notifications are best
effort; noninteractive hosts may ignore them. First/changed failure and recovery
notify; repeated errors use a brief message when the personal notification cache works.
SessionStart reports remembered failures. If the cache fails, deduplication cannot
be promised. Missing Python uses a fixed wrapper warning and cannot update that cache.

## Source workflows and live acceptance

Read [spec-kit](../compatibility/spec-kit.md),
[OpenSpec](../compatibility/openspec.md) or
[Matt-style skills](../compatibility/matt-workflow.md).
These are main-agent guidance, not source parsers or proof of model judgment.

1. Review exact personal config, local exclude and existing policy conflicts before enabling.
2. Keep the project's single-writer rule. Complete one bounded task and checkpoint.
3. Observe same-session compact, explicit new-session resume and controlled save
   failure/recovery with disposable data; confirm notifications are visible.
4. Verify actual Opus/high default and eligible Sonnet/high override separately.
   Check existence of routing overrides without exposing values.
5. Record time, waits, rework, main context and available usage; do not claim savings
   from unrelated tasks or count cached tokens as equal billing units.

Rollback: stop dispatch, reconcile workers, set mode legacy or remove the allowlist
root, then start a fresh session at a safe boundary. Preserve history/excludes.
Disabling does not cancel workers or retract already-injected context.

See [host validation status](../analysis/adaptive-host-validation.md).
