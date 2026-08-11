[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$markdownFiles = @(Get-ChildItem -LiteralPath $repositoryRoot -Recurse -File -Filter '*.md' |
    Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' })
$failures = @()
$checkedLinks = 0
$linkPattern = '(?<!!)\[[^\]]+\]\((?<target>[^)\s]+)'

foreach ($markdownFile in $markdownFiles) {
    $content = [System.IO.File]::ReadAllText($markdownFile.FullName)
    foreach ($match in [regex]::Matches($content, $linkPattern)) {
        $target = [string]$match.Groups['target'].Value
        $target = $target.Trim('<', '>')
        if ($target -match '^(?i:https?://|mailto:|#)') { continue }

        $pathPart = ($target -split '#', 2)[0]
        if ([string]::IsNullOrWhiteSpace($pathPart)) { continue }

        $checkedLinks++
        $decodedPath = [System.Uri]::UnescapeDataString($pathPart)
        $resolvedPath = [System.IO.Path]::GetFullPath((Join-Path $markdownFile.DirectoryName $decodedPath))
        if (-not (Test-Path -LiteralPath $resolvedPath)) {
            $relativeFile = $markdownFile.FullName.Substring($repositoryRoot.Length).TrimStart([char]92, [char]47)
            $failures += ($relativeFile + ': missing target ' + $target)
        }
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Output ('Markdown link checks passed: ' + $checkedLinks + ' relative links')
