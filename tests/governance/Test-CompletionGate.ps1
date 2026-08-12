[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

$resolvedTemporaryRoot = New-IsolatedTempRoot -Prefix 'provost-completion-gate-'

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

    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $draftPath
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    } | Out-Null

    Assert-ThrowsCode -Code 'VERIFY' -Label 'Complete PASS rejected before any task passed' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'Complete'
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            Outcome = 'PASS'
        }
    }

    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T01'
    } | Out-Null
    Invoke-ForemanHelper -Parameters @{
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
        Invoke-ForemanHelper -Parameters @{
            Action = 'Complete'
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            Outcome = 'PASS'
        }
    }

    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T02'
    } | Out-Null
    Invoke-ForemanHelper -Parameters @{
        Action = 'FinishTask'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
        TaskId = 'T02'
        Outcome = 'PASS'
        ChangedFilesJson = '[]'
        VerificationSummary = 'No material findings.'
    } | Out-Null

    $completed = Invoke-ForemanHelper -Parameters @{
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

    Write-Output ('Completion-gate checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    Remove-IsolatedTempRoot -Root $resolvedTemporaryRoot
}
