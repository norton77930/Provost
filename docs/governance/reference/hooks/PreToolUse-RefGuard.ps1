# Provost Foreman PreToolUse hook for Bash/PowerShell: external read roots
# declared via --ref are read-only, but permission deny rules only cover the
# Edit/Write tools -- a shell command can still write into a ref. This hook
# closes that gap: write-like ref command => deny; narrow pure-read => allow;
# composed or unclassified ref command => ask.
# Self-gated: inert unless the launching wrapper marked this session as foreman.
if ($env:PROVOST_SESSION_PROFILE -ne 'foreman') { exit 0 }

$declaredRefs = $env:PROVOST_FOREMAN_EXTERNAL_READ_ROOTS
if ([string]::IsNullOrWhiteSpace($declaredRefs)) { exit 0 }

function Send-PreToolUseDecision {
    param([string]$Decision, [string]$Reason)
    $response = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; permissionDecision = $Decision; permissionDecisionReason = $Reason } }
    Write-Output ($response | ConvertTo-Json -Depth 6 -Compress)
    exit 0
}

$payload = $null
try {
    $rawInput = [Console]::In.ReadToEnd()
    if (-not [string]::IsNullOrWhiteSpace($rawInput)) {
        $payload = $rawInput | ConvertFrom-Json -ErrorAction Stop
    }
}
catch {
    $payload = $null
}
if ($null -eq $payload -or $null -eq $payload.tool_input) {
    Send-PreToolUseDecision -Decision 'ask' -Reason 'Foreman ref-guard could not parse the tool input; confirm manually that this command does not write into a read-only --ref directory.'
}

$commandText = [string]$payload.tool_input.command
if ([string]::IsNullOrWhiteSpace($commandText)) {
    $commandText = [string]($payload.tool_input | ConvertTo-Json -Depth 16 -Compress)
}

$mentionsRef = $false
foreach ($refRoot in ($declaredRefs -split '\|')) {
    if ([string]::IsNullOrWhiteSpace($refRoot)) { continue }
    $spellings = @($refRoot, ($refRoot -replace '\\', '/'))
    foreach ($spelling in $spellings) {
        if ($commandText.IndexOf($spelling, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { $mentionsRef = $true; break }
    }
    if ($mentionsRef) { break }
}
if (-not $mentionsRef) { exit 0 }

$writeTokenPattern = '(?i)(\brm\b|\bdel\b|\berase\b|\bmv\b|\bmove\b|\bcp\b|\bcopy\b|\btee\b|\btouch\b|\bmkdir\b|\brmdir\b|\brd\b|\bsed\s+-i|>|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bNew-Item\b|\bRemove-Item\b|\bMove-Item\b|\bCopy-Item\b|\bRename-Item\b|\bClear-Content\b)'
$gitMutationPattern = '(?i)^\s*git(?:\.exe)?\s+(?:(?:-C|--git-dir|--work-tree)\s+(?:"[^"]*"|\S+)\s+|--no-pager\s+)*(?:apply|checkout|restore|reset|clean|switch|commit|merge|rebase|cherry-pick)\b'
if ($commandText -match $writeTokenPattern -or $commandText -match $gitMutationPattern) {
    Send-PreToolUseDecision -Decision 'deny' -Reason 'External --ref roots are read-only and this command mixes a ref path with a write-like token (redirection, file mutation, Git mutation, or copy/move). Split ref reads into their own command without redirections, or drop the write entirely.'
}

$hasCommandComposition = $commandText -match '[\r\n;&|`$(){}]'
$pureReadPattern = '(?i)^\s*(?:Get-Content|Test-Path|Get-ChildItem|Get-FileHash|Select-String|rg(?:\.exe)?|ripgrep(?:\.exe)?|ls|stat|readlink|realpath|pwd)\b'
$safeGitReadPattern = '(?i)^\s*git(?:\.exe)?\s+(?:(?:-C|--git-dir|--work-tree)\s+(?:"[^"]*"|\S+)\s+|--no-pager\s+)*(?:status|diff|log|show|rev-parse|ls-files|grep)\b'
if (-not $hasCommandComposition -and ($commandText -match $pureReadPattern -or $commandText -match $safeGitReadPattern)) {
    Send-PreToolUseDecision -Decision 'allow' -Reason 'Foreman ref-guard classified this as a narrow pure-read command against an external --ref root.'
}

Send-PreToolUseDecision -Decision 'ask' -Reason 'This command references a read-only --ref directory but is not in the narrow pure-read allowlist. Confirm it only reads from the ref before approving.'
