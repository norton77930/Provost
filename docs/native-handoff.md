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

Records are state cards kept per (session, `payload.work_id`): each work of a
session has its own chain in the session's `records/` dir, linked by
`parent_record`, and a chain belongs to the work_id of its newest record (its head).
The session's current card is the head written most recently (newest `recorded_at`;
`current.json` breaks a tie). Each work chain keeps its newest 5 records and the
session its newest 3 snapshots; older ones are removed when a new one is written,
and `horizon.json` records where each chain was cut so reads still verify it (one
id for a single cut chain, a list of ids when several work chains were cut).
Retrying the request ID of the most recently removed record of any chain returns
`REQUEST_ID_EXPIRED` (see Known limits below). Request IDs stay unique per session:
reusing one for another work's content is `REQUEST_ID_REUSED`.

Decisions and evidence live in a decision log, not in the cards. Each work_id has
one append-only JSON Lines file at the repository level, beside `sessions/`:
`<home>/v2/repos/<repo_key[:16]>/logs/<sha256(work_id as UTF-8)>.jsonl`. Every
session of the repository (a checkout and its worktrees) appends to the same file,
and pruning session directories never touches it. Line 1 is a header (`kind: "log"`,
`schema_version`, `workspace_id`, `work_id`); every later line is one entry:

- decision: `{id, kind: "decision", at, session, record, text, refs?, supersedes?}`
- evidence: `{id, kind: "evidence", at, session, record, claim, state, ref, updates?}`

`id` (`D1, D2, …` and `E1, E2, …`, sequential per work and kind), `kind`, `at`,
`session` and `record` (the revision of the card written with the entry) are filled
by the runtime. Ids are assigned and lines appended under a per-work lock
(`logs/<sha256(work_id)>.lock`) that a `record` waits for up to 10 seconds
(`STORE_BUSY` after), so concurrent writers from any session or worktree never
reuse an id. Entries are never rewritten. A line exists only once its terminating
newline is written: all lines of one `record` go out in one write followed by
fsync. Bytes after the last newline are a torn append (a crash mid-write): readers
ignore them and the next append cuts them off. A newline-terminated line that is
not a valid entry, or a header naming another repo or work, makes the log fail
closed (`CORRUPT_LOG`, `FOREIGN_REPO`): `status` reports the error and `record`
refuses to append. A `record` appends its entries before it writes its card; if it
is interrupted in between, replaying the same request appends only what is missing.
Entries logged by an interrupted request that is never replayed stay in the log.

Cards have `schema_version` 2 and no `decisions`/`evidence` fields; they carry
`log_entries`, the ids the record appended. Cards of schema 1 (written before the
decision log) stay readable as they are, with their decisions and evidence, and a
work's chain may mix both versions. Resuming a schema 1 snapshot adopts it as a
schema 1 card; resume never writes to the log.

Sessions written before per-work cards hold one chain whose records may carry
several work_ids. They are read as they are, with no migration: that chain is the
chain of its head's work_id, and the older works inside it have no chain of their
own. The first write for the head's work continues it (`--expected` the head, as
`status` reports); the first write for any other work starts a new chain
(`--expected null`, as `status --work <work_id>` reports).

## Excluding workspaces

The personal config enables adaptive mode per workspace (`workspace_roots`, exact
match) or everywhere (`all_workspaces: true`). The optional `exclude_roots` is a list
of absolute directories that always resolve to legacy, with no binding and no facts
marker, even under `all_workspaces` or an exact `workspace_roots` match. A workspace,
or the main checkout it belongs to, is excluded when it equals or lies inside an
excluded directory (path containment, not name prefix: `C:\Temp2` is not inside
`C:\Temp`). Use it for scratch locations such as `%TEMP%`, where automated `claude -p`
runs would otherwise bind a session each. An invalid list is `INVALID_EXCLUDE_ROOTS`.
Runtimes older than this key reject it as `INVALID_CONFIG`, which disables handoff
everywhere: add it only after installing a runtime that supports it, and remove it
before rolling back to an older one.

## Read status before writing

In Bash (Git Bash on Windows), quote each argument:

```bash
bash "<runtime>/hooks/runtime" status --workspace "<root>" --session "<session>"
```

