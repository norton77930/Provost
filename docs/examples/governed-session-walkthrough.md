# Governed session walkthrough

This session-level walkthrough opens a governed Claude Code session and walks a
small change through it: `Initialize`, a `Write` outside the running task's
`write_set` (denied by the hook), an allowed `Write`, and `Complete PASS`.

It is the document that answers "what happens when I use it". The other
examples each run one isolated check; this one uses
[`Start-GovernedSession.ps1`](../governance/reference/Start-GovernedSession.ps1).
The isolated write-gate check, including the deny JSON shape, is the
[write-scope demo](governed-write-scope-demo.md).

## Prerequisites

- Windows.
- Windows PowerShell 5.1 (`powershell.exe`).
- `git` on `PATH`.
- The `claude` CLI on `PATH` (this run used 2.1.258).
- An execution policy that allows local scripts, for example
  `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`. A clean Windows client
  defaults to `Restricted`, which blocks the launcher.
- A checkout of this repository, for the launcher and the helper. The governed
  tier needs nothing else: the roles are internal to the helper and the hooks
  come from the checkout, so the collaborator plugin is not required here.

The governed workspace is a throwaway Git repository, not the Provost checkout.
Paths, run ids, session ids, and hashes differ on each machine; the shape does
not.

## Run it

### Prepare a throwaway workspace

From a PowerShell prompt, with `$provost` pointing at the Provost checkout:

```powershell
$provost = 'D:\OSS\Provost'
$workspace = Join-Path ([System.IO.Path]::GetTempPath()) 'provost-governed-walkthrough-text'

if (Test-Path -LiteralPath $workspace) { Remove-Item -LiteralPath $workspace -Recurse -Force }
[System.IO.Directory]::CreateDirectory((Join-Path $workspace 'src')) | Out-Null
[System.IO.File]::WriteAllText(
    (Join-Path $workspace 'src\hello.txt'),
    "hello`n",
    [System.Text.UTF8Encoding]::new($false))
