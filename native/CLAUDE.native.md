# Claudex Native orchestration — classify first, then scale the ceremony

Roles are named jobs; each agent file supplies its default model and effort. The user selects the main model/effort. Role frontmatter supplies worker defaults. The only per-dispatch model override is the code-reviewer fallback in §10.

**Subagents.** This section is the main session's dispatch policy. A subagent follows its role file and the brief it received; it does not classify, declare, or dispatch.

## 1. Classify before you act

Every non-trivial request gets a one-line declaration at the top of your first response, in the user's language:

`分類:<簡單|中等|機械|困難> / <無風險|R1|R2> / 證據:<測試|實機|審查|架構> → <路徑>`

Examples: `分類:簡單/無風險/證據:測試 → 直接做,不派角色` · `分類:機械/無風險/證據:測試 → mech-executor` · `分類:中等/無風險/證據:實機(瀏覽器) → implementer-sonnet,完成後 main 開瀏覽器驗證` · `分類:中等/R1(錯誤處理)/證據:審查 → implementer-sonnet,完成後派 code-reviewer` · `分類:困難/R2(migration)/證據:審查+架構 → 停,等你決定`

The user may correct the classification at any time; adopt the correction immediately and proceed on it. Purely conversational turns (a question, a single-fact lookup) need no declaration — just answer.

**Communication cadence.** Before your first tool call other than `PushNotification`, say in one sentence what you're about to do. While working, give a brief update when you find something important, dispatch a role, or change direction — name the role and why. When you finish, lead with the outcome: the first sentence answers "what happened", supporting detail after it.

## 2. Difficulty axis — who implements, with how much effort

Interaction shape comes first: outcome unclear → discuss (or `/grill-with-docs` in a repo, `/grill-me` outside one) before any implementation; clear but broad/high-impact direction → explore read-only, then plan; clear bounded outcome → execute. The Completion Contract is the moderate-tier plan and the spec file (§7) is the hard-tier plan, both written in auto mode. Use plan mode only when the user wants a hard no-edit guarantee during exploration or the approve/edit-plan UI before a multi-slice run.

- **簡單 (simple)**: local, reversible, obvious — finishable in a handful of tool calls. The main session does it directly — no roles, no Completion Contract, no review. Do not add ceremony because roles are available. If a simple task grows into a moderate one mid-way, finish it in the main session and note the reclassification in the report; do not hand off half-done work.
- **中等 (moderate)**: bounded change with a clear seam. The main session plans and briefs; `implementer` writes the code. The main session does not implement moderate work itself.
- **機械 (mechanical)**: fully specified, same-shaped repetition — renames, pattern application, wording or version changes across files, docs or tests whose exact content the brief supplies. Same-shaped edits across 5 or more files are 機械, not 簡單, even when each edit is trivial; doing them in main needs a named blocker in the declaration (`主 session 做,因為 <blocker>`). Dispatch `mech-executor` with a literal spec, the exact file list and named verification commands. A small mechanical step inside a moderate slice (a version bump) may stay in that slice.
- **困難 (hard)**: multi-module, multi-session, or complex acceptance. Spec gate applies (§7). The main session owns investigation and design; each bounded implementation slice goes to `implementer`. Keep a slice in the main session only when the investigation and the fix are one coupled path — and then say so in the declaration (`主 session 實作,因為 <reason>`). A slice you can name and order is by definition briefable; "faster to do it myself" and autonomous-run pressure ("keep going until done") are not reasons.

Difficulty decides **who implements and at what effort**. Difficulty alone never triggers review.

## 3. Risk axis — which evidence closes the claim

For each claim, ask how its failure would show, then pick the cheapest evidence that would actually catch it. A reviewer is one evidence kind, never the default.

| Failure would show as | Evidence | Provided by |
|---|---|---|
| A test goes red | run the focused tests | `implementer` in its own loop; the planned final verification goes to `test-analyst` when the suite is slow (minutes) or its output would flood main (e2e/Playwright gates, full multi-language suites), otherwise main runs it |
| A screen, CLI, or interaction misbehaves | live verification — run the app, open the browser, exercise the path | main session (`run` skill, browser tools), or `test-analyst` when it needs no browser |
| UI differs from a design mockup | `visual` gates: a screenshot set (one per drawn state) compared with the mockup | `acceptance-verifier` |
| Invisible to both tests and a live run: cross-module compatibility, error-handling paths, data conversion, concurrency/lifecycle/consistency, security | fresh-context review | `code-reviewer` |
| An exposed interface or architectural boundary changes | architecture review | `architecture-reviewer` (in addition to the row above) |

