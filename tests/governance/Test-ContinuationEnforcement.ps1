[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

$savedProfile = $env:PROVOST_SESSION_PROFILE
$savedSessionId = $env:CLAUDE_CODE_SESSION_ID

function New-ContinuationDraft {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$RevisionNumber,
        [string]$PreviousManifestPath,
        [string]$PreviousHandoffPath
    )
    $revisionId = 'r' + $RevisionNumber.ToString('000')
    $manifestDirectory = Join-Path (Join-Path $Root '.claude\provost\foreman') 'manifests\none\continuation-change'
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null

    $revision = [ordered]@{ id = $revisionId; number = $RevisionNumber; supersedes = $null }
    $continuation = $null
    if ($RevisionNumber -gt 1) {
        $previousId = 'r' + ($RevisionNumber - 1).ToString('000')
        $revision = [ordered]@{
            id = $revisionId
            number = $RevisionNumber
            supersedes = [ordered]@{ revision = $previousId; sha256 = (Get-FileSha256 -Path $PreviousManifestPath) }
        }
        $continuation = [ordered]@{
            kind = 'adopt-prior-wip'
            source = [ordered]@{
                revision = $previousId
                manifest_sha256 = (Get-FileSha256 -Path $PreviousManifestPath)
                handoff_sha256 = (Get-FileSha256 -Path $PreviousHandoffPath)
            }
            adopt_paths = @('src/sample.txt')
        }
    }

    $draft = [ordered]@{
        schema = 'provost-foreman-manifest/v2'
        revision = $revision
        approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
        native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/plan.md' }
        spec = [ordered]@{ system = 'none'; reference = $null }
        change = [ordered]@{ id = 'continuation-change'; title = 'Continuation enforcement' }
        role_catalog = 'provost-foreman/v1'
        continuation = $continuation
        tasks = @(
            [ordered]@{
                id = 'T01'
                title = 'Writer'
                agent_key = 'foreman-implementer'
                kind = 'writer'
                depends_on = @()
                write_set = @('src/sample.txt')
                must_not_modify = @()
                acceptance = @()
            },
            [ordered]@{
                id = 'T02'
                title = 'Verifier'
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
    $draftPath = Join-Path $manifestDirectory ($revisionId + '.draft.json')
    Write-JsonFile -Path $draftPath -Value $draft
    return [pscustomobject]@{
        DraftPath = $draftPath
        ManifestPath = (Join-Path $manifestDirectory ($revisionId + '.json'))
    }
}

function Set-SessionEnforcement {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][bool]$Governed
    )
    if (-not $Governed) {
        $env:PROVOST_SESSION_PROFILE = $null
        return
    }
    $env:PROVOST_SESSION_PROFILE = 'foreman'
    $env:CLAUDE_CODE_SESSION_ID = [guid]::NewGuid().ToString()
    $foremanRoot = Join-Path $Root '.claude\provost\foreman'
    [System.IO.Directory]::CreateDirectory($foremanRoot) | Out-Null
    Write-JsonFile -Path (Join-Path $foremanRoot 'session-liveness.json') -Value ([ordered]@{
        session_id = [string]$env:CLAUDE_CODE_SESSION_ID
        written_utc = [DateTime]::UtcNow.ToString('o')
    })
}

function New-TerminatedPriorRun {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][bool]$Governed
    )
    New-GitWorkspace -Root $Root -RelativeFile 'src/sample.txt' -Content 'baseline'
    $foremanRoot = Join-Path $Root '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $plans 'plan.md'), '# Continuation plan', [System.Text.UTF8Encoding]::new($false))

    Set-SessionEnforcement -Root $Root -Governed $Governed
    $r001 = New-ContinuationDraft -Root $Root -RevisionNumber 1
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $r001.DraftPath
        ManifestPath = $r001.ManifestPath
        WorkspaceRoot = $Root
        SessionId = 'provost-continuation'
    } | Out-Null

    # The receipt only lists a path as work in progress if a task actually
    # touched it, and continuation refuses to adopt anything the receipt does
    # not name. So the prior run has to do some work before it fails.
    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $r001.ManifestPath
        WorkspaceRoot = $Root
        SessionId = 'provost-continuation'
        TaskId = 'T01'
    } | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $Root 'src\sample.txt'), 'work in progress', [System.Text.UTF8Encoding]::new($false))
    Invoke-ForemanHelper -Parameters @{
        Action = 'FinishTask'
        ManifestPath = $r001.ManifestPath
        WorkspaceRoot = $Root
        SessionId = 'provost-continuation'
        TaskId = 'T01'
        Outcome = 'PASS'
        ChangedFilesJson = '["src/sample.txt"]'
    } | Out-Null

    Invoke-ForemanHelper -Parameters @{
        Action = 'Complete'
        WorkspaceRoot = $Root
        SessionId = 'provost-continuation'
        Outcome = 'FAIL'
        FailureSignatureJson = ([ordered]@{ command = 'vitest'; status = 'exit:1'; error_summary = 'boom' } | ConvertTo-Json -Depth 8 -Compress)
    } | Out-Null
    $lock = Get-Content -LiteralPath (Join-Path $foremanRoot 'active-run.lock') -Raw | ConvertFrom-Json
    $handoffPath = [string]$lock.handoff_path
    Invoke-ForemanHelper -Parameters @{
        Action = 'CloseBlocked'
        WorkspaceRoot = $Root
        Acknowledge = $true
    } | Out-Null
    return [pscustomobject]@{ ManifestPath = $r001.ManifestPath; HandoffPath = $handoffPath }
}

