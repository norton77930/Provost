[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Passed = 0
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$hookPath = Join-Path $repositoryRoot 'docs\governance\reference\hooks\PreToolUse-RefGuard.ps1'
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path -LiteralPath $hookPath -PathType Leaf)) {
    throw ('Ref-guard hook is missing: ' + $hookPath)
}
if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
    throw 'This reference test requires Windows PowerShell 5.1.'
}

function Invoke-RefGuard {
    param(
        [string]$Profile,
        [string]$DeclaredRefs,
        [Parameter(Mandatory = $true)][string]$Command
    )

    $previousProfile = $env:PROVOST_SESSION_PROFILE
    $previousRefs = $env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS
    try {
        if ([string]::IsNullOrWhiteSpace($Profile)) {
            Remove-Item -LiteralPath 'Env:PROVOST_SESSION_PROFILE' -ErrorAction SilentlyContinue
        }
        else {
            $env:PROVOST_SESSION_PROFILE = $Profile
        }
        if ([string]::IsNullOrWhiteSpace($DeclaredRefs)) {
            Remove-Item -LiteralPath 'Env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS' -ErrorAction SilentlyContinue
        }
        else {
            $env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS = $DeclaredRefs
        }

        $payload = [ordered]@{
            tool_input = [ordered]@{ command = $Command }
        } | ConvertTo-Json -Compress
        $output = @($payload | & $windowsPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $hookPath)
        if ($LASTEXITCODE -ne 0) {
            throw ('Ref-guard process exited with code ' + $LASTEXITCODE + '.')
        }
        if ($output.Count -eq 0) { return $null }
        return (($output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop)
    }
    finally {
        if ($null -eq $previousProfile) {
            Remove-Item -LiteralPath 'Env:PROVOST_SESSION_PROFILE' -ErrorAction SilentlyContinue
        }
        else {
            $env:PROVOST_SESSION_PROFILE = $previousProfile
        }
        if ($null -eq $previousRefs) {
            Remove-Item -LiteralPath 'Env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS' -ErrorAction SilentlyContinue
        }
        else {
            $env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS = $previousRefs
        }
    }
}

function Assert-Silent {
    param($Decision, [Parameter(Mandatory = $true)][string]$Label)
    if ($null -ne $Decision) {
        throw ($Label + ' unexpectedly returned a hook decision: ' + ($Decision | ConvertTo-Json -Compress -Depth 8))
    }
    $script:Passed++
    Write-Output ('PASS: ' + $Label)
}

function Assert-Decision {
    param(
        $Decision,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($null -eq $Decision -or $null -eq $Decision.hookSpecificOutput) {
        throw ($Label + ' did not return a hook decision.')
    }
    $actual = [string]$Decision.hookSpecificOutput.permissionDecision
    if ($actual -ne $Expected) {
        throw ($Label + ' returned ' + $actual + ' instead of ' + $Expected + '.')
    }
    $script:Passed++
    Write-Output ('PASS: ' + $Label)
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('provost-ref-guard-' + [guid]::NewGuid().ToString('N'))
$resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
$resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())

if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create test state outside the system temporary directory.'
}

try {
    [System.IO.Directory]::CreateDirectory($resolvedTemporaryRoot) | Out-Null
    $refFile = Join-Path $resolvedTemporaryRoot 'a.txt'
    $writeCommand = 'Set-Content -LiteralPath "' + $refFile + '" -Value x'
    $readCommand = 'Get-Content -LiteralPath "' + $refFile + '"'
    $unclassifiedCommand = 'python inspect.py "' + $resolvedTemporaryRoot + '"'

    $decision = Invoke-RefGuard -Profile '' -DeclaredRefs $resolvedTemporaryRoot -Command $writeCommand
    Assert-Silent -Decision $decision -Label 'non-foreman session was silent'

    $decision = Invoke-RefGuard -Profile 'foreman' -DeclaredRefs '' -Command $writeCommand
    Assert-Silent -Decision $decision -Label 'foreman session with no declared refs was silent'

    $decision = Invoke-RefGuard -Profile 'foreman' -DeclaredRefs $resolvedTemporaryRoot -Command $writeCommand
    Assert-Decision -Decision $decision -Expected 'deny' -Label 'write-like command into a declared ref was denied'

    $decision = Invoke-RefGuard -Profile 'foreman' -DeclaredRefs $resolvedTemporaryRoot -Command $readCommand
    Assert-Decision -Decision $decision -Expected 'allow' -Label 'narrow Get-Content against a declared ref was allowed'

    $decision = Invoke-RefGuard -Profile 'foreman' -DeclaredRefs $resolvedTemporaryRoot -Command $unclassifiedCommand
    Assert-Decision -Decision $decision -Expected 'ask' -Label 'unclassified command that mentions a ref asked for review'

    # The launcher joins declared roots into one variable and this hook splits
    # them apart again. A mismatch shipped once: the launcher used the platform
    # path separator while the hook splits on '|', so with two roots the guard
    # matched nothing and stayed silent on every command — the silent fail-open
    # the governed tier exists to prevent.
    # A sibling, not a child: nested under the first root, the command would name
    # the first root too and the deny could come from matching that instead.
    $secondRoot = $resolvedTemporaryRoot + '-second'
    [System.IO.Directory]::CreateDirectory($secondRoot) | Out-Null
    $secondRootFile = Join-Path $secondRoot 'b.txt'
    $secondWriteCommand = 'Set-Content -LiteralPath "' + $secondRootFile + '" -Value x'
    $joinedRoots = @($resolvedTemporaryRoot, $secondRoot) -join '|'
    $decision = Invoke-RefGuard -Profile 'foreman' -DeclaredRefs $joinedRoots -Command $secondWriteCommand
    Assert-Decision -Decision $decision -Expected 'deny' -Label 'write into the second of several declared refs was denied'

    $launcherPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\docs\governance\reference\Start-GovernedSession.ps1'))
    $launcherText = [System.IO.File]::ReadAllText($launcherPath)
    if ($launcherText -notmatch "ExternalReadRoots\s+-join\s+'\|'") {
        throw 'Start-GovernedSession.ps1 no longer joins declared read roots on the "|" this hook splits on.'
    }
    $script:Passed++
    Write-Output ('PASS: The launcher joins declared refs on the separator this hook splits on')

    Write-Output ('Ref-guard checks passed: ' + $script:Passed)
}
finally {
    if (Test-Path -LiteralPath $resolvedTemporaryRoot) {
        if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to clean up a path outside the system temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath ($resolvedTemporaryRoot + '-second')) {
        Remove-Item -LiteralPath ($resolvedTemporaryRoot + '-second') -Recurse -Force
    }
}
