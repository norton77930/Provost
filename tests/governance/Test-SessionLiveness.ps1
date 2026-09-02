[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ForemanTestHelpers.ps1')
Initialize-ForemanTest

$resolvedTemporaryRoot = New-IsolatedTempRoot -Prefix 'provost-session-liveness-'
$savedProfile = $env:PROVOST_SESSION_PROFILE
$savedSessionId = $env:CLAUDE_CODE_SESSION_ID

try {
    New-GitWorkspace -Root $resolvedTemporaryRoot -RelativeFile 'src/sample.txt' -Content 'baseline'

    $foremanRoot = Join-Path $resolvedTemporaryRoot '.claude\provost\foreman'
    $plans = Join-Path $foremanRoot 'plans'
    $manifestDirectory = Join-Path $foremanRoot 'manifests\none\sample-change'
    [System.IO.Directory]::CreateDirectory($plans) | Out-Null
    [System.IO.Directory]::CreateDirectory($manifestDirectory) | Out-Null
    $planPath = Join-Path $plans 'sample-plan.md'
    $draftPath = Join-Path $manifestDirectory 'r001.draft.json'
    $manifestPath = Join-Path $manifestDirectory 'r001.json'
    $markerPath = Join-Path $foremanRoot 'session-liveness.json'
    $sessionId = 'provost-session-liveness'
    [System.IO.File]::WriteAllText($planPath, '# Sample plan', [System.Text.UTF8Encoding]::new($false))

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

    $initializeParameters = @{
        Action = 'Initialize'
        DraftPath = $draftPath
        ManifestPath = $manifestPath
        WorkspaceRoot = $resolvedTemporaryRoot
        SessionId = $sessionId
    }

    # A session that calls itself governed must prove the hooks are live before a
    # run begins. Without this the write gate can be absent and nothing says so.
    $env:PROVOST_SESSION_PROFILE = 'foreman'
    $env:CLAUDE_CODE_SESSION_ID = [guid]::NewGuid().ToString()

    Assert-ThrowsCode -Code 'ENFORCEMENT' -Label 'Initialize refuses a governed session with no liveness marker' -Action {
        Invoke-ForemanHelper -Parameters $initializeParameters
    }

    Write-JsonFile -Path $markerPath -Value ([ordered]@{
        session_id = [guid]::NewGuid().ToString()
        written_utc = [DateTime]::UtcNow.ToString('o')
    })
    Assert-ThrowsCode -Code 'ENFORCEMENT' -Label 'Initialize refuses a liveness marker left by another session' -Action {
        Invoke-ForemanHelper -Parameters $initializeParameters
    }

    # The marker is only worth checking if the hook that writes it works. Hand
    # writing it here would leave the producer half uncovered: a change to the
    # hook's path or payload handling would refuse every real session while this
    # suite stayed green. Drive the real hook instead.
    Remove-Item -LiteralPath $markerPath -Force
    $sessionStartHook = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\docs\governance\reference\hooks\SessionStart-WorkspaceCheck.ps1'))
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $hookPayload = [ordered]@{
        session_id = [string]$env:CLAUDE_CODE_SESSION_ID
        cwd = $resolvedTemporaryRoot
    } | ConvertTo-Json -Compress

    $previousWorkspaceRoot = $env:PROVOST_FOREMAN_WORKSPACE_ROOT
    try {
        $env:PROVOST_FOREMAN_WORKSPACE_ROOT = $resolvedTemporaryRoot
        $hookPayload | & $windowsPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $sessionStartHook | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw ('SessionStart hook exited with code ' + $LASTEXITCODE + '.')
        }
    }
    finally {
        $env:PROVOST_FOREMAN_WORKSPACE_ROOT = $previousWorkspaceRoot
    }

    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw ('The SessionStart hook wrote no liveness marker at ' + $markerPath + '.')
    }
    $writtenMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    if ([string]$writtenMarker.session_id -ne [string]$env:CLAUDE_CODE_SESSION_ID) {
        throw ('The SessionStart hook recorded session ' + [string]$writtenMarker.session_id + ' instead of this one.')
    }
    Write-Pass -Label 'The SessionStart hook writes a marker naming its own session'

    Invoke-ForemanHelper -Parameters $initializeParameters | Out-Null
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Initialize did not approve the manifest when the marker matched.'
    }
    Write-Pass -Label 'Initialize accepts the marker the hook itself wrote'

    # The launcher is the only thing that sets these and the hooks the only
    # things that read them. Nothing else joins the two, so a hook that started
    # reading a new variable would read empty and stand down in silence.
    $referenceDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\docs\governance\reference'))
    $launcherText = [System.IO.File]::ReadAllText((Join-Path $referenceDirectory 'Start-GovernedSession.ps1'))
    $assigned = @([regex]::Matches($launcherText, '\$env:(PROVOST_[A-Z_]+)\s*=') |
        ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)

    $readByHooks = @(Get-ChildItem -LiteralPath (Join-Path $referenceDirectory 'hooks') -File -Filter '*.ps1' |
        ForEach-Object { [regex]::Matches([System.IO.File]::ReadAllText($_.FullName), '\$env:(PROVOST_[A-Z_]+)') } |
        ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)

    if ($readByHooks.Count -eq 0) {
        throw 'Found no PROVOST_* reads in the hooks; this check is not looking at the right files.'
    }
    $unset = @($readByHooks | Where-Object { $assigned -notcontains $_ })
    if ($unset.Count -gt 0) {
        throw ('The hooks read ' + ($unset -join ', ') + ', which the launcher never sets.')
    }
    Write-Pass -Label ('The launcher sets every PROVOST_* variable the hooks read (' + $readByHooks.Count + ')')

    Write-Output ('Session-liveness checks passed: ' + (Get-ForemanTestPassCount))
}
finally {
    $env:PROVOST_SESSION_PROFILE = $savedProfile
    $env:CLAUDE_CODE_SESSION_ID = $savedSessionId
    Remove-IsolatedTempRoot -Root $resolvedTemporaryRoot
}
