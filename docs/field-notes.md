# Field notes: running Claude Code hard

Operational notes from pushing Claude Code — including running non-Anthropic
models behind an Anthropic-compatible proxy. These are observed behaviors on
specific versions (Claude Code 2.1.21x, early 2026); internals change, so treat
them as a starting point, not a contract.

## The bundled `claude-api` skill can blow your context window in one message

Claude Code ships a bundled `claude-api` skill whose trigger description matches
almost any prompt that mentions Claude or Anthropic. When it fires, it injects
its full multi-language SDK reference **as a single message**. Measured sizes:
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

## `CLAUDE_CODE_MAX_CONTEXT_TOKENS` only applies to non-`claude-` models

Claude Code resolves a model's context window and, for the auto-compact
threshold, only reads `CLAUDE_CODE_MAX_CONTEXT_TOKENS` when the **model ID does
not start with `claude-`**. For native Claude models it falls back to a constant
(200,000). So if you're running a non-Anthropic model through a proxy, this
variable lets you raise the window to what that model actually supports; on native
models it does nothing.

## `CLAUDE_CODE_AUTO_COMPACT_WINDOW` can only *lower* the threshold, never raise it

It is applied as `min(modelWindow, value)`. Setting it *above* the model window
has no effect — a common cause of "I set it to 280000 but it still compacts around
150k." To actually move the compaction point up on a non-native model, raise
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` first, then set the auto-compact window below it.

## The `general-purpose` route can emit a native model ID

When you bridge Claude Code to non-Anthropic models, the built-in
`general-purpose` route and the Auto classifier don't always inherit your bridge
model — they can emit a native `claude-*` model ID, which a non-Anthropic gateway
rejects (often as a 502). If you need a guaranteed model per delegation, pin it
explicitly (exactly what Provost's per-role `model` fields do) or constrain
delegation to roles you control.
