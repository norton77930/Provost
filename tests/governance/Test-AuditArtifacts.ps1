[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

$resolvedTemporaryRoot = New-IsolatedTempRoot -Prefix 'provost-audit-artifacts-'
try {
    New-GitWorkspace -Root $resolvedTemporaryRoot -RelativeFile 'src/sample.txt' -Content 'baseline'
    $foremanRoot = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot 'manifests\none\sample-change'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    $manifestPath = Join-Path $manifestDirectory 'r001.json'
    $lockPath = Join-Path $foremanRoot 'active-run.lock'
    $sessionId = 'provost-audit-artifacts'
    [System.IO.File]::WriteAllText((Join-Path $plans 'plan.md'), '# Audit artifacts plan', [System.Text.UTF8Encoding]::new($false))

    $draft = [ordered]@{
        schema = 'provost-foreman-manifest/v2'
        revision = [ordered]@{ id = 'r001'; number = 1; supersedes = $null }
        approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
        native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/plan.md' }
        spec = [ordered]@{ system = 'none'; reference = $null }
        change = [ordered]@{ id = 'sample-change'; title = 'Audit artifacts' }
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
    Write-JsonFile -Path $draftPath -Value $draft

    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $draftPath
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    } | Out-Null

    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $ledgerPath = [string]$lock.ledger_path
    if (-not (Test-Path -LiteralPath $ledgerPath -PathType Leaf)) {
        throw 'Initialize did not create a ledger.'
    }
    $initializedEvents = @(
        Get-Content -LiteralPath $ledgerPath |
            ForEach-Object { $_ | ConvertFrom-Json } |
            Where-Object { [string]$_.event -eq 'run_initialized' }
    )
    if ($initializedEvents.Count -lt 1) {
        throw 'Ledger is missing a run_initialized event.'
    }
    Write-Pass -Label 'Initialize appended a parseable run_initialized ledger event'

    Invoke-ForemanHelper -Parameters @{
        Action = 'Complete'
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        Outcome = 'BLOCKED'
    } | Out-Null
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $receiptPath = [string]$lock.handoff_path
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw 'Complete BLOCKED did not write a terminal handoff receipt.'
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ([string]$receipt.manifest_sha256 -ne (Get-FileSha256 -Path $manifestPath)) {
        throw 'Receipt manifest_sha256 does not match the approved manifest file.'
    }
    if ([string]$receipt.ledger_sha256 -ne (Get-FileSha256 -Path $ledgerPath)) {
        throw 'Receipt ledger_sha256 does not match the ledger file.'
    }
    if ([string]$lock.handoff_sha256 -ne (Get-FileSha256 -Path $receiptPath)) {
        throw 'Lock handoff_sha256 does not match the receipt file.'
    }
    Write-Pass -Label 'Terminal receipt pins manifest, ledger, and receipt hashes'

    Invoke-ForemanHelper -Parameters @{
        Action = 'CloseBlocked'
        WorkspaceRoot = $resolvedTemporaryRoot
        Acknowledge = $true
    } | Out-Null
    Add-Content -LiteralPath $ledgerPath -Value '{"event":"tampered"}' -Encoding ascii
    $r002DraftPath = Join-Path $manifestDirectory 'r002.draft.json'
    $r002Path = Join-Path $manifestDirectory 'r002.json'
    $r002 = [ordered]@{}
    foreach ($key in $draft.Keys) { $r002[$key] = $draft[$key] }
    $r002['revision'] = [ordered]@{
        id = 'r002'
        number = 2
        supersedes = [ordered]@{
            revision = 'r001'
            sha256 = (Get-FileSha256 -Path $manifestPath)
        }
    }
    Write-JsonFile -Path $r002DraftPath -Value $r002
    Assert-ThrowsCode -Code 'IMMUTABLE' -Label 'Later Initialize rejects a ledger that changed after the receipt' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Initialize'
            DraftPath = $r002DraftPath
            ManifestPath = $r002Path
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
        }
    }

    Write-Output ('Audit-artifacts checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    Remove-IsolatedTempRoot -Root $resolvedTemporaryRoot
}