Output: expected (revision or null), checkpoint (complete current record or null),
facts (summary of the session's `facts.json`, or null before the first PreCompact),
template (all request fields) and log (the decision log of the shown work: `total`
entries and the `newest` 10, oldest first; `{"error": <code>}` when the log is
unreadable; `total` 0 when no work is shown). Read-only. SESSION_NOT_BOUND means fix the
session startup/workspace configuration; do not fabricate binding files.

`expected` and `checkpoint` belong to the session's current card. Add
`--work "<work_id>"` to read that work's card instead (both null when the session
has not recorded it). When the session has cards of other works, `other_works` lists
them, most recently written first, as `work_id`, `phase`, `revision` and
`recorded_at`; a session with a single work has no `other_works` field.

`facts` holds `path` (the facts.json file), `updated_at`, `last_change_at`, `repos`
(at most 5: `root`, `branch`, `dirty` listed/`dirty_total` uncommitted entries,
`commits` made during the session, `error`; in facts.json each commit keeps `hash`,
`subject` and its commit time `ts`), `repos_omitted`, `agents` (dispatch
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
write_scope (list). blockers and suggested_skills are string lists. These card fields
are required; empty lists are fine when truthful. `new_decisions` and `new_evidence`
(optional, default empty) hold only what is new since this work's last record; the
runtime appends them to the work's decision log and returns their ids in
`log_entries`. new_decisions entries: `text` (non-empty), optional `refs` (string
list) and `supersedes` (the id of the decision it replaces). new_evidence entries:
`claim` (non-empty), `ref`, `state` (pass-observed|fail|unverified|stale), optional
`updates` (the id of the evidence entry whose claim it updates). The old `decisions`
and `evidence` fields are refused with `LEGACY_FIELDS` and a `detail` naming the new
fields; malformed entries are `INVALID_DECISION`/`INVALID_EVIDENCE`. No credentials,
raw transcripts or large logs. Reference existing tasks/tickets rather than copying
them. Maximum 32 KiB for the whole request.

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
  "blockers": [],
  "next": "Review T1 result before dispatching T2",
  "suggested_skills": [],
  "new_decisions": [
    {"text": "Keep the existing parser API because two callers pin its signature", "refs": ["TICKET-123"]}
  ],
  "new_evidence": [
    {"claim": "parser tests pass", "ref": "tests/test_parser.py", "state": "pass-observed"}
  ]
}
```

Then record it with a single-line command:

```bash
bash "<runtime>/hooks/runtime" record --workspace "<root>" --session "<session>" --expected "<revision-or-null>" --request-id "<unique-milestone-id>" --input "<payload-file>"
```

Keep each runtime call a single plain command: no heredoc, pipe, command
substitution, env-var prefix or line continuation, because worktree guards refuse those.

Use a new request ID for new content; retry the same ID only with identical content
(card and new entries): a replay returns the stored card and appends nothing.
`--expected` is the head of the payload's work chain: when switching to another
work, read it with `status --work "<work_id>"` first.
After REVISION_CONFLICT rerun status and reconcile. Never force an overwrite.
Record approved plan, dispatch, results and changed decisions/blockers. Do not
record after every tool call. Main owns records; workers report observations.

## Search past decisions

```bash
bash "<runtime>/hooks/runtime" search --workspace "<root>" --query "<terms>" \
  [--all-repos] [--since YYYY-MM-DD] [--limit N]
