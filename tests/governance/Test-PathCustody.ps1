[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

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

$resolvedTemporaryRoot = New-IsolatedTempRoot -Prefix 'provost-path-custody-'

try {
    $unlinkedRoot = Join-Path $resolvedTemporaryRoot 'unlinked'
    New-GitWorkspace -Root $unlinkedRoot -RelativeFile 'src/shared.txt' -Content 'baseline shared content'
    $unlinked = New-CustodyDraft -Root $unlinkedRoot -ChangeId 'unlinked-writers' -LinkWriters $false
    Assert-ThrowsCode -Code 'SCHEMA' -Label 'Initialize rejected unordered writers sharing a path' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Initialize'
            DraftPath = $unlinked.DraftPath
            ManifestPath = $unlinked.ManifestPath
            WorkspaceRoot = $unlinkedRoot
            SessionId = 'unlinked-custody'
        }
    }

    $linkedRoot = Join-Path $resolvedTemporaryRoot 'linked'
    New-GitWorkspace -Root $linkedRoot -RelativeFile 'src/shared.txt' -Content 'baseline shared content'
    $linked = New-CustodyDraft -Root $linkedRoot -ChangeId 'linked-writers' -LinkWriters $true
    $sessionId = 'linked-custody'
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $linked.DraftPath
        ManifestPath = $linked.ManifestPath
        WorkspaceRoot = $linkedRoot
        SessionId = $sessionId
    } | Out-Null
    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $linked.ManifestPath
        WorkspaceRoot = $linkedRoot
        SessionId = $sessionId
        TaskId = 'T01'
    } | Out-Null
    [System.IO.File]::WriteAllText($linked.SharedPath, 'first writer result', [System.Text.UTF8Encoding]::new($false))
    Invoke-ForemanHelper -Parameters @{
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

    Invoke-ForemanHelper -Parameters @{
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

    $driftRoot = Join-Path $resolvedTemporaryRoot 'drift'
    New-GitWorkspace -Root $driftRoot -RelativeFile 'src/shared.txt' -Content 'baseline shared content'
    $drift = New-CustodyDraft -Root $driftRoot -ChangeId 'custody-drift' -LinkWriters $true
    $driftSession = 'custody-drift'
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $drift.DraftPath
        ManifestPath = $drift.ManifestPath
        WorkspaceRoot = $driftRoot
        SessionId = $driftSession
    } | Out-Null
    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $drift.ManifestPath
        WorkspaceRoot = $driftRoot
        SessionId = $driftSession
        TaskId = 'T01'
    } | Out-Null
    [System.IO.File]::WriteAllText($drift.SharedPath, 'first writer pinned content', [System.Text.UTF8Encoding]::new($false))
    Invoke-ForemanHelper -Parameters @{
        Action = 'FinishTask'
        ManifestPath = $drift.ManifestPath
        WorkspaceRoot = $driftRoot
        SessionId = $driftSession
        TaskId = 'T01'
        Outcome = 'PASS'
        ChangedFilesJson = '["src/shared.txt"]'
        VerificationSummary = 'First writer pinned custody.'
    } | Out-Null
    [System.IO.File]::WriteAllText($drift.SharedPath, 'drift after custody handoff', [System.Text.UTF8Encoding]::new($false))
    Assert-ThrowsCode -Code 'SCOPE_ESCALATE' -Label 'StartTask T02 rejected shared-path content drift' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'StartTask'
            ManifestPath = $drift.ManifestPath
            WorkspaceRoot = $driftRoot
            SessionId = $driftSession
            TaskId = 'T02'
        }
    }
    if ((Get-ForemanLastThrowMessage) -notmatch 'custody_drift') {
        throw ('SCOPE_ESCALATE did not name custody_drift. Actual: ' + (Get-ForemanLastThrowMessage))
    }
    $driftLock = Get-Content -LiteralPath $drift.LockPath -Raw | ConvertFrom-Json
    if ([string]$driftLock.state -ne 'ESCALATE') {
        throw ('Drift lock state was ' + $driftLock.state + ' instead of ESCALATE.')
    }
    if ([string]::IsNullOrWhiteSpace([string]$driftLock.handoff_path) -or -not (Test-Path -LiteralPath ([string]$driftLock.handoff_path) -PathType Leaf)) {
        throw 'Custody drift did not persist a terminal handoff receipt.'
    }

    Write-Output ('Path-custody checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    Remove-IsolatedTempRoot -Root $resolvedTemporaryRoot
}
