# Adaptive host validation status

> **Status as of 2026-09-22.** Later live results (worktree read/write against the relocated store, SessionStart compact injection from a worktree) are recorded in §0 of `docs/analysis/2026-10-05-handoff-store-relocation-and-retention.md`. Recovery after a real auto-compact on the relocated store is still under observation.

Local baseline: Windows, Python 3.12.9, Claude Code 2.1.278.
No real personal settings or selected-project pilot modified during implementation.

- Config/store/CLI/hooks: synthetic temporary-workspace tests (see ledger).
- Windows wrappers: Git for Windows Bash. Linux/macOS CI provided, not locally run.
- Actual interactive auto-compact, PostCompact order and notification visibility: pending live pilot.
- Noninteractive terminal notification visibility: not guaranteed.
- Actual Opus/high default and Sonnet/high override: pending authorized live dispatch.
- Workflow fixtures check contract completeness, not agent behavior or enforcement.

Official interfaces: [Hooks](https://code.claude.com/docs/en/hooks) and
[subagents](https://code.claude.com/docs/en/sub-agents). Recheck target CLI before rollout.
No blocking compaction decision is emitted. Missing Python cannot update notification
cache. Model frontmatter and synthetic CI are not proof of live host behavior.

Local final checks: 50 Python tests (49 pass, 1 Windows symlink skip); 11 existing
governance scripts pass; both marketplace and plugin manifest validation pass.
See the implementation ledger for commands and limitations.
