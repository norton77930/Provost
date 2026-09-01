[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$agentDirectory = Join-Path $repositoryRoot 'agents'
$requiredKeys = @('name', 'description', 'tools', 'model')
$modelAliases = @('haiku', 'sonnet', 'opus', 'fable', 'inherit', 'default')
$readmeNames = @('README.md', 'README.zh-TW.md')

$failures = @()
$declaredModels = @{}

$agentFiles = @(Get-ChildItem -LiteralPath $agentDirectory -File -Filter '*.md' | Sort-Object Name)
if ($agentFiles.Count -eq 0) {
    Write-Error 'No role definitions found under agents/.'
    exit 1
}

foreach ($agentFile in $agentFiles) {
    $relativeFile = 'agents/' + $agentFile.Name
    $lines = @([System.IO.File]::ReadAllLines($agentFile.FullName))

    if ($lines.Count -eq 0 -or $lines[0].Trim() -ne '---') {
        $failures += ($relativeFile + ': missing frontmatter opening delimiter')
        continue
    }

    $closingIndex = -1
    for ($index = 1; $index -lt $lines.Count; $index++) {
        if ($lines[$index].Trim() -eq '---') { $closingIndex = $index; break }
    }
    if ($closingIndex -lt 0) {
        $failures += ($relativeFile + ': missing frontmatter closing delimiter')
        continue
    }

    $frontmatter = @{}
    for ($index = 1; $index -lt $closingIndex; $index++) {
        if ($lines[$index] -match '^(?<key>[A-Za-z][A-Za-z0-9_-]*):\s*(?<value>.*)$') {
            $frontmatter[[string]$Matches['key']] = ([string]$Matches['value']).Trim()
        }
    }

    foreach ($requiredKey in $requiredKeys) {
        if (-not $frontmatter.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($frontmatter[$requiredKey])) {
            $failures += ($relativeFile + ': frontmatter is missing required key ' + $requiredKey)
        }
    }

    if (-not $frontmatter.ContainsKey('name')) { continue }
    $roleName = $frontmatter['name']

    $expectedName = [System.IO.Path]::GetFileNameWithoutExtension($agentFile.Name)
    if ($roleName -ne $expectedName) {
        $failures += ($relativeFile + ": name '" + $roleName + "' does not match file name '" + $expectedName + "'")
    }

    if ($frontmatter.ContainsKey('model')) {
        $model = $frontmatter['model']
        if ($modelAliases -notcontains $model -and $model -notmatch '^claude-[a-z0-9-]+$') {
            $failures += ($relativeFile + ": model '" + $model +
                "' is neither a Claude Code alias (" + ($modelAliases -join ', ') + ") nor a claude-* model id")
        }
        $declaredModels[$roleName] = $model
    }
}

# The README role tables are a published contract; drift between them and the
# shipped frontmatter is what this check exists to catch.
$rowPattern = '^\|(?<roles>[^|]+)\|(?<models>[^|]+)\|'
$checkedCells = 0

foreach ($readmeName in $readmeNames) {
    $readmePath = Join-Path $repositoryRoot $readmeName
    if (-not (Test-Path -LiteralPath $readmePath)) {
        $failures += ($readmeName + ': file not found')
        continue
    }

    $documentedRoles = @{}
    foreach ($line in @([System.IO.File]::ReadAllLines($readmePath))) {
        if ($line -notmatch $rowPattern) { continue }

        $roleCell = ([string]$Matches['roles']).Replace('`', '')
        $modelCell = [string]$Matches['models']
        $roles = @($roleCell -split '/' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
        if ($roles.Count -eq 0) { continue }

        $unknown = @($roles | Where-Object { -not $declaredModels.ContainsKey($_) })
        if ($unknown.Count -gt 0) { continue }

        $models = @($modelCell -split '/' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
        if ($models.Count -eq 1 -and $roles.Count -gt 1) {
            $models = @($roles | ForEach-Object { $models[0] })
        }
        if ($models.Count -ne $roles.Count) {
            $failures += ($readmeName + ": role row '" + $roleCell.Trim() +
                "' lists " + $roles.Count + ' role(s) but ' + $models.Count + ' model(s)')
            continue
        }

        for ($index = 0; $index -lt $roles.Count; $index++) {
            $documentedRoles[$roles[$index]] = $true
            $checkedCells++
            if ($declaredModels[$roles[$index]] -ne $models[$index]) {
                $failures += ($readmeName + ": role '" + $roles[$index] + "' is documented as '" +
                    $models[$index] + "' but agents/ declares '" + $declaredModels[$roles[$index]] + "'")
            }
        }
    }

    foreach ($roleName in ($declaredModels.Keys | Sort-Object)) {
        if (-not $documentedRoles.ContainsKey($roleName)) {
            $failures += ($readmeName + ": role '" + $roleName + "' is shipped but absent from the role table")
        }
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ -ErrorAction Continue }
    exit 1
}

Write-Output ('Role definition checks passed: ' + $agentFiles.Count + ' roles, ' + $checkedCells + ' documented model cells')
