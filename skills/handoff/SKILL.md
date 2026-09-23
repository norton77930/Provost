---
name: handoff
description: Use when the user requests a Provost handoff or explicitly asks to continue previously saved work.
argument-hint: "[resume <snapshot-id>]"
disable-model-invocation: true
---

# Provost handoff

Use the runtime path, workspace root and session ID supplied by the active
adaptive SessionStart context. If unavailable, run the documented doctor or ask
for the missing session context; do not invent an ID. This skill does not enable
Provost, change personal settings, or authorize project-rule changes.

Invoke the runtime using Bash with separately quoted arguments:
`bash "<plugin-root>/hooks/runtime" <command> ...`.
A Python invocation is also documented in the pilot guide.

## Save

1. Refresh relevant task/source status and the short checkpoint. Reference existing
   specs, tickets, ADRs and evidence. Include unresolved decisions, observed worker
   status and next actions. Exclude credentials and full transcripts.
2. Use `record --workspace "<root>" --session "<id>" --expected "<revision-or-null>" --request-id "<unique-id>"`.
   Send the documented JSON payload on stdin using a single-quoted heredoc or
   input file; do not interpolate payload into executable shell text.
   Use the checkpoint revision shown at resume, or read the current record
   reference from SessionStart. A revision conflict means reread, never overwrite.
3. Use `snapshot --workspace "<root>" --session "<id>" --request-id "<unique-id>"`.
   Report the returned snapshot ID and local path. On failure, say it was not
   saved; do not call an old snapshot current.

## Resume

1. Only proceed when the user asks to continue or provides a snapshot ID.
2. Use `list --workspace "<root>"`. Each entry identifies a source session, work,
   time and parent snapshot. Match the requested work and exact workspace.
   Do not select a global latest timestamp across different work.
   List entries with error are damaged candidates, not resumable snapshots; report
   them briefly and retain usable candidates. Never delete history to recover.
3. If several candidates remain, ask which work/session. Never start a worker
   while the correct handoff or existing writer ownership remains unresolved.
4. Use `resume --workspace "<root>" --session "<current-id>" --from "<snapshot-id>"`.
   This adopts observations into the current session, not running worker ownership.
   For the same session use its supplied checkpoint rather than adopting itself.
5. Read referenced sources and relevant working changes/evidence. A closed issue,
   checked task or archived change is not proof that behavior passes acceptance.
   Establish whether recorded workers remain active before re-dispatching.
6. Reconcile completed, cancelled or superseded items and continue approved scope.
   Resolve factual gaps yourself; ask for product/authorization decisions or when
   duplicate execution remains possible.

Sources may be local Markdown, GitHub or GitLab. Preserve their native identities
and workflow gates. Do not archive, sync, close issues, create worktrees or commit
merely because a handoff was requested. History is local and not a code backup.
