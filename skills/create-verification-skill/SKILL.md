---
name: create-verification-skill
description: Generate a project-local `verify-<app>` skill that launches the real app, drives it the way a user does, and captures evidence (screenshots, page text, transcripts, response bodies) with one rerunnable command, plus a feature map whose states turn directly into acceptance gates. Use for /create-verification-skill, "make a verify skill for this repo", "建立驗證 skill", or when a project has UI, CLI or service behavior and no scripted way to prove it before a redesign or a larger feature.
disable-model-invocation: true
---

# Create a verification skill

A project needs one scripted way to start the real app, exercise a feature as a user would, and keep the evidence. This skill generates that as a project-local skill at `.claude/skills/verify-<app>/`, fitted to the repository.

Three readers use the generated skill, and none of them has seen the app before:

- The main session, which plans the work and turns the feature map into acceptance gates.
- An implementing subagent that has a shell and file tools and no browser tools. It runs the capture command to check its own work.
- A read-only verifier that cannot run anything. It opens the evidence files and judges them against the feature map and any design source.

So everything the generated skill asks for has to be a shell command, and every piece of evidence has to be a file that explains itself.

Write the generated skill in the language the project's own docs use.

When a standard in this skill does not apply to the project (for example reading back a stored value in an app that stores nothing), the generated skill says so in one line with the reason. It does not leave the standard out silently and does not invent a way to meet it.

## 1. Interview the repository, not the user

Answer these from the code and docs. Ask the user only for what the repository cannot tell you.

