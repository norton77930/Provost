[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Passed = 0
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$helperPath = Join-Path $repositoryRoot 'docs\governance\reference\Foreman-Manifest.ps1'

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw ('Foreman helper is missing: ' + $helperPath)
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'This reference test requires git.'
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 32), [System.Text.UTF8Encoding]::new($false))
}

function Invoke-Helper {
    param([hashtable]$Parameters)
    & $helperPath @Parameters
}

function Write-Pass {
    param([Parameter(Mandatory = $true)][string]$Label)
    $script:Passed++
    Write-Output ('PASS: ' + $Label)
}

function Assert-ThrowsCode {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Label
    )
    try {
        & $Action | Out-Null
    }
    catch {
        if ($_.Exception.Message -notmatch ('\[' + [regex]::Escape($Code) + '\]')) { throw }
        Write-Pass -Label $Label
        return
    }
    throw ($Label + ' did not throw [' + $Code + '].')
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('provost-completion-gate-' + [guid]::NewGuid().ToString('N'))
$resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
$resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())

if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create test state outside the system temporary directory.'
}

try {
    [System.IO.Directory]::CreateDirectory((Join-Path $resolvedTemporaryRoot 'src')) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $resolvedTemporaryRoot 'src\sample.txt'), 'baseline', [System.Text.UTF8Encoding]::new($false))
    & git -C $resolvedTemporaryRoot init -q
    if ($LASTEXITCODE -ne 0) { throw 'git init failed.' }
    & git -C $resolvedTemporaryRoot config user.email 'provost-test@example.invalid'
    & git -C $resolvedTemporaryRoot config user.name 'Provost Test'
    & git -C $resolvedTemporaryRoot add -- .
    & git -C $resolvedTemporaryRoot -c commit.gpgsign=false commit -qm 'baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the temporary Git baseline.' }

    $foremanRoot = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot 'manifests\none\sample-change'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    $manifestPath = Join-Path $manifestDirectory 'r001.json'
    $lockPath = Join-Path $foremanRoot 'active-run.lock'
    $sessionId = 'provost-completion-gate'
    [System.IO.File]::WriteAllText((Join-Path $plans 'sample-plan.md'), '# Sample plan', [System.Text.UTF8Encoding]::new($false))

    $draft = [ordered]@{
        schema = 'provost-foreman-manifest/v1'
        revision = [ordered]@{ id = 'r001'; number = 1; supersedes = $null }
        approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
        native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/sample-plan.md' }
        spec = [ordered]@{ system = 'none'; reference = $null }
        change = [ordered]@{ id = 'sample-change'; title = 'Sample change' }
        role_catalog = 'provost-foreman/v1'
        tasks = @(
            [ordered]@{
                id = 'T01'
                title = 'Create sample'
                agent_key = 'foreman-implementer'
                kind = 'writer'
                depends_on = @()
                write_set = @('src/sample.txt')
                must_not_modify = @()
                acceptance = @([ordered]@{ id = 'V01'; command = 'Get-Content src/sample.txt'; expect = 'exit 0' })
            },
            [ordered]@{
                id = 'T02'
                title = 'Review sample'
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
    Write-JsonFile -Path $draftPath -Value $draft

    Invoke-Helper -Parameters @{
        Action = 'Initialize'
        DraftPath = $draftPath
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    } | Out-Null

    Assert-ThrowsCode -Code 'VERIFY' -Label 'Complete PASS rejected before any task passed' -Action {
        Invoke-Helper -Parameters @{
            Action = 'Complete'
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            Outcome = 'PASS'
        }
    }

    Invoke-Helper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T01'
    } | Out-Null
    Invoke-Helper -Parameters @{
        Action = 'FinishTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T01'
        Outcome = 'PASS'
        ChangedFilesJson = '[]'
        VerificationSummary = 'Writer finished with no file changes.'
    } | Out-Null

    Assert-ThrowsCode -Code 'VERIFY' -Label 'Complete PASS rejected while the verifier task is unfinished' -Action {
        Invoke-Helper -Parameters @{
            Action = 'Complete'
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            Outcome = 'PASS'
        }
    }

    Invoke-Helper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T02'
    } | Out-Null
    Invoke-Helper -Parameters @{
        Action = 'FinishTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T02'
        Outcome = 'PASS'
        ChangedFilesJson = '[]'
        VerificationSummary = 'No material findings.'
    } | Out-Null

    $completed = Invoke-Helper -Parameters @{
        Action = 'Complete'
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        Outcome = 'PASS'
    }
    if ($completed.status -ne 'PASS') {
        throw ('Complete returned ' + $completed.status + ' instead of PASS.')
    }
    if (Test-Path -LiteralPath $lockPath) {
        throw 'Complete PASS left active-run.lock in place.'
    }
    Write-Pass -Label 'Complete PASS succeeded after every task was PASS'

    Write-Output ('Completion-gate checks passed: ' + $script:Passed)
}
finally {
    if (Test-Path -LiteralPath $resolvedTemporaryRoot) {
        if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to clean up a path outside the system temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
}
