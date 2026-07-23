# Provost Foreman PreToolUse hook for Edit/Write/NotebookEdit: engine-enforced
# write_set gate. A file write is allowed only when the active-run.lock has a
# RUNNING task whose approved write_set contains the target path (the Foreman
# private directory is always writable for plans/drafts). Fail-closed: if the
# governed state cannot be read, the write is denied rather than waved through.
# Self-gated: inert unless the launching wrapper marked this session as foreman.
if ($env:PROVOST_SESSION_PROFILE -ne 'foreman') { exit 0 }

function Send-PreToolUseDecision {
    param([string]$Decision, [string]$Reason)
    $response = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; permissionDecision = $Decision; permissionDecisionReason = $Reason } }
    Write-Output ($response | ConvertTo-Json -Depth 6 -Compress)
    exit 0
}

function Read-JsonWithRetry {
    param([string]$LiteralPath, [string]$Label)
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            return ([System.IO.File]::ReadAllText($LiteralPath) | ConvertFrom-Json -ErrorAction Stop)
        }
        catch {
            $lastError = $_
            Start-Sleep -Milliseconds 100
        }
    }
    Send-PreToolUseDecision -Decision 'deny' -Reason ('Foreman write-gate could not read the ' + $Label + ' (' + $lastError.Exception.Message + '). Failing closed; inspect the Foreman state before retrying.')
}

$workspaceRoot = $env:PROVOST_FOREMAN_WORKSPACE_ROOT
if ([string]::IsNullOrWhiteSpace($workspaceRoot)) {
    Send-PreToolUseDecision -Decision 'deny' -Reason 'Foreman write-gate is active but PROVOST_FOREMAN_WORKSPACE_ROOT is missing. Start governed sessions through provost-foreman; failing closed.'
}

$payload = $null
try {
    $rawInput = [Console]::In.ReadToEnd()
    if (-not [string]::IsNullOrWhiteSpace($rawInput)) {
        $payload = $rawInput | ConvertFrom-Json -ErrorAction Stop
    }
}
catch {
    $payload = $null
}
if ($null -eq $payload -or $null -eq $payload.tool_input) {
    Send-PreToolUseDecision -Decision 'deny' -Reason 'Foreman write-gate could not parse the tool input; failing closed for file writes.'
}

$targetPath = [string]$payload.tool_input.file_path
if ([string]::IsNullOrWhiteSpace($targetPath)) { $targetPath = [string]$payload.tool_input.notebook_path }
if ([string]::IsNullOrWhiteSpace($targetPath)) {
    Send-PreToolUseDecision -Decision 'deny' -Reason 'Foreman write-gate found no file path in the tool input; failing closed.'
}

if (-not [System.IO.Path]::IsPathRooted($targetPath)) {
    $baseDirectory = [string]$payload.cwd
    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        Send-PreToolUseDecision -Decision 'deny' -Reason 'Foreman write-gate cannot resolve a relative path without the session cwd; use an absolute path.'
    }
    $targetPath = Join-Path $baseDirectory $targetPath
}
$targetFull = [System.IO.Path]::GetFullPath($targetPath)

$rootFull = [System.IO.Path]::GetFullPath($workspaceRoot).TrimEnd([char]92, [char]47)
$rootPrefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
if (-not $targetFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    Send-PreToolUseDecision -Decision 'deny' -Reason ('Foreman blocks writes outside the governed workspace root (' + $rootFull + '): ' + $targetFull + '. Use provost for ungoverned work.')
}

$relativePath = $targetFull.Substring($rootPrefix.Length).Replace([char]92, [char]47)
$lockPath = Join-Path $rootFull '.claude\provost\foreman\active-run.lock'
if ($relativePath.StartsWith('.claude/provost/foreman/', [System.StringComparison]::OrdinalIgnoreCase)) {
    $planAuthoringPath = $relativePath -match '(?i)^\.claude/provost/foreman/plans/.+\.md$'
    $draftAuthoringPath = $relativePath -match '(?i)^\.claude/provost/foreman/manifests/(openspec|spec-kit|none)/[a-z0-9]+(?:-[a-z0-9]+)*/r\d{3}\.draft\.json$'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf) -and ($planAuthoringPath -or $draftAuthoringPath)) { exit 0 }
    Send-PreToolUseDecision -Decision 'deny' -Reason ('Foreman control-plane state is helper-owned and cannot be directly written: ' + $relativePath + '. Only native Plan files and correctly named rNNN.draft.json files are writable before Initialize.')
}

if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    Send-PreToolUseDecision -Decision 'deny' -Reason ('No active Foreman run (active-run.lock is absent), so "' + $relativePath + '" cannot be written yet. Follow the governed flow: author the native Plan, get user approval, Initialize the manifest, then StartTask for the task that owns this path.')
}

$lock = Read-JsonWithRetry -LiteralPath $lockPath -Label 'active-run.lock'
if ([string]$lock.state -ne 'ACTIVE') {
    Send-PreToolUseDecision -Decision 'deny' -Reason ('The Foreman run state is ' + [string]$lock.state + '; no writes are allowed until the user resolves it (see CloseBlocked/RecoverLock guidance).')
}

$manifestPath = [string]$lock.manifest_path
if ([string]::IsNullOrWhiteSpace($manifestPath) -or -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Send-PreToolUseDecision -Decision 'deny' -Reason 'The Foreman lock names no readable manifest; failing closed for file writes.'
}
$manifest = Read-JsonWithRetry -LiteralPath $manifestPath -Label 'approved manifest'

$runningTaskIds = @()
if ($null -ne $lock.task_states) {
    foreach ($stateProperty in $lock.task_states.PSObject.Properties) {
        if ([string]$stateProperty.Value -eq 'RUNNING') { $runningTaskIds += [string]$stateProperty.Name }
    }
}

$allowedPaths = @()
$forbiddenPaths = @()
if ($null -ne $manifest.tasks) {
    foreach ($task in @($manifest.tasks)) {
        if ($runningTaskIds -notcontains [string]$task.id) { continue }
        foreach ($mustNotModifyEntry in @($task.must_not_modify)) {
            if ([string]::IsNullOrWhiteSpace([string]$mustNotModifyEntry)) { continue }
            $forbiddenPaths += ([string]$mustNotModifyEntry).Replace([char]92, [char]47).TrimStart([char]47)
        }
        foreach ($writeEntry in @($task.write_set)) {
            if ([string]::IsNullOrWhiteSpace([string]$writeEntry)) { continue }
            $allowedPaths += ([string]$writeEntry).Replace([char]92, [char]47).TrimStart([char]47)
        }
    }
}

foreach ($forbiddenPath in $forbiddenPaths) {
    if ([string]::Equals($relativePath, $forbiddenPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        Send-PreToolUseDecision -Decision 'deny' -Reason ('Foreman write-gate: "' + $relativePath + '" is listed in must_not_modify for a RUNNING task. Create a new approved manifest revision if this path truly needs to change.')
    }
}
foreach ($allowedPath in $allowedPaths) {
    if ([string]::Equals($relativePath, $allowedPath, [System.StringComparison]::OrdinalIgnoreCase)) { exit 0 }
}

$runningLabel = 'none'
if ($runningTaskIds.Count -gt 0) { $runningLabel = ($runningTaskIds -join ', ') }
Send-PreToolUseDecision -Decision 'deny' -Reason ('Foreman write-gate: "' + $relativePath + '" is not in any RUNNING task write_set (running tasks: ' + $runningLabel + '). Call the helper StartTask for the task that owns this path, or cut a new approved manifest revision. Do not retry blindly.')
