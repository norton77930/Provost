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
chain), `list --legacy` lists their snapshots, and `resume` can adopt one. A
session whose SessionStart bound it only in a legacy store (it started before the
switch) still works: `status` reports `legacy_binding`, and the first `record` or
`snapshot` copies the validated binding into the new store. Nothing is ever written
to or deleted from legacy stores; remove them yourself when no longer needed.
This bridge is temporary.

## Read status before writing

In Bash (Git Bash on Windows), quote each argument:

```bash
bash "<runtime>/hooks/runtime" status --workspace "<root>" --session "<session>"
```

Output: expected (revision or null), checkpoint (complete current record or null),
facts (summary of the session's `facts.json`, or null before the first PreCompact)
and template (all required fields). Read-only. SESSION_NOT_BOUND means fix the
session startup/workspace configuration; do not fabricate binding files.

`facts` holds `path` (the facts.json file), `updated_at`, `last_change_at`, `repos`
(at most 5: `root`, `branch`, `dirty` listed/`dirty_total` uncommitted entries,
`commits` made during the session, `error`), `repos_omitted`, `agents` (dispatch
count by role), `agents_omitted`, `files`/`files_omitted` (edited repo files kept
or dropped by the size cap), `outside_repo_count`, and collection health:
`events_captured` (valid hook events folded so far) and `spool_errors` (unreadable
spool lines skipped). When facts exist,
`template.sources` is prefilled with up to 10 most recently edited repo files as
`{"kind": "file", "ref": <path>}`; delete or edit them like any other field.

## Query what the session changed

```bash
bash "<runtime>/hooks/runtime" changes --workspace "<root>" --session "<session>"
```

Read-only: `facts.json` merged with the spool lines the next PreCompact would fold
(the live spool and any `.folding-*` leftover past its recorded offset), plus a
fresh `git` query of branch, uncommitted entries and session commits per repo.
Nothing is written: not `facts.json`, not the spool. Output: `workspace`,
`session_id`, `facts_path`, `folded_at` (when facts.json was last written, or null),
`pending_events`, `pending_invalid` and `facts` (the merged body in the facts.json
format, without checksum). Git decides the current state; the hook log only shows
what the session touched.

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

SessionStart context stays within 8,000 characters (Claude Code replaces a larger
one with a 2 KB preview) and never fails for its size. Startup, clear and fork
carry identity, operations and the new-work notice only. Compact and resume add,
in priority order: the save status with the required action (after a facts-only
save: write a record from the compaction summary; status prefills facts and
`template.sources`), the facts summary (one line per repo, at most 5: branch,
uncommitted count, session commit count; a subagent count by role; edits outside
repos; a `Facts capture:` line when spool lines were unreadable or the session
compacted with zero captured events, which means capture may be failing; omitted
without `facts.json`), the checkpoint (payload, or its digest when
over 4,500 characters) and the edited repo files, most recent first. Over budget,
whole items are replaced, never cut: the file list first (by the facts.json path),
then the checkpoint payload (its digest, then only the checkpoint path), then the
facts summary (by its path), and last the identity (paths and `status` usage only).

PreCompact folds the facts spool into facts.json and snapshots an existing record,
without a model call. Without a record the save is partial ("decision record
missing"), never an empty snapshot; a record older than the latest change is
partial-stale. The fold and these save levels apply only to a session with the
enablement marker `<provost dir>/cache/facts/<session_id>.enabled`, which the
native SessionStart creates when adaptive mode resolves for the workspace and
handoff is enabled. Without it (the plugin path, or a session started before the
facts layer was installed) PreCompact only snapshots an existing record, or reports
`NO_CHECKPOINT`. Untracked (`??`) files are listed as dirty but never date a
change, and file times in the future count as now. Startup/clear/fork do not adopt
old tasks.
Notifications and host/model behavior still need a live pilot. Normal Bash path
and payload input must satisfy project guards; report refusals rather than bypassing.

## Pruning stale bindings and expired sessions

Every SessionStart binds its session, so the store collects directories for
sessions that never recorded anything. `doctor` reports them as `stale_sessions`.
`prune` lists session directories that hold only `binding.json` older than
`--older-than-days` (default 14); it deletes nothing unless `--apply` is given.

`prune` also lists, under `expired`, sessions that hold records, snapshots,
`facts.json` or `compaction.json` and whose newest activity is older than
`--expired-older-than-days` (default 30).
Activity is the newest record/snapshot timestamp (else file mtime) or the mtime of
`binding.json`, `compaction.json`, `current.json`, `latest.json`, `horizon.json` or
`facts.json`, so a session resumed today is not expired. Each listed session (and
reclaimable leftover) whose session id is known from `binding.json` or `facts.json`
also names, under `facts_cache`, its existing files in `<provost dir>/cache/facts`:
the spool `<session_id>.jsonl`, `.folding-*` spool leftovers, the three reminder
flags and the `.enabled` marker. `--apply` deletes them after the session dir itself is removed; a failed
delete is reported in `errors` with the file name in `left_at`. Cache files whose
session has no directory anywhere in the store are listed per session under
`orphan_facts_cache` (`candidates`, `removed`, `skipped`, `errors`; present only when
non-empty) once their newest file is older than the expiry age, and deleted only with
`--apply`; names without a `[A-Za-z0-9-]+` session id (`UNEXPECTED_NAME`) and linked
entries (`LINKED_STORAGE`) are skipped. `--older-than-days` (default 14) applies
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
reused against the current head records a new entry (the chain stays valid); a corrupt legacy store
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

`hooks/native-facts` captures facts and reminds the main session, which otherwise
tends to skip milestone records. Register it twice: for `PostToolUse` as an async
hook, and for `UserPromptSubmit` as a sync hook.

- `PostToolUse` appends facts to `<provost dir>/cache/facts/<session_id>.jsonl` and
  touches a flag in the same directory: `<session_id>.pending-edit` when the main
  session (no `agent_id`/`agent_type` in the input) edits a file with Edit, Write
  or NotebookEdit outside the scratch locations (`/.claude/jobs/`,
  `/.claude/projects/`, `AppData/Local/Temp`, `/tmp/`), and
  `<session_id>.pending-dispatch` when a writer subagent is dispatched (default
  roles `implementer`, `implementer-high`, `implementer-sonnet`, `implementer-deep`,
  `mech-executor`, `general-purpose`; override with the space-separated env var
  `PROVOST_REMINDER_ROLES`). Its output is always `{}`.
- `UserPromptSubmit` reminds at the start of the next turn, once: when a pending
  flag exists and `<session_id>.reminded` does not, it creates `.reminded` and adds
  the reminder as context; otherwise it answers `{}`. A successful `record`
  (including an idempotent replay) deletes all three flags, so the next edit or
  dispatch re-arms it. A background subagent's result also arrives as a
  `UserPromptSubmit`, so results get the reminder too.

Nothing is written or reminded unless the session's enablement marker
`<session_id>.enabled` exists in the facts directory. The native SessionStart
creates the directory and the marker, best effort, only when adaptive mode
resolves for the workspace and handoff is enabled; the marker is authoritative.
The hook first checks the config for `"handoff": true` only as a cheap early exit,
and also exits when the session id is not `[A-Za-z0-9-]+` or the facts directory
does not exist (it never creates either). `record` deletes the reminder flags, never
the marker; `prune` removes the marker with the session's other cache files. The
hook never blocks and always exits 0. It is pure bash with no interpreter,
subprocess or pipe, because each process costs about 0.5 s on Windows.

Known limits: `PostToolUse` is async, so an edit that happened just before a
`record` can land its flag after `record` cleared the flags, giving one spurious
reminder at the next turn; recording again (or ignoring it) clears it. A session
started before the facts layer was installed has no marker until its next
SessionStart (resume or compact); from then on its facts, including the
`git log --since` window of session commits, start at its first captured event,
not at the session's real start.

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Edit|Write|NotebookEdit|Agent|Bash|PowerShell|EnterWorktree|ExitWorktree", "hooks": [
        {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "async": true, "timeout": 20}
      ]}
    ],
    "UserPromptSubmit": [
      {"hooks": [
        {"type": "command", "command": "bash \"<runtime>/hooks/native-facts\"", "timeout": 10}
      ]}
    ]
  }
}
```

Do not register either hook for `SubagentStop`. That event's context is delivered
to the subagent's own conversation and keeps the subagent running, so it would loop
the worker instead of reaching main.

`hooks/native-reminder` is deprecated. It used to remind on `PostToolUse(Agent)`
synchronously, which cost every dispatch about 0.6 s. It now reads nothing and
answers `{}`, and stays one release so stale registrations do not error; remove
its registration.