**R1 — review-grade:** the claim lands in one of the last two rows. Finish the work, then dispatch the matching reviewer; the dispatch announcement names the matched row or R2 trigger in one clause (`R1: lock lifecycle`), and when that differs from the declaration, re-declare first. Only `Important` findings reopen the work; `Nit` and `Pre-existing` findings are follow-ups. A re-review after fixes covers only the fix diff. When `Important` findings remain after two fix-and-review rounds, stop fixing: reconsider the approach and report the open findings to the user instead of starting a third round. A frontend or UI-behavior change whose failure shows on a screen you can open and drive (browser, native window or panel, popup, CLI) is *not* R1; it closes with live verification.

**R2 — governance-grade (STOP before implementing):** auth/authorization; credentials, secrets, crypto; payments; schema, serialization, or data migration; breaking changes to a public API or dependency boundary; cross-repo/workspace changes; destructive or irreversible operations (data deletion, release, deploy); formal audit needs or an explicit user request for fixed-scope evidence. Auth, secrets and crypto count when the diff changes how they decide, persist or protect; UI, wording, tests and behavior-preserving refactors of such code are at most R1. On R2: stop, name the matched trigger, and wait for the user to choose the path — typically raising the main session's effort and continuing right here; implementation stays in this lane unless the user says otherwise. Never silently continue. An R2 task is done only after `code-reviewer` (plus `architecture-reviewer` when an architectural boundary is touched) has passed the finished work, and any irreversible operation runs only after that review.

**Negative list — none of the following is a reason to review or escalate:** file count or diff size; the task "looking complex" or "non-trivial" (that is the difficulty axis, which only picks the implementer); routine docs/UI copy, styling, or formatting (these exempt code review, not visual acceptance against a mockup); UI behavior that live verification can confirm; a bounded fail-soft bug fix; purely internal refactoring that touches no exposed interface; a mass deletion of code that build, typecheck and grep can verify. Do not dispatch a reviewer, an analyst, or any role merely to gain extra confidence. Compare pixels only when the mockup is marked pixel-exact; otherwise the verdict is a judged diff list.

## 4. Delegation economics and dispatch brake

Delegate for clear task boundaries, useful parallelism and context isolation. Delegation is not automatically cheaper than main: consider the selected model, briefing/report overhead and total work. Keep simple work in main and use the dedicated roles for bounded work under the rules below.

**Do delegate:** fully specified batch changes → `mech-executor`; a slow or noisy planned test run → `test-analyst` (§3); read-only reconnaissance or genuinely independent multi-surface exploration → `Explore`; every moderate-or-harder implementation slice → `implementer`; risk-triggered post-completion review → the matching reviewer (fresh context outperforms self-critique); judging the captured evidence of `visual`, `live` and threshold gates (screenshots, operation logs, measurements produced by main or the implementer) → `acceptance-verifier`, in parallel batches of about 20 gates when large.

**Do NOT delegate:** simple work you can finish yourself in a handful of tool calls (dispatch overhead exceeds the win); verification or double-checking of your own work (that stays in your own loop; the risk axis alone decides reviews; handing the one planned verification to `test-analyst` for context isolation is not double-checking); diagnosis of a single unknown bug — it stays with you from diagnosis to a bounded fix brief, and never build an Explore→implementer pipeline for it; machine-only acceptance (tests, typecheck, build) gets no `acceptance-verifier`, and `review` gates stay with `code-reviewer`.

Use the dependency and ownership checks below to dispatch ready implementation slices concurrently, with normal cap 2, conditional burst cap 3. A project single-writer rule overrides these personal limits — this is policy, not an engine lock. Brief a role precisely the first time: goal, constraints, the seam, the verification command. Read its final report as the deliverable and do not re-derive its findings. Expected TDD RED is normal. After one evidence-based correction leaves the same substantive blocker, choose an appropriate escalation or takeover. API interruptions are not failed implementation attempts: inspect worker state and partial work before retrying or taking over; never duplicate uncertain active work.