# Continuation adopts work in progress from a prior run. Nothing recorded
# whether that run was under enforcement, so an enforced run could build on
# unpoliced work and the audit trail would not show the join.
$refusedRoot = New-IsolatedTempRoot -Prefix 'provost-continuation-refuse-'
$acceptedRoot = New-IsolatedTempRoot -Prefix 'provost-continuation-accept-'
$legacyRoot = New-IsolatedTempRoot -Prefix 'provost-continuation-legacy-'
try {
    $unpoliced = New-TerminatedPriorRun -Root $refusedRoot -Governed $false
    Set-SessionEnforcement -Root $refusedRoot -Governed $true
    $afterUnpoliced = New-ContinuationDraft -Root $refusedRoot -RevisionNumber 2 -PreviousManifestPath $unpoliced.ManifestPath -PreviousHandoffPath $unpoliced.HandoffPath
    Assert-ThrowsCode -Code 'CONTINUATION' -Label 'An enforced run refuses to adopt work from a run that was not enforced' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Initialize'
            DraftPath = $afterUnpoliced.DraftPath
            ManifestPath = $afterUnpoliced.ManifestPath
            WorkspaceRoot = $refusedRoot
            SessionId = 'provost-continuation'
        }
    }
    # Nineteen different checks raise CONTINUATION, so the code alone would not
    # tell us the refusal came from the enforcement gate rather than a hash or
    # ordering complaint.
    if ((Get-ForemanLastThrowMessage) -notmatch 'source recorded none') {
        throw ('The refusal did not come from the enforcement gate: ' + (Get-ForemanLastThrowMessage))
    }

    $policed = New-TerminatedPriorRun -Root $acceptedRoot -Governed $true

    # StartTask, FinishTask, and Complete each rewrite the lock, and the receipt
    # takes its record from the lock. If any of them dropped the field the
    # receipt would silently carry nothing, so name the invariant rather than
    # leaving it to be implied by the adoption succeeding.
    $policedReceipt = Get-Content -LiteralPath $policed.HandoffPath -Raw | ConvertFrom-Json
    if ([string]$policedReceipt.enforcement.mode -ne 'hooks') {
        throw ('The receipt of an enforced run recorded mode ' + [string]$policedReceipt.enforcement.mode + ' after the lifecycle rewrote the lock.')
    }
    Write-Pass -Label 'The enforcement record survives the lock rewrites into the terminal receipt'

    Set-SessionEnforcement -Root $acceptedRoot -Governed $true
    $afterPoliced = New-ContinuationDraft -Root $acceptedRoot -RevisionNumber 2 -PreviousManifestPath $policed.ManifestPath -PreviousHandoffPath $policed.HandoffPath
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $afterPoliced.DraftPath
        ManifestPath = $afterPoliced.ManifestPath
        WorkspaceRoot = $acceptedRoot
        SessionId = 'provost-continuation'
    } | Out-Null
    if (-not (Test-Path -LiteralPath $afterPoliced.ManifestPath -PathType Leaf)) {
        throw 'An enforced run did not adopt work from a prior enforced run.'
    }
    Write-Pass -Label 'An enforced run adopts work from a prior enforced run'

    # A receipt written before any of this was tracked carries no record at all,
    # which is a different claim from having been unenforced and must be refused
    # just the same. Reproducing that state means restating the archived lock's
    # hash too, because the two are pinned to each other.
    $legacy = New-TerminatedPriorRun -Root $legacyRoot -Governed $true
    $legacyReceipt = Get-Content -LiteralPath $legacy.HandoffPath -Raw | ConvertFrom-Json
    $rewritten = [ordered]@{}
    foreach ($property in $legacyReceipt.PSObject.Properties) {
        if ($property.Name -ne 'enforcement') { $rewritten[$property.Name] = $property.Value }
    }
    Write-JsonFile -Path $legacy.HandoffPath -Value $rewritten
    $legacyHash = Get-FileSha256 -Path $legacy.HandoffPath
    $legacyForemanRoot = Join-Path $legacyRoot '.claude\provost\foreman'
    foreach ($archive in @(Get-ChildItem -LiteralPath $legacyForemanRoot -Filter 'archived-lock-*.json' -File)) {
        $archivedLock = Get-Content -LiteralPath $archive.FullName -Raw | ConvertFrom-Json
        if ([string]$archivedLock.handoff_path -ne $legacy.HandoffPath) { continue }
        $archivedLock.handoff_sha256 = $legacyHash
        Write-JsonFile -Path $archive.FullName -Value $archivedLock
    }

    Set-SessionEnforcement -Root $legacyRoot -Governed $true
    $afterLegacy = New-ContinuationDraft -Root $legacyRoot -RevisionNumber 2 -PreviousManifestPath $legacy.ManifestPath -PreviousHandoffPath $legacy.HandoffPath
    Assert-ThrowsCode -Code 'CONTINUATION' -Label 'An enforced run refuses a source receipt that records no enforcement at all' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Initialize'
            DraftPath = $afterLegacy.DraftPath
            ManifestPath = $afterLegacy.ManifestPath
            WorkspaceRoot = $legacyRoot
            SessionId = 'provost-continuation'
        }
    }
    if ((Get-ForemanLastThrowMessage) -notmatch 'none recorded') {
        throw ('The refusal did not come from the enforcement gate: ' + (Get-ForemanLastThrowMessage))
    }

    Write-Output ('Continuation-enforcement checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    $env:PROVOST_SESSION_PROFILE = $savedProfile
    $env:CLAUDE_CODE_SESSION_ID = $savedSessionId
    Remove-IsolatedTempRoot -Root $refusedRoot
    Remove-IsolatedTempRoot -Root $acceptedRoot
    Remove-IsolatedTempRoot -Root $legacyRoot
}
