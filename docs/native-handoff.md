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

Chains are validated per work. Records linked by `parent_record` form one chain; a
chain with a missing record (`INCOMPLETE_CHAIN`), an unreadable or tampered record
(its read error, usually `CORRUPT_RECORD`), a fork or a cycle (`AMBIGUOUS_CHAIN`), or
a work_id that heads two chains (`AMBIGUOUS_CHAIN`) is broken. An unreadable record
file breaks the work its content still names (so an older card of that work is never
served in place of a corrupt head) and the chain of the parent it names. A broken work
is left out of `status`, injection, snapshots and cross-session work listings; `status` lists it
under `broken_works`, compact and resume injection name it in one status line ("Broken work
chains: <work_id> (<code>); status lists broken_works"), and `record` refuses to write to it with the same code (starting
a second chain beside it would hide the break); the session's other works are read and
written as usual. Some failures cannot be tied to one work and still fail the whole
session closed: a corrupt `horizon.json`, a horizon that no record continues (a whole
cut chain deleted), an unreadable record file that names no work and that no record
names as parent, linked storage, and any problem in `snapshots/` (one chain per
session). A session whose record chains are all broken also fails closed on reads, with
the code of its first broken work by work_id, as before; it can still record a new work.

Decisions and evidence live in a decision log, not in the cards. Each work_id has
one append-only JSON Lines file at the repository level, beside `sessions/`:
`<home>/v2/repos/<repo_key[:16]>/logs/<sha256(work_id as UTF-8)>.jsonl`. Every
session of the repository (a checkout and its worktrees) appends to the same file,
and pruning session directories never touches it (only `prune --apply` of a long idle done
or orphaned work removes it, see Pruning). Line 1 is a header (`kind: "log"`,
`schema_version`, `workspace_id`, `work_id`); every later line is one entry:

- decision: `{id, kind: "decision", at, session, record, text, refs?, supersedes?}`
- evidence: `{id, kind: "evidence", at, session, record, claim, state, ref, updates?}`

`id` (`D1, D2, …` and `E1, E2, …`, sequential per work and kind), `kind`, `at`
(ISO-8601), `session` and `record` (the revision of the card written with the
entry) are filled by the runtime; entries imported by `migrate` also carry
`migrated: true` and may lack `record`. Ids are unique references, not time order
(migrated entries can take later ids than newer entries): every view orders entries
by `at`, and entries with the same `at` keep their log order (ids ascending per
kind). Ids are assigned and lines appended under a per-work lock
(`logs/<sha256(work_id)>.lock`) that a `record` waits for up to 10 seconds
(`STORE_BUSY` after), so concurrent writers from any session or worktree never
reuse an id. Entries are never rewritten, except by the maintainer-only `redact`
command (see [Redact](#redact)). A line exists only once its terminating
newline is written: all lines of one `record` go out in one write followed by
fsync. Bytes after the last newline are a torn append (a crash mid-write): readers
ignore them and the next append cuts them off. A newline-terminated line that is
not a valid entry (including an `at` that is not an ISO-8601 time or a `migrated`
other than `true`), or a header naming another repo or work, makes the log fail
closed (`CORRUPT_LOG`, `FOREIGN_REPO`): `status` reports the error and `record`
refuses to append. A `record` appends its entries before it writes its card; if it
is interrupted in between, replaying the same request appends only what is missing.
Entries logged by an interrupted request that is never replayed stay in the log.

**Current state of a claim.** Search, the recovery injection and the record echo
share one rule. An evidence chain follows `updates` links; its head is an evidence
entry that no evidence entry updates (entries in an update cycle head nothing). Chains
whose heads have exactly the same stored claim text (before secrets are masked in
output) are one claim, since a migrated chain and a live chain of a claim are never
linked. The claim's current entry is the head with the newest `at` (equal times: log
order); the other heads are superseded. A redacted head (`redacted_at` set or claim
`[removed]`) never joins by text: its chain is a claim of its own.

Cards have `schema_version` 2 and no `decisions`/`evidence` fields; they carry
`log_entries`, the ids the record appended. Cards of schema 1 (written before the
decision log) stay readable as they are, with their decisions and evidence, and a
work's chain may mix both versions. Resuming a schema 1 card a snapshot holds
adopts it as a schema 1 card; resume never writes to the log.

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
entries and the `newest` 10 by `at`, oldest first; `{"error": <code>}` when the log is
unreadable; `total` 0 when no work is shown). Read-only. SESSION_NOT_BOUND means fix the
session startup/workspace configuration; do not fabricate binding files.

Secret-like strings are replaced by `[REDACTED]`, as in search output, in the model-facing
text of `status`: the checkpoint's `phase`, `next`, `blockers`, source `item` and `ref`
and assignment `task` (for a schema 1 card also its `decisions` and the `claim` and `ref`
of its `evidence`), the `phase` of `other_works`, and the `text`, `claim`, `ref`, `refs`
(and `redaction_reason`) of the `log.newest` entries. What a `record` is built from stays verbatim: `expected`, the
revision, ids, `work_id`, `agent_id`, `write_scope`, `facts` and `template`.

