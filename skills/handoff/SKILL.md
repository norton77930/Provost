---
name: handoff
description: Use when the user requests a Provost handoff or explicitly asks to continue previously saved work.
argument-hint: "[resume <snapshot-id> | search <terms>]"
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

1. Run `status --workspace "<root>" --session "<id>"`. It is read-only and returns
   the expected revision, the current checkpoint and a complete payload template
   (recently edited files prefilled as sources when facts exist). Expected is
   per work_id: `other_works` lists the session's other works, and when the
   payload's work_id is not the checkpoint's, run
   `status --workspace "<root>" --session "<id>" --work "<work_id>"` instead
   (`expected` null starts that work's chain).
2. Fill the template with actual task/source status: reference existing specs,
   tickets, ADRs and evidence; the state card (phase, next, blockers,
   assignments, sources) is replaced in full each time. Decisions and evidence go
   to the work's append-only log: put only entries that are NEW since this
   session's last record in `new_decisions` (`{text, refs?, supersedes?}`) and
   `new_evidence` (`{claim, state, ref, updates?}`); the old `decisions` /
   `evidence` fields are refused. Follow Writing entries below. Exclude
   credentials and full transcripts. Never treat an unfilled template as saved work.
3. Write the payload to a file with the Write tool, then run one plain command:
   `record --workspace "<root>" --session "<id>" --expected "<revision-or-null>" --request-id "<unique-id>" [--join] --input "<payload-file>"`,
   using the `expected` value status returned for the payload's work_id (`null`
   when there is none).
   Worktree guards refuse heredocs, pipes, command substitution and line
   continuations; never interpolate payload into executable shell text.
   A revision conflict means rerun status (with `--work "<work_id>"`) and
   reconcile, never overwrite.
   `JOIN_REQUIRED` means another session already logged this work_id. Add
   `--join` only when you intend to continue that work (or resume its snapshot
   instead); otherwise choose a new work_id. Once this session has a card of the
   work, later records need no flag. Set phase `done` (any case) to close a
   finished work; a later record reopens it with a `REOPENED` warning.
   Read the returned `warnings` (short decision, unreferenced commits or
   dispatches, no new decision, duplicate skipped, concurrent session, reopened,
   large log) and the `echo` (state card, open evidence, newest decisions): fill
   real gaps with another record instead of ignoring them.
4. Use `snapshot --workspace "<root>" --session "<id>" --request-id "<unique-id>"`.
   It holds every open state card of the session, not only the last one written.
   Report the returned snapshot ID and local path. On failure, say it was not
   saved; do not call an old snapshot current.

## Writing entries

- A decision is one self-contained sentence: what was decided, `because` why,
  with specifics (files, values, PR/issue numbers). No pronouns or "as above":
  a session that never saw this conversation must understand it.
- Put commit hashes (7+ characters) and dispatched roles/tasks in `refs` or the
  text, so the commit and dispatch checks find them.
- Never restate old entries. To correct or retract a decision, write a new one
  whose `supersedes` names it (`D3`); supersede the latest entry of a chain. To
  change a claim's state, write new evidence whose `updates` names the earlier
  entry (`E2`). Targets are ids of the same work.
- Evidence `state` is `pass-observed`, `fail`, `unverified` or `stale`.

## Resume

1. Only proceed when the user asks to continue or provides a snapshot ID.
2. Use `list --workspace "<root>"`. Each entry identifies a source session, time,
   parent snapshot and `works` (every work the snapshot holds, with its phase).
   Match the requested work and exact workspace.
   Do not select a global latest timestamp across different work.
   List entries with error are damaged candidates, not resumable snapshots; report
   them briefly and retain usable candidates. Never delete history to recover.
3. If several candidates remain, ask which work/session. Never start a worker
   while the correct handoff or existing writer ownership remains unresolved.
4. Use `resume --workspace "<root>" --session "<current-id>" --from "<snapshot-id>"`,
   adding one `--work "<work_id>"` per work the user chose. A snapshot with one
   work needs no `--work`. When it holds several and none is named, resume adopts
   nothing and returns `WORK_CHOICE_REQUIRED` with `choices` (work, phase, next
   step): show them, ask which works to continue, and run resume again with
   those `--work` flags. Never pick for the user.
   This adopts observations into the current session, not running worker ownership.
   For the same session use its supplied checkpoint rather than adopting itself.
   Evidence that passed before the adoption is shown as "passed before adoption,
   not re-verified": re-check it before relying on it.
5. Read referenced sources and relevant working changes/evidence. A closed issue,
   checked task or archived change is not proof that behavior passes acceptance.
   Establish whether recorded workers remain active before re-dispatching.
6. Reconcile completed, cancelled or superseded items and continue approved scope.
   Resolve factual gaps yourself; ask for product/authorization decisions or when
   duplicate execution remains possible.

## Search

`/handoff search <terms>`: find why something was decided in earlier sessions.

1. Run `search --workspace "<root>" --query "<terms>"`; add `--all-repos` only
   when the user asks to look beyond this repo, `--since YYYY-MM-DD`,
   `--work "<work_id>"` or `--kind decision|evidence` to narrow.
2. Show the compact results (time, work, field, text) in the returned
   order, mark superseded entries, and mention `truncated` or `skipped` when nonzero.
3. Only for the one result the user picks, run `search --workspace "<root>" --show "<ref>"`:
   a log entry comes with its supersede/update chain and neighbouring entries.
   Search is read-only and does not resume or adopt anything.

## Never run

`redact` rewrites a log entry and is maintainer-only: the user runs it with the
`!` prefix. If an entry holds a secret, tell the user and point at the guide's
Redact section. `migrate --apply` and `prune --apply` run only after the user has
reviewed the dry-run output, `migrate --apply` before any `prune --apply`.

Sources may be local Markdown, GitHub or GitLab. Preserve their native identities
and workflow gates. Do not archive, sync, close issues, create worktrees or commit
merely because a handoff was requested. History is local and not a code backup.
