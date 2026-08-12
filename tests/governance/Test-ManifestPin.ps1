[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

$resolvedTemporaryRoot = New-IsolatedTempRoot -Prefix 'provost-manifest-pin-'

try {
    New-GitWorkspace -Root $resolvedTemporaryRoot -RelativeFile 'src/sample.txt' -Content 'baseline'

    $foremanRoot = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot 'manifests\none\sample-change'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $planPath = Join-Path $plans 'sample-plan.md'
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    $manifestPath = Join-Path $manifestDirectory 'r001.json'
    $sessionId = 'provost-manifest-pin'
    $planText = '# Sample plan'
    [System.IO.File]::WriteAllText($planPath, $planText, [System.Text.UTF8Encoding]::new($false))

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

    $initialized = Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $draftPath
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    }
    if ($initialized.status -ne 'INITIALIZED') {
        throw ('Initialize returned ' + $initialized.status + ' instead of INITIALIZED.')
    }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Initialize did not create r001.json.'
    }
    if (Test-Path -LiteralPath $draftPath) {
        throw 'Initialize left the draft in place.'
    }
    $lockPath = Join-Path $foremanRoot 'active-run.lock'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw 'Initialize did not create active-run.lock.'
    }
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $manifestHash = Get-FileSha256 -Path $manifestPath
    if ([string]$lock.manifest_sha256 -ne $manifestHash) {
        throw 'Lock manifest_sha256 does not match the approved manifest file.'
    }
    if (-not (Test-Path -LiteralPath ([string]$lock.ledger_path) -PathType Leaf)) {
        throw 'Initialize did not create a ledger.'
    }
    Write-Pass -Label 'Initialize created a pinned r001 manifest and lock hash'

    $validated = Invoke-ForemanHelper -Parameters @{
        Action = 'Validate'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
    }
    if ($validated.status -ne 'VALID') {
        throw ('Validate returned ' + $validated.status + ' instead of VALID.')
    }
    Write-Pass -Label 'Validate accepted the unchanged approved manifest'

    [System.IO.File]::WriteAllText($planPath, '# Tampered plan', [System.Text.UTF8Encoding]::new($false))
    Assert-ThrowsCode -Code 'IMMUTABLE' -Label 'Validate rejected a tampered native Plan' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Validate'
            ManifestPath = $manifestPath
            WorkspaceRoot = $resolvedTemporaryRoot
        }
    }
    [System.IO.File]::WriteAllText($planPath, $planText, [System.Text.UTF8Encoding]::new($false))

    $manifestJson = Get-Content -LiteralPath $manifestPath -Raw
    $tampered = $manifestJson -replace '"Sample change"', '"Tampered change"'
    if ($tampered -eq $manifestJson) {
        throw 'Could not tamper the approved manifest title for the hash check.'
    }
    [System.IO.File]::WriteAllText($manifestPath, $tampered, [System.Text.UTF8Encoding]::new($false))
    Assert-ThrowsCode -Code 'IMMUTABLE' -Label 'StartTask rejected a tampered approved manifest via the lock hash' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'StartTask'
            ManifestPath = $manifestPath
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            TaskId = 'T01'
        }
    }

    Write-Output ('Manifest-pin checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    Remove-IsolatedTempRoot -Root $resolvedTemporaryRoot
}