`expected` and `checkpoint` belong to the session's current card. Add
`--work "<work_id>"` to read that work's card instead (both null when the session
has not recorded it). When the session has cards of other works, `other_works` lists
them, most recently written first, as `work_id`, `phase`, `revision` and
`recorded_at`; a session with a single work has no `other_works` field. When some of
the session's work chains fail validation (see Where records live), `broken_works`
lists them as `work_id` and `error`; without any it is absent. When every chain of the
session is broken, `status` instead fails with the code of the first broken work by
work_id (for example `{"error": "INCOMPLETE_CHAIN"}`), as before per-chain validation.

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
fields; malformed entries are `INVALID_DECISION`/`INVALID_EVIDENCE`. A card with a wrong
field set or a malformed card field is `INVALID_PAYLOAD` with a `detail` naming the
missing or unexpected fields (or the field that is malformed). No credentials,
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
bash "<runtime>/hooks/runtime" record --workspace "<root>" --session "<session>" --expected "<revision-or-null>" --request-id "<unique-milestone-id>" [--join] --input "<payload-file>"
```

`--join` is only for a first record of a work that other sessions have logged (see
Joining work below); leave it out otherwise.

Keep each runtime call a single plain command: no heredoc, pipe, command
substitution, env-var prefix or line continuation, because worktree guards refuse those.

Use a new request ID for new content; retry the same ID only with identical content
(card and new entries): a replay returns the stored card and appends nothing.
One window cannot be enforced: while a request whose entries were logged has no card
yet (it was interrupted between the two writes), a replay is checked only against
the entries already logged, so one that adds further entries or carries a different
card is accepted and appends the extra entries.
`--expected` is the head of the payload's work chain: when switching to another
work, read it with `status --work "<work_id>"` first.
After REVISION_CONFLICT rerun status and reconcile. Never force an overwrite.
Record approved plan, dispatch, results and changed decisions/blockers. Do not
record after every tool call. Main owns records; workers report observations.

## Record checks and echo

`record` refuses a request that would put a broken entry into the log, warns about
what it may have missed, and echoes what the work looks like after the write.

Hard rejections write nothing (no entry, no card) and return `error` with a `detail`
naming the entry (`new_decisions[i]` / `new_evidence[i]`):

- `DECISION_TEXT_EMPTY`: decision text that is empty or whitespace only.
- `DECISION_TEXT_TOO_LONG`: decision text over 1000 characters.
- `TARGET_NOT_FOUND`: a `supersedes`/`updates` id that is not in this work's log, or
  that is not an entry id at all. Targets are bare ids of the same work (`D3`, `E2`);
  an entry of this record cannot be a target.
- `TARGET_OTHER_WORK`: a target written as `<other work_id>:<id>`.
- `TARGET_WRONG_KIND`: `supersedes` naming an evidence id or `updates` naming a
  decision id.
- `ALREADY_SUPERSEDED`: superseding a decision that another entry already supersedes;
  the detail names the latest entry of that chain, which is the one to supersede. Two
  entries of one record superseding the same decision are refused the same way.
- `JOIN_REQUIRED`: the session's first write to a work whose log already holds entries
  of other sessions, without `--join` (see Joining work below). The detail names up to
  5 of those sessions by their first 8 characters.

The text checks run before anything is read; the join and target checks run under the
work's log lock against the committed log (for a replayed request, the log as it was
before that request's own entries, or, when its card logged nothing, the log as it was
when the card was written). A record that appends no entries reads the log only when
the session has not joined the work, and without taking the lock, so it creates no log
or lock file.

Soft warnings come back with a successful write as `warnings`, a list of
`{code, message}` (empty when there is nothing to report):

- `SHORT_DECISION`: a new decision under 40 characters (it is still written).
- `DUPLICATE_SKIPPED`: a new decision whose exact text equals a current (not
  superseded) decision of this work, or an earlier decision of the same record. It is
  not appended and gets no id; the message names the existing id. A decision with
  `supersedes` is never skipped, and a decision superseded earlier in the same record
  no longer counts as current.
- `UNREFERENCED_COMMITS`: a commit captured since the session's last record of any
  work whose hash shares no prefix of at least 7 characters with a hex token
  (7 to 40 characters, case-insensitive) in this record's entries: decision `text`
  and `refs`, evidence `claim` and `ref`.
- `UNMENTIONED_DISPATCHES`: a subagent dispatch captured since that last record whose
  role and description both appear nowhere (case-insensitive substring) in this
  record's entries or card `assignments` (`task`, `agent_id`). This is best effort:
  naming a role once covers every dispatch of that role.
- `NO_NEW_DECISION`: edits, dispatches or commits were captured since that last record
  but the record appends no decision (skipped duplicates do not count).
- `LOG_LARGE`: the work's log holds more than 300 entries; consider splitting the work.
- `CONCURRENT_SESSION`: another session's newest card of this work was written less
  than 120 minutes ago; the message names each such session (first 8 characters) and
  how many minutes ago. It comes from the cards of the work across the repository's
  session dirs, not from the log, so a card that logged nothing counts too.
- `REOPENED`: the work's newest card across sessions has phase `done` and this record's
  phase is not `done`; the message names the session that closed it and when.

`CONCURRENT_SESSION` and `REOPENED` are read before the write, read-only and with no
lock held; when the cards cannot be read there are no such warnings, and a session dir
whose cards are unreadable is left out.

The commit, dispatch and activity warnings read the session's facts as of the
record, the way `changes` does: the stored `facts.json` merged with the spool lines
not folded yet, plus a fresh bounded `git` query of the touched repos. This runs after
the write, with no lock held, and is read-only; when it fails there are no such
warnings. "Since the
last record" is after the newest `recorded_at` of the session's cards of any work
(all captured activity before the session's first record).

Successful output keeps every earlier field and adds `echo`, a text of at most 1500
characters: the work (its first 80 characters) and the ids just assigned (more than 8
are shortened to the first 6, the last 2 and a count), the state card (phase, next,
blockers, assignments), open evidence (each claim's current entry, see "Current state
of a claim" under [Where records live](#where-records-live), that is not
`pass-observed`, oldest first), the 3 newest current decisions (newest first,
by `at`) and the warnings. Each listed item is clipped to 200 characters. Over the
cap it drops the oldest shown decisions first, then the oldest open evidence, then the
last warnings, and counts what it dropped as `N more, see status`; the card is always
kept (clipped only when it alone does not fit). Secret-like strings in `warnings` and
`echo` are replaced by `[REDACTED]`, as in search output.

### Writing entries

The checks catch structure and likely omissions, not meaning. Write entries so that
a session that never saw the conversation understands them:

- Write only what is new since this session's last record; never restate the log.
- A decision is one self-contained sentence: the decision, `because` the reason, with
  specifics (files, values, PR or issue numbers). No pronouns or "as above".
- Name commits by hash (7 or more characters) and dispatches by role and task, in
  `refs` or the text, so `UNREFERENCED_COMMITS` and `UNMENTIONED_DISPATCHES` stay quiet
  for the right reason.
- Correct or retract a decision with a new decision whose `supersedes` names the
  latest entry of its chain; record a changed verification state with new evidence
  whose `updates` names the earlier entry. Old entries are never edited.

The example under [Read status before writing](#read-status-before-writing) follows
these rules.

### Joining work

A work's log is shared by every session of the repository, so reusing a work_id by
coincidence would write into someone else's history. A session joins a work
explicitly:

- by resuming a snapshot that holds the work (the adopted card is the session's card
  of that work), or
- by adding `--join` to its first `record` of the work.

Until then, its first write to a work whose log holds entries of other sessions is
refused with `JOIN_REQUIRED`, whether or not the record has entries. A session that
already has a card of the work (written earlier or adopted, the card `--expected`
names) has joined it: later records need no flag, and `--join` on them changes
nothing. A work whose log holds only this session's entries, or none, needs no join.
Sessions that start the same new work at once with records that have entries are
serialized by the log lock: the first to append wins, and the others are refused unless
they join. Records without entries, and works with no log yet, are not serialized: two
such first writes can both succeed, and only `CONCURRENT_SESSION` reports the overlap.
An exact replay of a record whose card was already written needs no `--join`.

Joining does not lock anyone out: `CONCURRENT_SESSION` warns while another session's
card of the work is under 120 minutes old.

A card with phase `done` (ignoring case and surrounding whitespace, so `Done` and
` done ` count) closes its work: the work is closed while its newest card across
sessions is done. Any later record of the work with another phase reopens it,
with a `REOPENED` warning; a closed work's log is kept like any other.

## Search past decisions

```bash
bash "<runtime>/hooks/runtime" search --workspace "<root>" --query "<terms>" \
  [--all-repos] [--since YYYY-MM-DD] [--limit N] [--work "<work_id>"] [--kind decision|evidence]
