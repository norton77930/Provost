# Field notes: running Claude Code hard

Hard-won operational notes from running Claude Code. Some are universal; some are
specific to running *non-Anthropic* models behind an Anthropic-compatible proxy —
each note says which. Observed on Claude Code 2.1.21x (early 2026); internals
change, so treat these as a starting point, not a contract.

## Spotlight — taming GPT-5.6 Sol: structure beats the model lottery

*Running non-Anthropic models via a proxy — GPT-5.6 Sol especially.*

GPT-5.6 Sol has a rough reputation — "powerful but hard to drive." In practice the
problem usually isn't capability; in the author's observed proxy setup, it was
that the model did not reliably converge: it over-ran, gold-plated, wandered off
the task, and retried the same failure. The Tier 1 policy contains the disciplines
used in that setup. This is an operational field note, not a controlled benchmark
or a general reliability claim.

1. **Plan the whole execution *before* you start, and make "done" enumerable.** The
   biggest lever — one mechanism doing two jobs. Sol drifts when the goal is vague
   and the steps are improvised mid-run. Provost requires a **Completion Contract in
   the Plan before any implementation** (see [`orchestration.md`](../orchestration.md)):
   a finite list of required claims, the specific evidence for each, the roles that
   will resolve them, and *one* planned final verification. That pre-declared route
   stops the drift; the finite, enumerable claim list gives it a wall to stop at
   (`active → sealing → done`: once every claim is PASS-current, only the final
   verification runs, then you stop — no "while I'm here" additions). Vague goals let
   Sol wander; an enumerable contract makes it converge.

2. **Cap each role report (~4,000 tokens).** Provost's default role prompts don't
   hard-cap report size — native large-window models don't need it — but GPT-5.6 Sol
   behind a proxy does: a hard budget keeps sub-agent reports from bloating the main
   context on a tighter window, and it nudges the model to converge its own output.
   If you run Sol, add "stay within ~4,000 tokens" back to the role prompts.

3. **Fix the context ceiling.** Set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` (Sol isn't a
   `claude-` model, so it applies) and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` — see the
   context-window notes below — so a long session doesn't hit the wall.

4. **Pin a model to every role.** Avoids the `general-purpose` 502 (see the note
   below): a route that emits a native `claude-*` ID breaks against a non-Anthropic
   gateway.

5. **One active writer, plan first.** A single writer plus plan-before-Auto keeps a
   wander-prone model on one track instead of sprawling across the codebase.

**The lesson isn't about GPT-5.6.** None of the above is model-specific magic — it's
the same structure Provost applies to any crew. A model's reputation is often a
*lack-of-structure* problem wearing the model's name. **Structure beats the model
lottery.**

## Universal — the bundled `claude-api` skill can blow your context window in one message

*Applies to everyone, native models included.*

Claude Code ships a bundled `claude-api` skill whose trigger description matches
almost any prompt that mentions Claude or Anthropic. When it fires, it injects its
full multi-language SDK reference **as a single message**. Measured sizes:
~799,000 characters (~171,000 tokens) in one version, ~575,000 characters
(~123,000 tokens) in another.

On a normal ~200k-token window that single injection can push the prompt over the
limit *before the request is even sent*; Claude Code then auto-compacts, and if
the compacted prompt is still over budget the session can't recover. In a
Claude/Anthropic-related repo it hits nearly every turn.

Hide it from the model while keeping it available on demand:

```json
{ "skillOverrides": { "claude-api": "user-invocable-only" } }
```

`user-invocable-only` hides the skill from the model; you can still summon it with
`/claude-api`.

## Proxy-only — `CLAUDE_CODE_MAX_CONTEXT_TOKENS` applies only to non-`claude-` models

*Relevant only if you run a non-Anthropic model behind a proxy.*

Claude Code resolves a model's context window and, for the auto-compact threshold,
only reads `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when the **model ID does not start with
`claude-`**. For native Claude models it falls back to a constant (200,000). So if
you're running a non-Anthropic model through a proxy, this variable lets you raise
the window to what that model actually supports; on native models it does nothing.

## Proxy-only — `CLAUDE_CODE_AUTO_COMPACT_WINDOW` can only *lower* the threshold, never raise it

*The `min()` behavior is universal; the "raise it" use case is proxy-specific.*

It is applied as `min(modelWindow, value)`. Setting it *above* the model window
has no effect — a common cause of "I set it to 280000 but it still compacts around
150k." To actually move the compaction point up on a non-native model, raise
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` first, then set the auto-compact window below it.

## Proxy-only — the `general-purpose` route can emit a native model ID

*Relevant only when bridging Claude Code to non-Anthropic models.*

The built-in `general-purpose` route and the Auto classifier don't always inherit
your bridge model — they can emit a native `claude-*` model ID, which a
non-Anthropic gateway rejects (often as a 502). If you need a guaranteed model per
delegation, pin it explicitly (exactly what Provost's per-role `model` fields do)
or constrain delegation to roles you control.

## Universal — a hook whose interpreter is missing does not block, and says nothing

*Measured on Claude Code 2.1.257, Windows.*

A `PreToolUse` hook that exits 2 blocks the tool call. A hook whose command
cannot be found does not: the tool proceeds, and no hook event appears in the
`stream-json` output at all. Not a warning, not a failed-hook record — nothing.

This matters if you ship hooks written for one platform in something that
installs on all of them. The tolerant behaviour is convenient until the hook was
the thing enforcing a rule, at which point you have a session that believes it is
protected and is not. If a hook is load-bearing, have whatever depends on it
require positive evidence that it ran, rather than trusting that a missing hook
would have complained.

## Universal — nothing tells you which hooks are actually registered

*Measured on Claude Code 2.1.257.*

`claude doctor` reports the installation, not the hook configuration. There is no
hooks subcommand. A tool subprocess sees `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`
and friends, but nothing about hooks, and `CLAUDE_PLUGIN_ROOT` is set only for
hook processes.

What is available: a `SessionStart` hook's payload `session_id` is the same value
a tool subprocess reads from `CLAUDE_CODE_SESSION_ID` in that session. So a
`SessionStart` hook can write a marker carrying that id, and anything running as
a tool call can require a marker matching its own session before trusting that
hooks are live. Only a hook that actually ran can produce it, and the id match
rules out a marker left by an earlier session.

## Universal — `--settings` hooks are added to a project's, not swapped for them

*Measured on Claude Code 2.1.257.*

Hooks supplied through `claude --settings <file-or-json>` concatenate with the
hooks a project's own `.claude/settings.json` declares — both fire on the same
tool call. A project cannot displace an injected hook by declaring its own.

`disableAllHooks: true` suppresses everything, `SessionStart` included. That is
worth knowing if you rely on a marker written by a `SessionStart` hook: the
combination that would be dangerous — the marker present while a `PreToolUse`
gate is gone — does not occur, because disabling takes both.

## Universal — every hook costs a process on every matching tool call

*Measured on Windows PowerShell 5.1, an ordinary corporate laptop.*

A `PreToolUse` hook that exits immediately still cost about 2.6 seconds per
matching tool call, of which roughly 2.0 seconds was bare `powershell.exe
-NoProfile` startup. WSL `bash` was not cheaper as a prefilter. Antivirus is the
likely multiplier, but that is the machine users have.

The consequence for anything distributed: a hook registered for `Edit|Write|Bash`
taxes every one of those calls for everyone who installs it, including the
majority who never use the feature it guards. Register per-invocation through
`--settings` when the feature is opt-in, rather than shipping the hook in a
plugin that installs for all.
