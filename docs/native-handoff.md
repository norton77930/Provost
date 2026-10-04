# Native handoff operations

Use the runtime path, workspace and session supplied by SessionStart. Do not
invent an ID or enable settings yourself.
The personal CLAUDE.md controls delegation; this runtime does not select agents.

## Where records live

Records are stored outside the repository, under
`<home>/v2/repos/<repo_key[:16]>/sessions/<sha256(session)>/`. The home is
`PROVOST_HANDOFF_HOME`, else `$PROVOST_HOME/handoffs`, else
`~/.claude/provost/handoffs`. `repo_key` is derived from the main checkout, so a
main checkout and all of its worktrees share one store: entering a worktree
mid-session keeps the same records, and deleting a worktree deletes none of them.
A home inside a Git work tree is refused (`STORE_INSIDE_REPO`) unless Git ignores
it. `doctor` reports `store_home` and `repo_key`.

Each session keeps its newest 5 records and 3 snapshots; older ones are removed
when a new one is written, and `horizon.json` records where the chain was cut so
reads still verify it. Retrying the request ID of the most recently removed record
returns `REQUEST_ID_EXPIRED` (see Known limits below).

Older stores under `<checkout>/.provost/handoffs/v1` are read-only: when the new
store has nothing for a session, `status` and compact/resume recovery fall back to
them (marked `legacy`; `expected` stays null so the first record starts a fresh
chain), `list --legacy` lists their snapshots, and `resume` can adopt one. Nothing
is ever written to or deleted from them; remove them yourself when no longer needed.
This bridge is temporary.

## Read status before writing

In Bash (Git Bash on Windows), quote each argument:

```bash
bash "<runtime>/hooks/runtime" status --workspace "<root>" --session "<session>"
```

Output: expected (revision or null), checkpoint (complete current record or null),
and template (all required fields). Read-only. SESSION_NOT_BOUND means fix the
session startup/workspace configuration; do not fabricate binding files.

