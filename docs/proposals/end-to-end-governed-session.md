# Proposal: an end-to-end governed session walkthrough

Status: proposed, not started. Tracked under "Next" in
[`ROADMAP.md`](../../ROADMAP.md).

A proposal lives here only while it is unbuilt. When the change lands, the
roadmap entry moves to "Current" and this file is deleted — what shipped is
described by the code, the tests, and `CHANGELOG.md`, and a proposal left behind
starts contradicting them.

## Why

`docs/examples/` has seven walkthroughs. Every one of them runs a single check
against an isolated fixture and explains what that check proves. None of them
opens a governed session and walks a change through it.

That was the right shape while there was no launcher. There is one now, and a
reader who wants to know what using Provost is actually like has nothing to
read. The seven demos answer "is this decision implemented"; nobody has written
the one that answers "what happens when I use it".

## What changes

One new walkthrough, `docs/examples/governed-session-walkthrough.md`, that a
reader can follow on a Windows machine from an empty directory to a completed
governed run, and a second path showing a refusal.

It must be executable as written. Every command a reader is told to run has to
be a command that works when pasted, with the output they will actually see —
not a summary of what would happen.

## Requirements

- **R1 It runs from a clean machine.** Prerequisites stated: Windows,
  PowerShell, `git`, the `claude` CLI, and the plugin installed. No step assumes
  state left by another document.
- **R2 It shows a refusal, not only a success.** The value of the tier is what
  it stops. At minimum: a write outside the running task's `write_set` is denied
  by the hook, and the reader sees the denial.
- **R3 Every command and every output is real.** Run it, paste what came back.
  Where output varies between machines — paths, run ids, hashes — show the shape
  and say which parts differ.
- **R4 It states what the walkthrough does not prove.** The existing seven all
  carry a Limitations section; this one carries the honest version of "you have
  now seen the write gate stop something", including that Tier 2 is Windows-only
  and that the enforcement record is open-time truth.
- **R5 Existing documents stay consistent.** `README.md`, `README.zh-TW.md`, and
  `docs/governance/README.md` reference the examples; if this changes what a
  reader should read first, update them. `Test-MarkdownLinks.ps1` must pass.

## Non-goals

- No new automated check. This is documentation of a path the existing twelve
  checks already cover in pieces. Do not add a thirteenth CI step.
- No change to the helper, the hooks, or the launcher. If the walkthrough is
  awkward to write because a command is awkward to run, say so in the report —
  that is a finding, not something to fix inside this change.
- Not a quickstart for the collaborator tier. That already exists in both
  READMEs and is a different audience.

## Verification

- The full walkthrough was executed start to finish on a Windows machine, and
  the pasted output is from that run.
- The refusal path was executed and its denial pasted.
- `Test-MarkdownLinks.ps1` passes, and the full suite still passes: twelve
  checks, listed in `.github/workflows/ci.yml`.
- The document follows the shape the other seven use: title, Prerequisites, the
  steps, what is enforced, Limitations.

## What will bite you in this codebase

Each of these cost real time already.

1. **The launcher cannot be invoked through `powershell.exe -File` with an
   argument array.** `-File` passes everything as strings, so
   `-ClaudeArguments @('-p','...')` arrives as literal text. From a PowerShell
   prompt, `.\docs\governance\reference\Start-GovernedSession.ps1 -WorkspaceRoot
   <path>` works normally. Write the walkthrough for the prompt, and check any
   command you give the reader by pasting it yourself.

2. **A governed session refuses to `Initialize` unless the `SessionStart` hook
   wrote a liveness marker naming that session.** If you construct a session by
   exporting `PROVOST_SESSION_PROFILE` by hand instead of using the launcher,
   `Initialize` will refuse and it will be correct to. Use the launcher.

3. **A running lock is bound to the session that opened it.** You cannot open a
   run in one session and advance it from another; `StartTask` and the rest
   refuse with `SESSION`. Plan the walkthrough as one continuous session.

4. **Process startup costs about two seconds per hooked tool call on Windows.**
   The walkthrough will feel slow. That is the real experience and worth a
   sentence, not something to hide.

5. **Line endings are LF and files carry no BOM.** PowerShell's `Set-Content
   -Encoding utf8` writes a BOM on 5.1. Use `[System.IO.File]::WriteAllText`
   with `UTF8Encoding($false)`, as the rest of the repository does.

6. **`Test-MarkdownLinks.ps1` checks every relative link in every `.md`.** A
   walkthrough that links to a file it expects the reader to create will fail
   it.

## Ground rules from CONTRIBUTING

Documentation changes keep `README.md`, the core status section in
`README.zh-TW.md`, and the governance capability matrix consistent. Do not
weaken a stated guarantee, and do not claim more than the walkthrough
demonstrates.