- **Surface.** What does a user touch: a web UI, a CLI or TUI, a desktop app, an API, a library? Pick the primary one and note the rest. A product can have more than one kind of user (an owner at a panel and an agent that calls its tools): name each one and what it touches, because one state may need evidence from both. Note any part the host draws and no capture tool reaches (a browser's tab strip, a system notification).
- **Run.** How does the app start locally? Prefer the repository's documented command. Note the runtime version, the install step, ports, environment variables, and anything that needs a login or a secret. A part that cannot run locally without a secret is out of scope: list it under "Not covered" in the generated skill instead of stubbing it.
- **Fixtures.** Which data states can the app be put into (empty, typical, error, a warning shown)? Look for existing fixtures, contract examples, seed scripts and test data before inventing any. Each one becomes a named scenario with a command that switches to it. Where a state comes from what someone did and not from data the app loads (who is connected, what was granted), the scenario is a named sequence of actions that the capture performs, and its names and values live in one scenario file.
- **Drive.** How can a script interact with the app? Use an existing harness first (end-to-end specs, a PTY helper, HTTP endpoints). Otherwise choose one: a headless browser for web and Electron, a PTY or tmux session for a CLI, plain HTTP for a service. Note when the drive needs a visible window (a browser extension's panel, a desktop app): the run then takes over the desktop while it lasts, and the generated skill says so under Prerequisites.
- **Observe.** What can be captured: screenshots, the visible text, the accessibility tree, console errors, terminal transcripts, response bodies, exit codes, stored data?
- **Variants.** Which display variants matter: viewport widths, light and dark theme, locales? Take them from existing design files or CSS breakpoints, not from a default list. A value the app does not control (the width of a panel that the host sizes) is not a variant: the manifest records the value each capture had. Where the app has its own setting for a variant (a language or theme menu), the capture selects it there, as a user would.
- **Design sources.** Are there mockups or baseline screenshots, which screens do they cover, and how were they made (one viewport, or the whole page)?
- **Isolate.** Can two instances run side by side, and at which level: in one checkout, only in separate worktrees, or one on the whole machine? Name what they would share (a port, a data directory, a build output, a machine-wide registration). Also check what switching a scenario overwrites. If it replaces files the user may already have there, such as untracked local data, launch moves them aside and cleanup puts them back, and launch refuses to start while an earlier backup is still present.

If the checkout does not build or start, fix that or report it exactly before you generate anything. A skill written against a broken base teaches wrong steps.

If driving the app needs a new dependency (for example a headless browser library), stop and ask the user before you add it. Name the package, where it would be installed, what cannot be done without it, and everything it downloads or writes outside the repository (a browser binary in a user cache, for example). One approval then covers the package and those downloads. A missing piece can show only when the prerequisite check first runs (the package is installed and its browser binary is not). Stop and ask at that point in the same way.

A setup step that changes the machine outside the repository (trusting a certificate, registering a host program, granting a system permission) belongs to the user. The generated skill names its command under Prerequisites and doctor checks its result; no script performs it.

## 2. Generate the skill

Write `.claude/skills/verify-<app>/SKILL.md` with frontmatter (`name: verify-<app>`, and a `description` that names the app, the surface and when to use it) and the sections below. Each section holds what the interview found. Leave no placeholder.

- **Prerequisites.** The runtime version, the install command, and any one-time download. The helper script checks the runtime version and the installed dependencies before it touches the app and names what is missing, because doctor cannot run without them. Doctor reports a missing one-time download.
- **Launch.** The exact command that starts the app for verification and the sign that it is usable, not only that a process answers. It takes a scenario name where a scenario is data the app loads; where the capture builds scenarios by driving the app, launch takes none and the skill says so. It starts without leftovers from an earlier run: no old process, no data from another scenario. It refuses to start when an instance already runs where the interview found they cannot coexist, and it fails when its port is taken instead of moving to another port.
- **Scenarios.** A table of the named data states: name, what the user would see, and the command that switches to it, or "built by capture" with the actions that build it. Switching is repeatable and leaves no trace in tracked files.
- **Doctor.** One read-only command that answers "is this instance worth driving?": the process is up, it is the build under test, the port is ours, the expected scenario is loaded, and the state definitions agree with the feature map (section 3). To prove the instance is ours, launch writes a random token where the app serves or prints it, and doctor reads the token back through the app; an app with no such place carries the token where only this instance can (its command line, its profile path). To prove the scenario, doctor compares what the app serves with the scenario's source files; for a scenario the capture builds, doctor checks that what it builds on is in place (the fixture pages serve the names the scenario file holds). When the run uses several built artefacts (an extension, a server, a host program registered for the whole machine that may point at another checkout), doctor checks each one against this checkout's build, comparing the files this build produces, so that a stale file left in an output directory is not a difference. Run doctor before the first capture and after any failure.
- **Capture.** One command that produces evidence for a list of state ids, for example `<capture command> --states <id,id> --out <dir>`, and for all capturable states when no list is given. It reads the states from the state definitions file (section 3), switches scenario per state by itself, and gives each state a fresh session (a new browser context, a new shell), so that one state cannot leak into the next. Where the app's state belongs to the whole instance (an extension's storage), a fresh session is a reset to the launch state by the repository's own means. A step may start a call and not wait for it, because some states exist only while a call is open (a prompt that stays until the owner answers an agent's request). It uses real selectors, routes and commands from this repository and prefers stable handles (roles, accessible names, routes) over positions.
- **Evidence.** What a capture writes and where, by the rules in "Evidence files" below.
- **Cleanup.** How to stop what the run started and restore the data that launch moved aside. Stop only the process this run started, identified by the recorded pid and the launch token, never by process name. Evidence survives cleanup.
- **Not covered.** The parts that this skill cannot drive and the reason.
- **Using the map for acceptance.** Section 4 of this skill, written out for this project. It is the last section of the generated skill.

Put helper scripts in `.claude/skills/verify-<app>/scripts/` and show each one's invocation in the skill body. Add the evidence directory to `.gitignore` unless the project already commits such files. Where an acceptance record stamps a verdict with the state of the code tree, evidence written to a tracked or unignored path changes that state the moment the capture runs, so such a project keeps gate evidence in an ignored directory and commits a copy afterwards if it wants one.

### When a state cannot be reached

Each state has reach conditions: the few things that must be present (or absent) before the evidence is taken. Their job is to tell this state from any other, not to judge whether the app is right. Judging is the verifier's work, on the evidence.

