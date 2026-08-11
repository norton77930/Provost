[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Passed = 0
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$hookPath = Join-Path $repositoryRoot 'docs\governance\reference\hooks\PreToolUse-WriteGate.ps1'
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path -LiteralPath $hookPath -PathType Leaf)) {
    throw ('Write-gate hook is missing: ' + $hookPath)
}
if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
    throw 'This reference test requires Windows PowerShell 5.1.'
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 16), $encoding)
}

function Invoke-WriteGate {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    $previousProfile = $env:PROVOST_SESSION_PROFILE
    $previousRoot = $env:PROVOST_FOREMAN_WORKSPACE_ROOT
    try {
        $env:PROVOST_SESSION_PROFILE = 'foreman'
        $env:PROVOST_FOREMAN_WORKSPACE_ROOT = $WorkspaceRoot
        $payload = [ordered]@{
            cwd = $WorkspaceRoot
            tool_input = [ordered]@{ file_path = $TargetPath }
        } | ConvertTo-Json -Compress
        $output = @($payload | & $windowsPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $hookPath)
        if ($LASTEXITCODE -ne 0) {
            throw ('Write-gate process exited with code ' + $LASTEXITCODE + '.')
        }
        if ($output.Count -eq 0) { return $null }
        return (($output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop)
    }
    finally {
        $env:PROVOST_SESSION_PROFILE = $previousProfile
        $env:PROVOST_FOREMAN_WORKSPACE_ROOT = $previousRoot
    }
}

function Assert-Allowed {
    param($Decision, [Parameter(Mandatory = $true)][string]$Label)
    if ($null -ne $Decision) {
        throw ($Label + ' unexpectedly returned a hook decision: ' + ($Decision | ConvertTo-Json -Compress -Depth 8))
    }
    $script:Passed++
    Write-Output ('PASS: ' + $Label)
}

function Assert-Denied {
    param(
        $Decision,
        [Parameter(Mandatory = $true)][string]$ReasonPattern,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($null -eq $Decision -or $null -eq $Decision.hookSpecificOutput) {
        throw ($Label + ' did not return a hook decision.')
    }
    if ([string]$Decision.hookSpecificOutput.permissionDecision -ne 'deny') {
        throw ($Label + ' returned ' + [string]$Decision.hookSpecificOutput.permissionDecision + ' instead of deny.')
    }
    if ([string]$Decision.hookSpecificOutput.permissionDecisionReason -notmatch $ReasonPattern) {
        throw ($Label + ' returned an unexpected reason: ' + [string]$Decision.hookSpecificOutput.permissionDecisionReason)
    }
    $script:Passed++
    Write-Output ('PASS: ' + $Label)
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('provost-write-scope-' + [guid]::NewGuid().ToString('N'))
$resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
$resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())

if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create test state outside the system temporary directory.'
}

try {
    $stateDirectory = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    [System.IO.Directory]::CreateDirectory($stateDirectory) | Out-Null
    $manifestPath = Join-Path $resolvedTemporaryRoot 'manifest.json'
    $lockPath = Join-Path $stateDirectory 'active-run.lock'

    $manifest = [ordered]@{
        tasks = @(
            [ordered]@{
                id = 'writer'
                write_set = @('allowed.txt')
                must_not_modify = @()
            }
        )
    }
    $lock = [ordered]@{
        state = 'ACTIVE'
        manifest_path = $manifestPath
        task_states = [ordered]@{ writer = 'RUNNING' }
    }
    Write-JsonFile -Path $manifestPath -Value $manifest
    Write-JsonFile -Path $lockPath -Value $lock

    $decision = Invoke-WriteGate -WorkspaceRoot $resolvedTemporaryRoot -TargetPath (Join-Path $resolvedTemporaryRoot 'allowed.txt')
    Assert-Allowed -Decision $decision -Label 'approved write_set path was allowed'

    $decision = Invoke-WriteGate -WorkspaceRoot $resolvedTemporaryRoot -TargetPath (Join-Path $resolvedTemporaryRoot 'outside.txt')
    Assert-Denied -Decision $decision -ReasonPattern 'not in any RUNNING task write_set' -Label 'path outside write_set was denied'

    $manifest.tasks[0].must_not_modify = @('allowed.txt')
    Write-JsonFile -Path $manifestPath -Value $manifest
    $decision = Invoke-WriteGate -WorkspaceRoot $resolvedTemporaryRoot -TargetPath (Join-Path $resolvedTemporaryRoot 'allowed.txt')
    Assert-Denied -Decision $decision -ReasonPattern 'listed in must_not_modify' -Label 'must_not_modify took precedence over write_set'

    $outsideTarget = Join-Path $resolvedSystemTemp ('provost-outside-' + [guid]::NewGuid().ToString('N') + '.txt')
    $decision = Invoke-WriteGate -WorkspaceRoot $resolvedTemporaryRoot -TargetPath $outsideTarget
    Assert-Denied -Decision $decision -ReasonPattern 'blocks writes outside the governed workspace root' -Label 'path outside the governed workspace was denied'

    [System.IO.File]::Delete($lockPath)
    $decision = Invoke-WriteGate -WorkspaceRoot $resolvedTemporaryRoot -TargetPath (Join-Path $resolvedTemporaryRoot 'allowed.txt')
    Assert-Denied -Decision $decision -ReasonPattern 'No active Foreman run' -Label 'missing active-run lock failed closed'

    $lock.state = 'BROKEN'
    Write-JsonFile -Path $lockPath -Value $lock
    $decision = Invoke-WriteGate -WorkspaceRoot $resolvedTemporaryRoot -TargetPath (Join-Path $resolvedTemporaryRoot 'allowed.txt')
    Assert-Denied -Decision $decision -ReasonPattern 'run state is BROKEN' -Label 'invalid run state failed closed'

    Write-Output ('Write-scope checks passed: ' + $script:Passed)
}
finally {
    if (Test-Path -LiteralPath $resolvedTemporaryRoot) {
        if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to clean up a path outside the system temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
}