& git -C $workspace init -q
& git -C $workspace config user.email 'provost-walkthrough@example.invalid'
& git -C $workspace config user.name 'Provost Walkthrough'
& git -C $workspace add -- .
& git -C $workspace -c commit.gpgsign=false commit -qm 'baseline'
Write-Output "PROVOST=$provost"
Write-Output "WORKSPACE=$workspace"
Write-Output "HEAD=$(git -C $workspace rev-parse --short HEAD)"
```

This run printed:

```text
PROVOST=D:\OSS\Provost
WORKSPACE=C:\Users\NORTON.DENG\AppData\Local\Temp\provost-governed-walkthrough-text
HEAD=5829136
```

`Set-Content -Encoding utf8` on Windows PowerShell 5.1 writes a BOM. Use
`[System.IO.File]::WriteAllText` with `UTF8Encoding($false)`, as above.

Write the native Plan and an approved draft. The helper consumes the draft at
`Initialize` and refuses a dirty overlap if `src/hello.txt` is already
modified, so the file must match the Git baseline until the writer task is
running.

```powershell
$foremanRoot = Join-Path $workspace '.claude\provost\foreman'
$plans = Join-Path $foremanRoot 'plans'
$manifestDirectory = Join-Path $foremanRoot 'manifests\none\hello-change'
[System.IO.Directory]::CreateDirectory($plans) | Out-Null
[System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null

$plan = "# Hello change`n`nReplace the contents of src/hello.txt with a one-line greeting.`nVerifier reads the file and records that it exists.`n"
[System.IO.File]::WriteAllText(
    (Join-Path $plans 'hello-plan.md'),
    $plan,
    [System.Text.UTF8Encoding]::new($false))

$draft = [ordered]@{
    schema = 'provost-foreman-manifest/v1'
    revision = [ordered]@{ id = 'r001'; number = 1; supersedes = $null }
    approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
    native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/hello-plan.md' }
    spec = [ordered]@{ system = 'none'; reference = $null }
    change = [ordered]@{ id = 'hello-change'; title = 'Hello change' }
    role_catalog = 'provost-foreman/v1'
    tasks = @(
        [ordered]@{
            id = 'T01'
            title = 'Write greeting'
            agent_key = 'foreman-implementer'
            kind = 'writer'
            depends_on = @()
            write_set = @('src/hello.txt')
            must_not_modify = @()
            acceptance = @([ordered]@{
                id = 'V01'
                command = 'Get-Content src/hello.txt'
                expect = 'exit 0'
            })
        },
        [ordered]@{
            id = 'T02'
            title = 'Review greeting'
            agent_key = 'foreman-verifier'
            kind = 'review'
            depends_on = @('T01')
            write_set = @()
            must_not_modify = @()
            acceptance = @()
        }
    )
    final_reviews = [ordered]@{
        code = [ordered]@{ required = $true; agent_key = 'foreman-verifier' }
        architecture = [ordered]@{ required = $false; agent_key = 'foreman-architecture-verifier' }
    }
}
[System.IO.File]::WriteAllText(
    (Join-Path $manifestDirectory 'r001.draft.json'),
    ($draft | ConvertTo-Json -Depth 32),
    [System.Text.UTF8Encoding]::new($false))
```

Windows PowerShell 5.1's `ConvertTo-Json` inserts extra spaces. `Initialize`
accepted that draft as written.

### Open the session

From the Provost checkout:

```powershell
Set-Location $provost
.\docs\governance\reference\Start-GovernedSession.ps1 -WorkspaceRoot $workspace
```

The launcher printed this, then started `claude`:

```text
Governed workspace: C:\Users\NORTON.DENG\AppData\Local\Temp\provost-governed-walkthrough-text
Tier 2 hooks registered for this session only. Initialize refuses a run whose session shows no marker from the SessionStart hook.
```

The helper lives in the checkout, not in the throwaway workspace. Inside the
session, invoke it by its absolute path: replace `<PROVOST>` below with your own
checkout. A variable will not do: PowerShell variables are never inherited by a
child process, so `$provost` from the prompt above does not exist in there.
Environment variables are. `PROVOST_FOREMAN_WORKSPACE_ROOT` is set by the
launcher on the process `claude` inherits from; `CLAUDE_CODE_SESSION_ID` is
supplied by Claude Code to every tool call. That difference is what the liveness
proof rests on. Use `$env:CLAUDE_CODE_SESSION_ID` as
`-SessionId` and `$env:PROVOST_FOREMAN_WORKSPACE_ROOT` as `-WorkspaceRoot` on
every helper call. File writes must go through the `Write` / `Edit` /
`NotebookEdit` tools; the write gate does not see shell redirection.

### Initialize and start the writer

```powershell
& "<PROVOST>\docs\governance\reference\Foreman-Manifest.ps1" -Action Initialize -DraftPath ".claude\provost\foreman\manifests\none\hello-change\r001.draft.json" -ManifestPath ".claude\provost\foreman\manifests\none\hello-change\r001.json" -WorkspaceRoot $env:PROVOST_FOREMAN_WORKSPACE_ROOT -SessionId $env:CLAUDE_CODE_SESSION_ID
```

```text
status    : INITIALIZED
manifest  : C:\Users\NORTON.DENG\AppData\Local\Temp\provost-governed-walkthrough-text\.claude\provost\foreman\manifests
            \none\hello-change\r001.json
ledger    : C:\Users\NORTON.DENG\AppData\Local\Temp\provost-governed-walkthrough-text\.claude\provost\foreman\runs\none
            \hello-change\r001\run-20260902-125210-064e2aa8.jsonl
assurance : FULL
run_id    : run-20260902-125210-064e2aa8
```

```powershell
& "<PROVOST>\docs\governance\reference\Foreman-Manifest.ps1" -Action StartTask -TaskId T01 -ManifestPath ".claude\provost\foreman\manifests\none\hello-change\r001.json" -WorkspaceRoot $env:PROVOST_FOREMAN_WORKSPACE_ROOT -SessionId $env:CLAUDE_CODE_SESSION_ID
```

```text
status    : RUNNING
task_id   : T01
agent_key : foreman-implementer
model     : sonnet
effort    : high
```

### Refusal

With T01 running, use the Write tool (not the shell) on `src/outside.txt`. That
path is not in T01's `write_set`. The tool result was:

```text
Foreman write-gate: "src/outside.txt" is not in any RUNNING task write_set (running tasks: T01). Call the helper StartTask for the task that owns this path, or cut a new approved manifest revision. Do not retry blindly.
```

`src/outside.txt` was not created. Do not retry that `Write`, and do not create
the file through the shell.

### Allowed write and completion

Use the Write tool on `src/hello.txt`, which is in T01's `write_set`. The tool
result was:

```text
The file C:\Users\NORTON.DENG\AppData\Local\Temp\provost-governed-walkthrough-text\src\hello.txt has been updated successfully. (file state is current in your context — no need to Read it back)
```

```powershell
& "<PROVOST>\docs\governance\reference\Foreman-Manifest.ps1" -Action FinishTask -TaskId T01 -Outcome PASS -ChangedFilesJson '["src/hello.txt"]' -VerificationSummary 'Wrote the greeting.' -ManifestPath ".claude\provost\foreman\manifests\none\hello-change\r001.json" -WorkspaceRoot $env:PROVOST_FOREMAN_WORKSPACE_ROOT -SessionId $env:CLAUDE_CODE_SESSION_ID
```

```text
status task_id changed_files
------ ------- -------------
PASS   T01     {src/hello.txt}
```

```powershell
& "<PROVOST>\docs\governance\reference\Foreman-Manifest.ps1" -Action StartTask -TaskId T02 -ManifestPath ".claude\provost\foreman\manifests\none\hello-change\r001.json" -WorkspaceRoot $env:PROVOST_FOREMAN_WORKSPACE_ROOT -SessionId $env:CLAUDE_CODE_SESSION_ID
```

```text
status    : RUNNING
task_id   : T02
agent_key : foreman-verifier
model     : opus
effort    : high
```

```powershell
& "<PROVOST>\docs\governance\reference\Foreman-Manifest.ps1" -Action FinishTask -TaskId T02 -Outcome PASS -ChangedFilesJson '[]' -VerificationSummary 'No material findings.' -ManifestPath ".claude\provost\foreman\manifests\none\hello-change\r001.json" -WorkspaceRoot $env:PROVOST_FOREMAN_WORKSPACE_ROOT -SessionId $env:CLAUDE_CODE_SESSION_ID
```

```text
status task_id changed_files
------ ------- -------------
PASS   T02     {}
```

```powershell
& "<PROVOST>\docs\governance\reference\Foreman-Manifest.ps1" -Action Complete -Outcome PASS -WorkspaceRoot $env:PROVOST_FOREMAN_WORKSPACE_ROOT -SessionId $env:CLAUDE_CODE_SESSION_ID
```

```text
status
------
PASS
```

The launcher restores the caller's environment when `claude` exits, so the
operator's shell is not left marked governed.

After the session, from the same PowerShell prompt:

```powershell
Get-Content -LiteralPath (Join-Path $workspace 'src\hello.txt') -Raw
Test-Path -LiteralPath (Join-Path $workspace 'src\outside.txt')
Test-Path -LiteralPath (Join-Path $workspace '.claude\provost\foreman\active-run.lock')
& git -C $workspace status --short
```

```text
hello from a governed run
False
False
 M src/hello.txt
?? .claude/
```

## What is enforced

The launcher sets `PROVOST_SESSION_PROFILE` and `PROVOST_FOREMAN_WORKSPACE_ROOT`
and registers the three Tier 2 hooks for that session only, through
`claude --settings`. The `SessionStart` hook writes a liveness marker naming
the session. `Initialize` refuses without that marker, then records
`enforcement.mode = hooks` on the lock and in the ledger.

While T01 is `RUNNING`, `PreToolUse-WriteGate.ps1` compares `Write` /
`Edit` / `NotebookEdit` targets with the running task's literal `write_set`
and returns `permissionDecision: deny` for a mismatch. That is a
host-consumed tool decision, produced before the file-editing tool executes.
The model is not being asked to judge its own compliance.

`Complete PASS` walks every task in the approved manifest, requires each to be
`PASS`, and deletes `active-run.lock`. `StartTask`, `FinishTask`, and
`Complete` of an enforced lock require the same Claude Code session that
opened it.

## Limitations

This is a **Windows governed session**, not a packaged runtime and not an
operating-system sandbox.

- Tier 2 is Windows-only. The collaborator plugin still injects its
  `SessionStart` policy into the same session; hooks supplied through
  `--settings` are added to a project's own, not swapped for them.
- The enforcement record is open-time truth: it says the hooks were live when
  the run began, not that every write in the run was policed. Shell writes
  inside the workspace are not covered by the write gate.
- The helper is not on `PATH` and is not in the throwaway workspace. This run
  invoked it by absolute path from the Provost checkout.
- `powershell.exe -File` cannot pass `-ClaudeArguments`. The launch command
  above is for a PowerShell prompt.
- The liveness marker is a plain file in the workspace, not a credential.
- The shipped launcher does not create a fresh agent context per task.
- Acceptance entries are not separately bound as evidence records.
- This run used Claude Code 2.1.258 on one Windows machine. It has not been
  independently verified on a fresh VM.
- Two refusals described under "What is enforced" were not exercised by this
  run: an `Initialize` with no liveness marker, and advancing a lock from a
  different session. They are covered by
  [`Test-SessionLiveness.ps1`](../../tests/governance/Test-SessionLiveness.ps1)
  and
  [`Test-ContinuationEnforcement.ps1`](../../tests/governance/Test-ContinuationEnforcement.ps1).- It does not cover the ref guard, path custody, failure diagnosis, or
  continuation enforcement.
- The tool results above were recorded from a print-mode session of the same
  launcher. The reader path is the interactive session, without `-p`.