- Keep reach conditions structural: a named region, a heading, a route, a control in a given state. A broken layout or a wrong number should still be captured, so that the verifier can see it. Where fixed wording differs by locale, a condition names the message key and the capture resolves the text from the locale files at run time.
- Do not hard-code scenario values. A scenario value is anything shown to the user that is copied or computed from scenario data: a count, a price, a date, a displayed name. The capture reads each one from the scenario's files at run time, looks for it in the place the definition names, and records it in the manifest under `expected` with where it was read from and whether it was `found`. A value that is not found does not fail the state: the capture prints a warning and the verifier judges it. A rebuilt scenario then changes the expectation with it, and a capture does not fail on an app that still works.
- An identifier that chooses what to open (a route parameter, the record a state is about) stays in the definitions. When a state exists only because the scenario has a certain property (a record with no history, exactly one week of data), the definition declares that property as a precondition. The capture checks it against the scenario's files before it drives the app, and fails the state with "the scenario no longer provides this state" when it does not hold. Without this check a rebuilt scenario quietly turns the state into a different one. Declare a precondition only where other plausible data in the same scenario would show a different state; a state that any version of its scenario shows needs none. Write it as tightly as the row's wording: "exactly one alert" when the row describes one alert, a lower bound only when more would not change what the row says.
- A state may pick its record by a property instead of a fixed identifier ("the first indicator that has a newer daily value"). The definition names the rule, the capture resolves it from the scenario's files and records the chosen record in the manifest, and the table describes the rule in words.
- `expected` holds values as the scenario stores or formats them. The capture does not rebuild text that the app composes from several values (a "from → to" line, a difference), because that would copy the app's logic into the check. The verifier checks composed text on the text file, and the feature's `Gotchas` says which rows have it.
- When a condition is not met, the capture marks that state failed, exits non-zero and names the state and the missing condition. It writes no evidence file under the state's id: a before file is taken ahead of the action, so the capture holds a state's files in memory or a temporary directory and writes them under the id only once the state is reached. It may keep a debug file whose name shows the failure (`<id>.FAILED.png`). It never captures a different state in its place, because a wrong screenshot that looks plausible is worse than a missing one.
- An unknown id, or an id marked not capturable, fails the command before anything is captured when the id was named in `--states`. This check reads only the definitions, so it comes ahead of the dependency check: a mistyped id is reported even on a machine that cannot capture. A run of all states skips the states marked not capturable and lists them in the manifest with the reason.

### Evidence files

Every file name starts with the state id. A capture of one state writes:

| File | Content | When |
|---|---|---|
| `<id>.png` | What the user sees without scrolling. When the subject of the state is below the first screen, scroll to it first | every UI state |
| `<id>.full.png` | The whole page or surface. When the content scrolls inside a container and not the document, extend that container for the shot; otherwise the image repeats the first screen | every UI state |
| `<id>.txt` | UI: the visible text of the page. CLI: the command, stdout, stderr and the exit code. Tool call: the request, whether it was an error, the answer and the server's stderr | every state |
| `<id>.before.png` or `<id>.before.txt` | The same view before the action | states reached by an interaction |
| `<id>.json` | The request and the response (status, headers that matter, body) | service and tool-call states, and UI states that wait on or follow a caller's request, so that both users' evidence carries one id |

The text file is there because a read-only verifier checks an exact figure or label much more reliably in text than in a picture.

A whole-page screenshot goes wrong with fixed or sticky elements: a headless browser draws a fixed bar at its first-screen position, in the middle of the image and on top of content. Handle it in the capture (for example, size the viewport to the document height for that shot) and check the result in section 5. Say in the feature map which of the two screenshots is compared with a design source; it is the one made the same way the design source was made.

Each run writes one `manifest.json` beside the files. It records the run (start and end time, base URL or command, git commit, whether the tree had uncommitted changes, tool versions) and one entry per state: the id, feature, scenario, variant values, the start URL or command (for a surface without an address, a description of it), the steps performed, the files written, the design source or null, values read from scenario data as `expected`, the capture time read from the clock in UTC, console errors, uncaught exceptions and failed requests seen on every surface the state shows (a panel or popup the drive tool does not list as a page needs its own attachment), and a status of `captured`, `failed` (with the error) or `not-capturable` (with the reason). The manifest is what lets a verifier who cannot run anything confirm what each file shows.

