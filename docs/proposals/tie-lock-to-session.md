# Proposal: tie a running lock to the session that opened it

Status: proposed, not started. Tracked under "Next" in
[`ROADMAP.md`](../../ROADMAP.md).

A proposal lives here only while it is unbuilt. When the change lands, the
roadmap entry moves to "Current" and this file is deleted — what shipped is
described by the code, the tests, and `CHANGELOG.md`, and a proposal left behind
starts contradicting them.

## Why

`Initialize` records whether a run opened under hook enforcement, and refuses to
adopt continuation work from a run that did not. That record is **open-time
truth**: it says the hooks were live when the run began, not that every write in
it was policed.

The gap that leaves: a lock opened under enforcement can be driven through
`StartTask`, `FinishTask`, and `Complete` from a later plain session — no
`PROVOST_SESSION_PROFILE`, no hooks, nothing watching the writes. The agent
supplies `-SessionId` itself and can read the value out of the lock file. The
resulting handoff receipt still reads `enforcement.mode = 'hooks'`, and a later
enforced run adopts it.

So the continuation gate can be satisfied by work that was not, in fact, policed.

## What changes

`Get-ActiveLockForSession` gains a check: when the lock records
`enforcement.mode` as anything other than `none`, the calling session must be the
one that opened it. `enforcement.session_id` is already in the lock; the caller's
identity is `$env:CLAUDE_CODE_SESSION_ID`.

## Requirements

- **R1** A lock opened under enforcement is only advanced by the session named in
  its own `enforcement.session_id`. Any other caller is refused, with a code
  distinct from the existing `ACTIVE_LOCK` complaints so a test can tell which
  check fired.
- **R2** A lock opened with `mode = 'none'` is unaffected. Helper-level use is a
  supported mode and the five older tests must pass untouched.
- **R3** Refusal must not be silent and must not be recoverable by supplying a
  different `-SessionId`. The agent controls that argument; it does not control
  `CLAUDE_CODE_SESSION_ID`.
- **R4** No manifest schema change. The lock already carries what is needed.

## Non-goals

- Re-verifying the liveness marker on every lifecycle action. The marker proves
  the session; the session id then carries that forward. Re-reading it per action
  would cost a file read on every call for no additional claim.
- Anything about `RecoverLock`. A stuck lock still needs a way out, and that path
  is deliberately operator-driven. Decide explicitly whether it stays exempt and
  say so in the code, rather than leaving it to be discovered.

## Verification

- A governed run advances its own lock through `StartTask` and `FinishTask`.
- A plain session — profile unset — is refused when it tries to advance a lock
  recorded as enforced, and the refusal is matched on the new code, not on
  `ACTIVE_LOCK`.
- A different governed session, with its own marker and its own session id, is
  also refused. This is the case that separates "must be enforced" from "must be
  *the same* session", and a test that only covers the plain-session case cannot
  tell them apart.
- A `mode = 'none'` lock is advanced by any caller, as before.
- The full suite passes: 12 checks, `.github/workflows/ci.yml` lists them all.

## What will bite you in this codebase

None of these are hypothetical. Each one cost real time in the session that
produced the enforcement record.

1. **Nineteen different checks raise `CONTINUATION`, and several share
   `ACTIVE_LOCK`.** `Assert-ThrowsCode -Code X` alone does not prove which check
   fired. Match on the message, or add a distinct code — this proposal asks for
   the latter.

2. **Artifacts are hash-pinned to each other.** A handoff receipt is hashed into
   the archived lock that references it; the approved manifest is hashed into the
   lock and into any continuation that names it. Editing one in a fixture without
   restating the other fails an earlier integrity check and never reaches the
   code under test. Build fixtures by driving the real lifecycle wherever
   possible.

3. **Every hook self-gates on `PROVOST_SESSION_PROFILE -ne 'foreman'` and exits
   0.** The tests that set that variable (`Test-WriteScope`, `Test-RefGuard`) do
   not call `Initialize`; the tests that call `Initialize` do not set it. That
   separation is why the enforcement check could be added without touching them.
   Preserve it.

4. **The lock is constructed once, in `Invoke-Initialize`, and every other write
   path mutates a copy read back through `Read-JsonMap`.** Fields survive that
   round trip. If you find yourself rebuilding the lock object anywhere else,
   stop — that is how a field gets silently dropped.

5. **A test that only proves a refusal cannot distinguish a working gate from one
   that refuses everything.** Cover the accepting case in the same test.

6. **Process startup costs about two seconds on a normal Windows machine.** Do
   not add a hook, or a per-action subprocess, without weighing that. See
   `docs/field-notes.md`.

7. **Line endings are LF and files carry no BOM.** PowerShell's
   `Set-Content -Encoding utf8` writes a BOM on 5.1. Use
   `[System.IO.File]::WriteAllText` with `UTF8Encoding($false)`, as the rest of
   the repository does.

## Ground rules from CONTRIBUTING

A reference-runtime change needs a focused failing check first, the smallest
coherent fix, and regression coverage. Changes must not silently weaken a stated
guarantee; if a tradeoff requires weaker enforcement, say so in the proposal, the
tests, the docs, and the pull request.
