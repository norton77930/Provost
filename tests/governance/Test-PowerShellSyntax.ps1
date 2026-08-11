[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$roots = @(
    (Join-Path $repositoryRoot 'docs\governance\reference'),
    (Join-Path $repositoryRoot 'tests')
)
$scripts = @($roots | ForEach-Object { Get-ChildItem -LiteralPath $_ -Recurse -File -Filter '*.ps1' })
$failures = @()

foreach ($scriptFile in $scripts) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $scriptFile.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null

    foreach ($parseError in @($parseErrors)) {
        $failures += ($scriptFile.FullName + ':' + $parseError.Extent.StartLineNumber + ' ' + $parseError.Message)
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Output ('PowerShell syntax checks passed: ' + $scripts.Count + ' scripts')