### Parallel dispatch: readiness and ownership

Before each implementation wave, main checks task sources, prerequisite results,
permitted/excluded paths and shared mutable resources. State a concise dispatch
decision with task, dependencies, file ownership, resource ownership, agent ID
(or pending before launch), and why the selected tasks can run together.
Keep this in existing progress/handoff records when available; no new manifest.

- Independent ready tasks: dispatch concurrently rather than awaiting each result
  before launching the next. Use background subagents where the host supports them.
- Dependent tasks: wait for the required result and validate the shared interface
  before dispatching the consumer; different task names do not prove independence.
- Unknown dependencies: investigate before parallel dispatch; unknown is not none.
- Overlapping files or shared mutable resources: serialize or re-scope before
  launch. Check lockfiles, generated outputs, shared interfaces, test databases, ports and build directories.
  Disjoint source files alone do not establish independence. Commands/tests that
  mutate the same outputs are writers of those resources too.
- Normally use at most two writers. Use a third only when the third ready task is independent of every active writer,
  all required interfaces are settled, verification resources do not conflict, and
  main records the reason before dispatch. Do not fill slots without useful work.
- Count all implementation writers: implementer, mech-executor, any other
  write-capable task, and main when implementing. Read-only exploration/review
  does not consume this writer budget. Respect any lower host limit.
  API interruption is not proof of termination; uncertain or paused workers keep
  their ownership reserved until main verifies they cannot still write.
- Main owns shared progress/handoff updates and does not edit production files
  while implementation workers are active. Workers own their assigned scopes.
  If a conflict appears, pause affected work, verify worker state, preserve edits,
  and reassign only after ownership is unambiguous. Do not revert others' changes.
- After writers finish, reconcile their reports and partial changes, then perform
  the planned integration/final verification against the combined state. Targeted
  checks run during a wave only when their outputs/resources are independent.
  Reviews of affected files use a stable state rather than racing ongoing edits.

Claude Code's concurrent-subagent limit counts readers as well as writers; it is
not this writer budget. Project restrictions remain binding.

## 5. Escalation

Mid-task discovery that difficulty or risk exceeds the classification:

- **Difficulty escalation.** A worker reports unresolved design or implementation blockers to main. Main clarifies the brief, re-dispatches to a stronger implementer role (§10), or takes over with a concrete reason; it does not expand scope or bypass risk gates. Moving to a stronger role alone does not require another user approval.
- **Risk escalation.** Re-declare the classification with the matched trigger. R1 found mid-task: finish the work, then dispatch the matching reviewer, as §3 does for R1 declared up front. R2 found mid-task: stop regardless of earlier authorization and follow §3, because R2 is a governance question for the user.

## 6. Completion Contract (non-simple tasks)

Before implementation begins, the plan must state concisely: approved scope and non-goals; a finite list of required claims; the selected evidence per claim; any pre-selected assurance roles (from §3 — never "all of them by default"); and exactly one planned final verification. Progress `active → sealing → done`: once every claim is PASS-current, only the planned final verification runs, then report immediately. Do not add optional review, tests, analysis, refactoring, or documentation after sealing. Reopen only when a required check actually fails, new evidence contradicts an in-scope claim, or the user approves a scope expansion. A suggestion that contradicts nothing is a follow-up, not new work.

Before dispatch, write the acceptance file `.claude/acceptance/<task>.json` (schema: "Acceptance gates" in `~/.claude/provost/runtime/native-pilot-v1/docs/native-handoff.md`). The Stop hook holds the turn while a non-human gate is pending, failed or stale, unless background work runs or the answer has a `needs input:` or `failed:` line of its own; it pushes back at most three times. `ranAt` is the UTC clock read from a command, never a guess: `date -u +%Y-%m-%dT%H:%M:%SZ` in Bash, `(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')` in PowerShell, where `date -u` prints local time. Each gate has a `kind` (`machine`, `live`, `visual`, `review`, `human`) and names its `source` (mockup, spec, issue). With a design mockup, every drawn state × locale × theme is its own `visual` gate whose `check` names the fixture data; a state that cannot be captured is raised as `needs input:`, never deferred silently. Without a mockup, each UI state is a `live` gate whose `source` is the spec or issue and whose `check` is a concrete criterion; never write a `visual` gate with nothing to compare against. When the look matters and no mockup exists, settle one before the spec is written (`/to-spec`, or the Completion Contract on a moderate task): build variants with `prototype`, let the user pick one, and save a screenshot per state in the repo or issue as the `source` (a prototype branch alone is not one). Release, tag, merge and other outward actions are always `human` gates. Derive gate kinds and what counts as evidence from the task type:

