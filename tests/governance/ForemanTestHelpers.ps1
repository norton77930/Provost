# Shared helpers for Foreman lifecycle tests. Dot-source from Test-*.ps1.
# $script: state here belongs to this file, not the caller.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ForemanTest = @{
    Passed = 0
    LastThrowMessage = $null
    ManifestPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\docs\governance\reference\Foreman-Manifest.ps1'))
}

if (-not (Test-Path -LiteralPath $script:ForemanTest.ManifestPath -PathType Leaf)) {
    throw ('Foreman helper is missing: ' + $script:ForemanTest.ManifestPath)
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'This reference test requires git.'
}

function Initialize-ForemanTest {
    $script:ForemanTest.Passed = 0
    $script:ForemanTest.LastThrowMessage = $null
}

function Write-Pass {
    param([Parameter(Mandatory = $true)][string]$Label)
    $script:ForemanTest.Passed++
    Write-Output ('PASS: ' + $Label)
}

function Get-ForemanTestPassCount {
    return [int]$script:ForemanTest.Passed
}

function Get-ForemanLastThrowMessage {
    return [string]$script:ForemanTest.LastThrowMessage
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 32), [System.Text.UTF8Encoding]::new($false))
}

function Invoke-ForemanHelper {
    param([hashtable]$Parameters)
    & $script:ForemanTest.ManifestPath @Parameters
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-ThrowsCode {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Label
    )
    try {
        & $Action | Out-Null
    }
    catch {
        if ($_.Exception.Message -notmatch ('\[' + [regex]::Escape($Code) + '\]')) { throw }
        $script:ForemanTest.LastThrowMessage = $_.Exception.Message
        Write-Pass -Label $Label
        return
    }
    throw ($Label + ' did not throw [' + $Code + '].')
}

function New-IsolatedTempRoot {
    param([Parameter(Mandatory = $true)][string]$Prefix)
    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ($Prefix + [guid]::NewGuid().ToString('N'))
    $resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
    $resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to create test state outside the system temporary directory.'
    }
    return $resolvedTemporaryRoot
}

function Remove-IsolatedTempRoot {
    param([Parameter(Mandatory = $true)][string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root)
    if (-not $resolvedRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to clean up a path outside the system temporary directory.'
    }
    # A file written moments earlier can still be held briefly by an external
    # scanner, which surfaces here as an IOException on the recursive delete.
    # It reproduces only when the whole suite runs back to back, never when a
    # check runs on its own. Cleanup is not a governance result, so a run whose
    # assertions all passed must not be reported as a failure because of it.
    $retryDelaysMs = @(50, 150, 400, 1000)
    for ($attempt = 0; ; $attempt++) {
        try {
            Remove-Item -LiteralPath $resolvedRoot -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -ge $retryDelaysMs.Count) { throw }
            Start-Sleep -Milliseconds $retryDelaysMs[$attempt]
        }
    }
}

function New-GitWorkspace {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativeFile,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $filePath = Join-Path $Root ($RelativeFile -replace '/', [string][System.IO.Path]::DirectorySeparatorChar)
    $directory = Split-Path -Parent $filePath
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    [System.IO.File]::WriteAllText($filePath, $Content, [System.Text.UTF8Encoding]::new($false))
    & git -C $Root init -q
    if ($LASTEXITCODE -ne 0) { throw 'git init failed.' }
    & git -C $Root config user.email 'provost-test@example.invalid'
    & git -C $Root config user.name 'Provost Test'
    & git -C $Root add -- .
    & git -C $Root -c commit.gpgsign=false commit -qm 'baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the temporary Git baseline.' }
}
