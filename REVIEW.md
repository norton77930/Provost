# Review guide

What failure looks like in this repository, for `code-reviewer` and for people.

## What failure looks like

- **Hooks:** a hook that blocks, crashes or prints a traceback where it should
  fail soft; a synchronous hook that does work before its cheap early exit.
- **Records:** a handoff record written partially, overwritten without the
  expected revision, or carrying a credential, private path or raw transcript.
- **State across runs:** the store is read by later runs, other sessions, retries,
  `migrate`, `prune`, `search` and hooks, so check each written or deleted value
  from their side. The reliability checks below list the past misses of this kind.
- **Installer:** `install.py` changing a file without a backup, a rollback that
  does not restore the previous state, or `check` reporting healthy when it is not.
- **Policy and docs:** they lie — a rule in `native/CLAUDE.native.md`, a role
  file, `README.md`, `README.zh-TW.md` or the operations guide that contradicts
  another or describes behaviour the code does not have.

## Reliability checks

Each check names a trigger you can read from the diff, what to check, and the
commits that fixed the miss behind it. Read the diff, then apply only the checks
whose trigger it matches.

- **File identity.** Trigger: the diff compares files by `st_dev`/`st_ino`, or
  deletes and recreates a lock or store file. Check: a recreated file can reuse a
  deleted file's inode, so identity must not match across a delete.
  Source: 5ff7d26.
- **Replayed requests.** Trigger: record, join or adoption code that handles a
  repeated request id. Check: a replay gets the same result as its first attempt,
  including a first attempt that logged nothing or that created the card.
  Source: 630583d, fdbd0c5.
- **Phase comparisons.** Trigger: code that compares a phase or state string such
  as `done`. Check: every module compares the same way (`store.is_done`), not with
  its own literal. Source: fdbd0c5.
- **Deletion by age.** Trigger: `prune` or other code that deletes by idle time or
  expiry, or lists the sources it may delete. Check: no writer keeps the time old
  while it rewrites the file (`migrate` keeps the legacy `at`), and every source a
  later run reads (`archive/`) counts, so nothing just written, or about to be
  imported again, is deleted. Source: 03812f3.
- **Corrections.** Trigger: `supersedes`, `updates` or duplicate detection in
  records. Check: the replaced entry stops being current, also when an earlier
  entry of the same record replaced it. Source: 630583d.
- **Reading stored records.** Trigger: code that reads `snapshot_version`, matches
  card ids or prefixes, or quotes stored text in an error. Check: a newer version
  is not read as the reader's own, a prefix that matches several cards is refused
  as ambiguous, and quoted card text is redacted. Source: 2d254db.
- **Hook exclusion patterns.** Trigger: a pattern that makes a hook skip content
  (notice filters, exclude lists). Check: it does not also match legitimate
  content, such as a real result that mentions `SubagentHandback`.
  Source: 31b7a4f.

## Adding a check

Add a reliability check only when its failure class appeared in at least two
PRs, or when the original diff, replayed to `code-reviewer` three times, showed
the defect in at least two runs although the original review missed it on a
small diff. A miss that meets neither stays an eval case (#56). Every check
states its trigger, check and source commits. The checks above share one class,
state read by later runs, which appeared in #20, #21, #23 and #26. Remove a check
when its trigger no longer matches any code.

## Always review

A change touching one of these paths is at least the tier named, whatever the
declaration would otherwise be.

- `hooks/**`: R1. A hook that blocks or crashes where it should fail soft stops
  every session, and tests run the hook in isolation.
- `scripts/provost_runtime/**`: R1. Record writes and acceptance checks fail
  across sessions and restarts, which a focused test rarely covers.
- `install.py`: R1. A wrong backup or rollback changes the live Claude config.

## Limits

- Report at most three `Nit` findings.
- Skip `docs/analysis/` (dated design notes) and `docs/demo/` (promo media)
  unless the change is to them.
- Cite the file and line that shows each defect.