bash "<runtime>/hooks/runtime" search --workspace "<root>" --show "<ref>" [--all-repos]
```

Finds why something was decided. Read-only keyword search over what the store
already holds: no model call, no index, no daemon, and nothing is created or written
(no files, locks or directories; the decision logs are read without taking their
lock). The scope is the repo of `--workspace`; `--all-repos` scans every repo dir under
the store home. Like `list`, it needs handoff enabled for the workspace
(`HANDOFF_DISABLED` otherwise).

The corpus is every entry of every decision log of the repo (`logs/*.jsonl`), each
session's newest valid record **per work_id** (a session that worked on several works
contributes one card for each), every card each valid snapshot holds, and every valid file in
`archive/`. A log is read header first: its header must name this repo and a `work_id`
whose digest is the file name, and every entry must parse (under "Where records live"). A log
that fails (`FOREIGN_REPO`, `CORRUPT_LOG`, a name that does not match its header) and
any unreadable, checksum-invalid or foreign record, snapshot or archive file is skipped
and counted under `skipped`, never fatal; bytes of an interrupted append at the end of a
log are ignored, as everywhere. Searched items are each log entry (a decision's `text`,
an evidence entry's `claim`) and, of the records, each decision, blocker, `next` and
evidence `claim`. Only schema 1 cards hold decisions and evidence; for schema 2 cards
they live in the log. An item matches when every whitespace-separated query term
(case-insensitive substring) appears in the item text or in its `work_id` (or, for a
record item, its `phase`). Identical (`work_id`, `field`, text) record items are reported
once, for the newest record, and not at all when a log entry of that work holds the same
text (for example one that `migrate` imported). Results rank by how often the terms occur
in the item text, then by time, newest first. `--since` keeps items dated on or after
that UTC date (a log entry's `at`, a record's `recorded_at`); `--work` keeps one
`work_id` (exact match; only that work's log file is read, so other logs, good or bad, are
neither read nor counted under `skipped`); `--kind` keeps decisions or evidence only, so it drops blockers
and `next` (`INVALID_KIND` for any other value; an empty `--work` is
`INVALID_ARGUMENTS`); `--limit` defaults to 10 and must be 1 to 50 (`INVALID_LIMIT`);
`truncated` counts the matches beyond it.

Output: `query`, `repos_scanned`, `results`, `truncated`, `skipped`. A result from a
record, snapshot or archive file has `recorded_at`, `worktree_path`, `work_id`,
`phase`, `field` (`decision`, `blocker`, `next` or `evidence`), `text` (at most 160
characters), `source` (`record`, `snapshot` or `archive`) and `ref`. A result from a log
(`source: "log"`) is one line per entry: `work_id`, `id` (`D3`, `E1`), `kind`
(`decision` or `evidence`, also the `field`), `date` (UTC, `YYYY-MM-DD`), `recorded_at`
(the entry's `at`), `text` (at most 160 characters), `ref` and `superseded_by`: the id of
the entry that replaced it, or `null`. Evidence also shows its chain's latest state:
`state` is the state of the work's current entry for that claim and `latest` that
entry's id, so an old "fail" whose chain ended in "pass-observed" reads
`pass-observed`. It is the current entry of the claim whose chain head the entry's
`updates` links lead to (see "Current state of a claim" under [Where records
live](#where-records-live)), so a migrated chain and a live chain of one claim that were
never linked still agree on the current state. (`--show`'s `chain` keeps only linked
entries.)

`--show <ref>` returns one hit in full. Refs of record, snapshot and archive results are
opaque (`r:`, `s:` or `a:` prefix) and returned as before: the full `payload` with
`ref`, `source`, `recorded_at`, `worktree_path`, `work_id` and `phase`. A snapshot ref
names the snapshot's current card; each other card it holds has its own ref, the
snapshot ref plus `:` and the first 12 characters of that card's revision. The suffix
form is matched against every card the snapshot holds, the current one included;
a prefix matching several is `REF_AMBIGUOUS`. A log entry's
ref is `<work_id>:<entry id>`, for example `hook-perf:D2` (the entry id is the part after
the last `:`, so a work_id may contain colons). Outside the workspace's repo (with
`--all-repos`) it carries the repo dir as a prefix, `@<repo dir>:<work_id>:<entry id>`,
exactly as search results print it. Showing a log entry returns `ref`, `source: "log"`,
`work_id`, `recorded_at`, `entry` (the full stored entry), `superseded_by`, for evidence
`state` and `latest`, `chain`, `dangling`, `before` and `after`. `chain` holds the
entry and every entry connected to it through `supersedes`/`updates` links, followed in
both directions, oldest first; links that point at no entry (logs written before the
record checks may hold such, or links to an entry of the other kind) are tolerated:
cross-kind targets are followed like any other, missing ones are listed under
`dangling`. `before` and `after` are the two entries on each side of the shown one in
time order (the same ordering as `status`: by `at`, then log order), fewer at the ends
of the log. Use `--show` only for a result worth reading in full. A `ref` is valid
while its file exists (the store trims old records), and only
resolves inside the scope of the call (`REF_NOT_FOUND` otherwise). Other errors:
`QUERY_REQUIRED`, `INVALID_SINCE`, `INVALID_REF`, `REF_AMBIGUOUS`, and for a shown log
that fails verification `CORRUPT_LOG` or `FOREIGN_REPO`.

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

A snapshot (explicit `snapshot` or PreCompact) holds every open card of the
session: the head card of each work chain whose phase is not `done`, most recently
written first. When no card is open it holds the session's current card alone, so a
snapshot is never empty. The snapshot file is a copy of the first held card (its
`schema_version`, `revision`, `recorded_at`, `payload` and the rest of the record)
plus `snapshot_id`, `captured_at`, `parent_snapshot`, `snapshot_version: 2` and
`other_cards`: the other held cards, each exactly as stored in `records/` (sealed,
with its own checksum). The top-level copy marks the snapshot's current card.
Snapshots written before multi-card snapshots have neither `snapshot_version` nor
`other_cards` and hold their one card; every reader accepts both. A snapshot whose
`other_cards` are not cards of the same repo and session, one per work_id, is
unreadable (`CORRUPT_RECORD` or `RECORD_IDENTITY_MISMATCH`) like any other damaged
snapshot, as is one with `other_cards` but no `snapshot_version` or with a
`snapshot_version` below 2. A `snapshot_version` above 2 (written by a newer runtime)
is read as its current card only: its `other_cards` are ignored, not checked.

`list` returns, per snapshot, `snapshot_id`, `session_id`, `captured_at`,
`parent_snapshot`, the current card's `work_id` and `phase`, and `works`: every
held card as `{work_id, phase, recorded_at, revision}`, current card first. A
snapshot of a newer `snapshot_version` also has a `note` saying only its current card
is read.

`resume --from <snapshot-id>` adopts the snapshot's only card. When the snapshot
holds several, it adopts nothing and fails with `WORK_CHOICE_REQUIRED`, returning
`snapshot_id` and `choices` (`work_id`, `phase`, `next`, `recorded_at` per held card,
current first): ask which works to continue, then run it again with one
`--work "<work_id>"` per work to adopt (repeatable; only `resume` accepts more than
one). A named work the snapshot does not hold is `WORK_NOT_IN_SNAPSHOT` (with the same
`choices`). One adopted work returns its new card; several return `adopted_from` and
`cards` (in the snapshot's order); `reconciliation` follows either way. Each adopted
work starts its own chain in the new session: assignments are marked
`needs-verification`, a schema 1 card's evidence `unverified`, nothing is written to
any decision log, and the card records `adopted_from` (the snapshot id) and
`adopted_at` (the adoption time). The adopted works keep the snapshot's order, so its
most recently written chosen card becomes the session's current card. Adopting into
the snapshot's own session is `SAME_SESSION_ADOPTION`; naming a work the session
already has a chain for is `REVISION_CONFLICT` with that `work_id`. Existing chains
are checked for every named work before any is adopted, so a conflict that exists
when resume starts adopts nothing. A record of a named work written concurrently
(`REVISION_CONFLICT` with its `work_id`) or a busy session lock (`STORE_BUSY`) can
stop resume after some works were adopted: replaying the same resume (same snapshot,
same `--work` set) returns the cards already adopted and adopts the rest. Replaying a
completed resume returns the cards it adopted. Error results quote card text
(`choices`) through the same secret redaction as search.

`adopted_at` is the join marker of an adopted work: every later card of that chain
inherits it (a card that was not adopted has none), so it survives trimming.
Adoption counts as joining the adopted works: a later join check reads a session's
head card of the work and treats `adopted_at` as joined, needing no `--join`.

Verify current sources, affected evidence and worker ownership before dispatch.
A closed ticket and recorded running worker are observations, not proof of
completion or current ownership. Do not duplicate uncertain live work.

SessionStart context stays within 8,000 characters (Claude Code replaces a larger
one with a 2 KB preview) and never fails for its size. Startup, clear and fork
carry identity, operations and the new-work notice, plus a menu of joinable works:
up to 5 works of the repository whose newest card is not `done` (from `work_states`,
across all sessions), newest card first, one line each with the work_id (cut to 80
characters), phase, last update (UTC) and the first 8 characters of the session that
wrote that card, then a hint to continue one by running `list` to get the id of a
snapshot that holds it and then `/handoff resume <snapshot-id>`, or to record the
work_id with `--join` on the first write. The menu is redacted and loads no
card content, decisions or evidence; it is omitted without error when the store cannot
be read. Compact and resume add,
in priority order: the save status with the required action (after a facts-only
save: write a record from the compaction summary; status prefills facts and
`template.sources`), the facts summary (one line per repo, at most 5: branch,
uncommitted count, session commit count; a subagent count by role; edits outside
repos; a `Facts capture:` line when spool lines were unreadable or the session
compacted with zero captured events, which means capture may be failing; omitted
without `facts.json`), the checkpoint (the session's most recently written card in
full, or its digest when over 4,500 characters), one line per other open card of the
session (`work_id`, phase and next step cut to 160 characters; a card with phase
`done` is closed and not listed), the open evidence of the current work (each claim's
current entry, see "Current state of a claim" under [Where records
live](#where-records-live), whose state is not `pass-observed`, oldest first, plus
`Passed claims: N`; on an adopted card, see `adopted_at` above, a claim whose current
entry is `pass-observed` with an `at` before the adoption time is
listed after the open ones as `[passed before adoption, not re-verified]` and not
counted as passed), the current work's decisions (the newest 15
that no entry supersedes, ordered by `at` then log order and listed oldest first,
with id, date, text, refs and the id each replaces), the activity since the last record, and the edited repo
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
first (by the facts.json path), then the record contract section, then the decisions one at a time from the oldest
shown (down to a count), so decisions fill the budget the other sections leave,
newest first; then the open evidence (to its counts), the other open cards (to a
count), a full checkpoint payload by its digest, the activity section (its headline,
which keeps the staleness line, and the facts.json path), the checkpoint digest
(only the checkpoint path), the facts summary (by its path), and last the identity
(paths and `status` usage only).

A schema 1 checkpoint is injected as before (its payload or digest carries its
decisions), its checkpoint line labelled `Checkpoint (old format, not migrated)`.
A schema 2 card whose work has no decision log yet falls back to the newest schema 1
card of the same work chain in this session: its decisions and open evidence are
shown labelled `not migrated` with that card's path; without such a card the section
says the log has no entries yet (schema 1 cards of other works in the same chain are
never used). A session with no record left falls back to its newest snapshot: the
snapshot's current card is the checkpoint and its other open cards are the
other-work lines. The card payload, other-work lines, decisions and evidence are passed
through the same secret redaction as search; each decision or evidence line is capped
at 400 characters and a work_id in a section title at 80. An unreadable decision log (for example `CORRUPT_LOG`) never breaks the
injection: one line names the error code and every other section is kept.

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

PreCompact folds the facts spool into facts.json and snapshots the session's open
cards (see Explicit save and resume) when it has a record, without a model call. Without a record the save is partial ("decision record
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
the spool `<session_id>.jsonl`, `.folding-*` spool leftovers, the reminder
flags, the declaration scan's `.declare-cursor` and the `.enabled` marker. `--apply` deletes them after the session dir itself is removed; a failed
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
repo (`FOREIGN_REPO`). Under `--apply`, an expired session kept because removal
was refused is listed only in `skipped`, and one kept by an OS error
(for example a permission error or a recreated dir) only in `errors`, not under
`expired`; the dry run lists it under `expired`. Nothing is deleted automatically.

Decisions outlive expiry: they live in the decision logs, which removing a session
never touches. `prune` no longer writes the repo-level archive
`<store home>/v2/repos/<repo dir>/archive/`; archive files written by earlier runtimes
stay as they are and remain readable by `search` and `migrate`. Schema 1 cards still
hold decisions and evidence that `migrate --apply` has not imported, so an expired
session or reclaimable leftover holding a schema 1 card (a record, or any card a
snapshot holds) with non-empty decisions or evidence of a work that a `migrate` dry run
would still import, or reports an error for, is kept and listed in `skipped` as
`UNMIGRATED_DECISIONS` (not under `expired`); the check repeats at removal time. The dry
run runs at most once per `prune`, only when such a session is found. Run
`migrate --apply` first, then `prune`.

Idle logs. `prune` also lists, under `idle_logs.candidates`, the decision log of each work
that is idle for more than 180 days (`older_than_days`) and either has no card left in
any readable session (`kind: "orphan"`, for example after its sessions expired) or whose
newest card across all sessions is done (`kind: "done"`). Idle time is the latest of that
newest card's `recorded_at`, the log's newest entry `at` and the log file's mtime: an
append or a `migrate` import rewrites the file, and migrated entries keep their original
`at`, so a freshly written or migrated log gets the full 180 days. Logs of open works are never listed or removed. Each candidate has
`work_id`, `kind`, `idle_since` and `removable`; one that is not removable also has
`reason`:

- `SESSIONS_UNREADABLE`: a session dir could not be read at all, so its cards are unknown
  (checked before deciding that a work is an orphan);
- `LEFTOVER_PENDING`: a `.pruning-` leftover exists, whose cards are not read either;
- a chain code (`INCOMPLETE_CHAIN`, `CORRUPT_RECORD`, `AMBIGUOUS_CHAIN`, …): the work's
  chain is broken in some session;
- `SCHEMA1_SOURCES`: a session or a repo-level `archive/` file still holds a schema 1 card
  of the work with decisions or evidence (the sources `migrate` reads); removing the log
  would let `migrate` import them again. Sessions go first, the log in a later run; an
  archive file keeps the log until it is deleted by hand.

`--apply` removes the removable ones (`removed`, `removed_work_ids`): only
`logs/<sha256(work_id)>.jsonl`, never its `.lock`, and only while holding that work's log
lock after reading every session and the log again there, so a work reopened or logged to
since the listing (`OPEN`, `RECENT`) or newly guarded is kept and listed in `skipped`
(`work_id`, `reason`). An unreadable log is listed in `skipped` by file name (`log`,
`reason`, e.g. `CORRUPT_LOG`); removal OS errors go to `errors`. Idle logs are evaluated
before any session dir is removed in the same run. Nothing is archived: an abandoned work
that was never marked done loses its log once its sessions are gone and it has been idle
for 180 days, and a later `record` of a removed work starts a new log at `D1`/`E1`. A
`record` without new entries does not take the log lock, so a card-only reopen that lands
between the re-check and the delete is not seen (that window is only the delete itself).

Migrate backups. `log_backups` lists `migrate`'s `backups/logs-<UTC time>` dirs, oldest
first, as `dir`, `created_at` (from the name, else the dir mtime) and `age_days`.
`prune` never deletes them, with or without `--apply`: delete a backup dir by hand once
the migration is settled.

Known limits: `horizon.json` carries no checksum, so tampering with it can hide or
remove records; a reader that overlaps a trim can see a transient
`CORRUPT_RECORD`/`INCOMPLETE_CHAIN` that a retry clears; `REQUEST_ID_EXPIRED`
covers the newest trimmed id of each chain and on-disk leftovers, while an older trimmed id
reused against the current head records a new entry (the chain stays valid); a session
with cards of several works cannot be read by a runtime older than per-work cards
(`AMBIGUOUS_CHAIN`, or `CORRUPT_RECORD` once several chains were cut); a runtime older
than multi-card snapshots reads only the current card of a `snapshot_version` 2
snapshot and ignores its `other_cards`, and its prune archives that current card with
`snapshot_version` and `other_cards` left inside the archived record (this runtime
still reads such an archive file); a record written by such an older runtime on an
adopted chain drops `adopted_at`, and the marker is not restored after upgrading
again; record fields must never use the names a snapshot reserves (`snapshot_id`,
`captured_at`, `parent_snapshot`, `snapshot_version`, `other_cards`), since they are
dropped when a snapshot's current card is read back; leftovers of
an interrupted trim stay on disk until that work's chain is trimmed again, so a chain
never written again keeps them (reads skip them); submodules, bare-backed and
separate-git-dir checkouts are refused (`REPO_KEY_UNRESOLVED`); Git older than
2.31 is refused (`GIT_UNSUPPORTED`); on Windows without long-path support a long
user or `PROVOST_HOME` path can exceed 260 characters; redacting a log entry changes the
log only: schema 1 cards, snapshots, archive files and `migrate` backups keep the
original text, and `search` can still find it there (redacting those means deleting the
files by hand).
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

## Redact

Maintainer only. Removes the content of one decision log entry, for example a
decision or claim that accidentally holds a secret:

```bash
! bash "<runtime>/hooks/runtime" redact --workspace "<root>" --work "<work_id>" \
  --entry "<id>" [--reason "<text>"]
```

A decision's `text`, or an evidence entry's `claim` and `ref`, becomes `[removed]`.
Everything else stays: `id`, `kind`, `at`, `session`, `record`, a decision's `refs`
and `supersedes`, an evidence entry's `state` and `updates`. The entry gains
`redacted_at` (ISO-8601) and, with `--reason`, `redaction_reason` (1 to 1000
characters; it is stored and printed, so never put the secret in it). Links from or to
the entry still resolve, and `status` shows the marker instead of the content.
Redacting an entry that is already redacted succeeds without any change (`changed`
false; the first reason stays). Output: `work_id`, `entry` (the redacted entry) and
`changed`. Errors: `ENTRY_NOT_FOUND` (no such id, or the work has no log),
`INVALID_REASON`, `INVALID_ARGUMENTS` (`--work` or `--entry` missing), `STORE_BUSY`,
`CORRUPT_LOG`/`FOREIGN_REPO` (a log that fails closed is not rewritten) and
`WRITE_FAILED`.

This is the one rewrite of a log. It runs under the work's log lock (the lock
`record` appends under, waiting up to 10 seconds, `STORE_BUSY` after) and takes no
session lock. Committed lines are copied unchanged except the redacted one, and a
torn tail is dropped (redacting an already redacted entry writes nothing and leaves
it; the next append cuts it off). The new log is written to a temporary `.pending-*` file in `logs/`,
fsynced, then moved over the log in one `os.replace`, so a crash leaves either the
old log or the new one, never a mix, and the result ends with a newline as appends
require. A crash before the move can leave the `.pending-*` file; it holds the
redacted log; readers and appends ignore it, and it can be deleted. On Windows the move fails while another process has
the log open (a `status` or hook reading it): that is `WRITE_FAILED`, the
log is unchanged, and running the same command again is safe.

Replaying a `record` request whose entries were redacted returns `REQUEST_ID_REUSED`,
even when the earlier attempt was interrupted before its card was written: the log
no longer holds what the request sent. Write a new request ID instead. Redaction only
rewrites the log: copies elsewhere (session transcripts, schema 1 cards, snapshots,
archive files, migrate backups and output already shown) keep the content. Redacting
a migrated entry makes `migrate` report `REDACTED_ENTRIES` for that work, so `prune`
keeps every expired session holding a schema 1 card of it as `UNMIGRATED_DECISIONS`
until those cards are deleted by hand.

The model must not run this command. Deny it in the Claude Code settings with a
permission rule rather than relying on instructions, and run it yourself with the `!`
prefix, which runs the command directly in your shell and not as a model tool call,
so the deny rule does not apply to it. Example for the `permissions` object of
`~/.claude/settings.json` (this repository does not install it):

```json
{"permissions": {"deny": ["Bash(*runtime* redact*)"]}}
```

`*` matches any characters, so the rule covers the quoted wrapper path
(`bash "<runtime>/hooks/runtime" redact`), both path separators and the module form
(`python -m provost_runtime.cli redact`). A Bash rule matches the command text, so it
blocks the documented invocation, not every conceivable way of rewriting the file.

## Migrate

```bash
bash "<runtime>/hooks/runtime" migrate --workspace "<root>" [--apply]
```

A one-time import of the decisions and evidence that schema 1 cards carried into
the decision logs. Without `--apply` it is a dry run that writes nothing (no files,
locks or directories). Like `prune`, it needs handoff enabled for the workspace.
Run the dry run, show it to the user, and run `--apply` only after the user has
reviewed it, and before any `prune --apply` (prune keeps sessions with unimported
decisions as `UNMIGRATED_DECISIONS` until then). Migrate soon after installing: a schema 1 card trimmed from its chain
(5 newer records of the work) is no longer a source unless a snapshot or archive
file still holds it.

Sources are the repo's valid schema 1 records and snapshots in every session dir
(including `.pruning-` leftovers) and its valid `archive/` files. Unreadable,
checksum-invalid, mismatched or foreign files are skipped and listed under
`skipped` (`file` relative to the repo dir, `reason`), as are single items the log
cannot hold (`INVALID_DECISION` for empty decision text, `INVALID_EVIDENCE` for an
empty claim). Schema 2 cards hold nothing to import and are not listed. Source
files are only read, never modified.

A source stands for the card it holds. A snapshot or archive file carries its
card's `recorded_at` and `revision`, so every copy of one card has the same time and
identity, whichever copies still exist; a snapshot holding several cards is one
source per schema 1 card. Per work_id, sources are read in time order
by `recorded_at`. Equal times are ordered by `parent_record` descent (a card after
its ancestors), then records before snapshots before archive files, then by path.

- Decisions are deduplicated by exact text: the first occurrence is imported, with
  its time, session and card.
- Evidence with the same `claim` text forms one chain: its first state is one entry,
  each later different state a new entry whose `updates` names the previous one, and
  a state equal to the previous one adds nothing. Sessions interleave by time.

Imported entries take `at` from the card's `recorded_at`, `session` from the card's
session, `record` from the card's revision, and `migrated: true`. Their ids continue
after the work's highest existing ids; entries already in the log are kept as they
are, and every view orders by `at`, so older imported entries sort before newer live
ones. Migrated chains never link to live entries. Expected consequences: a live entry
recorded before the migration can coexist with a migrated entry of the same text (after
the migration, `record` skips an exact restatement as `DUPLICATE_SKIPPED`), and
when a claim has both a migrated chain and a live chain, its current state is the
chain head with the newest `at` (see "Current state of a claim" under [Where records
live](#where-records-live)).

Idempotent: all of one work's new entries go out in one write, so a candidate whose
card revision already appears as `record` among the work's `migrated: true`
entries was handled by an earlier run and is not imported again. A high-water mark
applies as well: a candidate whose card time is at or before the newest `at` among
the work's migrated entries is not imported, because an earlier run already
evaluated every source up to that time. This keeps a redacted entry from coming
back through another card that restated it once the card it was imported from is
trimmed. In consequence, a source that only becomes readable later with an older
time (for example a repaired file) is not imported; an adopted schema 1 card (see
below) has a newer time and is. As a further guard, a decision whose text a
migrated decision already has is not imported again. A later chain link to an
earlier import resolves by position among that card's migrated entries (kind and
evidence state must match at every position), else by a (`record`, `claim`,
`state`) that exactly one migrated entry has; when neither finds exactly one entry
the work fails with `MIGRATE_CHAIN_UNRESOLVED` and nothing is appended to it.
Skipped candidates are counted under `already_migrated`, so a second `--apply`
imports nothing (and makes no backup), also after a source card was trimmed (a
surviving copy is the same card). A work that failed in one run (for example
`STORE_BUSY`) is imported by the next.

After a migrated entry is redacted (`migrated: true` with `redacted_at`), its work
is no longer migrated automatically: the dry run and `--apply` both report it with
error `REDACTED_ENTRIES` and append nothing, while other works continue. With the
original text gone, no guard could tell a card that restated it apart, so this is
what keeps the redacted text from coming back. Re-log anything from that work's
schema 1 cards that still matters as new entries.

Resuming a schema 1 snapshot after migrating creates a new schema 1 card, which the
next `--apply` imports: its evidence states are `unverified`, chained onto the
claims' migrated entries, and its restated decisions are deduplicated.

Output: `applied`, `backup` (null unless something was imported), `works` and
`skipped`. Each work lists `work_id`, `sources` (source files contributing to it),
`decisions` and `evidence` (entries the dry run would import, or `--apply`
imported) and `already_migrated`. A work whose log cannot be read, backed up or
appended to (`CORRUPT_LOG`, `FOREIGN_REPO`, `STORE_BUSY`, `WRITE_FAILED`,
`MIGRATE_CHAIN_UNRESOLVED`, `REDACTED_ENTRIES`) has `error` instead of the counts and gets nothing
appended; other works continue.

`--apply` takes each work's log lock (the same lock `record` uses, waiting up to 10
seconds), copies that work's committed log bytes into
`<store home>/v2/repos/<repo dir>/backups/logs-<UTC time>/<log file name>` (an
empty file when the work had no log; fsynced), and only then appends the work's
entries in one write and cuts any torn tail. A failed backup aborts that work before
anything is appended, and an empty backup dir is removed. This store-level
`backups/` is not the installer's `<config dir>/provost/backups`. `prune` lists its
dirs with their age (`log_backups`) but never deletes them, and its copies hold log text unredacted (a later `redact` does not reach them):
delete backup dirs by hand once the migration is settled.

Rollback, keeping live entries recorded after the migration: remove every line with
`migrated: true` from the logs. A live entry whose `updates` or `supersedes` named a
migrated id then points at a missing entry; fix or accept that residue. Full revert:
replace each log with its copy in the oldest backup (a copy that is an empty file
means the work had no log: delete that log), which also drops every entry recorded
after the migration. Either way, reinstall the previous runtime if needed, with
[its limits](#install-and-rollout). Record,
snapshot and archive files are never modified, so schema 1 cards still hold their
decisions and evidence. A runtime with the decision log but without `migrate` reads
migrated entries as they are.

## Install and rollout

A new installation uses `python install.py setup`, which also registers all seven
hook entries (SessionStart, PreCompact, PostCompact, PostToolUse, UserPromptSubmit,
and Stop twice: the async declaration scan and the synchronous
[acceptance gate](#acceptance-gates)) in `settings.json` and writes
`provost/config.json`. `python install.py install` is for upgrading an existing
installation and never touches `settings.json`. The full
registration JSON is under [Install](../README.md#install) in the README.

`python install.py install` copies the runtime and the repository's `/handoff` skill
(`skills/handoff/SKILL.md`) into the Claude config dir; `python install.py check`
reports the skill like every other target (`missing`, `modified` or `ok`). The first
time `install` replaces a local skill that differs, it also keeps that copy beside it
as `skills/handoff/SKILL.md.local-<UTC time>.bak` (a name that already exists gets a
`-1`, `-2`, … suffix; nothing is overwritten). Once such a copy exists, later installs
back the skill up only in `<config dir>/provost/backups/install-<UTC time>/`, like
every other changed file. A failed install removes the copy it kept together with its
other changes. `--dry-run` names the copy it would keep.

Rolling out the decision log, in this order:

1. Merge, then an existing installation runs `python install.py install` (a new
   installation uses `setup`).
2. `migrate` (dry run); the user reviews its output.
3. `migrate --apply`.
4. Only then `prune` and, after the user has reviewed its listing, `prune --apply`
   (before the migration, prune keeps sessions with `UNMIGRATED_DECISIONS`).

Rolling back the runtime is not clean once the new one has recorded: a runtime
without schema 2 cannot read the cards recorded since the install, so every session
that recorded after the install fails closed there (`status` errors, compaction
recovery shows the checkpoint as unavailable) until the new runtime is reinstalled.
No file is lost; only sessions that never recorded after the install roll back
cleanly. Further limits are under [Migrate](#migrate) (removing migrated entries or
restoring the log backups) and [Redact](#redact) (what redaction does not reach).

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
- Policy §1 asks the main session to open each non-trivial request with a `分類:`
  declaration. `hooks/native-event`, registered for `Stop` as an async hook, reads
  the transcript bytes appended since its last scan (the offset is kept in
  `<session_id>.declare-cursor`; the first scan, or more than 16 MB of new bytes,
  reads only the newest 16 MB; a partial last line waits for the next scan). A
  main-session assistant text line that starts with `分類:` (or `分類：`, optionally
  behind markdown marks) and a difficulty (`簡單`, `中等`, `機械`, `困難`) touches
  `<session_id>.declared` and removes `.undeclared`; otherwise an Edit, Write or
  NotebookEdit of a non-scratch path (the scratch locations above) whose result
  is not an error touches `<session_id>.undeclared` unless `.declared` exists.
  Sidechain lines and subagent transcripts never count. The next
  `UserPromptSubmit` with `.undeclared`, without `.declared` or
  `<session_id>.declare-reminded`, creates `.declare-reminded` and appends the
  declaration reminder to the same context. The declaration window therefore runs
  from the first declaration until a `record` whose phase is `done` deletes
  `.declared`, `.undeclared` and `.declare-reminded` (other records keep them,
  because follow-up turns of the same request do not repeat the declaration), so
  it reminds at most once per window and never on follow-ups. The scan stores no
  transcript text and never raises.
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

`python install.py setup` registers these hooks (together with SessionStart,
PreCompact and PostCompact); the JSON below is for registering them by hand.

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
    ],
    "Stop": [
      {"hooks": [
        {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "async": true, "timeout": 20}
      ]}
    ]
  }
}
```

`python install.py check` reports `settings: not-registered` until the
`SessionStart`, `PostToolUse` and both `Stop` registrations (this one and the
synchronous [acceptance gate](#acceptance-gates)) are all present.

Do not register either hook for `SubagentStop`. That event's context is delivered
to the subagent's own conversation and keeps the subagent running, so it would loop
the worker instead of reaching main.

`hooks/native-reminder` has been removed. It used to remind on `PostToolUse(Agent)`
synchronously, which cost every dispatch about 0.6 s; `native-facts` took over its
reminders. Remove any registration that still points at it.

## Acceptance gates

A non-simple task can carry an **acceptance file**: the list of gates the main
session must pass before it reports the task done. The main session writes it with
the Write tool as `<working tree>/.claude/acceptance/<task>.json`, where the working
tree is the `cwd` Claude Code sends with the `Stop` event.

```json
{
  "task": "Chat panel matches the feature-020 mockup",
  "gates": [
    {"id": "unit-tests", "kind": "machine", "check": "npm test", "status": "pass",
     "ranAt": "2026-10-21T09:12:00Z", "evidence": "212 passed", "source": "docs/spec.md"},
    {"id": "panel-dark-zh", "kind": "visual", "check": "panel, dark theme, zh-TW, fixture: 3 sites",
     "status": "pending", "source": "design/feature-020.png"},
    {"id": "merge", "kind": "human", "check": "merge the PR", "status": "pending"}
  ]
}
```

| Field | Required | Value |
|---|---|---|
| `task` | yes | string: the task the gates accept |
| `gates` | yes | list of gate objects (may be empty) |
| `gates[].id` | yes | non-empty string, named in the block reason |
| `gates[].kind` | yes | `machine`, `live`, `visual`, `review` or `human` |
| `gates[].check` | yes | string: what closes the gate (command, steps, state and fixture data) |
| `gates[].status` | yes | `pass`, `fail` or `pending` |
| `gates[].ranAt` | no | ISO 8601 time the check last ran (no zone counts as UTC) |
| `gates[].evidence` | no | string: result summary, observed output, screenshot path or verdict |
| `gates[].source` | no | string: path or URL of the mockup, spec or issue the gate is judged against |

Other keys are ignored. `human` gates (release, tag, merge and other outward
actions) are the owner's decision and never hold the turn.

`hooks/acceptance-gate`, registered for `Stop` as a **synchronous** hook (beside
`native-event`'s async `Stop` registration, which keeps the 分類 declaration scan),
answers at every main-turn end:

- No `<cwd>/.claude/acceptance` directory: the bash part exits 0 with no output
  before any interpreter starts. This is the common case and costs no Python start.
- Otherwise the runtime reads every `*.json` file in that directory whose
  modification time is at or after the session start, which is the first timestamped
  line of the session's transcript. A resume or compaction appends to the same
  transcript, so a resumed session keeps its own files; files from earlier sessions
  (and from before a `/clear`) are ignored.
- While any such file has a non-`human` gate whose status is `pending` or `fail`, it
  returns `{"decision": "block", "reason": ...}`; the reason says which push this is
  (`push 1/3`) and lists every unmet gate id with its status, grouped under its file's
  path. Claude Code shows the reason to the main session, which keeps working instead
  of ending the turn.
- A file that does not match the schema (not UTF-8 JSON, larger than 1 MB, a missing
  or mistyped field, an unknown `kind` or `status`, an unparsable `ranAt`) also
  blocks; the reason names the file and its first problem, so a typo cannot switch
  enforcement off unnoticed.
- Legitimate pauses end the turn with no decision and spend no push: a `Stop` event
  that carries `agent_id` (a subagent's; only the main session is gated); a non-empty
  `background_tasks` (a test run or other work still running in the background); and
  a stated blocker, a line of `last_assistant_message` that starts with
  `needs input:` or `failed:` (any case, optionally behind a `-` or `*` bullet, a `>`
  quote or `**`/`__` bold). A mention inside a sentence is not a blocker line.
- **Push budget.** It blocks at most three times per episode; the fourth unmet `Stop`
  returns no decision, and so does every later one until a new episode starts, so a
  stuck session stops looping and waits for the owner. A new episode starts when a
  `Stop` finds no unmet gate (the counter is deleted) or when the person sends a
  prompt (see below). The count is kept per session in
  `<provost dir>/cache/acceptance/<session_id>.pushes` (beside `cache/facts` and
  `cache/notifications`; `<provost dir>` is the folder of `provost/config.json`, or of
  `PROVOST_CONFIG`), replaced atomically on each push. A counter that cannot be read
  or written, or a session id that cannot name it, ends the turn instead of blocking.
- Otherwise, and on any internal error (an unreadable transcript, an unexpected
  exception, invalid hook input), it returns no decision and the turn ends: a broken
  check never traps a session. It needs no `provost/config.json` and works with
  handoff off.

The person-prompt reset runs in `hooks/native-facts`' synchronous `UserPromptSubmit`
registration (which `install.py setup` adds), with bash builtins only: when the
session's `.pushes` file holds a count, it empties it, which the gate reads as zero.
This works with handoff off. Limits: without that `UserPromptSubmit` registration
only a clean `Stop` resets the count. A prompt that starts as a `<task-notification>`
or an agent message (a subagent's hand-back, optionally behind the "Another Claude
session sent a message:" wrapper) is not the person's and does not reset it; every
other submitted prompt does, including any a tool or hook submits on the person's
behalf, because a hook cannot tell those apart. With the `cache/acceptance` folder
present (after the first push), `native-facts` also reads every `PostToolUse` event
with handoff off, only to find that it has nothing to do.

`python install.py setup` registers it; by hand, add it as its own `Stop` entry
without `"async"`:

```json
{
  "hooks": {
    "Stop": [
      {"hooks": [
        {"type": "command", "command": "bash \"<runtime>/hooks/native-event\"", "async": true, "timeout": 20}
      ]},
      {"hooks": [
        {"type": "command", "command": "bash \"<runtime>/hooks/acceptance-gate\"", "timeout": 30}
      ]}
    ]
  }
}
```

`python install.py check` reports `settings: not-registered` while the synchronous
registration is missing; an `acceptance-gate` entry marked `"async": true` does not
count, because an async hook cannot block. Do not register it for `SubagentStop`.
