# Roadmap

This roadmap describes intended work, not promised dates. Items move to
"Current" only when they are present and verifiable in the repository.

## Current

- Orchestration policy with difficulty × risk classification, evidence-based
  review triggers, an R2 stop, a writer budget and a finite Completion Contract
  ([`native/CLAUDE.native.md`](native/CLAUDE.native.md)).
- Eight roles across Haiku, Sonnet, Opus and Fable ([`agents/`](agents/)).
- Handoff runtime: sealed per-session records shared across worktrees,
  compaction recovery, facts capture and reminders, search, retention and
  prune with archive.
- `install.py` with backups, rollback and a read-only drift check.
- CI on Windows, Ubuntu and macOS for the runtime and installer.

## Next

- Remove the unused plugin-mode SessionStart path and the stale
  `native-reminder` entry from the runtime, then `legacy.py`.
- Follow-ups from review: hold the session lock through POSIX prune removal,
  roll back `install.py` on interrupts, exercise junction escapes on Windows.
- Bind policy statements to tests so the policy text, role table and README
  cannot drift apart.
- Re-measure how much the main session implements itself (next around
  2026-11 mid) and publish de-identified results.

## Later / exploration

- Fixed verdict vocabulary for reviewer reports so outcomes can be counted
  across sessions.
- A plan challenge before hard or R2 work, using the existing reviewer.

This project has no official OpenAI, Anthropic, or other vendor partnership or
integration unless explicitly documented in the future.