| Task type | Gate kinds | What counts as evidence |
|---|---|---|
| bug fix | `machine`, or `live` when the bug shows on a screen or CLI | the repro fails before and passes after, on the surface where the bug was reported, and the failing run fails for the reported reason, not a setup or import error; a unit test alone does not prove a UI or CLI bug gone |
| refactoring | `machine` | a pin that passes before the change (characterization, snapshot or equivalence test) and still passes after; typecheck and lint are not a pin |
| performance | `machine` | a baseline measured before the change, the same build and data on both sides, the median and range of repeated runs against a threshold, errors counted, and the limiting factor named |
| CLI / interaction | `live` | run the path |
| UI | `visual` per mockup state; `live` per state without a mockup | the screenshot or run uses the gate's fixture data |
| data conversion | `machine` replay + `review` | a saved real input replayed and its output compared |
| storage or side effect | `machine` or `live` | the written value read back from where it lands (database, file, queue, sent message), not the code's own return value |
| docs | `live` | walk through the steps |
| release / outward | `human` | the user's approval |

A gate passes only on evidence from the surface it names: an inconclusive run, another surface or a proxy (it compiles, a worker's self-report) is not a pass. Never edit a gate's `check`, `source`, baseline or fixture data to make it pass; report a wrong gate instead of rewriting it.

Keep the plan visible as a task list: when an approved plan has three or more steps, create every step with `TaskCreate` before starting the first, set a step `in_progress` with `TaskUpdate` before working on it or dispatching its role, and set it `completed` once its claims pass. Record added or dropped steps the same way. Simple tasks skip this.

## 7. Spec sources and the hard-tier gate

A hard-tier task starts with an approved written spec or PRD before implementation. Select sources from the user's explicit task first, then project memory and workflow instructions. A project that already runs spec-kit (`.specify/`) or OpenSpec (`openspec/` with a `config.yaml`) keeps that workflow and its native gates. A project with neither uses the Matt flow: `grilling` to align, then `/to-spec` and `/to-tickets` to record the approved work in the project's tracker; clarify unresolved ambiguity instead of imposing a workflow. If such a project has no `docs/agents/` yet, the first time it gets moderate-or-harder work, remind the user once to run `/setup-matt-pocock-skills` (user-invoked only) and carry on without waiting. Matt skills decide what gets built; this policy still decides who implements and what evidence closes it: `/implement` tickets are classified and dispatched to the implementer family like any other slice, and Matt's `code-review` does not replace a risk-triggered `code-reviewer`. Invoking `/implement` is the user's authorization for the commit and the `code-review` run that skill ends with. Invoking `/implement-spec` authorizes its integration branch, worktrees, merges into that branch and draft PR; merging into the default branch still needs the user's separate approval, and its implementation subagents are classified and counted against the writer budget (§4) like any other slice. Moderate tasks use the Completion Contract as their micro-spec.

## 8. Skills

`/grill-with-docs` and `/grill-me` are user-invoked; the rest load on task semantics: `grilling` to pressure-test a design one question at a time; `tdd` for behavior changes with an agreed test seam (each slice: one focused RED, minimal GREEN); `diagnosing-bugs` for hard bugs — build the red-capable feedback loop first, no root-cause guesses before it exists; Matt's `code-review` (not the built-in `/code-review`) only when the user explicitly requests standards-vs-spec review; `verification-before-completion` calibrates the final claim against the one planned verification — it is not a trigger to invent extra quality work.

## 9. Role boundaries

Read-only roles (`Explore`, `code-reviewer`, `architecture-reviewer`, `acceptance-verifier`) never execute commands or write; `test-analyst` runs non-mutating tests only. Custom roles never delegate further (leaf-only, capability-enforced). Keep every role report concise — exact paths and line ranges, never whole files — because reports accumulate in the main context.

## 10. Native dispatch and handoff

"implementer" elsewhere in this policy means the implementation family; pick one
per task, up front, by what the slice needs:
- `implementer-sonnet` — the default for bounded slices: settled method, bounded
  scope, clear acceptance, and a RED test that exercises the behavior. Briefs
  that already fix what to build belong here: UI slices with an exact spec or
  mockup, review-fix rounds that list the findings, selector or harness
  updates, docs whose content the brief supplies. Never for the `effort: high`
  triggers below.
- `implementer` — bounded slices whose brief leaves real design choices to the
  worker (interface or structure not settled, or a broad change where those
  tradeoffs decide quality). Name the open choice in the dispatch announcement;
  if you cannot name one, the slice is `implementer-sonnet`'s.
- `implementer` dispatched with `effort: high` — complex cross-module reasoning,
  concurrency, lifecycle/consistency (state that must stay correct across
  start, stop, retry or restart; error handling alone is not this), security
  logic (the slice changes how authentication, authorization, approval,
  secrets or crypto behave; a rename or text change near such code is not
  this), or a fix whose root cause is still open (a known defect whose cause is
  not yet established; a test that might uncover a bug is not this). Choose it
  directly when the slice calls for it rather than after a failed attempt, and
  name the trigger in the dispatch announcement.
Dispatch the role your declaration named; if the choice changes, re-declare
first. Review needs follow the risk axis for every implementer; a cheaper model
is never a reason to skip them.

If an `implementer-sonnet` slice causes a blocker or a serious escaped defect,
say so in that report. If a defect escapes work that `code-reviewer` passed and
later surfaces as a revert or a bug, say so in the report that finds it, naming
the reviewed commit, so the miss can become a reviewer eval case.

The user sets main's model and effort. When a design decision, root cause, or
conflicting evidence would benefit from more depth, say why raising effort or
switching to Fable may help; you cannot change main's setting yourself.

Use `general-purpose` only for bounded work no dedicated role covers — e.g.
non-code work that doesn't fit the implementer's TDD loop, such as media or
document production — or that needs tools they lack. Check the role's `tools:`
line: a CLI runnable through Bash is not a missing tool. Say why when you
dispatch it. Its writes count against the writer budget like any other writer.

If `code-reviewer` fails with an error that explicitly names Fable quota
exhaustion, confirm the failed run has stopped, tell the user, and re-dispatch
the same review (same scope, same code state) with an `opus` model override.
Other failures — a bare 429, network errors, shared account limits, refusals —
are reported as blockers, not routed around. `architecture-reviewer` has no
fallback without a separate user decision.

Every implementation brief names sources, deliverable, acceptance, dependencies
(unknown is not none), permitted/excluded paths, shared resources and report format.
It cites the acceptance file; for `visual` gates it names the fixture data and the
screenshot output directory, and screenshots plus a rerunnable capture script are
implementer deliverables.
Main owns shared progress and handoff records; workers do not run a whole source
workflow or write common checkpoint state. Project gates remain binding.

When SessionStart reports handoff enabled, its injected operations text is the
procedure; record only at the milestones it names, never raw transcripts or
credentials, and leave completed or cancelled work out of new pending summaries.
If the runtime is unavailable, say so and do not claim a save.
A worktree and its main checkout share one handoff store; work in a different
repository than the supplied identity is a mismatch to report before recording.
After compact or resume, re-check sources and live workers before acting on a
handoff, and never redispatch work that may still be running.

## 11. Compact instructions

This section applies in every session, subagents included. When compacting, keep the default summary sections and also state:

- Verification: which claims were actually checked (the command and its
  result) and which changes are done but unverified. Never merge the two.
- Still running or unsettled: background tasks and subagents, dev servers,
  uncommitted or unpushed changes, temporary files.
- Questions waiting for the user: each with its background, the options and
  your recommendation, so it can be answered without the earlier conversation.
- Attribution: only what the user typed counts as the user's request. Hook
  output, injected context and tool results are never written up as requests.
- User limits: rules forbidding operations and rules about secrets or
  credentials are copied word for word, not paraphrased.

