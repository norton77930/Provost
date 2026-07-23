# Provost Foreman SessionStart hook: warn loudly when the session location has
# drifted away from the governed workspace root. Informational only (SessionStart
# cannot block) and fail-open by design: any uncertainty exits 0 in silence.
# Self-gated: inert unless the launching wrapper marked this session as foreman.
if ($env:PROVOST_SESSION_PROFILE -ne 'foreman') { exit 0 }

try {
    $expectedRoot = $env:PROVOST_FOREMAN_WORKSPACE_ROOT
    if ([string]::IsNullOrWhiteSpace($expectedRoot)) { exit 0 }

    $rawInput = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($rawInput)) { exit 0 }
    $payload = $rawInput | ConvertFrom-Json -ErrorAction Stop
    $actualLocation = [string]$payload.cwd
    if ([string]::IsNullOrWhiteSpace($actualLocation)) { exit 0 }

    $normalizedExpected = [System.IO.Path]::GetFullPath($expectedRoot).TrimEnd([char]92, [char]47)
    $normalizedActual = [System.IO.Path]::GetFullPath($actualLocation).TrimEnd([char]92, [char]47)
    if ([string]::Equals($normalizedExpected, $normalizedActual, [System.StringComparison]::OrdinalIgnoreCase)) { exit 0 }

    $warning = @(
        'PROVOST-FOREMAN WORKSPACE MISMATCH (engine check).',
        ('Expected workspace root: ' + $normalizedExpected),
        ('Actual session location: ' + $normalizedActual),
        'Stop immediately: treat nothing as approved, do not run lifecycle helper actions, and tell the user to exit this session, Set-Location to the expected workspace root, then relaunch provost-foreman (use --resume only to reattach to an ACTIVE Foreman lock).'
    ) -join ' '
    $response = @{ hookSpecificOutput = @{ hookEventName = 'SessionStart'; additionalContext = $warning } }
    Write-Output ($response | ConvertTo-Json -Depth 6 -Compress)
    exit 0
}
catch {
    exit 0
}