Console errors and failed requests are recorded and do not fail a state on their own. The feature map's proof rules say which of them count as a defect, and the verifier applies those rules.

State the proof standards in the generated skill: exercise the real user path, not internal setters or test-only endpoints, and reach a state through the app's own navigation when the state is about getting there; for an interaction, keep the view before the action and the resulting view; for a change to stored data, read the value back from where it lands; an exit code of zero means every state was reached, not that any screen is correct.

## 3. Seed the feature map and the state definitions

Create `.claude/skills/verify-<app>/features/README.md` and one file per user-facing feature. Start with the three to five that matter most, found from routes, commands, menus or docs. Read `references/feature-map.md` for the file contract and an example before you write the first one.

Each feature file answers from the user's point of view: what the feature is, how a user reaches it, what the user must be able to tell or do there, and which states exist. The **States** table is the part the rest of the workflow depends on. One row is one thing that can be captured and judged: a screen in one scenario at one viewport and theme, or one command with one input.

A state is described in two places with different readers, and they must not drift:

- The **state definitions file** in `scripts/` is what the capture command executes: the id, feature, scenario, variant values, the entry point, the steps, the reach conditions, the design source, and whether it is capturable.
- The **States table** in the feature file is what people and the verifier read: the same id, scenario, variant values, design source and capturable flag, plus the observable result in words.

Doctor and capture compare the shared fields of the two and fail on any difference. A state is added, changed or retired in both in the same change.

Do not list the full matrix of variants. List the combinations that differ in what the user sees, by the rules in `references/feature-map.md` ("Which combinations to list"). Write the rule you applied into the README's Variants section, so that the next person adds rows the same way.

Write the observable result so that a person who has not seen the app can check it on the evidence alone: "the banner names the indicator, shows its latest value and its level label" can be checked, "the banner works" cannot. Name a scenario value by what it is ("its latest value") and do not quote the figure. The figure is in the manifest's `expected`, which is the authority for values: the verifier reads the wording from the table and the numbers from the manifest, and the table does not go stale when a scenario is rebuilt. Without figures a row still has to be specific: name the parts that must be present, how many there are relative to the scenario ("one row per indicator in the scenario"), the exact wording of fixed labels and messages, and what an action changes. A row for another width or theme says what the layout must do there ("the overview and the list sit side by side"), not "same as above".

If a scenario needed for a state does not exist, or the part that shows the state is drawn by the host where no capture tool reaches, record the gap under that feature's `Gotchas` and mark the row not capturable. Do not weaken the state to make it capturable.

## 4. How states become acceptance gates

This mapping is what lets a session know at dispatch time how the work will be accepted.

- A state row with a design source becomes a `visual` gate: `source` is the mockup path, `check` names the state id and its scenario, and the evidence is the screenshot the feature map names for comparison.
- A state row without a design source becomes a `live` gate: `source` is the feature file, `check` is the row's observable result, and the evidence is the state's files and its manifest entry.
- The gate id is the state id, so that a gate, its evidence files and its row in the map share one name.
- A task takes only the rows it touches. The brief to an implementing subagent lists those state ids and the full command sequence (launch, doctor, capture, cleanup), and the subagent runs it before it reports.
- Each statement under "What the user must be able to tell or do" is named in the `check` of at least one gate, with the file it is judged on (a statement about the first screen is judged on `<id>.png`, not on the whole page).
- A state the change adds is defined before dispatch. The main session decides its id, the accessible names and the fixed wording its reach conditions rely on, writes them into the state definitions, and puts them into the brief as names the implementation must use.
- A touched state that is not capturable gets an entry of its own in the acceptance record, naming what covers it instead (a unit test, or nothing), and appears in the acceptance report.
- Evidence is captured once, on the content that will be judged. The manifest records the commit and whether the tree was clean, so a capture the subagent made on that same content is the gate's evidence and is not repeated. A capture on uncommitted work identifies no content by its commit: where the session's acceptance tooling can print a digest of the tree, the brief asks the subagent to read it before capturing and to report it.
- When a change adds, removes or alters a state, the same change updates the feature file and the state definitions. A map that lags the app turns every later verdict into a guess.
- A design source that no longer matches the app for reasons outside the task (it predates later features, or its data changed) is reported to the user with the list of differences. It is not used as a gate until the user has it redrawn or records the accepted differences beside it. Until then its rows are `live` gates judged on their observable result; the table keeps the path in the design source column, and the feature's `Gotchas` says that it is suspended and why.

