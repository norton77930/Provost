[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
    [string[]]$ExternalReadRoots = @(),
    [string[]]$ClaudeArguments = @()
)

# Opens a Claude Code session with the environment the Tier 2 hooks read. It
# sets three variables and starts a session; it never writes governed state.
#
# This script cannot prove that Claude Code has the hooks registered — nothing
# reports that. What it can do is refuse the cases it can see, and leave the
# authoritative check to Initialize, which requires a liveness marker that only
# a hook that actually ran can write.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Stop-Launcher {
    param([string]$Message)
    Write-Error ('[LAUNCH] ' + $Message)
    exit 1
}

$resolvedRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
    Stop-Launcher -Message ('WorkspaceRoot does not exist: ' + $resolvedRoot)
}
if (-not (Test-Path -LiteralPath (Join-Path $resolvedRoot '.git'))) {
    Stop-Launcher -Message ('A governed workspace must be a Git repository: ' + $resolvedRoot)
}

# The hooks are PowerShell. On a platform without it they would not run, would
# block nothing, and would say nothing — a session that calls itself governed
# while enforcing nothing. Refuse instead.
$powerShellCommand = Get-Command powershell.exe -ErrorAction SilentlyContinue
if ($null -eq $powerShellCommand) { $powerShellCommand = Get-Command pwsh -ErrorAction SilentlyContinue }
if ($null -eq $powerShellCommand) {
    Stop-Launcher -Message 'No PowerShell interpreter is available, so the Tier 2 hooks cannot enforce anything on this platform. Tier 2 is Windows-only today.'
}
$powerShellPath = [string]$powerShellCommand.Source

$hookDirectory = Join-Path $PSScriptRoot 'hooks'
foreach ($hookName in @('SessionStart-WorkspaceCheck.ps1', 'PreToolUse-WriteGate.ps1', 'PreToolUse-RefGuard.ps1')) {
    $hookPath = Join-Path $hookDirectory $hookName
    if (-not (Test-Path -LiteralPath $hookPath -PathType Leaf)) {
        Stop-Launcher -Message ('A governance hook is missing: ' + $hookPath)
    }
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($hookPath, [ref]$null, [ref]$parseErrors) | Out-Null
    if (@($parseErrors).Count -gt 0) {
        Stop-Launcher -Message ('A governance hook does not parse and would fail to run: ' + $hookPath)
    }
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Stop-Launcher -Message 'The claude CLI is not on PATH.'
}

# A stale marker from an earlier session must not be mistaken for this one.
# Initialize compares session ids, so this is belt and braces, but it keeps the
# governed directory honest about what has actually run.
$markerPath = Join-Path (Join-Path $resolvedRoot '.claude\provost\foreman') 'session-liveness.json'
if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
    Remove-Item -LiteralPath $markerPath -Force
}

$env:PROVOST_SESSION_PROFILE = 'foreman'
$env:PROVOST_SESSION_PROFILE = 'foreman'
$env:PROVOST_FOREMAN_WORKSPACE_ROOT = $resolvedRoot
if ($ExternalReadRoots.Count -gt 0) {
    $env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS = ($ExternalReadRoots -join [System.IO.Path]::PathSeparator)
}
else {
    $env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS = $null
}

# The Tier 2 hooks are registered here rather than by the plugin. Every hook
# costs a PowerShell process on every matching tool call — measured at about two
# seconds on a normal Windows machine — and the plugin installs for everyone,
# including the majority who never open a governed session. Registering them per
# launch keeps that cost with the people who asked for it.
function New-HookEntry {
    param([string]$ScriptName)
    return [ordered]@{
        type = 'command'
        command = $powerShellPath
        args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $hookDirectory $ScriptName))
        timeout = 10
    }
}

$governedSettings = [ordered]@{
    hooks = [ordered]@{
        SessionStart = @([ordered]@{ hooks = @((New-HookEntry -ScriptName 'SessionStart-WorkspaceCheck.ps1')) })
        PreToolUse = @(
            [ordered]@{ matcher = 'Edit|Write|NotebookEdit'; hooks = @((New-HookEntry -ScriptName 'PreToolUse-WriteGate.ps1')) },
            [ordered]@{ matcher = 'Bash|PowerShell'; hooks = @((New-HookEntry -ScriptName 'PreToolUse-RefGuard.ps1')) }
        )
    }
}

$settingsPath = Join-Path ([System.IO.Path]::GetTempPath()) ('provost-governed-' + [guid]::NewGuid().ToString('N') + '.json')
[System.IO.File]::WriteAllText($settingsPath, ($governedSettings | ConvertTo-Json -Depth 12), [System.Text.UTF8Encoding]::new($false))

Write-Output ('Governed workspace: ' + $resolvedRoot)
Write-Output 'Tier 2 hooks registered for this session only. Enforcement is confirmed at Initialize, which requires a liveness marker the SessionStart hook writes.'

try {
    Set-Location -LiteralPath $resolvedRoot
    & claude --settings $settingsPath @ClaudeArguments
    $claudeExitCode = $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $settingsPath -Force -ErrorAction SilentlyContinue
}
exit $claudeExitCode
