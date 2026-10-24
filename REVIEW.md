# Review guide

What failure looks like in this repository, for `code-reviewer` and for people.

## What failure looks like

- **Hooks:** a hook that blocks, crashes or prints a traceback where it should
  fail soft; a synchronous hook that does work before its cheap early exit.
- **Records:** a handoff record written partially, overwritten without the
  expected revision, or carrying a credential, private path or raw transcript.
- **Installer:** `install.py` changing a file without a backup, a rollback that
  does not restore the previous state, or `check` reporting healthy when it is not.
- **Policy and docs:** they lie — a rule in `native/CLAUDE.native.md`, a role
  file, `README.md`, `README.zh-TW.md` or the operations guide that contradicts
  another or describes behaviour the code does not have.

## Limits

- Report at most three `Nit` findings.
- Skip `docs/analysis/` (dated design notes) and `docs/demo/` (promo media)
  unless the change is to them.
- Cite the file and line that shows each defect.
