[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Passed = 0
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$helperPath = Join-Path $repositoryRoot 'docs\governance\reference\Foreman-Manifest.ps1'

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw ('Foreman helper is missing: ' + $helperPath)
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'This reference test requires git.'
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 32), [System.Text.UTF8Encoding]::new($false))
}

function Invoke-Helper {
    param([hashtable]$Parameters)
    & $helperPath @Parameters
}

function Write-Pass {
    param([Parameter(Mandatory = $true)][string]$Label)
    $script:Passed++
    Write-Output ('PASS: ' + $Label)
}

function Assert-ThrowsImmutable {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Label
    )
    try {
        & $Action | Out-Null
    }
    catch {
        if ($_.Exception.Message -notmatch '\[IMMUTABLE\]') { throw }
        Write-Pass -Label $Label
        return
    }
    throw ($Label + ' did not throw [IMMUTABLE].')
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('provost-manifest-pin-' + [guid]::NewGuid().ToString('N'))
$resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
$resolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())

if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create test state outside the system temporary directory.'
}

try {
    [System.IO.Directory]::CreateDirectory((Join-Path $resolvedTemporaryRoot 'src')) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $resolvedTemporaryRoot 'src\sample.txt'), 'baseline', [System.Text.UTF8Encoding]::new($false))
    & git -C $resolvedTemporaryRoot init -q
    if ($LASTEXITCODE -ne 0) { throw 'git init failed.' }
    & git -C $resolvedTemporaryRoot config user.email 'provost-test@example.invalid'
    & git -C $resolvedTemporaryRoot config user.name 'Provost Test'
    & git -C $resolvedTemporaryRoot add -- .
    & git -C $resolvedTemporaryRoot -c commit.gpgsign=false commit -qm 'baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the temporary Git baseline.' }

    $foremanRoot = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot 'manifests\none\sample-change'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $planPath = Join-Path $plans 'sample-plan.md'
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    $manifestPath = Join-Path $manifestDirectory 'r001.json'
    $sessionId = 'provost-manifest-pin'
    $planText = '# Sample plan'
    [System.IO.File]::WriteAllText($planPath, $planText, [System.Text.UTF8Encoding]::new($false))

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

    $initialized = Invoke-Helper -Parameters @{
        Action = 'Initialize'
        DraftPath = $draftPath
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    }
    if ($initialized.status -ne 'INITIALIZED') {
        throw ('Initialize returned ' + $initialized.status + ' instead of INITIALIZED.')
    }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Initialize did not create r001.json.'
    }
    if (Test-Path -LiteralPath $draftPath) {
        throw 'Initialize left the draft in place.'
    }
    $lockPath = Join-Path $foremanRoot 'active-run.lock'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw 'Initialize did not create active-run.lock.'
    }
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    $manifestHash = Get-FileSha256 -Path $manifestPath
    if ([string]$lock.manifest_sha256 -ne $manifestHash) {
        throw 'Lock manifest_sha256 does not match the approved manifest file.'
    }
    if (-not (Test-Path -LiteralPath ([string]$lock.ledger_path) -PathType Leaf)) {
        throw 'Initialize did not create a ledger.'
    }
    Write-Pass -Label 'Initialize created a pinned r001 manifest and lock hash'

    $validated = Invoke-Helper -Parameters @{
        Action = 'Validate'
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
    }
    if ($validated.status -ne 'VALID') {
        throw ('Validate returned ' + $validated.status + ' instead of VALID.')
    }
    Write-Pass -Label 'Validate accepted the unchanged approved manifest'

    [System.IO.File]::WriteAllText($planPath, '# Tampered plan', [System.Text.UTF8Encoding]::new($false))
    Assert-ThrowsImmutable -Label 'Validate rejected a tampered native Plan' -Action {
        Invoke-Helper -Parameters @{
            Action = 'Validate'
            ManifestPath = $manifestPath
            WorkspaceRoot = $resolvedTemporaryRoot
        }
    }
    [System.IO.File]::WriteAllText($planPath, $planText, [System.Text.UTF8Encoding]::new($false))

    $manifestJson = Get-Content -LiteralPath $manifestPath -Raw
    $tampered = $manifestJson -replace '"Sample change"', '"Tampered change"'
    if ($tampered -eq $manifestJson) {
        throw 'Could not tamper the approved manifest title for the hash check.'
    }
    [System.IO.File]::WriteAllText($manifestPath, $tampered, [System.Text.UTF8Encoding]::new($false))
    Assert-ThrowsImmutable -Label 'StartTask rejected a tampered approved manifest via the lock hash' -Action {
        Invoke-Helper -Parameters @{
            Action = 'StartTask'
            ManifestPath = $manifestPath
            WorkspaceRoot = $resolvedTemporaryRoot
            SessionId = $sessionId
            TaskId = 'T01'
        }
    }

    Write-Output ('Manifest-pin checks passed: ' + $script:Passed)
}
finally {
    if (Test-Path -LiteralPath $resolvedTemporaryRoot) {
        if (-not $resolvedTemporaryRoot.StartsWith($resolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to clean up a path outside the system temporary directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
}