bash "<runtime>/hooks/runtime" search --workspace "<root>" --show "<ref>" [--all-repos]
```

Finds why something was decided. Read-only keyword search over what the store
already holds: no model call, no index, no daemon, and nothing is created or written
(no files, locks or directories). The scope is the repo of `--workspace`; `--all-repos`
scans every repo dir under the store home. Like `list`, it needs handoff enabled for
the workspace (`HANDOFF_DISABLED` otherwise).

The corpus is each session's newest valid record (a session's decision list is
cumulative, so older records add nothing), every valid snapshot, and every valid file
in `archive/`. Unreadable, checksum-invalid or foreign files are skipped and counted
under `skipped`, never fatal. Searched items are each decision, each blocker, `next`
and each evidence `claim`. An item matches when every whitespace-separated query
term (case-insensitive substring) appears in the item text or in its record's
`work_id` or `phase`. Identical (`work_id`, `field`, text) hits are reported once, for
the newest record. Results rank by how often the terms occur in the item text, then by
`recorded_at`, newest first. `--since` keeps records recorded on or after that UTC
date; `--limit` defaults to 10 and must be 1 to 50 (`INVALID_LIMIT`); `truncated`
counts the matches beyond it.

Output: `query`, `repos_scanned`, `results`, `truncated`, `skipped`. Each result has
`recorded_at`, `worktree_path`, `work_id`, `phase`, `field` (`decision`, `blocker`,
`next` or `evidence`), `text` (at most 160 characters), `source` (`record`,
`snapshot` or `archive`) and `ref`. `--show <ref>` returns that one record's full
`payload` with `ref`, `source`, `recorded_at`, `worktree_path`, `work_id` and `phase`;
use it only for a result worth reading in full. A `ref` is opaque, valid while its
file exists (the store trims old records), and only resolves inside the scope of the
call (`REF_NOT_FOUND` otherwise). Other errors: `QUERY_REQUIRED`, `INVALID_SINCE`,
`INVALID_REF`, `REF_AMBIGUOUS`.

Secret-like strings (GitHub tokens, `github_pat_` tokens, `sk-` keys, AWS access key
ids and private-key headers) are replaced by `[REDACTED]` in all output. Records
should not hold credentials in the first place; this is a backstop, not a scanner.

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
over 4,500 characters), the activity since the last record, and the edited repo
files, most recent first (at most 10, then `N more not listed` with the facts.json
path; N also counts the oldest files the facts size cap dropped, which are in
neither the list nor facts.json: `N more not listed: K in <facts.json>, D older not
kept`; when the cap dropped every file, the section is just `Edited repo files: D
older not kept`). The activity section compares facts.json with the checkpoint's
`recorded_at`: a staleness line (`Checkpoint is N min older than the latest captured
activity; K dispatches / M commits after it.`, where the latest activity is
`last_change_at` or the newest dispatch; `No activity captured after the
checkpoint.` when facts.json holds no activity time at all; commits without a time
are named and not counted), the 5 newest later dispatches (role and description)
and, per repo, the count and 3 newest later commits. Without a checkpoint it says so
and counts all captured dispatches and commits as unrecorded; without `facts.json`
it is omitted. Over budget, whole items are replaced, never cut: the file list
first (by the facts.json path), then a full checkpoint payload by its digest, then
the activity section (its headline, which keeps the staleness line, and the
facts.json path), then the checkpoint digest (only the checkpoint path), then the
facts summary (by its path), and last the identity (paths and `status` usage only).

The save status names a genuine failure as `previous handoff save failed; verify
any older checkpoint`. When the last PreCompact found no record (`NO_CHECKPOINT`)
and the session has no record or snapshot at all, it says
instead: `No handoff checkpoint: this session has not recorded yet; continue from
the compact summary and record a milestone now.`

SessionStart with source `compact` folds the facts spool again before rendering, so
dispatches, edits and commits between PreCompact and the compaction boundary (auto
compaction runs PreCompact minutes earlier) are included. It runs only on the native
path for a session whose enablement marker existed before this SessionStart, uses
the same git budget and facts.json lock as PreCompact, and is fail-soft: when it
fails (for example `STORE_BUSY`) the facts.json PreCompact wrote is rendered, with
no warning. Resume does not refold.

On a git timeout or failure in any fold (PreCompact or this refold), each failed
step for a repo (`rev-parse` for `branch`, `status` for `dirty`/`dirty_total`, `log`
for `commits`) keeps the value the previous fold stored and the repo's `error` names
the code (`GIT_TIMEOUT`, `GIT_FAILED`, `GIT_UNAVAILABLE`); steps that succeed are
current, and the next fold where git answers clears `error`. A repo with no previous
entry gets `branch` null, no dirty entries and no commits. Kept dirty entries never
date a change; `last_change_at` keeps the previous value plus what this fold read.

PreCompact folds the facts spool into facts.json and snapshots an existing record,
without a model call. Without a record the save is partial ("decision record
missing"), never an empty snapshot; a record older than the latest change is
partial-stale. The fold and these save levels apply only to a session with the
enablement marker `<provost dir>/cache/facts/<session_id>.enabled`, which the
native SessionStart creates when adaptive mode resolves for the workspace and
handoff is enabled. Without it (a session started before the facts layer was
installed) PreCompact only snapshots an existing record, or reports
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
repo (`FOREIGN_REPO`). Under `--apply`, an expired session kept because removal or
archiving was refused is listed only in `skipped`, and one kept by an OS error
(for example a permission error or a recreated dir) only in `errors`, not under
`expired`; the dry run lists it under `expired`. Nothing is deleted automatically.

Decisions outlive expiry. Before an expired session (or a reclaimable `.pruning-`
leftover) is moved aside or deleted, `--apply` preserves its newest record for each
`payload.work_id` in the repo-level archive `<store home>/v2/repos/<repo dir>/archive/`,
beside `sessions/`. Each work_id has one file,
`sha256(work_id as UTF-8)` hex, first 32 characters, plus `.json`, holding that
record exactly as stored in `records/` (same schema, checksum intact, no wrapper).
Candidates are the session's checksum-valid records plus the records its valid
snapshots copied, so a record already trimmed from `records/` still counts; per
work_id the newest `recorded_at` wins. The newest also wins across sessions: an
archive file is replaced only by a strictly newer record, never by an older or equal
one. The dry run writes nothing and lists the work_ids under `archive` on each
expired entry (on leftovers when non-empty); unreadable, checksum-invalid or
mismatched records are skipped and named under `archive_skipped` (`file`, `reason`)
without blocking removal. If archiving fails the session is kept: a guard refusal,
busy archive lock (`STORE_BUSY`), write failure (`WRITE_FAILED`), an existing
archive file that is not a valid record of that work_id (`CORRUPT_ARCHIVE`, never
overwritten) or one of another repo (`FOREIGN_REPO`) is listed in `skipped`; an OS
error is listed in `errors` with `left_at: null`. Archiving takes the archive lock,
which refuses a store home inside a repo that does not ignore it: there, an expired
session or leftover with archivable records is listed in `skipped` as
`STORE_INSIDE_REPO` (or `GIT_UNREADABLE` when Git cannot answer) and kept, while one
with nothing archivable is still removed.

Known limits: `horizon.json` carries no checksum, so tampering with it can hide or
remove records; a reader that overlaps a trim can see a transient
`CORRUPT_RECORD`/`INCOMPLETE_CHAIN` that a retry clears; `REQUEST_ID_EXPIRED`
covers the newest trimmed id of each chain and on-disk leftovers, while an older trimmed id
reused against the current head records a new entry (the chain stays valid); a session
with cards of several works cannot be read by a runtime older than per-work cards
(`AMBIGUOUS_CHAIN`, or `CORRUPT_RECORD` once several chains were cut); submodules, bare-backed and
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
  session (no `agent_id` in the input; background-job sessions also send
  `agent_type: "claude"` on main-session events, so `agent_type` alone does not
  decide; that every subagent event carries `agent_id` was probed once, on Claude
  Code 2.1.291, so re-probe after an upgrade) edits a file with Edit, Write
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
  dispatch re-arms it. A subagent's result also arrives as a `UserPromptSubmit`:
  usually its hand-back message, a prompt that starts with
  `<agent-message from="...">` (optionally behind `Another Claude session sent a
  message:`) followed by `[Subagent hand-back]`; otherwise an agent's
  `<task-notification>` whose first `<status>` is `completed`, whose summary
  starts with `Agent `, and which does not point at a `SubagentHandback`
  delivery (the pointer `(its SubagentHandback call)` or the interim notice
  `... will deliver its report through SubagentHandback ...`; a result that merely
  mentions `SubagentHandback`, even `sent through SubagentHandback`, still counts). For either, of any role, the hook also reminds main to record the
  result milestone if it changes the work state. That reminder fires for every
  result and ignores the flags; when both apply they share one context.
  Teammate messages without the hand-back tag, background-command
  notifications, other statuses and ordinary prompts behave as above.
- A `cd`/`git -C`/`Set-Location` target that names the event's own `cwd` (after
  normalizing slashes, case, a trailing `/` and a Git Bash `/x/` drive prefix) is
  not stored as a `dir` fact; relative targets are kept as written.

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

`hooks/native-reminder` has been removed. It used to remind on `PostToolUse(Agent)`
synchronously, which cost every dispatch about 0.6 s; `native-facts` took over its
reminders. Remove any registration that still points at it.
