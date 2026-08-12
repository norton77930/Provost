[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

function New-RevisionDraft {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ChangeId,
        [Parameter(Mandatory = $true)][int]$RevisionNumber,
        [string]$PreviousManifestPath,
        [System.Collections.IDictionary]$Diagnosis
    )
    $revisionId = 'r' + $RevisionNumber.ToString('000')
    $foremanRoot = Join-Path $Root '.claude\provost\foreman'
    $manifestDirectory = Join-Path $foremanRoot ('manifests\none\' + $ChangeId)
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
    if ($null -ne $Diagnosis) { $approval['diagnosis'] = $Diagnosis }
    $revision = if ($RevisionNumber -eq 1) {
        [ordered]@{ id = $revisionId; number = $RevisionNumber; supersedes = $null }
    }
    else {
        $previousId = 'r' + ($RevisionNumber - 1).ToString('000')
        [ordered]@{
            id = $revisionId
            number = $RevisionNumber
            supersedes = [ordered]@{
                revision = $previousId
                sha256 = (Get-FileSha256 -Path $PreviousManifestPath)
            }
        }
    }
    $draft = [ordered]@{
        schema = 'provost-foreman-manifest/v2'
        revision = $revision
        approval = $approval
        native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/plan.md' }
        spec = [ordered]@{ system = 'none'; reference = $null }
        change = [ordered]@{ id = $ChangeId; title = 'Failure diagnosis' }
        role_catalog = 'provost-foreman/v1'
        continuation = $null
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

$resolvedTemporaryRoot = New-IsolatedTempRoot -Prefix 'provost-failure-diagnosis-'
try {
    New-GitWorkspace -Root $resolvedTemporaryRoot -RelativeFile 'src/sample.txt' -Content 'baseline'
    $foremanRoot = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $plans 'plan.md'), '# Failure diagnosis plan', [System.Text.UTF8Encoding]::new($false))
    $sessionId = 'provost-failure-diagnosis'
    $changeId = 'failure-diagnosis'
    $lockPath = Join-Path $foremanRoot 'active-run.lock'

    $signature = [ordered]@{
        command = 'vitest'
        status = 'exit:1'
        error_summary = 'EADDRINUSE on localhost:5173'
        environment = [ordered]@{ runtime = 'node-22'; os = 'windows' }
    }
    $signatureJson = $signature | ConvertTo-Json -Depth 8 -Compress
    $equivalentSignatureJson = ([ordered]@{
        command = '  VITEST  '
        status = ' EXIT:1 '
        error_summary = 'EADDRINUSE   ON localhost:5173'
        environment = [ordered]@{ OS = ' WINDOWS '; Runtime = ' NODE-22 ' }
    } | ConvertTo-Json -Depth 8 -Compress)

    function Complete-AsFail {
        param([Parameter(Mandatory = $true)][string]$SignatureJson)
        Invoke-ForemanHelper -Parameters @{
            Action = 'Complete'
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            Outcome = 'FAIL'
            FailureSignatureJson = $SignatureJson
        } | Out-Null
        $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        $receipt = Get-Content -LiteralPath ([string]$lock.handoff_path) -Raw | ConvertFrom-Json
        Invoke-ForemanHelper -Parameters @{
            Action = 'CloseBlocked'
            WorkspaceRoot = $resolvedTemporaryRoot
            Acknowledge = $true
        } | Out-Null
        return $receipt
    }

    $r001 = New-RevisionDraft -Root $resolvedTemporaryRoot -ChangeId $changeId -RevisionNumber 1
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $r001.DraftPath
        ManifestPath = $r001.ManifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    } | Out-Null

    Assert-ThrowsCode -Code 'SCHEMA' -Label 'Complete FAIL without a signature is rejected on v2' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Complete'
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            Outcome = 'FAIL'
        }
    }

    $receipt1 = Complete-AsFail -SignatureJson $signatureJson
    if ([string]::IsNullOrWhiteSpace([string]$receipt1.failure_signature_sha256)) {
        throw 'A V2 FAIL receipt did not persist failure_signature_sha256.'
    }
    Write-Pass -Label 'Complete FAIL recorded a normalized failure signature'

    $r002 = New-RevisionDraft -Root $resolvedTemporaryRoot -ChangeId $changeId -RevisionNumber 2 -PreviousManifestPath $r001.ManifestPath
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $r002.DraftPath
        ManifestPath = $r002.ManifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    } | Out-Null
    $receipt2 = Complete-AsFail -SignatureJson $equivalentSignatureJson
    if ([string]$receipt2.failure_signature_sha256 -ne [string]$receipt1.failure_signature_sha256) {
        throw 'Equivalent signatures did not normalize to the same hash.'
    }
    Write-Pass -Label 'Equivalent failure signatures normalize to the same hash'

    $r003Bare = New-RevisionDraft -Root $resolvedTemporaryRoot -ChangeId $changeId -RevisionNumber 3 -PreviousManifestPath $r002.ManifestPath
    Assert-ThrowsCode -Code 'ESCALATE' -Label 'A third same-signature attempt requires diagnosis' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Initialize'
            DraftPath = $r003Bare.DraftPath
            ManifestPath = $r003Bare.ManifestPath
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
        }
    }

    $diagnosis = [ordered]@{
        failure_signature_sha256 = [string]$receipt2.failure_signature_sha256
        hypothesis = 'A stale Vite listener owns port 5173.'
        measurement = 'Capture the owning PID before rerunning Vitest.'
        evidence_delta = 'The next run records netstat output and the process command line.'
    }
    $r003 = New-RevisionDraft -Root $resolvedTemporaryRoot -ChangeId $changeId -RevisionNumber 3 -PreviousManifestPath $r002.ManifestPath -Diagnosis $diagnosis
    $initialized = Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $r003.DraftPath
        ManifestPath = $r003.ManifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    }
    if ($initialized.status -ne 'INITIALIZED') {
        throw ('Diagnosed r003 Initialize returned ' + $initialized.status + '.')
    }
    Write-Pass -Label 'A same-signature retry with diagnosis can initialize'
    [void](Complete-AsFail -SignatureJson $signatureJson)

    $sameDiagnosis = [ordered]@{
        failure_signature_sha256 = ([string]$receipt2.failure_signature_sha256).ToUpperInvariant()
        hypothesis = '  A   STALE Vite listener owns PORT 5173.  '
        measurement = 'Capture the owning PID before RERUNNING Vitest.'
        evidence_delta = 'The next run records NETSTAT output and the process command line.'
    }
    $r004 = New-RevisionDraft -Root $resolvedTemporaryRoot -ChangeId $changeId -RevisionNumber 4 -PreviousManifestPath $r003.ManifestPath -Diagnosis $sameDiagnosis
    Assert-ThrowsCode -Code 'ESCALATE' -Label 'Reusing a normalized diagnosis is rejected' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Initialize'
            DraftPath = $r004.DraftPath
            ManifestPath = $r004.ManifestPath
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
        }
    }

    Write-Output ('Failure-diagnosis checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    Remove-IsolatedTempRoot -Root $resolvedTemporaryRoot
}
