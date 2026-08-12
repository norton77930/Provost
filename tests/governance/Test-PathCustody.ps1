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

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-GitWorkspace {
    param([Parameter(Mandatory = $true)][string]$Root)
    [System.IO.Directory]::CreateDirectory((Join-Path $Root 'src')) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Root 'src\shared.txt'), 'baseline shared content', [System.Text.UTF8Encoding]::new($false))
    & git -C $Root init -q
    if ($LASTEXITCODE -ne 0) { throw 'git init failed.' }
    & git -C $Root config user.email 'provost-test@example.invalid'
    & git -C $Root config user.name 'Provost Test'
    & git -C $Root add -- .
    & git -C $Root -c commit.gpgsign=false commit -qm 'baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the temporary Git baseline.' }
}

function New-CustodyDraft {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ChangeId,
        [bool]$LinkWriters = $true
    )
    $foremanRoot = Join-Path $Root '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot ('manifests\none\' + $ChangeId)
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $plans 'plan.md'), '# Shared path custody plan', [System.Text.UTF8Encoding]::new($false))
    $secondDependencies = @()
    if ($LinkWriters) { $secondDependencies = @('T01') }
    $draft = [ordered]@{
        schema = 'provost-foreman-manifest/v2'
        revision = [ordered]@{ id = 'r001'; number = 1; supersedes = $null }
        approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
        native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/plan.md' }
        spec = [ordered]@{ system = 'none'; reference = $null }
        change = [ordered]@{ id = $ChangeId; title = 'Shared path custody' }
        role_catalog = 'provost-foreman/v1'
        continuation = $null
        tasks = @(
            [ordered]@{
                id = 'T01'
                title = 'First writer'
                agent_key = 'foreman-implementer'
                kind = 'writer'
                depends_on = @()
                write_set = @('src/shared.txt')
                must_not_modify = @()
                acceptance = @()
            },
            [ordered]@{
                id = 'T02'
                title = 'Second writer'
                agent_key = 'foreman-implementer'
                kind = 'writer'
                depends_on = $secondDependencies
                write_set = @('src/shared.txt')
                must_not_modify = @()
                acceptance = @()
            },
            [ordered]@{
                id = 'T03'
                title = 'Verifier'
                agent_key = 'foreman-verifier'
                kind = 'review'
                depends_on = @('T01', 'T02')
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
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    Write-JsonFile -Path $draftPath -Value $draft
    return [pscustomobject]@{
        DraftPath = $draftPath
        ManifestPath = (Join-Path $manifestDirectory 'r001.json')
        LockPath = (Join-Path $foremanRoot 'active-run.lock')
        SharedPath = (Join-Path $Root 'src\shared.txt')
    }
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('provost-path-custody-' + [guid]::NewGuid().ToString('N'))
$resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
$resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())

if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create test state outside the system temporary directory.'
}

try {
    $unlinkedRoot = Join-Path $resolvedTemporaryRoot 'unlinked'
    New-GitWorkspace -Root $unlinkedRoot
    $unlinked = New-CustodyDraft -Root $unlinkedRoot -ChangeId 'unlinked-writers' -LinkWriters $false
    Assert-ThrowsCode -Code 'SCHEMA' -Label 'Initialize rejected unordered writers sharing a path' -Action {
        Invoke-Helper -Parameters @{
            Action = 'Initialize'
            DraftPath = $unlinked.DraftPath
            ManifestPath = $unlinked.ManifestPath
            WorkspaceRoot = $unlinkedRoot
            SessionId = 'unlinked-custody'
        }
    }

    $linkedRoot = Join-Path $resolvedTemporaryRoot 'linked'
    New-GitWorkspace -Root $linkedRoot
    $linked = New-CustodyDraft -Root $linkedRoot -ChangeId 'linked-writers' -LinkWriters $true
    $sessionId = 'linked-custody'
    Invoke-Helper -Parameters @{
        Action = 'Initialize'
        DraftPath = $linked.DraftPath
        ManifestPath = $linked.ManifestPath
        WorkspaceRoot = $linkedRoot
        SessionId = $sessionId
    } | Out-Null
    Invoke-Helper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $linked.ManifestPath
        WorkspaceRoot = $linkedRoot
        SessionId = $sessionId
        TaskId = 'T01'
    } | Out-Null
    [System.IO.File]::WriteAllText($linked.SharedPath, 'first writer result', [System.Text.UTF8Encoding]::new($false))
    Invoke-Helper -Parameters @{
        Action = 'FinishTask'
        ManifestPath = $linked.ManifestPath
        WorkspaceRoot = $linkedRoot
        SessionId = $sessionId
        TaskId = 'T01'
        Outcome = 'PASS'
        ChangedFilesJson = '["src/shared.txt"]'
        VerificationSummary = 'First writer passed ownership forward.'
    } | Out-Null
    $afterFirst = Get-Content -LiteralPath $linked.LockPath -Raw | ConvertFrom-Json
    $firstCustody = $afterFirst.path_custody.'src/shared.txt'
    if ($null -eq $firstCustody) {
        throw 'FinishTask T01 did not record path_custody for src/shared.txt.'
    }
    if ([string]$firstCustody.task_id -ne 'T01') {
        throw ('First writer custody task_id was ' + $firstCustody.task_id + ' instead of T01.')
    }
    if ([string]$firstCustody.sha256 -ne (Get-FileSha256 -Path $linked.SharedPath)) {
        throw 'First writer custody sha256 does not match the shared file.'
    }
    Write-Pass -Label 'FinishTask T01 recorded custody for the shared path'

    Invoke-Helper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $linked.ManifestPath
        WorkspaceRoot = $linkedRoot
        SessionId = $sessionId
        TaskId = 'T02'
    } | Out-Null
    $afterTransfer = Get-Content -LiteralPath $linked.LockPath -Raw | ConvertFrom-Json
    $secondCustody = $afterTransfer.path_custody.'src/shared.txt'
    if ([string]$secondCustody.task_id -ne 'T02') {
        throw ('StartTask T02 custody task_id was ' + $secondCustody.task_id + ' instead of T02.')
    }
    if ([string]$secondCustody.received_from_task_id -ne 'T01') {
        throw ('StartTask T02 received_from_task_id was ' + $secondCustody.received_from_task_id + ' instead of T01.')
    }
    Write-Pass -Label 'StartTask T02 transferred custody from T01'

    Write-Output ('Path-custody checks passed: ' + $script:Passed)
}
finally {
    if (Test-Path -LiteralPath $resolvedTemporaryRoot) {
        if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to clean up a path outside the system temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
}
