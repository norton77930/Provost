# Review guide

What failure looks like in this repository, for `code-reviewer` and for people.

## What failure looks like

- **Hooks:** a hook that blocks, crashes or prints a traceback where it should
  fail soft; a synchronous hook that does work before its cheap early exit.
- **Records:** a handoff record written partially, overwritten without the
  expected revision, or carrying a credential, private path or raw transcript.
- **State across runs:** the store is read by later runs, other sessions, retries,
  `migrate`, `prune`, `search` and hooks, so check each written or deleted value
  from their side. Past misses here:
  - a file identity check (`st_dev`/`st_ino`) that matches a deleted lock file
    whose inode a recreated one reused;
  - a replay of the same request handled differently from its first attempt, or
    a phase like `done` compared differently in different modules;
  - `prune` judging idleness from a time that `migrate` keeps old while it
    rewrites the file, or missing a source `migrate` reads (`archive/`), so it
    deletes what was just written or will be imported again;
  - a correction (`supersedes`) that leaves the replaced entry current;
  - a reader treating a newer `snapshot_version` as its own, a prefix that
    matches several cards, or an error payload quoting card text unredacted;
  - a hook exclusion pattern that also matches legitimate content (a result
    that merely mentions `SubagentHandback`).
- **Installer:** `install.py` changing a file without a backup, a rollback that
  does not restore the previous state, or `check` reporting healthy when it is not.
- **Policy and docs:** they lie — a rule in `native/CLAUDE.native.md`, a role
  file, `README.md`, `README.zh-TW.md` or the operations guide that contradicts
  another or describes behaviour the code does not have.

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
