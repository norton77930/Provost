[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

$savedProfile = $env:PROVOST_SESSION_PROFILE
$savedSessionId = $env:CLAUDE_CODE_SESSION_ID

function New-LifecycleDraft {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ChangeId
    )
    $foremanRoot = Join-Path $Root '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot ('manifests\none\' + $ChangeId)
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $planPath = Join-Path $plans 'plan.md'
    if (-not (Test-Path -LiteralPath $planPath -PathType Leaf)) {
        [System.IO.File]::WriteAllText($planPath, '# Lifecycle actions plan', [System.Text.UTF8Encoding]::new($false))
    }
    $draft = [ordered]@{
        schema = 'provost-foreman-manifest/v2'
        revision = [ordered]@{ id = 'r001'; number = 1; supersedes = $null }
        approval = [ordered]@{ state = 'approved'; source = 'native-plan-auto' }
        native_plan = [ordered]@{ relative_path = '.claude/provost/foreman/plans/plan.md' }
        spec = [ordered]@{ system = 'none'; reference = $null }
        change = [ordered]@{ id = $ChangeId; title = 'Lifecycle actions' }
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
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    Write-JsonFile -Path $draftPath -Value $draft
    return [pscustomobject]@{
        DraftPath = $draftPath
        ManifestPath = (Join-Path $manifestDirectory 'r001.json')
        LockPath = (Join-Path $foremanRoot 'active-run.lock')
        ForemanRoot = $foremanRoot
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

function New-ApprovedRun {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ChangeId,
        [Parameter(Mandatory = $true)][bool]$Governed,
        [string]$SessionId = 'provost-lifecycle'
    )
    Set-SessionEnforcement -Root $Root -Governed $Governed
    $draft = New-LifecycleDraft -Root $Root -ChangeId $ChangeId
    Invoke-ForemanHelper -Parameters @{
        Action = 'Initialize'
        DraftPath = $draft.DraftPath
        ManifestPath = $draft.ManifestPath
        WorkspaceRoot = $Root
        SessionId = $SessionId
    } | Out-Null
    return [pscustomobject]@{
        ManifestPath = $draft.ManifestPath
        LockPath = $draft.LockPath
        ForemanRoot = $draft.ForemanRoot
        SessionId = $SessionId
    }
}

$ungovernedRoot = New-IsolatedTempRoot -Prefix 'provost-lifecycle-retry-'
$governedRoot = New-IsolatedTempRoot -Prefix 'provost-lifecycle-recover-'
try {
    New-GitWorkspace -Root $ungovernedRoot -RelativeFile 'src/sample.txt' -Content 'baseline'
    $retryRun = New-ApprovedRun -Root $ungovernedRoot -ChangeId 'retry-change' -Governed $false -SessionId 'lifecycle-retry'
    $recordRetry = @{
        Action = 'RecordRetry'
        ManifestPath = $retryRun.ManifestPath
        WorkspaceRoot = $ungovernedRoot
        SessionId = $retryRun.SessionId
        TaskId = 'T01'
        RetryKind = 'http_429'
    }

    Assert-ThrowsCode -Code 'RETRY' -Label 'RecordRetry refuses when the named task is not RUNNING' -Action {
        Invoke-ForemanHelper -Parameters $recordRetry
    }
    # RETRY is also the one-retry-per-task check, so the code alone would not
    # tell us which arm fired.
    if ((Get-ForemanLastThrowMessage) -notmatch 'Only a running task may record a retry') {
        throw ('The refusal did not come from the running-task check: ' + (Get-ForemanLastThrowMessage))
    }

    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $retryRun.ManifestPath
        WorkspaceRoot = $ungovernedRoot
        SessionId = $retryRun.SessionId
        TaskId = 'T01'
    } | Out-Null
    $recorded = Invoke-ForemanHelper -Parameters $recordRetry
    if ([string]$recorded.status -ne 'RETRY_RECORDED') {
        throw ('RecordRetry returned ' + [string]$recorded.status + ' instead of RETRY_RECORDED.')
    }
    $lockAfterRetry = Get-Content -LiteralPath $retryRun.LockPath -Raw | ConvertFrom-Json
    if ([int]$lockAfterRetry.retries.T01 -ne 1) {
        throw ('retries[T01] was ' + [string]$lockAfterRetry.retries.T01 + ' instead of 1.')
    }
    $retryEvents = @(
        Get-Content -LiteralPath ([string]$lockAfterRetry.ledger_path) |
            ForEach-Object { $_ | ConvertFrom-Json } |
            Where-Object { [string]$_.event -eq 'retry' }
    )
    if ($retryEvents.Count -ne 1) {
        throw ('Expected one retry ledger event, found ' + $retryEvents.Count + '.')
    }
    $retryEvent = $retryEvents[0]
    if ([string]$retryEvent.task_id -ne 'T01') {
        throw ('retry event task_id was ' + [string]$retryEvent.task_id + ' instead of T01.')
    }
    if ([string]$retryEvent.agent_key -ne 'foreman-implementer') {
        throw ('retry event agent_key was ' + [string]$retryEvent.agent_key + ' instead of foreman-implementer.')
    }
    if ([string]$retryEvent.model -ne 'sonnet') {
        throw ('retry event model was ' + [string]$retryEvent.model + ' instead of sonnet.')
    }
    if ([string]$retryEvent.effort -ne 'high') {
        throw ('retry event effort was ' + [string]$retryEvent.effort + ' instead of high.')
    }
    $classification = ''
    if ($null -ne $retryEvent.PSObject.Properties['classification']) { $classification = [string]$retryEvent.classification }
    if ($classification -ne 'http_429') {
        throw ('retry event classification was ' + $classification + ' instead of http_429.')
    }
    Write-Pass -Label 'RecordRetry records a retry for a running task'

    Assert-ThrowsCode -Code 'RETRY' -Label 'RecordRetry refuses a second retry for the same task' -Action {
        Invoke-ForemanHelper -Parameters $recordRetry
    }
    if ((Get-ForemanLastThrowMessage) -notmatch 'Only one transient retry is allowed for a task') {
        throw ('The refusal did not come from the one-retry check: ' + (Get-ForemanLastThrowMessage))
    }

    Assert-ThrowsCode -Code 'ACTIVE_LOCK' -Label 'RecoverLock refuses without -Acknowledge' -Action {
        Invoke-ForemanHelper -Parameters @{
            Action = 'RecoverLock'
            WorkspaceRoot = $ungovernedRoot
        }
    }
    # ACTIVE_LOCK is also "no lock", "already active", and CloseBlocked's
    # acknowledge check, so the code alone would not tell us which arm fired.
    if ((Get-ForemanLastThrowMessage) -notmatch 'RecoverLock requires WorkspaceRoot and explicit -Acknowledge') {
        throw ('The refusal did not come from the Acknowledge check: ' + (Get-ForemanLastThrowMessage))
    }

    Invoke-ForemanHelper -Parameters @{
        Action = 'RecoverLock'
        WorkspaceRoot = $ungovernedRoot
        Acknowledge = $true
    } | Out-Null
    if (Test-Path -LiteralPath $retryRun.LockPath -PathType Leaf) {
        throw 'RecoverLock left active-run.lock in place.'
    }
    $abandonedArchives = @(Get-ChildItem -LiteralPath $retryRun.ForemanRoot -Filter 'abandoned-lock-*.json' -File)
    if ($abandonedArchives.Count -ne 1) {
        throw ('Expected one abandoned-lock archive, found ' + $abandonedArchives.Count + '.')
    }
    if ($abandonedArchives[0].Name -notmatch '^abandoned-lock-\d{8}-\d{6}-[0-9a-f]{8}\.json$') {
        throw ('Lock archive name carries no collision-resistant suffix: ' + $abandonedArchives[0].Name)
    }
    Write-Pass -Label 'RecoverLock archives the lock to an abandoned-lock file with a collision-resistant suffix and removes active-run.lock'

    # RecoverLock deletes the lock, so a second recovery has to open a new run
    # through Initialize. Reusing retry-change/r001.json is IMMUTABLE.
    New-ApprovedRun -Root $ungovernedRoot -ChangeId 'second-recovery' -Governed $false -SessionId 'lifecycle-second-recovery' | Out-Null
    Invoke-ForemanHelper -Parameters @{
        Action = 'RecoverLock'
        WorkspaceRoot = $ungovernedRoot
        Acknowledge = $true
    } | Out-Null
    $abandonedArchives = @(Get-ChildItem -LiteralPath $retryRun.ForemanRoot -Filter 'abandoned-lock-*.json' -File)
    if ($abandonedArchives.Count -lt 2) {
        throw ('Expected one archive per recovery, found ' + $abandonedArchives.Count + '.')
    }
    foreach ($abandonedArchive in $abandonedArchives) {
        if ($abandonedArchive.Name -notmatch '^abandoned-lock-\d{8}-\d{6}-[0-9a-f]{8}\.json$') {
            throw ('Lock archive name carries no collision-resistant suffix: ' + $abandonedArchive.Name)
        }
    }
    Write-Pass -Label 'Two RecoverLock archives in one second do not overwrite each other'

    New-GitWorkspace -Root $governedRoot -RelativeFile 'src/sample.txt' -Content 'baseline'
    $owned = New-ApprovedRun -Root $governedRoot -ChangeId 'enforced-change' -Governed $true -SessionId 'lifecycle-enforced'
    Invoke-ForemanHelper -Parameters @{
        Action = 'StartTask'
        ManifestPath = $owned.ManifestPath
        WorkspaceRoot = $governedRoot
        SessionId = $owned.SessionId
        TaskId = 'T01'
    } | Out-Null
    $recordRetryOwned = @{
        Action = 'RecordRetry'
        ManifestPath = $owned.ManifestPath
        WorkspaceRoot = $governedRoot
        SessionId = $owned.SessionId
        TaskId = 'T01'
        RetryKind = 'http_429'
    }

    # Keep the helper -SessionId as the lock's session_id; CLAUDE_CODE_SESSION_ID
    # is the identity the gate actually reads.
    Set-SessionEnforcement -Root $governedRoot -Governed $true
    Assert-ThrowsCode -Code 'SESSION' -Label 'An enforced lock refuses RecordRetry from a different session' -Action {
        Invoke-ForemanHelper -Parameters $recordRetryOwned
    }
    if ((Get-ForemanLastThrowMessage) -notmatch 'from a different session') {
        throw ('The refusal did not come from the session-binding gate: ' + (Get-ForemanLastThrowMessage))
    }

    $recovered = Invoke-ForemanHelper -Parameters @{
        Action = 'RecoverLock'
        WorkspaceRoot = $governedRoot
        Acknowledge = $true
    }
    if ([string]$recovered.status -ne 'RECOVERED') {
        throw ('RecoverLock from a different session returned ' + [string]$recovered.status + ' instead of RECOVERED.')
    }
    if (Test-Path -LiteralPath $owned.LockPath -PathType Leaf) {
        throw 'RecoverLock from a different session left active-run.lock in place.'
    }
    Write-Pass -Label 'RecoverLock succeeds from a session other than the one that opened an enforced lock'

    Write-Output ('Lifecycle-actions checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    $env:PROVOST_SESSION_PROFILE = $savedProfile
    $env:CLAUDE_CODE_SESSION_ID = $savedSessionId
    Remove-IsolatedTempRoot -Root $ungovernedRoot
    Remove-IsolatedTempRoot -Root $governedRoot
}