Claude Code moves a transcript to a sibling `<project>--claude-worktrees-<name>`
project dir when a session enters a worktree; the runtime follows it (newest
same-named file wins) so `transcript_cursor` stays non-null, and it is still null
when no such file exists. `doctor` reports `last_binding_at` (UTC time of the newest
`binding.json` in this workspace's store, else null) and sets `host_verified` true
exactly when one exists, that is, when a SessionStart hook has bound a session here.

Fill work_id, phase and next; use actual sources and observations. sources entries:
kind=file|url, ref, optional item. assignments entries: task, agent_id, observed_state,
write_scope (list). evidence entries: claim, ref, state (pass-observed|fail|unverified|stale).
decisions, blockers and suggested_skills are string lists. Fields are required;
empty lists are fine when truthful. No credentials, raw transcripts or large logs.
Reference existing tasks/tickets rather than copying them. Maximum 32 KiB.

Write UTF-8 JSON with Write/Edit to an allowed scratch file, for example:

```json
{
  "work_id": "TICKET-123",
  "phase": "implementation",
  "sources": [
    {"kind": "file", "ref": "docs/specs/feature.md", "item": "acceptance criteria"},
    {"kind": "url", "ref": "https://example.com/tickets/TICKET-123"}
  ],
  "assignments": [
    {"task": "T1 add parser", "agent_id": "implementer-1", "observed_state": "running", "write_scope": ["src/parser"]}
  ],
  "decisions": ["Keep the existing parser API"],
  "blockers": [],
  "evidence": [
    {"claim": "parser tests pass", "ref": "tests/test_parser.py", "state": "pass-observed"}
  ],
  "next": "Review T1 result before dispatching T2",
  "suggested_skills": []
}
```

Then record it with a single-line command:

```bash
bash "<runtime>/hooks/runtime" record --workspace "<root>" --session "<session>" --expected "<revision-or-null>" --request-id "<unique-milestone-id>" --input "<payload-file>"
```

Keep each runtime call a single plain command: no heredoc, pipe, command
substitution, env-var prefix or line continuation, because worktree guards refuse those.

Use a new request ID for new content; retry the same ID only with identical content.
After REVISION_CONFLICT rerun status and reconcile. Never force an overwrite.
Record approved plan, dispatch, results and changed decisions/blockers. Do not
record after every tool call. Main owns records; workers report observations.

## Explicit save and resume

```bash
bash "<runtime>/hooks/runtime" snapshot --workspace "<root>" --session "<session>" \
  --request-id "<unique-save-id>"
bash "<runtime>/hooks/runtime" list --workspace "<root>"
bash "<runtime>/hooks/runtime" resume --workspace "<root>" --session "<new-session>" \
  --from "<snapshot-id>"
```

For /handoff: refresh record first, snapshot second. Report snapshot ID only after
success. For continuation: select exact work/workspace; error entries are not
usable candidates. Ask when multiple candidates remain. Same-session compact or
resume already supplies observations: do not adopt that session into itself.

Verify current sources, affected evidence and worker ownership before dispatch.
A closed ticket and recorded running worker are observations, not proof of
completion or current ownership. Do not duplicate uncertain live work.

PreCompact snapshots existing records without a model call. No checkpoint means
NO_CHECKPOINT, not a successful empty save. Startup/clear/fork do not adopt old tasks.
Notifications and host/model behavior still need a live pilot. Normal Bash path
and payload input must satisfy project guards; report refusals rather than bypassing.

## Pruning stale bindings and expired sessions

Every SessionStart binds its session, so the store collects directories for
sessions that never recorded anything. `doctor` reports them as `stale_sessions`.
`prune` lists session directories that hold only `binding.json` older than
`--older-than-days` (default 14); it deletes nothing unless `--apply` is given.

`prune` also lists, under `expired`, sessions that hold records or snapshots and
whose newest activity is older than `--expired-older-than-days` (default 30).
Activity is the newest record/snapshot timestamp (else file mtime) or the mtime of
`binding.json`, `compaction.json`, `current.json`, `latest.json` or `horizon.json`,
so a session resumed today is not expired. `--older-than-days` (default 14) applies
to binding-only dirs only. `doctor` reports `expired_sessions` at the default age.
`--apply` removes expired sessions (counted in `expired_removed`) with the same
move, re-check and delete steps as below; a session that became fresh or locked in
between is moved back. Skipped: the `--session` dir and its `.pruning-` leftover, a
dir whose lock is held (`STORE_BUSY`), a dir with unexpected content
(`UNEXPECTED_CONTENT`), and a dir whose stored `workspace_id` belongs to another
repo (`FOREIGN_REPO`). Nothing is deleted automatically, and legacy `.provost`
stores are never pruned.

Known limits: `horizon.json` carries no checksum, so tampering with it can hide or
remove records; a reader that overlaps a trim can see a transient
`CORRUPT_RECORD`/`INCOMPLETE_CHAIN` that a retry clears; `REQUEST_ID_EXPIRED`
covers the newest trimmed id and on-disk leftovers, while an older trimmed id
reused against the current head records a new entry (the chain stays valid); a
session that compacted but never recorded never expires; a corrupt legacy store
surfaces as "Checkpoint unavailable" during recovery; submodules, bare-backed and
separate-git-dir checkouts are refused (`REPO_KEY_UNRESOLVED`); Git older than
2.31 is refused (`GIT_UNSUPPORTED`); on Windows without long-path support a long
user or `PROVOST_HOME` path can exceed 260 characters.
Pass `--session <current-id>` to keep the running session. Directories with any
record, snapshot, lock or other file, linked or reparse-point entries, and names
that are not session digests are never candidates. With `--apply`, each candidate
is first moved to `.pruning-<digest>` and re-checked; if its `binding.json`
changed meanwhile (a resume rebinding it) the directory is moved back untouched,
and a failed delete is reported in `errors`: if `binding.json` itself could not be
removed it is moved back; if only the final directory removal fails, an empty
`.pruning-<digest>` is left and named in `left_at`. Later runs list such staging
dirs in `leftovers` and, with `--apply`, reclaim the empty ones and those holding
only an old `binding.json` (counted in `reclaimed`); any other content is kept.
Linked or reparse-point entries are listed in `skipped` with the reason. Age is the
only liveness signal: a session idle for longer than the threshold that never
recorded looks the same as a finished one, and a later resume simply rebinds it.

```bash
bash "<runtime>/hooks/runtime" prune --workspace "<root>" --session "<session>"
```

Show the listed candidates to the user before running the same command with
`--apply`.

## Reminder hook

`hooks/native-reminder` is an optional, advisory nudge for the main session, which
otherwise tends to skip milestone records. Register it for `PostToolUse` with
matcher `Agent` only: when a writer subagent is dispatched it reminds main to
record the dispatch now and the result once it arrives.

Do not register it for `SubagentStop`. That event's context is delivered to the
subagent's own conversation and keeps the subagent running, so it would loop the
worker instead of reaching main; the hook answers `{}` there.

It only reminds for writer roles (default `implementer`, `implementer-high`,
`implementer-sonnet`, `mech-executor`, `general-purpose`; override with the
space-separated env var `PROVOST_REMINDER_ROLES`). Read-only roles, unknown events,
malformed input and a config without `"handoff": true` produce `{}`. The hook adds
context only: it never blocks, never records, and always exits 0. It is pure bash
with no interpreter launch, so it stays cheap where process spawns are slow.

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Agent", "hooks": [
        {"type": "command", "command": "bash \"<runtime>/hooks/native-reminder\"", "timeout": 10}
      ]}
    ]
  }
}
```