A project that keeps no acceptance file still uses the map the same way: the state ids are the checklist.

### When the task is a redesign

A redesign changes the statements and the design source before it changes the code, in this order:

1. Capture the feature's current states and keep them outside the evidence directory of later runs. They are the "before" of the acceptance report.
2. Settle the statements with the user and write them into the feature file.
3. Build the variants as temporary states, so that the same capture command shoots them. Check every variant's captures against every statement and show the misses beside the variants. A miss found here costs one sentence; found after implementation it costs a ruling and a second round of judging.
4. The captures of the variant the user picks become the design source, saved in the repository with a note on width, theme, scenario and whether a pixel-exact match is required. When the pick realises a statement in another form than its wording (two rows instead of "old → new"), reword the statement now. Remove the temporary states.
5. A design source made from the prototype the implementation is built from will match it almost by construction. The comparison still catches drift, but the statements and the observable results are what the verifier's judgement is for; say so in the brief to the verifier.

## 5. Prove the generated skill before you hand it over

Run its own instructions from start to finish in a clean state: launch with one scenario, run doctor, capture every capturable state, clean up. A state definition can be wrong in ways only a run shows (a label that does not match, a control that is not where the map says), so one feature is not enough for the run. Then confirm:

- Launch followed by doctor passes three times in a row, with cleanup between. A start-up race (a first request slower than a timeout) shows only on some runs.
- Every evidence file exists after cleanup and is listed in the manifest, and nothing the run started is still running.
- Every `expected` value in the manifest was found, or the report explains each one that was not.
- Opening the files shows the state each id names. For one feature, open every screenshot of every state (the before file of an interaction included) and the text file of one state. For every other feature, open the screenshots of at least one state. Look at the files; do not trust the exit code.
- Every row's observable result agrees with its evidence. Exact wording and arrangement are often known only once the first capture exists, so correct each row that described the app wrongly. Where the app falls short of a statement under "What the user must be able to tell or do", keep the statement, write the row as the app behaves and put the gap into the report as a finding about the product.
- In the whole-page screenshots, fixed elements do not cover content.
- Where a design source exists for a captured state, put the two side by side and say in the report what matches and what differs. This calibrates the capture settings (viewport, theme, scenario); it is not acceptance of the app. Do it yourself and do not send it to a verifier role. Differences that come from the app having changed since the design source was made go into the report as a separate list.
- An id that does not exist, and an id marked not capturable, each make the capture command fail when named.
- A failure path that cannot be exercised on this machine (a wrong runtime version, for example) is listed in the report as not tested, with the reason. It does not block the hand-over.

Fix what fails, and run cleanup after each failed attempt so that no process or port is left behind. After the last fix, run the whole sequence once more on the finished files: only that run is the proof, and only its evidence directory goes into the report or into an acceptance record. A generated skill that was never run is a draft.

## 6. Report

End with: the skill path, the features mapped and the number of states in each, the states that are not capturable and why, what is listed under "Not covered", any dependency added and what it downloaded outside the repository, the setup steps left to the user, whether a run takes over the desktop and how many instances a machine can hold, the findings about the product from the proof run, the evidence directory of the proof run, and the design sources that no longer match the app. Say that the feature map and the state definitions need updating whenever the UI changes.

## Attribution

The structure (interview, launch, doctor, drive, evidence, cleanup, feature map, prove before handing over) follows `create-verification-skill` from pstack by Lauren Tan (MIT). See `NOTICE.md`.
