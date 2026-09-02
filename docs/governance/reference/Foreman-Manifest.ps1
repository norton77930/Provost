[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
[ValidateSet('Initialize', 'Validate', 'StartTask', 'FinishTask', 'RecordRetry', 'Complete', 'CloseBlocked', 'RecoverLock')]
    [string]$Action,
    [string]$DraftPath,
    [string]$ManifestPath,
    [string]$WorkspaceRoot,
    [string]$SessionId,
    [string]$TaskId,
    [string]$ChangedFilesJson,
    [string]$VerificationSummary,
    [ValidateSet('http_429', 'http_5xx', 'connection', 'stream_interrupted')]
    [string]$RetryKind,
    [ValidateSet('PASS', 'FAIL', 'BLOCKED', 'ESCALATE')]
    [string]$Outcome,
    [string]$FailureSignatureJson,
    [switch]$Acknowledge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:RoleCatalog = [ordered]@{
    'Explore' = [ordered]@{ model = 'sonnet'; effort = 'medium'; writer = $false }
    'foreman-implementer' = [ordered]@{ model = 'sonnet'; effort = 'high'; writer = $true }
    'foreman-deep-implementer' = [ordered]@{ model = 'opus'; effort = 'xhigh'; writer = $true }
    'foreman-test-analyst' = [ordered]@{ model = 'sonnet'; effort = 'medium'; writer = $false }
    'foreman-verifier' = [ordered]@{ model = 'opus'; effort = 'high'; writer = $false }
    'foreman-architecture-verifier' = [ordered]@{ model = 'opus'; effort = 'high'; writer = $false }
}

function Stop-Foreman {
    param([Parameter(Mandatory = $true)][string]$Code, [Parameter(Mandatory = $true)][string]$Message)
    throw ('[' + $Code + '] ' + $Message)
}

function ConvertTo-ForemanValue {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $result = [ordered]@{}
        foreach ($key in $Value.Keys) { $result[[string]$key] = ConvertTo-ForemanValue -Value $Value[$key] }
        return $result
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $result = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) { $result[$property.Name] = ConvertTo-ForemanValue -Value $property.Value }
        return $result
    }
    if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        $items = @()
        foreach ($item in $Value) { $items += ,(ConvertTo-ForemanValue -Value $item) }
        return ,$items
    }
    return $Value
}

function Get-RequiredMapValue {
    param([System.Collections.IDictionary]$Map, [string]$Name, [string]$Context)
    if (-not $Map.Contains($Name)) { Stop-Foreman -Code 'SCHEMA' -Message ($Context + ' is missing ' + $Name + '.') }
    return $Map[$Name]
}

function Assert-Map {
    param($Value, [string]$Context)
    if (-not ($Value -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'SCHEMA' -Message ($Context + ' must be a JSON object.') }
}

function Assert-String {
    param($Value, [string]$Context)
    if (-not ($Value -is [string]) -or [string]::IsNullOrWhiteSpace([string]$Value)) { Stop-Foreman -Code 'SCHEMA' -Message ($Context + ' must be a non-empty string.') }
    return [string]$Value
}

function Assert-StringArray {
    param($Value, [string]$Context)
    if ($null -eq $Value) { return @() }
    $items = @($Value)
    foreach ($item in $items) { [void](Assert-String -Value $item -Context $Context) }
    return $items
}

function Assert-Boolean {
    param($Value, [string]$Context)
    if (-not ($Value -is [bool])) { Stop-Foreman -Code 'SCHEMA' -Message ($Context + ' must be a Boolean.') }
    return [bool]$Value
}

function Test-ForemanSecretProperty {
    param($Value, [string]$Path = 'manifest')
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            $name = [string]$key
            if ($name -match '(?i)(token|secret|authorization|auth|api.?key|password|cookie|credential)') { Stop-Foreman -Code 'SCHEMA' -Message ($Path + ' contains a forbidden secret-like property: ' + $name + '.') }
            Test-ForemanSecretProperty -Value $Value[$key] -Path ($Path + '.' + $name)
        }
    }
    elseif (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        $index = 0
        foreach ($item in $Value) { Test-ForemanSecretProperty -Value $item -Path ($Path + '[' + $index + ']'); $index++ }
    }
}

function Assert-ForemanSafeText {
    param([AllowNull()][string]$Text, [Parameter(Mandatory = $true)][string]$Context)

    if ([string]::IsNullOrWhiteSpace($Text)) { return }
    $secretPattern = '(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\b(?:access|refresh)[_-]?token\s*[:=]|\bauthorization\s*[:=]|\bbearer\s+[A-Za-z0-9._~-]{12,}|\bchatgpt(?:[_ -]?session)?\s*[:=])'
    if ($Text -match $secretPattern) {
        Stop-Foreman -Code 'SCHEMA' -Message ($Context + ' appears to contain credential material and cannot be written to the Foreman ledger.')
    }
}

function Test-ForemanRoleCatalogOverride {
    param($Value, [string]$Path = 'manifest')
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            $name = [string]$key
            if ($name -match '(?i)^(model|effort)$') {
                Stop-Foreman -Code 'SCHEMA' -Message ($Path + ' cannot override role catalog model or effort.')
            }
            Test-ForemanRoleCatalogOverride -Value $Value[$key] -Path ($Path + '.' + $name)
        }
    }
    elseif (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        $index = 0
        foreach ($item in $Value) { Test-ForemanRoleCatalogOverride -Value $item -Path ($Path + '[' + $index + ']'); $index++ }
    }
}

function Get-NormalizedRoot {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { Stop-Foreman -Code 'PATH' -Message 'WorkspaceRoot is required.' }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([char]92, [char]47)
}

function Get-ForemanLifecycleLeaseName {
    param([Parameter(Mandatory = $true)][string]$Root)

    $normalizedRoot = (Get-NormalizedRoot -Path $Root).ToUpperInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalizedRoot)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $sha256.Dispose()
    }
    return ('Local\ProvostForemanLifecycle-' + $hash.Substring(0, 32))
}

function Enter-ForemanLifecycleLease {
    param([Parameter(Mandatory = $true)][string]$Root)

    $lease = [System.Threading.Mutex]::new($false, (Get-ForemanLifecycleLeaseName -Root $Root))
    try {
        try {
            $owned = $lease.WaitOne(15000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $owned = $true
        }
        if (-not $owned) {
            Stop-Foreman -Code 'LIFECYCLE_LOCK' -Message 'Another Foreman lifecycle operation is active for this worktree. Retry after it completes.'
        }
        return $lease
    }
    catch {
        $lease.Dispose()
        throw
    }
}

function Exit-ForemanLifecycleLease {
    param([AllowNull()][System.Threading.Mutex]$Lease)

    if ($null -eq $Lease) { return }
    try { $Lease.ReleaseMutex() } catch { }
    $Lease.Dispose()
}

function Resolve-WorkspaceChild {
    param([string]$Root, [string]$RelativePath, [string]$Context)
    $relative = Assert-String -Value $RelativePath -Context $Context
    if ([System.IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') { Stop-Foreman -Code 'PATH' -Message ($Context + ' must stay inside the workspace.') }
    $normalizedRoot = Get-NormalizedRoot -Path $Root
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $normalizedRoot ($relative -replace '/', '\\')))
    $prefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { Stop-Foreman -Code 'PATH' -Message ($Context + ' escapes the workspace.') }
    return $candidate
}

function Assert-LiteralWritePath {
    param([string]$Path, [string]$Context)
    $value = Assert-String -Value $Path -Context $Context
    if ([System.IO.Path]::IsPathRooted($value) -or $value -match '(^|[\\/])\.\.([\\/]|$)' -or $value -match '[*?\[]') { Stop-Foreman -Code 'PATH' -Message ($Context + ' must be a repository-relative literal path.') }
    $normalized = ($value -replace '\\', '/').TrimStart('/')
    if ($normalized -match '(?i)(^|/)\.git(/|$)' -or $normalized -match '(?i)(^|/)\.claude/provost/foreman(/|$)') { Stop-Foreman -Code 'PATH' -Message ($Context + ' cannot include Git metadata or the private Foreman directory at any path segment.') }
    return $normalized
}

function Get-ForemanRoot {
    param([string]$Root)
    return Join-Path (Get-NormalizedRoot -Path $Root) '.claude\provost\foreman'
}

function Get-Sha256 {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Foreman -Code 'PATH' -Message ('Required file does not exist: ' + $Path) }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function ConvertTo-CanonicalForemanValue {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $result = [ordered]@{}
        foreach ($key in @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            $result[$key] = ConvertTo-CanonicalForemanValue -Value $Value[$key]
        }
        return $result
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        return ConvertTo-CanonicalForemanValue -Value (ConvertTo-ForemanValue -Value $Value)
    }
    if (($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])) {
        $items = @()
        foreach ($item in $Value) { $items += ,(ConvertTo-CanonicalForemanValue -Value $item) }
        return ,$items
    }
    return $Value
}

function Get-JsonValueSha256 {
    param([AllowNull()]$Value)
    $canonical = ConvertTo-CanonicalForemanValue -Value $Value
    $json = ConvertTo-Json -InputObject $canonical -Depth 64 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try { return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha256.Dispose() }
}

function ConvertTo-NormalizedFailureText {
    param([string]$Text)
    return (($Text -replace '\s+', ' ').Trim()).ToLowerInvariant()
}

function Get-FailureSignatureEvidence {
    param([AllowNull()][string]$Json)
    if ([string]::IsNullOrWhiteSpace($Json)) { return $null }

    $parsed = $null
    try { $parsed = ConvertTo-ForemanValue -Value (ConvertFrom-Json -InputObject $Json -ErrorAction Stop) }
    catch { Stop-Foreman -Code 'SCHEMA' -Message ('Unable to parse FailureSignatureJson: ' + $_.Exception.Message) }
    Assert-Map -Value $parsed -Context 'FailureSignatureJson'
    Test-ForemanSecretProperty -Value $parsed -Path 'FailureSignatureJson'

    $command = Assert-String -Value (Get-RequiredMapValue -Map $parsed -Name 'command' -Context 'FailureSignatureJson') -Context 'FailureSignatureJson.command'
    $status = Assert-String -Value (Get-RequiredMapValue -Map $parsed -Name 'status' -Context 'FailureSignatureJson') -Context 'FailureSignatureJson.status'
    $errorSummary = Assert-String -Value (Get-RequiredMapValue -Map $parsed -Name 'error_summary' -Context 'FailureSignatureJson') -Context 'FailureSignatureJson.error_summary'
    Assert-ForemanSafeText -Text $command -Context 'FailureSignatureJson.command'
    Assert-ForemanSafeText -Text $status -Context 'FailureSignatureJson.status'
    Assert-ForemanSafeText -Text $errorSummary -Context 'FailureSignatureJson.error_summary'

    $environment = [ordered]@{}
    if ($parsed.Contains('environment') -and $null -ne $parsed['environment']) {
        Assert-Map -Value $parsed['environment'] -Context 'FailureSignatureJson.environment'
        foreach ($key in $parsed['environment'].Keys) {
            $originalKey = [string]$key
            $normalizedKey = $originalKey.Trim().ToLowerInvariant()
            if ([string]::IsNullOrWhiteSpace($normalizedKey)) { Stop-Foreman -Code 'SCHEMA' -Message 'FailureSignatureJson.environment keys must be non-empty strings.' }
            if ($environment.Contains($normalizedKey)) { Stop-Foreman -Code 'SCHEMA' -Message ('FailureSignatureJson.environment contains duplicate normalized key: ' + $normalizedKey) }
            $value = $parsed['environment'][$key]
            if ($value -is [string]) {
                Assert-ForemanSafeText -Text ([string]$value) -Context ('FailureSignatureJson.environment.' + $originalKey)
                $environment[$normalizedKey] = ConvertTo-NormalizedFailureText -Text ([string]$value)
            }
            elseif ($null -eq $value -or $value -is [bool] -or $value -is [byte] -or $value -is [int16] -or $value -is [int32] -or $value -is [int64] -or $value -is [single] -or $value -is [double] -or $value -is [decimal]) {
                $environment[$normalizedKey] = $value
            }
            else {
                Stop-Foreman -Code 'SCHEMA' -Message ('FailureSignatureJson.environment.' + $originalKey + ' must be a scalar JSON value.')
            }
        }
    }

    $signature = [ordered]@{
        schema = 'provost-foreman-failure-signature/v1'
        command = ConvertTo-NormalizedFailureText -Text $command
        status = ConvertTo-NormalizedFailureText -Text $status
        error_summary = ConvertTo-NormalizedFailureText -Text $errorSummary
        environment = $environment
    }
    return [ordered]@{
        signature = $signature
        sha256 = Get-JsonValueSha256 -Value $signature
    }
}

function Get-ManifestDiagnosisEvidence {
    param([System.Collections.IDictionary]$Approval)
    if (-not $Approval.Contains('diagnosis') -or $null -eq $Approval['diagnosis']) { return $null }

    $diagnosis = $Approval['diagnosis']
    Assert-Map -Value $diagnosis -Context 'manifest.approval.diagnosis'
    $signatureHash = Assert-String -Value (Get-RequiredMapValue -Map $diagnosis -Name 'failure_signature_sha256' -Context 'manifest.approval.diagnosis') -Context 'manifest.approval.diagnosis.failure_signature_sha256'
    if ($signatureHash -notmatch '^[a-fA-F0-9]{64}$') { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.approval.diagnosis.failure_signature_sha256 must be a SHA-256 value.' }

    $result = [ordered]@{ failure_signature_sha256 = $signatureHash.ToLowerInvariant() }
    foreach ($name in @('hypothesis', 'measurement', 'evidence_delta')) {
        $value = Assert-String -Value (Get-RequiredMapValue -Map $diagnosis -Name $name -Context 'manifest.approval.diagnosis') -Context ('manifest.approval.diagnosis.' + $name)
        Assert-ForemanSafeText -Text $value -Context ('manifest.approval.diagnosis.' + $name)
        $result[$name] = $value.Trim()
    }
    return $result
}

function Get-DiagnosisEvidenceSha256 {
    param([System.Collections.IDictionary]$Diagnosis)
    $identity = [ordered]@{
        failure_signature_sha256 = ([string]$Diagnosis['failure_signature_sha256']).ToLowerInvariant()
        hypothesis = ConvertTo-NormalizedFailureText -Text ([string]$Diagnosis['hypothesis'])
        measurement = ConvertTo-NormalizedFailureText -Text ([string]$Diagnosis['measurement'])
        evidence_delta = ConvertTo-NormalizedFailureText -Text ([string]$Diagnosis['evidence_delta'])
    }
    return Get-JsonValueSha256 -Value $identity
}

function Get-ManifestIntentSha256 {
    param([System.Collections.IDictionary]$Manifest)
    $intent = [ordered]@{
        schema = 'provost-foreman-intent/v1'
        native_plan_relative_path = [string]$Manifest['native_plan']['relative_path']
        spec = $Manifest['spec']
        change = $Manifest['change']
        role_catalog = [string]$Manifest['role_catalog']
        external_read_roots = if ($Manifest.Contains('external_read_roots')) { @($Manifest['external_read_roots']) } else { @() }
        tasks = @($Manifest['tasks'])
        final_reviews = $Manifest['final_reviews']
    }
    return Get-JsonValueSha256 -Value $intent
}

function Get-WorkspaceSnapshotSha256 {
    param([System.Collections.IDictionary]$Snapshot)
    $entries = @()
    foreach ($entry in @($Snapshot['entries'] | Sort-Object { [string]$_['path'] })) {
        $entries += ,[ordered]@{
            path = [string]$entry['path']
            status = [string]$entry['status']
            sha256 = if ($entry.Contains('sha256')) { $entry['sha256'] } else { $null }
        }
    }
    $identity = [ordered]@{ kind = [string]$Snapshot['kind']; entries = @($entries) }
    if ($Snapshot.Contains('head')) { $identity['head'] = [string]$Snapshot['head'] }
    if ($Snapshot.Contains('heads')) { $identity['heads'] = $Snapshot['heads'] }
    return Get-JsonValueSha256 -Value $identity
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Read-JsonMap {
    param([string]$Path, [string]$Context)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Foreman -Code 'PATH' -Message ($Context + ' does not exist: ' + $Path) }
    try { $value = ConvertTo-ForemanValue -Value ((Get-Content -LiteralPath $Path -Raw) | ConvertFrom-Json -ErrorAction Stop) }
    catch { Stop-Foreman -Code 'SCHEMA' -Message ('Unable to parse ' + $Context + ': ' + $_.Exception.Message) }
    Assert-Map -Value $value -Context $Context
    return $value
}

function Get-GitOutput {
    param([string]$Root, [string[]]$GitArguments)
    $output = @(& git -C $Root @GitArguments 2>$null)
    if ($LASTEXITCODE -ne 0) { Stop-Foreman -Code 'GIT' -Message ('Git command failed: git ' + ($GitArguments -join ' ')) }
    return @($output)
}

function Test-GitWorkspace {
    param([string]$Root)
    try {
        $output = @(& git -C $Root rev-parse --show-toplevel 2>$null)
        if ($LASTEXITCODE -ne 0 -or $output.Count -eq 0) { return $false }
        return ((Get-NormalizedRoot -Path ([string]$output[0])) -ieq (Get-NormalizedRoot -Path $Root))
    }
    catch { return $false }
}

function Get-GitDirtyEntries {
    param([string]$Root)
    $output = Get-GitOutput -Root $Root -GitArguments @('status', '--porcelain=v1', '--untracked-files=all')
    $entries = @()
    foreach ($lineValue in $output) {
        $line = [string]$lineValue
        if ($line.Length -lt 4) { continue }
        $status = $line.Substring(0, 2)
        $path = ($line.Substring(3) -replace '\\', '/')
        if ($path -match ' -> ') { $path = ($path -split ' -> ')[-1] }
        $entries += [ordered]@{ status = $status; path = $path }
    }
    return ,$entries
}

function Get-GitHead {
    param([string]$Root)
    return [string](Get-GitOutput -Root $Root -GitArguments @('rev-parse', 'HEAD') | Select-Object -First 1)
}

function Get-GitBaseline {
    param([string]$Root)
    $head = Get-GitHead -Root $Root
    $dirty = @()
    foreach ($entry in (Get-GitDirtyEntries -Root $Root)) {
        $fullPath = Join-Path $Root ($entry.path -replace '/', '\\')
        $hash = if (Test-Path -LiteralPath $fullPath -PathType Leaf) { Get-Sha256 -Path $fullPath } else { $null }
        $dirty += [ordered]@{ path = $entry.path; status = $entry.status; sha256 = $hash }
    }
    return [ordered]@{ captured_at_utc = [DateTime]::UtcNow.ToString('o'); head = $head; dirty_paths = @($dirty) }
}

function Get-ForemanWorkspaceDeclaration {
    param([string]$Root)
    $declarationPath = Join-Path (Get-ForemanRoot -Root $Root) 'workspace.json'
    if (-not (Test-Path -LiteralPath $declarationPath -PathType Leaf)) { return $null }
    $declaration = Read-JsonMap -Path $declarationPath -Context 'Foreman workspace declaration'
    if ((Assert-String -Value (Get-RequiredMapValue -Map $declaration -Name 'schema' -Context 'workspace declaration') -Context 'workspace declaration.schema') -ne 'provost-foreman-workspace/v1') { Stop-Foreman -Code 'SCHEMA' -Message 'workspace declaration schema must be provost-foreman-workspace/v1.' }
    $members = Assert-StringArray -Value (Get-RequiredMapValue -Map $declaration -Name 'members' -Context 'workspace declaration') -Context 'workspace declaration.members'
    if (@($members).Count -lt 1) { Stop-Foreman -Code 'SCHEMA' -Message 'workspace declaration must list at least one member.' }
    $normalizedRoot = Get-NormalizedRoot -Path $Root
    $normalizedMembers = @()
    foreach ($memberValue in $members) {
        $member = [string]$memberValue
        if ($member -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' -or $member -eq '.' -or $member -eq '..') { Stop-Foreman -Code 'SCHEMA' -Message ('workspace member must be a plain child directory name: ' + $member) }
        foreach ($existing in $normalizedMembers) {
            if ([string]::Equals($existing, $member, [System.StringComparison]::OrdinalIgnoreCase)) { Stop-Foreman -Code 'SCHEMA' -Message ('workspace member is duplicated: ' + $member) }
        }
        $memberRoot = Join-Path $normalizedRoot $member
        if (-not (Test-Path -LiteralPath $memberRoot -PathType Container)) { Stop-Foreman -Code 'PATH' -Message ('workspace member directory does not exist: ' + $memberRoot) }
        if (-not (Test-GitWorkspace -Root $memberRoot)) { Stop-Foreman -Code 'PATH' -Message ('workspace member must be a Git repository root (git init it first): ' + $memberRoot) }
        $headOutput = @(& git -C $memberRoot rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -ne 0 -or $headOutput.Count -eq 0) { Stop-Foreman -Code 'GIT' -Message ('workspace member has no commits yet; create an initial baseline commit: ' + $memberRoot) }
        $normalizedMembers += $member
    }
    return @($normalizedMembers)
}

function Get-GitWorkspaceSnapshotData {
    param([string]$Root)
    $privatePrefix = '.claude/provost/foreman/'
    $entries = @()
    foreach ($entry in (Get-GitDirtyEntries -Root $Root)) {
        $path = [string]$entry.path
        if ($path.StartsWith($privatePrefix, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $fullPath = Join-Path $Root ($path -replace '/', '\\')
        $hash = if (Test-Path -LiteralPath $fullPath -PathType Leaf) { Get-Sha256 -Path $fullPath } else { $null }
        $entries += [ordered]@{ path = $path; status = [string]$entry.status; sha256 = $hash }
    }
    return [ordered]@{ kind = 'git'; captured_at_utc = [DateTime]::UtcNow.ToString('o'); head = Get-GitHead -Root $Root; entries = @($entries) }
}

function Get-MemberAggregateSnapshotData {
    param([string]$Root, [string[]]$Members)
    $entries = @()
    $heads = [ordered]@{}
    foreach ($memberValue in $Members) {
        $member = [string]$memberValue
        $memberSnapshot = Get-GitWorkspaceSnapshotData -Root (Join-Path $Root $member)
        $heads[$member] = [string]$memberSnapshot['head']
        foreach ($entry in @($memberSnapshot['entries'])) {
            $entry['path'] = ($member + '/' + [string]$entry['path'])
            $entries += $entry
        }
    }
    return [ordered]@{ kind = 'workspace'; captured_at_utc = [DateTime]::UtcNow.ToString('o'); heads = $heads; entries = @($entries) }
}

function Get-WorkspaceSnapshotData {
    param([string]$Root, [AllowNull()][string[]]$Members = $null)
    if ($null -ne $Members -and @($Members).Count -gt 0) { return Get-MemberAggregateSnapshotData -Root $Root -Members $Members }
    if (Test-GitWorkspace -Root $Root) { return Get-GitWorkspaceSnapshotData -Root $Root }
    Stop-Foreman -Code 'NON_GIT' -Message 'Workspace root is not a Git repository. Either git init it, or declare sibling members in .claude/provost/foreman/workspace.json.'
}

function Get-SnapshotEntrySignature {
    param([System.Collections.IDictionary]$Entry)
    $parts = @()
    foreach ($name in @('status', 'length', 'last_write_utc', 'sha256')) {
        $value = if ($Entry.Contains($name)) { [string]$Entry[$name] } else { '<absent>' }
        $parts += ($name + '=' + $value)
    }
    return ($parts -join '|')
}

function Get-SnapshotChangedPaths {
    param([System.Collections.IDictionary]$Before, [System.Collections.IDictionary]$After)
    $beforeByPath = @{}
    foreach ($entry in @($Before['entries'])) { $beforeByPath[[string]$entry['path']] = $entry }
    $afterByPath = @{}
    foreach ($entry in @($After['entries'])) { $afterByPath[[string]$entry['path']] = $entry }
    $changed = @()
    foreach ($path in @($beforeByPath.Keys + $afterByPath.Keys | Select-Object -Unique)) {
        if (-not $beforeByPath.ContainsKey($path) -or -not $afterByPath.ContainsKey($path)) { $changed += $path; continue }
        if ((Get-SnapshotEntrySignature -Entry $beforeByPath[$path]) -ne (Get-SnapshotEntrySignature -Entry $afterByPath[$path])) { $changed += $path }
    }
    return @($changed | Sort-Object -Unique)
}

function Get-SnapshotEntryByPath {
    param([System.Collections.IDictionary]$Snapshot, [string]$Path)
    foreach ($entry in @($Snapshot['entries'])) {
        if ([string]::Equals([string]$entry['path'], $Path, [System.StringComparison]::OrdinalIgnoreCase)) { return $entry }
    }
    return $null
}

function Test-PathContainsReparsePoint {
    param([string]$Root, [string]$Path)
    $normalizedRoot = Get-NormalizedRoot -Path $Root
    $currentPath = [System.IO.Path]::GetFullPath($Path)
    while ($currentPath.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $currentPath) {
            $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $true }
        }
        if ([string]::Equals($currentPath, $normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $parent = Split-Path -Parent $currentPath
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $currentPath) { break }
        $currentPath = $parent
    }
    return $false
}

function Remove-NewGeneratedArtifacts {
    param(
        [string]$Root,
        [System.Collections.IDictionary]$BeforeSnapshot,
        [System.Collections.IDictionary]$AfterSnapshot,
        [string]$LedgerPath,
        [string]$TaskId
    )
    $cleaned = 0
    foreach ($path in (Get-SnapshotChangedPaths -Before $BeforeSnapshot -After $AfterSnapshot)) {
        if ($path -notmatch '(?i)(^|/)__pycache__/[^/]+\.pyc$') { continue }
        if ($null -ne (Get-SnapshotEntryByPath -Snapshot $BeforeSnapshot -Path $path)) { continue }
        $afterEntry = Get-SnapshotEntryByPath -Snapshot $AfterSnapshot -Path $path
        if ($null -eq $afterEntry -or [string]$afterEntry['status'] -ne '??') { continue }

        $fullPath = Resolve-WorkspaceChild -Root $Root -RelativePath $path -Context 'generated artifact path'
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { continue }
        if (Test-PathContainsReparsePoint -Root $Root -Path $fullPath) {
            Stop-Foreman -Code 'ARTIFACT_CLEANUP' -Message ('Generated artifact path contains a reparse point and cannot be cleaned automatically: ' + $path)
        }
        $artifactHash = Get-Sha256 -Path $fullPath
        try {
            Remove-Item -LiteralPath $fullPath -Force -ErrorAction Stop
            $cacheDirectory = Split-Path -Parent $fullPath
            if ((Split-Path -Leaf $cacheDirectory) -ieq '__pycache__' -and @(Get-ChildItem -LiteralPath $cacheDirectory -Force -ErrorAction Stop).Count -eq 0) {
                Remove-Item -LiteralPath $cacheDirectory -Force -ErrorAction Stop
            }
        }
        catch {
            Stop-Foreman -Code 'ARTIFACT_CLEANUP' -Message ('Unable to remove generated Python bytecode artifact ' + $path + ': ' + $_.Exception.Message)
        }
        Append-LedgerEvent -LedgerPath $LedgerPath -Event ([ordered]@{
            timestamp_utc = [DateTime]::UtcNow.ToString('o')
            event = 'generated_artifact_cleaned'
            task_id = $TaskId
            path = $path
            profile = 'python-bytecode'
            sha256 = $artifactHash
            status = 'CLEANED'
        })
        $cleaned++
    }
    return $cleaned
}

function Test-ManifestStructure {
    param([System.Collections.IDictionary]$Manifest, [string]$WorkspaceRoot, [string]$ManifestPath)

    $absoluteManifestPath = [System.IO.Path]::GetFullPath($ManifestPath)
    Test-ForemanSecretProperty -Value $Manifest
    Test-ForemanRoleCatalogOverride -Value $Manifest
    $schema = Assert-String -Value (Get-RequiredMapValue -Map $Manifest -Name 'schema' -Context 'manifest') -Context 'manifest.schema'
    if (@('provost-foreman-manifest/v1', 'provost-foreman-manifest/v2') -notcontains $schema) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.schema must be provost-foreman-manifest/v1 or provost-foreman-manifest/v2.' }
    $revision = Get-RequiredMapValue -Map $Manifest -Name 'revision' -Context 'manifest'
    Assert-Map -Value $revision -Context 'manifest.revision'
    $revisionId = Assert-String -Value (Get-RequiredMapValue -Map $revision -Name 'id' -Context 'manifest.revision') -Context 'manifest.revision.id'
    if ($revisionId -notmatch '^r\d{3}$') { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.revision.id must be rNNN.' }
    $revisionNumber = [int](Get-RequiredMapValue -Map $revision -Name 'number' -Context 'manifest.revision')
    if ($revisionNumber -ne [int]$revisionId.Substring(1)) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.revision.number must match manifest.revision.id.' }
    if ((Split-Path -Leaf $absoluteManifestPath) -ne ($revisionId + '.json')) { Stop-Foreman -Code 'PATH' -Message 'Manifest filename must match its revision id.' }
    if ($revisionNumber -eq 1) {
        if (-not $revision.Contains('supersedes') -or $null -ne $revision['supersedes']) { Stop-Foreman -Code 'SCHEMA' -Message 'r001 must set supersedes to null.' }
    }
    else {
        $supersedes = Get-RequiredMapValue -Map $revision -Name 'supersedes' -Context 'manifest.revision'
        Assert-Map -Value $supersedes -Context 'manifest.revision.supersedes'
        $previousId = ('r' + ($revisionNumber - 1).ToString('000'))
        if ((Assert-String -Value (Get-RequiredMapValue -Map $supersedes -Name 'revision' -Context 'manifest.revision.supersedes') -Context 'manifest.revision.supersedes.revision') -ne $previousId) { Stop-Foreman -Code 'IMMUTABLE' -Message 'A new manifest revision must supersede the immediately previous revision.' }
        $previousPath = Join-Path (Split-Path -Parent $absoluteManifestPath) ($previousId + '.json')
        if (-not (Test-Path -LiteralPath $previousPath -PathType Leaf)) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Previous manifest revision is missing: ' + $previousId) }
        if ((Assert-String -Value (Get-RequiredMapValue -Map $supersedes -Name 'sha256' -Context 'manifest.revision.supersedes') -Context 'manifest.revision.supersedes.sha256').ToLowerInvariant() -ne (Get-Sha256 -Path $previousPath)) { Stop-Foreman -Code 'IMMUTABLE' -Message 'Previous manifest SHA-256 does not match the supersedes record.' }
    }

    $continuation = $null
    if ($schema -eq 'provost-foreman-manifest/v1') {
        if ($Manifest.Contains('continuation') -and $null -ne $Manifest['continuation']) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.continuation requires provost-foreman-manifest/v2.' }
    }
    elseif ($Manifest.Contains('continuation') -and $null -ne $Manifest['continuation']) {
        if ($revisionNumber -eq 1) { Stop-Foreman -Code 'SCHEMA' -Message 'r001 cannot adopt prior WIP.' }
        $continuation = $Manifest['continuation']
        Assert-Map -Value $continuation -Context 'manifest.continuation'
        if ((Assert-String -Value (Get-RequiredMapValue -Map $continuation -Name 'kind' -Context 'manifest.continuation') -Context 'manifest.continuation.kind') -ne 'adopt-prior-wip') { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.continuation.kind must be adopt-prior-wip.' }
        if ($continuation.Contains('reuse_prior_approval')) { [void](Assert-Boolean -Value $continuation['reuse_prior_approval'] -Context 'manifest.continuation.reuse_prior_approval') }
        $source = Get-RequiredMapValue -Map $continuation -Name 'source' -Context 'manifest.continuation'
        Assert-Map -Value $source -Context 'manifest.continuation.source'
        $previousId = ('r' + ($revisionNumber - 1).ToString('000'))
        if ((Assert-String -Value (Get-RequiredMapValue -Map $source -Name 'revision' -Context 'manifest.continuation.source') -Context 'manifest.continuation.source.revision') -ne $previousId) { Stop-Foreman -Code 'CONTINUATION' -Message 'Continuation must reference the immediately previous revision.' }
        foreach ($hashName in @('manifest_sha256', 'handoff_sha256')) {
            $hashValue = Assert-String -Value (Get-RequiredMapValue -Map $source -Name $hashName -Context 'manifest.continuation.source') -Context ('manifest.continuation.source.' + $hashName)
            if ($hashValue -notmatch '^[a-fA-F0-9]{64}$') { Stop-Foreman -Code 'SCHEMA' -Message ('manifest.continuation.source.' + $hashName + ' must be a SHA-256 value.') }
            $source[$hashName] = $hashValue.ToLowerInvariant()
        }
        $adoptPaths = Assert-StringArray -Value (Get-RequiredMapValue -Map $continuation -Name 'adopt_paths' -Context 'manifest.continuation') -Context 'manifest.continuation.adopt_paths'
        if (@($adoptPaths).Count -eq 0) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.continuation.adopt_paths must contain at least one path.' }
        $normalizedAdoptPaths = @()
        foreach ($path in $adoptPaths) { $normalizedAdoptPaths += Assert-LiteralWritePath -Path $path -Context 'manifest.continuation.adopt_paths' }
        if (@($normalizedAdoptPaths | Select-Object -Unique).Count -ne @($normalizedAdoptPaths).Count) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.continuation.adopt_paths cannot contain duplicates.' }
        $continuation['adopt_paths'] = @($normalizedAdoptPaths)
    }

    $approval = Get-RequiredMapValue -Map $Manifest -Name 'approval' -Context 'manifest'
    Assert-Map -Value $approval -Context 'manifest.approval'
    if ((Assert-String -Value (Get-RequiredMapValue -Map $approval -Name 'state' -Context 'manifest.approval') -Context 'manifest.approval.state') -ne 'approved' -or (Assert-String -Value (Get-RequiredMapValue -Map $approval -Name 'source' -Context 'manifest.approval') -Context 'manifest.approval.source') -ne 'native-plan-auto') { Stop-Foreman -Code 'SCHEMA' -Message 'Manifest approval must be approved through native-plan-auto.' }
    $diagnosis = Get-ManifestDiagnosisEvidence -Approval $approval
    if ($null -ne $diagnosis) {
        if ($schema -ne 'provost-foreman-manifest/v2') { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.approval.diagnosis requires provost-foreman-manifest/v2.' }
        $approval['diagnosis'] = $diagnosis
    }

    $nativePlan = Get-RequiredMapValue -Map $Manifest -Name 'native_plan' -Context 'manifest'
    Assert-Map -Value $nativePlan -Context 'manifest.native_plan'
    $planRelative = Assert-String -Value (Get-RequiredMapValue -Map $nativePlan -Name 'relative_path' -Context 'manifest.native_plan') -Context 'manifest.native_plan.relative_path'
    $planPath = Resolve-WorkspaceChild -Root $WorkspaceRoot -RelativePath $planRelative -Context 'manifest.native_plan.relative_path'
    $plansRoot = Join-Path (Get-ForemanRoot -Root $WorkspaceRoot) 'plans'
    $plansPrefix = (Get-NormalizedRoot -Path $plansRoot) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $planPath.StartsWith($plansPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { Stop-Foreman -Code 'PATH' -Message 'Native Plan must be inside the Foreman plans directory.' }
    if (-not (Test-Path -LiteralPath $planPath -PathType Leaf)) { Stop-Foreman -Code 'PATH' -Message 'Native Plan file does not exist.' }

    $spec = Get-RequiredMapValue -Map $Manifest -Name 'spec' -Context 'manifest'
    Assert-Map -Value $spec -Context 'manifest.spec'
    if (@('openspec', 'spec-kit', 'none') -notcontains (Assert-String -Value (Get-RequiredMapValue -Map $spec -Name 'system' -Context 'manifest.spec') -Context 'manifest.spec.system')) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.spec.system is not supported.' }

    $change = Get-RequiredMapValue -Map $Manifest -Name 'change' -Context 'manifest'
    Assert-Map -Value $change -Context 'manifest.change'
    $changeId = Assert-String -Value (Get-RequiredMapValue -Map $change -Name 'id' -Context 'manifest.change') -Context 'manifest.change.id'
    if ($changeId -notmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$') { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.change.id must be a readable lowercase slug.' }
    $expectedManifestDirectory = Join-Path (Join-Path (Join-Path (Get-ForemanRoot -Root $WorkspaceRoot) 'manifests') ([string]$spec['system'])) $changeId
    if ((Get-NormalizedRoot -Path (Split-Path -Parent $absoluteManifestPath)) -ine (Get-NormalizedRoot -Path $expectedManifestDirectory)) {
        Stop-Foreman -Code 'PATH' -Message 'ManifestPath must be manifests/<spec-system>/<change-id>/rNNN.json.'
    }
    if ((Assert-String -Value (Get-RequiredMapValue -Map $Manifest -Name 'role_catalog' -Context 'manifest') -Context 'manifest.role_catalog') -ne 'provost-foreman/v1') { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.role_catalog must be provost-foreman/v1.' }

    if ($Manifest.Contains('external_read_roots') -and $null -ne $Manifest['external_read_roots']) {
        $externalReadRootValues = Assert-StringArray -Value $Manifest['external_read_roots'] -Context 'manifest.external_read_roots'
        $workspacePrefix = (Get-NormalizedRoot -Path $WorkspaceRoot) + [System.IO.Path]::DirectorySeparatorChar
        $normalizedExternalReadRoots = @()
        foreach ($externalReadRootValue in $externalReadRootValues) {
            if (-not [System.IO.Path]::IsPathRooted($externalReadRootValue)) { Stop-Foreman -Code 'PATH' -Message 'manifest.external_read_roots entries must be absolute directory paths.' }
            $normalizedExternalReadRoot = Get-NormalizedRoot -Path $externalReadRootValue
            if (-not (Test-Path -LiteralPath $normalizedExternalReadRoot -PathType Container)) { Stop-Foreman -Code 'PATH' -Message ('manifest.external_read_roots entry does not exist: ' + $normalizedExternalReadRoot) }
            $externalPrefix = $normalizedExternalReadRoot + [System.IO.Path]::DirectorySeparatorChar
            if ($externalPrefix.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase) -or $workspacePrefix.StartsWith($externalPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Foreman -Code 'PATH' -Message ('manifest.external_read_roots entry must stay outside the workspace root: ' + $normalizedExternalReadRoot)
            }
            $normalizedExternalReadRoots += $normalizedExternalReadRoot
        }
        $Manifest['external_read_roots'] = @($normalizedExternalReadRoots)
    }

    $tasks = @((Get-RequiredMapValue -Map $Manifest -Name 'tasks' -Context 'manifest'))
    if ($tasks.Count -eq 0) { Stop-Foreman -Code 'SCHEMA' -Message 'manifest.tasks must contain at least one task.' }
    $taskIds = @{}
    $taskMaps = @()
    foreach ($task in $tasks) {
        Assert-Map -Value $task -Context 'manifest.tasks[]'
        $taskId = Assert-String -Value (Get-RequiredMapValue -Map $task -Name 'id' -Context 'manifest.tasks[]') -Context 'manifest.tasks[].id'
        if ($taskIds.ContainsKey($taskId)) { Stop-Foreman -Code 'SCHEMA' -Message ('Task id is duplicated: ' + $taskId) }
        $taskIds[$taskId] = $true
        $agentKey = Assert-String -Value (Get-RequiredMapValue -Map $task -Name 'agent_key' -Context ('task ' + $taskId)) -Context ('task ' + $taskId + '.agent_key')
        if (-not $script:RoleCatalog.Contains($agentKey)) { Stop-Foreman -Code 'SCHEMA' -Message ('Task uses an unsupported custom role: ' + $agentKey) }
        $writeSet = Assert-StringArray -Value (Get-RequiredMapValue -Map $task -Name 'write_set' -Context ('task ' + $taskId)) -Context ('task ' + $taskId + '.write_set')
        $normalizedWriteSet = @()
        foreach ($path in $writeSet) { $normalizedWriteSet += Assert-LiteralWritePath -Path $path -Context ('task ' + $taskId + '.write_set') }
        if ($script:RoleCatalog[$agentKey].writer -and $normalizedWriteSet.Count -eq 0) { Stop-Foreman -Code 'SCHEMA' -Message ('Writer task ' + $taskId + ' needs a non-empty write_set.') }
        if (-not $script:RoleCatalog[$agentKey].writer -and $normalizedWriteSet.Count -ne 0) { Stop-Foreman -Code 'SCHEMA' -Message ('Read-only task ' + $taskId + ' must have an empty write_set.') }
        [void](Assert-StringArray -Value (Get-RequiredMapValue -Map $task -Name 'depends_on' -Context ('task ' + $taskId)) -Context ('task ' + $taskId + '.depends_on'))
        $mustNotModify = Assert-StringArray -Value (Get-RequiredMapValue -Map $task -Name 'must_not_modify' -Context ('task ' + $taskId)) -Context ('task ' + $taskId + '.must_not_modify')
        $normalizedMustNotModify = @()
        foreach ($path in $mustNotModify) { $normalizedMustNotModify += Assert-LiteralWritePath -Path $path -Context ('task ' + $taskId + '.must_not_modify') }
        $acceptance = @((Get-RequiredMapValue -Map $task -Name 'acceptance' -Context ('task ' + $taskId)))
        foreach ($criterion in $acceptance) {
            Assert-Map -Value $criterion -Context ('task ' + $taskId + '.acceptance[]')
            [void](Assert-String -Value (Get-RequiredMapValue -Map $criterion -Name 'id' -Context ('task ' + $taskId + '.acceptance[]')) -Context ('task ' + $taskId + '.acceptance[].id'))
            [void](Assert-String -Value (Get-RequiredMapValue -Map $criterion -Name 'command' -Context ('task ' + $taskId + '.acceptance[]')) -Context ('task ' + $taskId + '.acceptance[].command'))
            [void](Assert-String -Value (Get-RequiredMapValue -Map $criterion -Name 'expect' -Context ('task ' + $taskId + '.acceptance[]')) -Context ('task ' + $taskId + '.acceptance[].expect'))
        }
        $task['write_set'] = @($normalizedWriteSet)
        $task['must_not_modify'] = @($normalizedMustNotModify)
        $taskMaps += ,$task
    }
    foreach ($task in $taskMaps) {
        foreach ($dependency in @($task['depends_on'])) { if (-not $taskIds.ContainsKey([string]$dependency)) { Stop-Foreman -Code 'SCHEMA' -Message ('Task ' + $task['id'] + ' depends on unknown task ' + $dependency + '.') } }
    }
    $taskById = @{}
    foreach ($task in $taskMaps) { $taskById[[string]$task['id']] = $task }
    $visiting = @{}
    $visited = @{}
    function Visit-ManifestDependency {
        param([string]$TaskId)
        if ($visited.ContainsKey($TaskId)) { return }
        if ($visiting.ContainsKey($TaskId)) { Stop-Foreman -Code 'SCHEMA' -Message ('Task dependency cycle includes ' + $TaskId + '.') }
        $visiting[$TaskId] = $true
        foreach ($dependency in @($taskById[$TaskId]['depends_on'])) { Visit-ManifestDependency -TaskId ([string]$dependency) }
        $visiting.Remove($TaskId)
        $visited[$TaskId] = $true
    }
    foreach ($taskId in $taskById.Keys) { Visit-ManifestDependency -TaskId ([string]$taskId) }
    if ($schema -eq 'provost-foreman-manifest/v2') {
        function Test-ManifestDependencyReachability {
            param([string]$TaskId, [string]$RequiredTaskId)
            foreach ($dependency in @($taskById[$TaskId]['depends_on'])) {
                $dependencyId = [string]$dependency
                if ($dependencyId -eq $RequiredTaskId) { return $true }
                if (Test-ManifestDependencyReachability -TaskId $dependencyId -RequiredTaskId $RequiredTaskId) { return $true }
            }
            return $false
        }
        $lastWriterByPath = @{}
        foreach ($task in $taskMaps) {
            if (-not $script:RoleCatalog[[string]$task['agent_key']].writer) { continue }
            foreach ($path in @($task['write_set'])) {
                $normalizedPath = [string]$path
                if ($lastWriterByPath.ContainsKey($normalizedPath)) {
                    $previousWriterId = [string]$lastWriterByPath[$normalizedPath]
                    if (-not (Test-ManifestDependencyReachability -TaskId ([string]$task['id']) -RequiredTaskId $previousWriterId)) {
                        Stop-Foreman -Code 'SCHEMA' -Message ('V2 writers sharing path "' + $normalizedPath + '" require dependency-ordered custody from ' + $previousWriterId + ' to ' + [string]$task['id'] + '.')
                    }
                }
                $lastWriterByPath[$normalizedPath] = [string]$task['id']
            }
        }
    }
    $finalReviews = Get-RequiredMapValue -Map $Manifest -Name 'final_reviews' -Context 'manifest'
    Assert-Map -Value $finalReviews -Context 'manifest.final_reviews'
    $codeReview = Get-RequiredMapValue -Map $finalReviews -Name 'code' -Context 'manifest.final_reviews'
    Assert-Map -Value $codeReview -Context 'manifest.final_reviews.code'
    if (-not (Assert-Boolean -Value (Get-RequiredMapValue -Map $codeReview -Name 'required' -Context 'manifest.final_reviews.code') -Context 'manifest.final_reviews.code.required')) {
        Stop-Foreman -Code 'FINAL_REVIEW' -Message 'The final code review is required for every Foreman manifest.'
    }
    if ((Assert-String -Value (Get-RequiredMapValue -Map $codeReview -Name 'agent_key' -Context 'manifest.final_reviews.code') -Context 'manifest.final_reviews.code.agent_key') -ne 'foreman-verifier') {
        Stop-Foreman -Code 'FINAL_REVIEW' -Message 'The final code review must use foreman-verifier.'
    }
    $architectureReview = Get-RequiredMapValue -Map $finalReviews -Name 'architecture' -Context 'manifest.final_reviews'
    Assert-Map -Value $architectureReview -Context 'manifest.final_reviews.architecture'
    $architectureRequired = Assert-Boolean -Value (Get-RequiredMapValue -Map $architectureReview -Name 'required' -Context 'manifest.final_reviews.architecture') -Context 'manifest.final_reviews.architecture.required'
    if ((Assert-String -Value (Get-RequiredMapValue -Map $architectureReview -Name 'agent_key' -Context 'manifest.final_reviews.architecture') -Context 'manifest.final_reviews.architecture.agent_key') -ne 'foreman-architecture-verifier') {
        Stop-Foreman -Code 'FINAL_REVIEW' -Message 'The architecture review must use foreman-architecture-verifier.'
    }
    $nonReviewTaskIds = @()
    foreach ($task in $taskMaps) {
        if (@('foreman-verifier', 'foreman-architecture-verifier') -notcontains [string]$task['agent_key']) {
            $nonReviewTaskIds += [string]$task['id']
        }
    }
    foreach ($reviewDefinition in @(
        [ordered]@{ agent_key = 'foreman-verifier'; required = $true; label = 'code' },
        [ordered]@{ agent_key = 'foreman-architecture-verifier'; required = $architectureRequired; label = 'architecture' }
    )) {
        if (-not $reviewDefinition['required']) { continue }
        $qualifyingReview = $false
        foreach ($task in $taskMaps) {
            if ([string]$task['agent_key'] -ne [string]$reviewDefinition['agent_key']) { continue }
            $dependencies = @($task['depends_on'])
            $coversAllWork = $true
            foreach ($workTaskId in $nonReviewTaskIds) {
                if ($dependencies -notcontains $workTaskId) { $coversAllWork = $false; break }
            }
            if ($coversAllWork) { $qualifyingReview = $true; break }
        }
        if (-not $qualifyingReview) {
            Stop-Foreman -Code 'FINAL_REVIEW' -Message ('A final ' + $reviewDefinition['label'] + ' verifier task must depend on every non-review task.')
        }
    }
    return [ordered]@{ schema = $schema; plan_path = $planPath; revision_id = $revisionId; revision_number = $revisionNumber; change_id = $changeId; spec_system = [string]$spec['system']; tasks = @($taskMaps); continuation = $continuation }
}

function Get-ManifestPathHash {
    param([string]$Path)
    return Get-Sha256 -Path $Path
}

function Get-ManifestLedgerPath {
    param([System.Collections.IDictionary]$Manifest, [string]$Root)
    $run = Get-RequiredMapValue -Map $Manifest -Name 'run' -Context 'manifest'
    Assert-Map -Value $run -Context 'manifest.run'
    return Resolve-WorkspaceChild -Root (Get-ForemanRoot -Root $Root) -RelativePath (Assert-String -Value (Get-RequiredMapValue -Map $run -Name 'ledger_relative_path' -Context 'manifest.run') -Context 'manifest.run.ledger_relative_path') -Context 'manifest.run.ledger_relative_path'
}

function Append-LedgerEvent {
    param([string]$LedgerPath, [System.Collections.IDictionary]$Event)
    $directory = Split-Path -Parent $LedgerPath
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $stream = [System.IO.File]::Open($LedgerPath, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try {
        $writer = New-Object System.IO.StreamWriter($stream, [System.Text.UTF8Encoding]::new($false))
        try { $writer.WriteLine((ConvertTo-Json -InputObject $Event -Depth 64 -Compress)); $writer.Flush() }
        finally { $writer.Dispose() }
    }
    finally { $stream.Dispose() }
}

function Read-Lock {
    param([string]$Root)
    $path = Join-Path (Get-ForemanRoot -Root $Root) 'active-run.lock'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'No active Foreman lock exists.' }
    return [ordered]@{ path = $path; value = Read-JsonMap -Path $path -Context 'active Foreman lock' }
}

function Write-Lock {
    param([string]$Path, [System.Collections.IDictionary]$Value)
    Write-Utf8NoBom -Path $Path -Text (ConvertTo-Json -InputObject $Value -Depth 64)
}

function Write-TerminalHandoffReceipt {
    param(
        [string]$Root,
        [System.Collections.IDictionary]$Lock,
        [string]$LockPath
    )
    $terminalState = [string]$Lock['state']
    if (@('FAIL', 'BLOCKED', 'ESCALATE') -notcontains $terminalState) { Stop-Foreman -Code 'HANDOFF' -Message 'A handoff receipt requires a terminal non-PASS state.' }
    if ($Lock.Contains('handoff_path') -and -not [string]::IsNullOrWhiteSpace([string]$Lock['handoff_path'])) {
        $existingPath = [string]$Lock['handoff_path']
        $existingHash = Get-Sha256 -Path $existingPath
        if (-not $Lock.Contains('handoff_sha256') -or $existingHash -ne [string]$Lock['handoff_sha256']) { Stop-Foreman -Code 'IMMUTABLE' -Message 'The terminal handoff receipt no longer matches the active lock.' }
        return [ordered]@{ path = $existingPath; sha256 = $existingHash }
    }

    $manifestPath = [string]$Lock['manifest_path']
    $actualManifestHash = Get-Sha256 -Path $manifestPath
    $manifest = $null
    $metadata = $null
    if ($Lock.Contains('handoff_metadata') -and $Lock['handoff_metadata'] -is [System.Collections.IDictionary]) { $metadata = $Lock['handoff_metadata'] }
    if ($actualManifestHash -eq [string]$Lock['manifest_sha256']) {
        $manifest = Read-JsonMap -Path $manifestPath -Context 'terminal Foreman manifest'
        if ($null -eq $metadata) {
            $metadata = [ordered]@{
                revision = [string]$manifest['revision']['id']
                spec_system = [string]$manifest['spec']['system']
                change_id = [string]$manifest['change']['id']
                native_plan_sha256 = [string]$manifest['native_plan']['sha256']
                intent_sha256 = if ($manifest.Contains('intent_sha256')) { [string]$manifest['intent_sha256'] } else { Get-ManifestIntentSha256 -Manifest $manifest }
                workspace_members = @(Get-ManifestWorkspaceMembers -Manifest $manifest)
            }
        }
    }
    elseif ($null -eq $metadata) {
        Stop-Foreman -Code 'HANDOFF' -Message 'The manifest is corrupt and the active lock lacks trusted handoff metadata.'
    }
    $workspaceMembers = @($metadata['workspace_members'])
    $workspaceSnapshot = Get-WorkspaceSnapshotData -Root $Root -Members $workspaceMembers
    $ledgerPath = [string]$Lock['ledger_path']
    $receiptPath = [System.IO.Path]::ChangeExtension($ledgerPath, '.handoff.json')
    if (Test-Path -LiteralPath $receiptPath) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt already exists without matching lock metadata: ' + $receiptPath) }
    $receipt = [ordered]@{
        schema = 'provost-foreman-handoff/v1'
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        helper_version = '2'
        terminal_state = $terminalState
        session_id = [string]$Lock['session_id']
        run_id = [string]$Lock['run_id']
        spec_system = [string]$metadata['spec_system']
        change_id = [string]$metadata['change_id']
        revision = [string]$metadata['revision']
        manifest_path = [System.IO.Path]::GetFullPath($manifestPath)
        manifest_sha256 = [string]$Lock['manifest_sha256']
        manifest_actual_sha256 = $actualManifestHash
        native_plan_sha256 = [string]$metadata['native_plan_sha256']
        intent_sha256 = [string]$metadata['intent_sha256']
        task_states = $Lock['task_states']
        workspace_snapshot = $workspaceSnapshot
        workspace_snapshot_sha256 = Get-WorkspaceSnapshotSha256 -Snapshot $workspaceSnapshot
        ledger_path = [System.IO.Path]::GetFullPath($ledgerPath)
        ledger_sha256 = Get-Sha256 -Path $ledgerPath
    }
    if ($metadata.Contains('diagnosis') -and $metadata.Contains('diagnosis_sha256')) {
        $receipt['diagnosis'] = $metadata['diagnosis']
        $receipt['diagnosis_sha256'] = [string]$metadata['diagnosis_sha256']
    }
    if ($Lock.Contains('failure_signature') -and $Lock.Contains('failure_signature_sha256')) {
        $receipt['failure_signature'] = $Lock['failure_signature']
        $receipt['failure_signature_sha256'] = [string]$Lock['failure_signature_sha256']
    }
    $temporaryReceipt = Join-Path (Split-Path -Parent $receiptPath) ('.handoff-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
    try {
        Write-Utf8NoBom -Path $temporaryReceipt -Text (ConvertTo-Json -InputObject $receipt -Depth 64)
        [System.IO.File]::Move($temporaryReceipt, $receiptPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporaryReceipt) { Remove-Item -LiteralPath $temporaryReceipt -Force -ErrorAction SilentlyContinue }
    }
    $Lock['handoff_path'] = $receiptPath
    $Lock['handoff_sha256'] = Get-Sha256 -Path $receiptPath
    Write-Lock -Path $LockPath -Value $Lock
    return [ordered]@{ path = $receiptPath; sha256 = [string]$Lock['handoff_sha256'] }
}

function Get-ManifestBaselineDirtyPaths {
    param([System.Collections.IDictionary]$Manifest)
    if (-not $Manifest.Contains('workspace') -or -not ($Manifest['workspace'] -is [System.Collections.IDictionary])) { return @() }
    $workspace = $Manifest['workspace']
    if (-not $workspace.Contains('baseline') -or -not ($workspace['baseline'] -is [System.Collections.IDictionary])) { return @() }
    $baseline = $workspace['baseline']
    $paths = @()
    if ([string]$workspace['mode'] -eq 'git') {
        foreach ($entry in @($baseline['dirty_paths'])) { $paths += [string]$entry['path'] }
    }
    elseif ([string]$workspace['mode'] -eq 'workspace' -and $baseline.Contains('members')) {
        foreach ($member in $baseline['members'].Keys) {
            foreach ($entry in @($baseline['members'][$member]['dirty_paths'])) { $paths += ([string]$member + '/' + [string]$entry['path']) }
        }
    }
    return @($paths | Select-Object -Unique)
}

function Resolve-ContinuationAdoption {
    param(
        [string]$Root,
        [string]$ForemanRoot,
        [string]$ManifestPath,
        [System.Collections.IDictionary]$Draft,
        [System.Collections.IDictionary]$Shape,
        [AllowNull()][string[]]$WorkspaceMembers = $null
    )
    if ($null -eq $Shape['continuation']) { return @() }
    $continuation = $Shape['continuation']
    $source = $continuation['source']
    $previousRevision = [string]$source['revision']
    $previousManifestPath = Join-Path (Split-Path -Parent $ManifestPath) ($previousRevision + '.json')
    $previousManifestHash = Get-Sha256 -Path $previousManifestPath
    if ($previousManifestHash -ne [string]$source['manifest_sha256']) { Stop-Foreman -Code 'CONTINUATION' -Message 'Continuation source manifest SHA-256 does not match the prior immutable manifest.' }
    $previousManifest = Read-JsonMap -Path $previousManifestPath -Context 'prior Foreman manifest'
    if ([string]$previousManifest['spec']['system'] -ne [string]$Draft['spec']['system'] -or [string]$previousManifest['change']['id'] -ne [string]$Draft['change']['id']) {
        Stop-Foreman -Code 'CONTINUATION' -Message 'Continuation must stay within the same spec system and change id.'
    }

    $receiptDirectory = Join-Path (Join-Path (Join-Path (Join-Path $ForemanRoot 'runs') ([string]$Shape['spec_system'])) ([string]$Shape['change_id'])) $previousRevision
    if (-not (Test-Path -LiteralPath $receiptDirectory -PathType Container)) { Stop-Foreman -Code 'CONTINUATION' -Message 'The prior revision has no terminal handoff receipt directory.' }
    $matchingReceipts = @()
    foreach ($candidate in @(Get-ChildItem -LiteralPath $receiptDirectory -Filter '*.handoff.json' -File)) {
        if ((Get-Sha256 -Path $candidate.FullName) -eq [string]$source['handoff_sha256']) { $matchingReceipts += $candidate.FullName }
    }
    if ($matchingReceipts.Count -ne 1) { Stop-Foreman -Code 'CONTINUATION' -Message 'Exactly one immutable handoff receipt must match continuation.source.handoff_sha256.' }
    $receiptPath = [string]$matchingReceipts[0]
    $receipt = Read-JsonMap -Path $receiptPath -Context 'terminal handoff receipt'
    if ([string]$receipt['schema'] -ne 'provost-foreman-handoff/v1') { Stop-Foreman -Code 'CONTINUATION' -Message 'The terminal handoff receipt schema is unsupported.' }
    if (@('FAIL', 'BLOCKED', 'ESCALATE') -notcontains [string]$receipt['terminal_state']) { Stop-Foreman -Code 'CONTINUATION' -Message 'The handoff receipt is not terminal.' }
    if ([string]$receipt['revision'] -ne $previousRevision -or [string]$receipt['spec_system'] -ne [string]$Shape['spec_system'] -or [string]$receipt['change_id'] -ne [string]$Shape['change_id']) {
        Stop-Foreman -Code 'CONTINUATION' -Message 'The handoff receipt lineage does not match this revision.'
    }
    if ([System.IO.Path]::GetFullPath([string]$receipt['manifest_path']) -ine [System.IO.Path]::GetFullPath($previousManifestPath) -or [string]$receipt['manifest_sha256'] -ne $previousManifestHash) {
        Stop-Foreman -Code 'CONTINUATION' -Message 'The handoff receipt does not identify the prior immutable manifest.'
    }
    if ((Get-Sha256 -Path ([string]$receipt['ledger_path'])) -ne [string]$receipt['ledger_sha256']) { Stop-Foreman -Code 'CONTINUATION' -Message 'The prior run ledger changed after the terminal handoff receipt was written.' }
    if ((Get-WorkspaceSnapshotSha256 -Snapshot $receipt['workspace_snapshot']) -ne [string]$receipt['workspace_snapshot_sha256']) { Stop-Foreman -Code 'CONTINUATION' -Message 'The handoff receipt workspace snapshot is internally inconsistent.' }
    $currentSnapshot = Get-WorkspaceSnapshotData -Root $Root -Members $WorkspaceMembers
    if ((Get-WorkspaceSnapshotSha256 -Snapshot $currentSnapshot) -ne [string]$receipt['workspace_snapshot_sha256']) { Stop-Foreman -Code 'CONTINUATION' -Message 'Workspace HEAD or dirty content changed after the terminal handoff receipt was written.' }

    $reusePriorApproval = $continuation.Contains('reuse_prior_approval') -and [bool]$continuation['reuse_prior_approval']
    if ($reusePriorApproval) {
        if ((Get-ManifestIntentSha256 -Manifest $Draft) -ne [string]$receipt['intent_sha256']) { Stop-Foreman -Code 'CONTINUATION' -Message 'Prior approval cannot be reused because the manifest intent changed.' }
        if ((Get-Sha256 -Path ([string]$Shape['plan_path'])) -ne [string]$receipt['native_plan_sha256']) { Stop-Foreman -Code 'CONTINUATION' -Message 'Prior approval cannot be reused because the native Plan changed.' }
    }

    $writerPaths = @()
    $managedProtectedPaths = @()
    foreach ($task in @($Shape['tasks'])) {
        if ($script:RoleCatalog[[string]$task['agent_key']].writer) { $writerPaths += @($task['write_set']) }
        $managedProtectedPaths += @($task['must_not_modify'])
    }
    $priorBaselineDirtyPaths = @(Get-ManifestBaselineDirtyPaths -Manifest $previousManifest)
    $adoptedPaths = @()
    foreach ($path in @($continuation['adopt_paths'])) {
        if ($priorBaselineDirtyPaths -contains [string]$path) { Stop-Foreman -Code 'CONTINUATION' -Message ('Adopted path was already dirty before the prior Foreman run and remains user-owned: ' + $path) }
        if ($writerPaths -notcontains [string]$path) { Stop-Foreman -Code 'CONTINUATION' -Message ('Adopted WIP path is not owned by a writer in the new revision: ' + $path) }
        if ($managedProtectedPaths -contains [string]$path) { Stop-Foreman -Code 'CONTINUATION' -Message ('Adopted WIP path is protected by must_not_modify in the new revision: ' + $path) }
        if ($null -eq (Get-SnapshotEntryByPath -Snapshot $receipt['workspace_snapshot'] -Path ([string]$path))) { Stop-Foreman -Code 'CONTINUATION' -Message ('Adopted WIP path is absent from the terminal receipt: ' + $path) }
        $adoptedPaths += [string]$path
    }
    $continuation['verified_at_utc'] = [DateTime]::UtcNow.ToString('o')
    return @($adoptedPaths)
}

function Assert-LockedManifestIntegrity {
    param([System.Collections.IDictionary]$LockInfo, [string]$RequestedManifestPath)
    $lock = $LockInfo.value
    if (-not $lock.Contains('manifest_path') -or -not $lock.Contains('manifest_sha256') -or -not $lock.Contains('ledger_path') -or [string]::IsNullOrWhiteSpace([string]$lock['manifest_sha256'])) {
        Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'The active Foreman lock is missing immutable manifest metadata.'
    }
    $lockedManifestPath = [System.IO.Path]::GetFullPath([string]$lock['manifest_path'])
    if (-not [string]::IsNullOrWhiteSpace($RequestedManifestPath) -and [System.IO.Path]::GetFullPath($RequestedManifestPath) -ine $lockedManifestPath) {
        Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'The requested manifest does not match the active Foreman lock.'
    }
    $actualHash = Get-ManifestPathHash -Path $lockedManifestPath
    if ($actualHash -eq [string]$lock['manifest_sha256']) { return }

    $lock['state'] = 'ESCALATE'
    $lock['active_writer'] = $null
    $lock['active_readonly'] = @()
    Write-Lock -Path $LockInfo.path -Value $lock
    Append-LedgerEvent -LedgerPath ([string]$lock['ledger_path']) -Event ([ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        event = 'manifest_integrity_violation'
        status = 'ESCALATE'
        reason = 'manifest_sha256_changed'
    })
    [void](Write-TerminalHandoffReceipt -Root ([string]$lock['workspace_root']) -Lock $lock -LockPath $LockInfo.path)
    Stop-Foreman -Code 'IMMUTABLE' -Message 'The approved manifest changed after its lock was created; create a new revision.'
}

function Get-PriorRevisionFailureEvidence {
    param([string]$ForemanRoot, [string]$SpecSystem, [string]$ChangeId)
    $changeRunRoot = Join-Path (Join-Path (Join-Path $ForemanRoot 'runs') $SpecSystem) $ChangeId
    if (-not (Test-Path -LiteralPath $changeRunRoot -PathType Container)) { return @() }

    $trustedReceiptHashes = @{}
    foreach ($archivePattern in @('archived-lock-*.json', 'abandoned-lock-*.json')) {
        foreach ($archivedFile in @(Get-ChildItem -LiteralPath $ForemanRoot -Filter $archivePattern -File -ErrorAction SilentlyContinue)) {
            $archivedLock = $null
            try { $archivedLock = Read-JsonMap -Path $archivedFile.FullName -Context 'archived Foreman lock' } catch { continue }
            if (-not $archivedLock.Contains('handoff_path') -or -not $archivedLock.Contains('handoff_sha256')) { continue }
            $archivedReceiptPath = [string]$archivedLock['handoff_path']
            $archivedReceiptHash = [string]$archivedLock['handoff_sha256']
            if ([string]::IsNullOrWhiteSpace($archivedReceiptPath) -or $archivedReceiptHash -notmatch '^[a-fA-F0-9]{64}$') { continue }
            $trustedReceiptHashes[[System.IO.Path]::GetFullPath($archivedReceiptPath)] = $archivedReceiptHash.ToLowerInvariant()
        }
    }
    $changeRunPrefix = [System.IO.Path]::GetFullPath($changeRunRoot).TrimEnd([char]92, [char]47) + [System.IO.Path]::DirectorySeparatorChar
    foreach ($trustedReceiptPath in @($trustedReceiptHashes.Keys)) {
        if (-not ([string]$trustedReceiptPath).StartsWith($changeRunPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        if (-not (Test-Path -LiteralPath $trustedReceiptPath -PathType Leaf)) {
            Stop-Foreman -Code 'IMMUTABLE' -Message ('Archived terminal handoff receipt is missing: ' + [string]$trustedReceiptPath)
        }
        if ((Get-Sha256 -Path ([string]$trustedReceiptPath)) -ne [string]$trustedReceiptHashes[$trustedReceiptPath]) {
            Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt changed after its lock was archived: ' + [string]$trustedReceiptPath)
        }
    }

    $result = @()
    foreach ($revisionDirectory in @(Get-ChildItem -LiteralPath $changeRunRoot -Directory -ErrorAction SilentlyContinue)) {
        foreach ($receiptFile in @(Get-ChildItem -LiteralPath $revisionDirectory.FullName -Filter '*.handoff.json' -File -ErrorAction SilentlyContinue)) {
            $receiptPath = [System.IO.Path]::GetFullPath($receiptFile.FullName)
            if (-not $trustedReceiptHashes.ContainsKey($receiptPath)) {
                Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt has no archived lock trust record: ' + $receiptPath)
            }
            if ((Get-Sha256 -Path $receiptPath) -ne [string]$trustedReceiptHashes[$receiptPath]) {
                Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt changed after its lock was archived: ' + $receiptPath)
            }
            $receipt = $null
            try { $receipt = Read-JsonMap -Path $receiptPath -Context 'terminal handoff receipt' }
            catch { Stop-Foreman -Code 'IMMUTABLE' -Message ('Unable to read terminal failure evidence: ' + $receiptPath) }
            if ([string]$receipt['schema'] -ne 'provost-foreman-handoff/v1') { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt schema is invalid: ' + $receiptPath) }
            if ([string]$receipt['spec_system'] -ne $SpecSystem -or [string]$receipt['change_id'] -ne $ChangeId) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt lineage does not match its run directory: ' + $receiptPath) }
            $revisionId = [string]$receipt['revision']
            if ($revisionId -notmatch '^r\d{3}$' -or [string]$revisionDirectory.Name -ne $revisionId) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt revision does not match its run directory: ' + $receiptPath) }
            $expectedManifestPath = Join-Path (Join-Path (Join-Path (Join-Path $ForemanRoot 'manifests') $SpecSystem) $ChangeId) ($revisionId + '.json')
            if ([System.IO.Path]::GetFullPath([string]$receipt['manifest_path']) -ine [System.IO.Path]::GetFullPath($expectedManifestPath) -or -not (Test-Path -LiteralPath $expectedManifestPath -PathType Leaf) -or (Get-Sha256 -Path $expectedManifestPath) -ne [string]$receipt['manifest_sha256']) {
                Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt manifest evidence is invalid: ' + $receiptPath)
            }
            $receiptLedgerPath = [System.IO.Path]::GetFullPath([string]$receipt['ledger_path'])
            $revisionRunPrefix = [System.IO.Path]::GetFullPath($revisionDirectory.FullName).TrimEnd([char]92, [char]47) + [System.IO.Path]::DirectorySeparatorChar
            if (-not $receiptLedgerPath.StartsWith($revisionRunPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $receiptLedgerPath -PathType Leaf) -or [string]$receipt['ledger_sha256'] -notmatch '^[a-fA-F0-9]{64}$' -or (Get-Sha256 -Path $receiptLedgerPath) -ne ([string]$receipt['ledger_sha256']).ToLowerInvariant()) {
                Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal handoff receipt ledger evidence is invalid: ' + $receiptPath)
            }
            if (@('FAIL', 'BLOCKED') -notcontains [string]$receipt['terminal_state']) { continue }
            $signatureHash = $null
            $hasSignature = $receipt.Contains('failure_signature')
            $hasSignatureHash = $receipt.Contains('failure_signature_sha256')
            if ($hasSignature -ne $hasSignatureHash) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal failure evidence is incomplete: ' + $receiptPath) }
            if ($hasSignature) {
                if (-not ($receipt['failure_signature'] -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal failure signature is not a JSON object: ' + $receiptPath) }
                $signatureHash = [string]$receipt['failure_signature_sha256']
                if ($signatureHash -notmatch '^[a-fA-F0-9]{64}$' -or (Get-JsonValueSha256 -Value $receipt['failure_signature']) -ne $signatureHash.ToLowerInvariant()) {
                    Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal failure signature hash is invalid: ' + $receiptPath)
                }
                $signatureHash = $signatureHash.ToLowerInvariant()
            }
            $diagnosisHash = $null
            $hasDiagnosis = $receipt.Contains('diagnosis')
            $hasDiagnosisHash = $receipt.Contains('diagnosis_sha256')
            if ($hasDiagnosis -ne $hasDiagnosisHash) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal diagnosis evidence is incomplete: ' + $receiptPath) }
            if ($hasDiagnosis) {
                if (-not ($receipt['diagnosis'] -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal diagnosis is not a JSON object: ' + $receiptPath) }
                $diagnosisHash = [string]$receipt['diagnosis_sha256']
                if ($diagnosisHash -notmatch '^[a-fA-F0-9]{64}$' -or (Get-DiagnosisEvidenceSha256 -Diagnosis $receipt['diagnosis']) -ne $diagnosisHash.ToLowerInvariant()) {
                    Stop-Foreman -Code 'IMMUTABLE' -Message ('Terminal diagnosis hash is invalid: ' + $receiptPath)
                }
                $diagnosisHash = $diagnosisHash.ToLowerInvariant()
            }
            $result += ,([ordered]@{
                receipt = $receiptFile.FullName
                revision = $revisionId
                revision_number = [int]$revisionId.Substring(1)
                signature_sha256 = $signatureHash
                diagnosis_sha256 = $diagnosisHash
            })
        }
    }
    return @($result)
}

function Assert-NoRepeatedFailureLoop {
    param([System.Collections.IDictionary]$Draft, [string]$ForemanRoot, [string]$SpecSystem, [string]$ChangeId, [object[]]$Tasks)
    if ([string]$Draft['schema'] -ne 'provost-foreman-manifest/v2') { return }

    $priorFailures = @(Get-PriorRevisionFailureEvidence -ForemanRoot $ForemanRoot -SpecSystem $SpecSystem -ChangeId $ChangeId)
    if ($priorFailures.Count -lt 2) { return }

    $latestFailure = @($priorFailures | Sort-Object { [int]$_['revision_number'] })[-1]
    $repeatedSignature = [string]$latestFailure['signature_sha256']
    if ([string]::IsNullOrWhiteSpace($repeatedSignature)) { return }
    $repeatedCount = @($priorFailures | Where-Object { [string]$_['signature_sha256'] -eq $repeatedSignature }).Count
    if ($repeatedCount -lt 2) { return }

    $diagnosis = Get-ManifestDiagnosisEvidence -Approval $Draft['approval']
    if ($null -eq $diagnosis) {
        Stop-Foreman -Code 'ESCALATE' -Message ('Failure signature ' + $repeatedSignature + ' has already occurred ' + $repeatedCount + ' times on this change_id. Add approval.diagnosis with the signature, a falsifiable hypothesis, a measurement, and an evidence delta before another attempt.')
    }
    if ([string]$diagnosis['failure_signature_sha256'] -ne $repeatedSignature) {
        Stop-Foreman -Code 'ESCALATE' -Message ('approval.diagnosis targets failure signature ' + [string]$diagnosis['failure_signature_sha256'] + ', but the repeated signature requiring diagnosis is ' + $repeatedSignature + '.')
    }

    $diagnosisHash = Get-DiagnosisEvidenceSha256 -Diagnosis $diagnosis
    foreach ($priorFailure in $priorFailures) {
        if ([string]$priorFailure['signature_sha256'] -eq $repeatedSignature -and [string]$priorFailure['diagnosis_sha256'] -eq $diagnosisHash) {
            Stop-Foreman -Code 'ESCALATE' -Message ('Failure signature ' + $repeatedSignature + ' still has no new diagnosis evidence delta. Change the falsifiable hypothesis, measurement, or evidence_delta before another attempt.')
        }
    }
}

function Invoke-Initialize {
    if ([string]::IsNullOrWhiteSpace($DraftPath) -or [string]::IsNullOrWhiteSpace($ManifestPath) -or [string]::IsNullOrWhiteSpace($WorkspaceRoot) -or [string]::IsNullOrWhiteSpace($SessionId)) { Stop-Foreman -Code 'SCHEMA' -Message 'Initialize requires DraftPath, ManifestPath, WorkspaceRoot, and SessionId.' }
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { Stop-Foreman -Code 'PATH' -Message 'WorkspaceRoot does not exist.' }
    $foremanRoot = Get-ForemanRoot -Root $root
    $absoluteManifestPath = [System.IO.Path]::GetFullPath($ManifestPath)
    $foremanManifestsPrefix = (Join-Path $foremanRoot 'manifests').TrimEnd([char]92) + [char]92
    if (-not $absoluteManifestPath.StartsWith($foremanManifestsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { Stop-Foreman -Code 'PATH' -Message 'ManifestPath must be under the private Foreman manifests directory.' }
    if (Test-Path -LiteralPath $absoluteManifestPath) { Stop-Foreman -Code 'IMMUTABLE' -Message 'The target manifest revision already exists.' }
    $lockPath = Join-Path $foremanRoot 'active-run.lock'
    if (Test-Path -LiteralPath $lockPath) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message ('A Foreman run is already active: ' + $lockPath) }
    $draft = Read-JsonMap -Path $DraftPath -Context 'Foreman manifest draft'
    $shape = Test-ManifestStructure -Manifest $draft -WorkspaceRoot $root -ManifestPath $absoluteManifestPath
    Assert-NoRepeatedFailureLoop -Draft $draft -ForemanRoot $foremanRoot -SpecSystem $shape.spec_system -ChangeId $shape.change_id -Tasks @($shape.tasks)
    $isGit = Test-GitWorkspace -Root $root
    $workspaceMembers = $null
    if ($isGit) {
        if ($null -ne (Get-ForemanWorkspaceDeclaration -Root $root)) { Stop-Foreman -Code 'SCHEMA' -Message 'A workspace declaration is not allowed inside a Git workspace root.' }
    }
    else {
        $workspaceMembers = Get-ForemanWorkspaceDeclaration -Root $root
    }
    if (-not $isGit -and $null -eq $workspaceMembers) { Stop-Foreman -Code 'NON_GIT' -Message 'Workspace root is not a Git repository. Either git init it, or declare sibling members in .claude/provost/foreman/workspace.json.' }

    $adoptedPaths = @(Resolve-ContinuationAdoption -Root $root -ForemanRoot $foremanRoot -ManifestPath $absoluteManifestPath -Draft $draft -Shape $shape -WorkspaceMembers $workspaceMembers)
    $writerPaths = @()
    foreach ($task in @($shape.tasks)) { if ($script:RoleCatalog[[string]$task['agent_key']].writer) { $writerPaths += @($task['write_set']) } }
    if ($null -ne $workspaceMembers) {
        foreach ($task in @($shape.tasks)) {
            foreach ($scopePath in @(@($task['write_set']) + @($task['must_not_modify']))) {
                $firstSegment = ([string]$scopePath -split '/')[0]
                if ($workspaceMembers -notcontains $firstSegment) { Stop-Foreman -Code 'PATH' -Message ('Task ' + $task['id'] + ' path must target a declared workspace member: ' + $scopePath) }
            }
        }
    }
    $baseline = $null
    $workspaceMode = if ($null -ne $workspaceMembers) { 'workspace' } else { 'git' }
    $assurance = 'FULL'
    if ($workspaceMode -eq 'git') {
        $baseline = Get-GitBaseline -Root $root
        foreach ($dirty in @($baseline['dirty_paths'])) {
            if ($writerPaths -contains [string]$dirty['path'] -and $adoptedPaths -notcontains [string]$dirty['path']) { Stop-Foreman -Code 'DIRTY_OVERLAP' -Message ('Existing dirty path overlaps a writer write_set: ' + $dirty['path']) }
        }
    }
    else {
        $memberBaselines = [ordered]@{}
        foreach ($member in $workspaceMembers) {
            $memberBaseline = Get-GitBaseline -Root (Join-Path $root $member)
            foreach ($dirty in @($memberBaseline['dirty_paths'])) {
                $prefixedDirtyPath = $member + '/' + [string]$dirty['path']
                if ($writerPaths -contains $prefixedDirtyPath -and $adoptedPaths -notcontains $prefixedDirtyPath) { Stop-Foreman -Code 'DIRTY_OVERLAP' -Message ('Existing dirty path overlaps a writer write_set: ' + $prefixedDirtyPath) }
            }
            $memberBaselines[$member] = $memberBaseline
        }
        $baseline = [ordered]@{ captured_at_utc = [DateTime]::UtcNow.ToString('o'); members = $memberBaselines }
    }

    $runId = ('run-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $ledgerRelativePath = ('runs/' + $shape.spec_system + '/' + $shape.change_id + '/' + $shape.revision_id + '/' + $runId + '.jsonl')
    $ledgerPath = Resolve-WorkspaceChild -Root $foremanRoot -RelativePath $ledgerRelativePath -Context 'ledger path'

    $draft['created_at_utc'] = [DateTime]::UtcNow.ToString('o')
    $draft['native_plan']['sha256'] = Get-Sha256 -Path $shape.plan_path
    if ([string]$shape['schema'] -eq 'provost-foreman-manifest/v2') { $draft['intent_sha256'] = Get-ManifestIntentSha256 -Manifest $draft }
    $draft['workspace'] = [ordered]@{ root = $root; mode = $workspaceMode; assurance = $assurance; baseline = $baseline }
    if ($null -ne $workspaceMembers) { $draft['workspace']['members'] = @($workspaceMembers) }
    $draft['run'] = [ordered]@{ id = $runId; ledger_relative_path = $ledgerRelativePath }
    $draft['approval']['approved_at_utc'] = [DateTime]::UtcNow.ToString('o')
    $handoffIntentSha256 = if ($draft.Contains('intent_sha256')) { [string]$draft['intent_sha256'] } else { Get-ManifestIntentSha256 -Manifest $draft }
    $handoffWorkspaceMembers = @()
    if ($null -ne $workspaceMembers) { $handoffWorkspaceMembers = @($workspaceMembers) }
    $handoffDiagnosis = Get-ManifestDiagnosisEvidence -Approval $draft['approval']

    $lock = [ordered]@{
        session_id = $SessionId
        state = 'INITIALIZING'
        workspace_root = $root
        manifest_path = $absoluteManifestPath
        manifest_sha256 = $null
        ledger_path = $ledgerPath
        run_id = $runId
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        active_writer = $null
        active_readonly = @()
        task_states = [ordered]@{}
        retries = [ordered]@{}
        workspace_snapshot = Get-WorkspaceSnapshotData -Root $root -Members $workspaceMembers
        task_baselines = [ordered]@{}
        path_custody = [ordered]@{}
        handoff_metadata = [ordered]@{
            schema = [string]$shape['schema']
            revision = [string]$shape['revision_id']
            spec_system = [string]$shape['spec_system']
            change_id = [string]$shape['change_id']
            native_plan_sha256 = [string]$draft['native_plan']['sha256']
            intent_sha256 = $handoffIntentSha256
            workspace_members = $handoffWorkspaceMembers
        }
    }
    if ($null -ne $handoffDiagnosis) {
        $lock['handoff_metadata']['diagnosis'] = $handoffDiagnosis
        $lock['handoff_metadata']['diagnosis_sha256'] = Get-DiagnosisEvidenceSha256 -Diagnosis $handoffDiagnosis
    }
    $lockDirectory = Split-Path -Parent $lockPath
    if (-not (Test-Path -LiteralPath $lockDirectory)) { New-Item -ItemType Directory -Path $lockDirectory -Force | Out-Null }
    $lockStream = $null
    $temporaryManifest = $null
    try {
        $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $lockWriter = New-Object System.IO.StreamWriter($lockStream, [System.Text.UTF8Encoding]::new($false))
        $lockWriter.Write((ConvertTo-Json -InputObject $lock -Depth 32)); $lockWriter.Flush(); $lockWriter.Dispose(); $lockStream = $null
        Append-LedgerEvent -LedgerPath $ledgerPath -Event ([ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'run_initialized'; run_id = $runId; session_id = $SessionId; assurance = $assurance; task_count = @($shape.tasks).Count })
        $temporaryManifest = $absoluteManifestPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        Write-Utf8NoBom -Path $temporaryManifest -Text (ConvertTo-Json -InputObject $draft -Depth 64)
        if (Test-Path -LiteralPath $absoluteManifestPath) { Stop-Foreman -Code 'IMMUTABLE' -Message 'The target manifest revision already exists.' }
        [System.IO.File]::Move($temporaryManifest, $absoluteManifestPath); $temporaryManifest = $null
        $lock['state'] = 'ACTIVE'; $lock['manifest_sha256'] = Get-ManifestPathHash -Path $absoluteManifestPath; Write-Lock -Path $lockPath -Value $lock
        Remove-Item -LiteralPath $DraftPath -Force -ErrorAction Stop
    }
    catch {
        if ($lockStream) { $lockStream.Dispose() }
        if ($temporaryManifest -and (Test-Path -LiteralPath $temporaryManifest)) { Remove-Item -LiteralPath $temporaryManifest -Force -ErrorAction SilentlyContinue }
        if (-not (Test-Path -LiteralPath $absoluteManifestPath) -and (Test-Path -LiteralPath $lockPath)) { Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue }
        throw
    }
    [pscustomobject]@{ status = 'INITIALIZED'; manifest = $absoluteManifestPath; ledger = $ledgerPath; assurance = $assurance; run_id = $runId }
}

function Invoke-Validate {
    param([string]$ManifestToValidate = $ManifestPath, [string]$RootToValidate = $WorkspaceRoot)
    if ([string]::IsNullOrWhiteSpace($ManifestToValidate) -or [string]::IsNullOrWhiteSpace($RootToValidate)) { Stop-Foreman -Code 'SCHEMA' -Message 'Validate requires ManifestPath and WorkspaceRoot.' }
    $root = Get-NormalizedRoot -Path $RootToValidate
    $manifest = Read-JsonMap -Path $ManifestToValidate -Context 'Foreman manifest'
    $shape = Test-ManifestStructure -Manifest $manifest -WorkspaceRoot $root -ManifestPath $ManifestToValidate
    $nativePlan = $manifest['native_plan']
    if (-not $nativePlan.Contains('sha256') -or (Get-Sha256 -Path $shape.plan_path) -ne [string]$nativePlan['sha256']) { Stop-Foreman -Code 'IMMUTABLE' -Message 'The native Plan changed after approval; create a new manifest revision.' }
    if ([string]$shape['schema'] -eq 'provost-foreman-manifest/v2') {
        if (-not $manifest.Contains('intent_sha256') -or [string]$manifest['intent_sha256'] -ne (Get-ManifestIntentSha256 -Manifest $manifest)) { Stop-Foreman -Code 'IMMUTABLE' -Message 'The V2 manifest intent hash is missing or invalid.' }
    }
    [pscustomobject]@{ status = 'VALID'; manifest = $ManifestToValidate; revision = $shape.revision_id; task_count = @($shape.tasks).Count }
}

function Get-ManifestTask {
    param([System.Collections.IDictionary]$Manifest, [string]$RequestedTaskId)
    $taskId = Assert-String -Value $RequestedTaskId -Context 'TaskId'
    foreach ($task in @($Manifest['tasks'])) {
        if ([string]$task['id'] -eq $taskId) { return $task }
    }
    Stop-Foreman -Code 'SCHEMA' -Message ('Manifest does not contain task ' + $taskId + '.')
}

function Get-ActiveLockForSession {
    param([string]$Root, [string]$ExpectedSessionId)
    $lockInfo = Read-Lock -Root $Root
    $lock = $lockInfo.value
    if ([string]$lock['session_id'] -ne (Assert-String -Value $ExpectedSessionId -Context 'SessionId')) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'The active Foreman lock belongs to another session.' }
    if ([string]$lock['state'] -ne 'ACTIVE') { Stop-Foreman -Code 'ACTIVE_LOCK' -Message ('The Foreman lock is not runnable: ' + [string]$lock['state']) }
    foreach ($name in @('task_states', 'retries', 'active_readonly', 'ledger_path')) {
        if (-not $lock.Contains($name)) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message ('The active Foreman lock is missing ' + $name + '.') }
    }
    return $lockInfo
}

function Get-TaskChangedPaths {
    param([string]$Json)
    if ([string]::IsNullOrWhiteSpace($Json) -or $Json.Trim() -eq '[]') { return @() }
    try { $parsedPaths = ConvertFrom-Json -InputObject $Json -ErrorAction Stop }
    catch { Stop-Foreman -Code 'SCHEMA' -Message ('ChangedFilesJson is invalid: ' + $_.Exception.Message) }
    $normalized = @()
    foreach ($path in @($parsedPaths)) { $normalized += Assert-LiteralWritePath -Path $path -Context 'ChangedFilesJson' }
    return @($normalized | Select-Object -Unique)
}

function Get-AllowedWritePaths {
    param([System.Collections.IDictionary]$Manifest, [System.Collections.IDictionary]$Lock, [System.Collections.IDictionary]$CurrentTask)
    $allowed = @()
    foreach ($task in @($Manifest['tasks'])) {
        $taskId = [string]$task['id']
        $agent = [string]$task['agent_key']
        $state = if ($Lock['task_states'].Contains($taskId)) { [string]$Lock['task_states'][$taskId] } else { $null }
        if ($script:RoleCatalog[$agent].writer -and ($state -eq 'PASS' -or $taskId -eq [string]$CurrentTask['id'])) { $allowed += @($task['write_set']) }
    }
    return @($allowed | Select-Object -Unique)
}

function Set-ScopeEscalated {
    param(
        [System.Collections.IDictionary]$Lock,
        [string]$LockPath,
        [string]$LedgerPath,
        [System.Collections.IDictionary]$CurrentTask,
        [string]$Path,
        [string]$Reason
    )
    $Lock['state'] = 'ESCALATE'
    $Lock['active_writer'] = $null
    $Lock['active_readonly'] = @()
    $taskId = [string]$CurrentTask['id']
    if ($Lock['task_states'].Contains($taskId)) { $Lock['task_states'][$taskId] = 'ESCALATE' }
    Write-Lock -Path $LockPath -Value $Lock
    Append-LedgerEvent -LedgerPath $LedgerPath -Event ([ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        event = 'scope_escalated'
        task_id = $taskId
        path = $Path
        reason = $Reason
        status = 'ESCALATE'
    })
    [void](Write-TerminalHandoffReceipt -Root ([string]$Lock['workspace_root']) -Lock $Lock -LockPath $LockPath)
    Stop-Foreman -Code 'SCOPE_ESCALATE' -Message ('Changed path violates the approved scope (' + $Reason + '): ' + $Path)
}

function Assert-PathWithinApprovedScope {
    param(
        [string]$Path,
        [string]$Reason,
        [System.Collections.IDictionary]$Manifest,
        [System.Collections.IDictionary]$Lock,
        [System.Collections.IDictionary]$CurrentTask,
        [string]$LockPath,
        [string]$LedgerPath
    )
    if (@($CurrentTask['must_not_modify']) -contains $Path) {
        Set-ScopeEscalated -Lock $Lock -LockPath $LockPath -LedgerPath $LedgerPath -CurrentTask $CurrentTask -Path $Path -Reason 'must_not_modify'
    }
    $allowed = Get-AllowedWritePaths -Manifest $Manifest -Lock $Lock -CurrentTask $CurrentTask
    if ($allowed -notcontains $Path) {
        Set-ScopeEscalated -Lock $Lock -LockPath $LockPath -LedgerPath $LedgerPath -CurrentTask $CurrentTask -Path $Path -Reason $Reason
    }
}

function Assert-ReportedTaskPath {
    param(
        [string]$Path,
        [System.Collections.IDictionary]$Task,
        [System.Collections.IDictionary]$Agent,
        [System.Collections.IDictionary]$Lock,
        [string]$LockPath,
        [string]$LedgerPath
    )
    if (@($Task['must_not_modify']) -contains $Path) {
        Set-ScopeEscalated -Lock $Lock -LockPath $LockPath -LedgerPath $LedgerPath -CurrentTask $Task -Path $Path -Reason 'must_not_modify'
    }
    if (-not $Agent.writer) {
        Set-ScopeEscalated -Lock $Lock -LockPath $LockPath -LedgerPath $LedgerPath -CurrentTask $Task -Path $Path -Reason 'readonly_changed_file'
    }
    if (@($Task['write_set']) -notcontains $Path) {
        Set-ScopeEscalated -Lock $Lock -LockPath $LockPath -LedgerPath $LedgerPath -CurrentTask $Task -Path $Path -Reason 'outside_task_write_set'
    }
}

function Get-ManifestWorkspaceMembers {
    param([System.Collections.IDictionary]$Manifest)
    if (-not $Manifest.Contains('workspace')) { return $null }
    $workspace = $Manifest['workspace']
    if (-not ($workspace -is [System.Collections.IDictionary]) -or -not $workspace.Contains('members') -or $null -eq $workspace['members']) { return $null }
    return @(Assert-StringArray -Value $workspace['members'] -Context 'manifest.workspace.members')
}

function Assert-GitScopeForRoot {
    param(
        [string]$GitRoot,
        [string]$PathPrefix,
        [System.Collections.IDictionary]$BaselineMap,
        [System.Collections.IDictionary]$Manifest,
        [System.Collections.IDictionary]$Lock,
        [System.Collections.IDictionary]$CurrentTask,
        [string]$LockPath,
        [string]$LedgerPath
    )
    $privatePrefix = '.claude/provost/foreman/'
    $baselineHead = [string]$BaselineMap['head']
    $currentHead = Get-GitHead -Root $GitRoot
    if ($currentHead -ne $baselineHead) {
        Set-ScopeEscalated -Lock $Lock -LockPath $LockPath -LedgerPath $LedgerPath -CurrentTask $CurrentTask -Path ($PathPrefix + '.git/HEAD') -Reason 'git_head_changed'
    }
    $baselineDirty = @{}
    foreach ($entry in @($BaselineMap['dirty_paths'])) { $baselineDirty[[string]$entry['path']] = $entry }
    $currentDirty = @{}
    foreach ($entry in (Get-GitDirtyEntries -Root $GitRoot)) {
        $path = [string]$entry['path']
        if ($path.StartsWith($privatePrefix, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $currentDirty[$path] = $entry
        if ($baselineDirty.ContainsKey($path)) {
            $baselineEntry = $baselineDirty[$path]
            $fullPath = Join-Path $GitRoot ($path -replace '/', '\\')
            $currentHash = if (Test-Path -LiteralPath $fullPath -PathType Leaf) { Get-Sha256 -Path $fullPath } else { $null }
            if ([string]$baselineEntry['status'] -eq [string]$entry['status'] -and [string]$baselineEntry['sha256'] -eq [string]$currentHash) { continue }
            Assert-PathWithinApprovedScope -Path ($PathPrefix + $path) -Reason 'preexisting_dirty_modified' -Manifest $Manifest -Lock $Lock -CurrentTask $CurrentTask -LockPath $LockPath -LedgerPath $LedgerPath
        }
        else {
            Assert-PathWithinApprovedScope -Path ($PathPrefix + $path) -Reason 'outside_write_set' -Manifest $Manifest -Lock $Lock -CurrentTask $CurrentTask -LockPath $LockPath -LedgerPath $LedgerPath
        }
    }
    foreach ($path in $baselineDirty.Keys) {
        if ($path.StartsWith($privatePrefix, [System.StringComparison]::OrdinalIgnoreCase) -or $currentDirty.ContainsKey($path)) { continue }
        Assert-PathWithinApprovedScope -Path ($PathPrefix + $path) -Reason 'preexisting_dirty_modified' -Manifest $Manifest -Lock $Lock -CurrentTask $CurrentTask -LockPath $LockPath -LedgerPath $LedgerPath
    }
}

function Assert-CurrentScope {
    param([string]$Root, [System.Collections.IDictionary]$Manifest, [System.Collections.IDictionary]$Lock, [System.Collections.IDictionary]$CurrentTask, [string]$LockPath, [string]$LedgerPath)
    $workspaceMembers = Get-ManifestWorkspaceMembers -Manifest $Manifest
    if ($null -ne $workspaceMembers) {
        $baselineMembers = $Manifest['workspace']['baseline']['members']
        foreach ($memberValue in $workspaceMembers) {
            $member = [string]$memberValue
            if (-not $baselineMembers.Contains($member)) { Stop-Foreman -Code 'SCHEMA' -Message ('manifest.workspace.baseline is missing member ' + $member + '.') }
            Assert-GitScopeForRoot -GitRoot (Join-Path $Root $member) -PathPrefix ($member + '/') -BaselineMap $baselineMembers[$member] -Manifest $Manifest -Lock $Lock -CurrentTask $CurrentTask -LockPath $LockPath -LedgerPath $LedgerPath
        }
        return
    }
    Assert-GitScopeForRoot -GitRoot $Root -PathPrefix '' -BaselineMap $Manifest['workspace']['baseline'] -Manifest $Manifest -Lock $Lock -CurrentTask $CurrentTask -LockPath $LockPath -LedgerPath $LedgerPath
}

function Test-ManifestPathSharedByWriters {
    param([System.Collections.IDictionary]$Manifest, [string]$Path)
    $writerCount = 0
    foreach ($candidate in @($Manifest['tasks'])) {
        if ($script:RoleCatalog[[string]$candidate['agent_key']].writer -and @($candidate['write_set']) -contains $Path) { $writerCount++ }
    }
    return ($writerCount -gt 1)
}

function Get-PreviousSharedWriterTask {
    param([System.Collections.IDictionary]$Manifest, [System.Collections.IDictionary]$CurrentTask, [string]$Path)
    $previous = $null
    foreach ($candidate in @($Manifest['tasks'])) {
        if ([string]$candidate['id'] -eq [string]$CurrentTask['id']) { break }
        if ($script:RoleCatalog[[string]$candidate['agent_key']].writer -and @($candidate['write_set']) -contains $Path) { $previous = $candidate }
    }
    return $previous
}

function Get-PathCustodyEvidence {
    param([string]$Root, [string]$Path, [System.Collections.IDictionary]$Snapshot)
    $fullPath = Resolve-WorkspaceChild -Root $Root -RelativePath $Path -Context 'path custody'
    $snapshotEntry = Get-SnapshotEntryByPath -Snapshot $Snapshot -Path $Path
    $kind = 'missing'
    $sha256 = $null
    if (Test-Path -LiteralPath $fullPath -PathType Leaf) { $kind = 'file'; $sha256 = Get-Sha256 -Path $fullPath }
    elseif (Test-Path -LiteralPath $fullPath -PathType Container) { $kind = 'directory' }
    return [ordered]@{
        kind = $kind
        status = if ($null -eq $snapshotEntry) { 'CLEAN' } else { [string]$snapshotEntry['status'] }
        sha256 = $sha256
    }
}

function Test-PathCustodyEvidenceMatches {
    param([System.Collections.IDictionary]$Custody, [System.Collections.IDictionary]$Evidence)
    foreach ($name in @('kind', 'status', 'sha256')) {
        $custodyValue = if ($Custody.Contains($name)) { [string]$Custody[$name] } else { '' }
        $evidenceValue = if ($Evidence.Contains($name)) { [string]$Evidence[$name] } else { '' }
        if ($custodyValue -ne $evidenceValue) { return $false }
    }
    return $true
}

function Invoke-StartTask {
    if ([string]::IsNullOrWhiteSpace($ManifestPath) -or [string]::IsNullOrWhiteSpace($WorkspaceRoot) -or [string]::IsNullOrWhiteSpace($SessionId) -or [string]::IsNullOrWhiteSpace($TaskId)) { Stop-Foreman -Code 'SCHEMA' -Message 'StartTask requires ManifestPath, WorkspaceRoot, SessionId, and TaskId.' }
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    $lockInfo = Get-ActiveLockForSession -Root $root -ExpectedSessionId $SessionId
    Assert-LockedManifestIntegrity -LockInfo $lockInfo -RequestedManifestPath $ManifestPath
    Invoke-Validate | Out-Null
    $manifest = Read-JsonMap -Path $ManifestPath -Context 'Foreman manifest'
    $task = Get-ManifestTask -Manifest $manifest -RequestedTaskId $TaskId
    $lock = $lockInfo.value
    $states = $lock['task_states']
    if ($states.Contains([string]$task['id'])) { Stop-Foreman -Code 'DEPENDENCY' -Message ('Task is already started or finished: ' + $task['id']) }
    foreach ($dependency in @($task['depends_on'])) {
        if (-not $states.Contains([string]$dependency) -or [string]$states[[string]$dependency] -ne 'PASS') { Stop-Foreman -Code 'DEPENDENCY' -Message ('Task dependency is not PASS: ' + $dependency) }
    }
    $agent = $script:RoleCatalog[[string]$task['agent_key']]
    $custodyTransfers = @()
    if ($agent.writer) {
        if (-not [string]::IsNullOrWhiteSpace([string]$lock['active_writer'])) { Stop-Foreman -Code 'ACTIVE_WRITER' -Message ('Writer task already active: ' + $lock['active_writer']) }
        if (@($lock['active_readonly']).Count -gt 0) { Stop-Foreman -Code 'ACTIVE_READONLY' -Message 'A writer task cannot start while read-only tasks are active.' }
        if ([string]$manifest['schema'] -eq 'provost-foreman-manifest/v2') {
            if (-not $lock.Contains('path_custody') -or -not ($lock['path_custody'] -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'CUSTODY' -Message 'The active V2 lock is missing path custody state.' }
            $custodySnapshot = Get-WorkspaceSnapshotData -Root $root -Members (Get-ManifestWorkspaceMembers -Manifest $manifest)
            foreach ($path in @($task['write_set'])) {
                $previousWriter = Get-PreviousSharedWriterTask -Manifest $manifest -CurrentTask $task -Path ([string]$path)
                if ($null -eq $previousWriter) { continue }
                $previousTaskId = [string]$previousWriter['id']
                if (-not $states.Contains($previousTaskId) -or [string]$states[$previousTaskId] -ne 'PASS') { Stop-Foreman -Code 'CUSTODY' -Message ('Previous shared-path writer is not PASS: ' + $previousTaskId) }
                if (-not $lock['path_custody'].Contains([string]$path) -or [string]$lock['path_custody'][[string]$path]['task_id'] -ne $previousTaskId) { Stop-Foreman -Code 'CUSTODY' -Message ('Shared path lacks custody from the previous writer: ' + $path) }
                $evidence = Get-PathCustodyEvidence -Root $root -Path ([string]$path) -Snapshot $custodySnapshot
                if (-not (Test-PathCustodyEvidenceMatches -Custody $lock['path_custody'][[string]$path] -Evidence $evidence)) {
                    Set-ScopeEscalated -Lock $lock -LockPath $lockInfo.path -LedgerPath ([string]$lock['ledger_path']) -CurrentTask $task -Path ([string]$path) -Reason 'custody_drift'
                }
                $lock['path_custody'][[string]$path] = [ordered]@{
                    task_id = [string]$task['id']
                    received_from_task_id = $previousTaskId
                    kind = [string]$evidence['kind']
                    status = [string]$evidence['status']
                    sha256 = $evidence['sha256']
                    recorded_at_utc = [DateTime]::UtcNow.ToString('o')
                }
                $custodyTransfers += ,[ordered]@{ path = [string]$path; from_task_id = $previousTaskId; to_task_id = [string]$task['id']; sha256 = $evidence['sha256'] }
            }
        }
        $lock['active_writer'] = [string]$task['id']
    }
    else {
        if (-not [string]::IsNullOrWhiteSpace([string]$lock['active_writer'])) { Stop-Foreman -Code 'ACTIVE_WRITER' -Message ('Read-only task cannot start while writer task is active: ' + $lock['active_writer']) }
        $activeReadonly = @($lock['active_readonly'])
        if ($activeReadonly.Count -ge 3) { Stop-Foreman -Code 'READONLY_LIMIT' -Message 'At most three read-only tasks may run concurrently.' }
        $lock['active_readonly'] = @($activeReadonly + [string]$task['id'])
    }
    if (-not ($lock['task_baselines'] -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'LOCK' -Message 'Foreman lock is missing task baseline state.' }
    $lock['task_baselines'][[string]$task['id']] = Get-WorkspaceSnapshotData -Root $root -Members (Get-ManifestWorkspaceMembers -Manifest $manifest)
    $states[[string]$task['id']] = 'RUNNING'
    Write-Lock -Path $lockInfo.path -Value $lock
    foreach ($transfer in $custodyTransfers) {
        Append-LedgerEvent -LedgerPath ([string]$lock['ledger_path']) -Event ([ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'path_custody_transferred'; path = $transfer['path']; from_task_id = $transfer['from_task_id']; to_task_id = $transfer['to_task_id']; sha256 = $transfer['sha256']; status = 'TRANSFERRED' })
    }
    Append-LedgerEvent -LedgerPath ([string]$lock['ledger_path']) -Event ([ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'task_started'; task_id = $task['id']; agent_key = $task['agent_key']; model = $agent.model; effort = $agent.effort; status = 'RUNNING' })
    [pscustomobject]@{ status = 'RUNNING'; task_id = $task['id']; agent_key = $task['agent_key']; model = $agent.model; effort = $agent.effort }
}

function Invoke-FinishTask {
    if ([string]::IsNullOrWhiteSpace($ManifestPath) -or [string]::IsNullOrWhiteSpace($WorkspaceRoot) -or [string]::IsNullOrWhiteSpace($SessionId) -or [string]::IsNullOrWhiteSpace($TaskId) -or [string]::IsNullOrWhiteSpace($Outcome)) { Stop-Foreman -Code 'SCHEMA' -Message 'FinishTask requires ManifestPath, WorkspaceRoot, SessionId, TaskId, and Outcome.' }
    Assert-ForemanSafeText -Text $VerificationSummary -Context 'VerificationSummary'
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    $lockInfo = Get-ActiveLockForSession -Root $root -ExpectedSessionId $SessionId
    Assert-LockedManifestIntegrity -LockInfo $lockInfo -RequestedManifestPath $ManifestPath
    Invoke-Validate | Out-Null
    $manifest = Read-JsonMap -Path $ManifestPath -Context 'Foreman manifest'
    $failureEvidence = Get-FailureSignatureEvidence -Json $FailureSignatureJson
    if ([string]$manifest['schema'] -eq 'provost-foreman-manifest/v2' -and $Outcome -eq 'FAIL' -and $null -eq $failureEvidence) {
        Stop-Foreman -Code 'SCHEMA' -Message 'A V2 FAIL outcome requires FailureSignatureJson.'
    }
    if ($null -ne $failureEvidence) {
        if ($Outcome -eq 'PASS') { Stop-Foreman -Code 'SCHEMA' -Message 'FailureSignatureJson is only valid for a non-PASS outcome.' }
        if ([string]$manifest['schema'] -ne 'provost-foreman-manifest/v2') { Stop-Foreman -Code 'SCHEMA' -Message 'FailureSignatureJson requires provost-foreman-manifest/v2.' }
    }
    $task = Get-ManifestTask -Manifest $manifest -RequestedTaskId $TaskId
    $lock = $lockInfo.value
    if (-not $lock['task_states'].Contains([string]$task['id']) -or [string]$lock['task_states'][[string]$task['id']] -ne 'RUNNING') { Stop-Foreman -Code 'DEPENDENCY' -Message ('Task is not running: ' + $task['id']) }
    $reportedChangedPaths = @(Get-TaskChangedPaths -Json $ChangedFilesJson)
    $agent = $script:RoleCatalog[[string]$task['agent_key']]
    $ledgerPath = [string]$lock['ledger_path']
    foreach ($path in $reportedChangedPaths) {
        Assert-ReportedTaskPath -Path $path -Task $task -Agent $agent -Lock $lock -LockPath $lockInfo.path -LedgerPath $ledgerPath
    }
    if (-not ($lock['task_baselines'] -is [System.Collections.IDictionary]) -or -not $lock['task_baselines'].Contains([string]$task['id'])) { Stop-Foreman -Code 'LOCK' -Message ('Foreman lock is missing the task baseline: ' + $task['id']) }
    $beforeSnapshot = $lock['task_baselines'][[string]$task['id']]
    $rawAfterSnapshot = Get-WorkspaceSnapshotData -Root $root -Members (Get-ManifestWorkspaceMembers -Manifest $manifest)
    [void](Remove-NewGeneratedArtifacts -Root $root -BeforeSnapshot $beforeSnapshot -AfterSnapshot $rawAfterSnapshot -LedgerPath $ledgerPath -TaskId ([string]$task['id']))
    $afterSnapshot = Get-WorkspaceSnapshotData -Root $root -Members (Get-ManifestWorkspaceMembers -Manifest $manifest)
    Assert-CurrentScope -Root $root -Manifest $manifest -Lock $lock -CurrentTask $task -LockPath $lockInfo.path -LedgerPath $ledgerPath
    $observedChangedPaths = @(Get-SnapshotChangedPaths -Before $beforeSnapshot -After $afterSnapshot)
    foreach ($path in $observedChangedPaths) {
        Assert-ReportedTaskPath -Path $path -Task $task -Agent $agent -Lock $lock -LockPath $lockInfo.path -LedgerPath $ledgerPath
    }
    $reportedByPath = @{}
    foreach ($path in $reportedChangedPaths) { $reportedByPath[$path] = $true }
    $observedByPath = @{}
    foreach ($path in $observedChangedPaths) { $observedByPath[$path] = $true }
    $evidenceMismatchPath = @($reportedByPath.Keys | Where-Object { -not $observedByPath.ContainsKey($_) } | Select-Object -First 1)
    if ($evidenceMismatchPath.Count -eq 0) { $evidenceMismatchPath = @($observedByPath.Keys | Where-Object { -not $reportedByPath.ContainsKey($_) } | Select-Object -First 1) }
    if ($evidenceMismatchPath.Count -gt 0) {
        Set-ScopeEscalated -Lock $lock -LockPath $lockInfo.path -LedgerPath $ledgerPath -CurrentTask $task -Path ([string]$evidenceMismatchPath[0]) -Reason 'changed_files_mismatch'
    }
    $lock['workspace_snapshot'] = $afterSnapshot
    $lock['task_baselines'].Remove([string]$task['id'])
    if ($agent.writer) { $lock['active_writer'] = $null }
    else { $lock['active_readonly'] = @($lock['active_readonly'] | Where-Object { [string]$_ -ne [string]$task['id'] }) }
    $lock['task_states'][[string]$task['id']] = $Outcome
    $custodyRecords = @()
    if ($Outcome -eq 'PASS' -and $agent.writer -and [string]$manifest['schema'] -eq 'provost-foreman-manifest/v2') {
        if (-not $lock.Contains('path_custody') -or -not ($lock['path_custody'] -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'CUSTODY' -Message 'The active V2 lock is missing path custody state.' }
        foreach ($path in @($task['write_set'])) {
            if (-not (Test-ManifestPathSharedByWriters -Manifest $manifest -Path ([string]$path))) { continue }
            $evidence = Get-PathCustodyEvidence -Root $root -Path ([string]$path) -Snapshot $afterSnapshot
            $receivedFromTaskId = $null
            if ($lock['path_custody'].Contains([string]$path) -and $lock['path_custody'][[string]$path].Contains('received_from_task_id')) { $receivedFromTaskId = [string]$lock['path_custody'][[string]$path]['received_from_task_id'] }
            $custody = [ordered]@{
                task_id = [string]$task['id']
                kind = [string]$evidence['kind']
                status = [string]$evidence['status']
                sha256 = $evidence['sha256']
                recorded_at_utc = [DateTime]::UtcNow.ToString('o')
            }
            if (-not [string]::IsNullOrWhiteSpace($receivedFromTaskId)) { $custody['received_from_task_id'] = $receivedFromTaskId }
            $lock['path_custody'][[string]$path] = $custody
            $custodyRecords += ,[ordered]@{ path = [string]$path; task_id = [string]$task['id']; sha256 = $evidence['sha256'] }
        }
    }
    if ($null -ne $failureEvidence) {
        $lock['failure_signature'] = $failureEvidence['signature']
        $lock['failure_signature_sha256'] = [string]$failureEvidence['sha256']
    }
    if ($Outcome -ne 'PASS') { $lock['state'] = $Outcome }
    Write-Lock -Path $lockInfo.path -Value $lock
    $summary = if ([string]::IsNullOrWhiteSpace($VerificationSummary)) { $null } else { $VerificationSummary }
    $taskFinishedEvent = [ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'task_finished'; task_id = $task['id']; agent_key = $task['agent_key']; model = $agent.model; effort = $agent.effort; changed_files = @($observedChangedPaths); verification_summary = $summary; retry_count = if ($lock['retries'].Contains([string]$task['id'])) { [int]$lock['retries'][[string]$task['id']] } else { 0 }; status = $Outcome }
    if ($lock.Contains('failure_signature') -and $lock.Contains('failure_signature_sha256')) {
        $taskFinishedEvent['failure_signature'] = $lock['failure_signature']
        $taskFinishedEvent['failure_signature_sha256'] = [string]$lock['failure_signature_sha256']
    }
    Append-LedgerEvent -LedgerPath $ledgerPath -Event $taskFinishedEvent
    foreach ($custodyRecord in $custodyRecords) {
        Append-LedgerEvent -LedgerPath $ledgerPath -Event ([ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'path_custody_recorded'; path = $custodyRecord['path']; task_id = $custodyRecord['task_id']; sha256 = $custodyRecord['sha256']; status = 'RECORDED' })
    }
    if ($Outcome -ne 'PASS') { [void](Write-TerminalHandoffReceipt -Root $root -Lock $lock -LockPath $lockInfo.path) }
    [pscustomobject]@{ status = $Outcome; task_id = $task['id']; changed_files = @($observedChangedPaths) }
}

function Invoke-RecordRetry {
    if ([string]::IsNullOrWhiteSpace($ManifestPath) -or [string]::IsNullOrWhiteSpace($WorkspaceRoot) -or [string]::IsNullOrWhiteSpace($SessionId) -or [string]::IsNullOrWhiteSpace($TaskId) -or [string]::IsNullOrWhiteSpace($RetryKind)) { Stop-Foreman -Code 'SCHEMA' -Message 'RecordRetry requires ManifestPath, WorkspaceRoot, SessionId, TaskId, and RetryKind.' }
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    $lockInfo = Get-ActiveLockForSession -Root $root -ExpectedSessionId $SessionId
    Assert-LockedManifestIntegrity -LockInfo $lockInfo -RequestedManifestPath $ManifestPath
    $manifest = Read-JsonMap -Path $ManifestPath -Context 'Foreman manifest'
    $task = Get-ManifestTask -Manifest $manifest -RequestedTaskId $TaskId
    $lock = $lockInfo.value
    if (-not $lock['task_states'].Contains([string]$task['id']) -or [string]$lock['task_states'][[string]$task['id']] -ne 'RUNNING') { Stop-Foreman -Code 'RETRY' -Message 'Only a running task may record a retry.' }
    $count = if ($lock['retries'].Contains([string]$task['id'])) { [int]$lock['retries'][[string]$task['id']] } else { 0 }
    if ($count -ge 1) { Stop-Foreman -Code 'RETRY' -Message 'Only one transient retry is allowed for a task.' }
    $lock['retries'][[string]$task['id']] = 1
    Write-Lock -Path $lockInfo.path -Value $lock
    $agent = $script:RoleCatalog[[string]$task['agent_key']]
    Append-LedgerEvent -LedgerPath ([string]$lock['ledger_path']) -Event ([ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'retry'; task_id = $task['id']; agent_key = $task['agent_key']; model = $agent.model; effort = $agent.effort; classification = $RetryKind; retry_count = 1 })
    [pscustomobject]@{ status = 'RETRY_RECORDED'; task_id = $task['id']; classification = $RetryKind }
}

function Invoke-Complete {
    if ([string]::IsNullOrWhiteSpace($WorkspaceRoot) -or [string]::IsNullOrWhiteSpace($SessionId) -or [string]::IsNullOrWhiteSpace($Outcome)) { Stop-Foreman -Code 'SCHEMA' -Message 'Complete requires WorkspaceRoot, SessionId, and Outcome.' }
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    $lockInfo = Get-ActiveLockForSession -Root $root -ExpectedSessionId $SessionId
    $lock = $lockInfo.value
    Assert-LockedManifestIntegrity -LockInfo $lockInfo -RequestedManifestPath ([string]$lock['manifest_path'])
    Invoke-Validate -ManifestToValidate ([string]$lock['manifest_path']) -RootToValidate $root | Out-Null
    $manifest = Read-JsonMap -Path ([string]$lock['manifest_path']) -Context 'Foreman manifest'
    $ledgerPath = [string]$lock['ledger_path']
    $failureEvidence = Get-FailureSignatureEvidence -Json $FailureSignatureJson
    if ([string]$manifest['schema'] -eq 'provost-foreman-manifest/v2' -and $Outcome -eq 'FAIL' -and $null -eq $failureEvidence) {
        Stop-Foreman -Code 'SCHEMA' -Message 'A V2 FAIL outcome requires FailureSignatureJson.'
    }
    if ($null -ne $failureEvidence) {
        if ($Outcome -eq 'PASS') { Stop-Foreman -Code 'SCHEMA' -Message 'FailureSignatureJson is only valid for a non-PASS outcome.' }
        if ([string]$manifest['schema'] -ne 'provost-foreman-manifest/v2') { Stop-Foreman -Code 'SCHEMA' -Message 'FailureSignatureJson requires provost-foreman-manifest/v2.' }
        $lock['failure_signature'] = $failureEvidence['signature']
        $lock['failure_signature_sha256'] = [string]$failureEvidence['sha256']
    }
    if ($Outcome -eq 'PASS') {
        foreach ($task in @($manifest['tasks'])) {
            $taskId = [string]$task['id']
            if (-not $lock['task_states'].Contains($taskId) -or [string]$lock['task_states'][$taskId] -ne 'PASS') { Stop-Foreman -Code 'VERIFY' -Message ('Cannot complete PASS while task is not PASS: ' + $taskId) }
        }
        if (-not ($lock['workspace_snapshot'] -is [System.Collections.IDictionary])) { Stop-Foreman -Code 'LOCK' -Message 'Foreman lock is missing the approved workspace snapshot.' }
        $rawFinalSnapshot = Get-WorkspaceSnapshotData -Root $root -Members (Get-ManifestWorkspaceMembers -Manifest $manifest)
        [void](Remove-NewGeneratedArtifacts -Root $root -BeforeSnapshot $lock['workspace_snapshot'] -AfterSnapshot $rawFinalSnapshot -LedgerPath $ledgerPath -TaskId '__complete__')
        $finalSnapshot = Get-WorkspaceSnapshotData -Root $root -Members (Get-ManifestWorkspaceMembers -Manifest $manifest)
        Assert-CurrentScope -Root $root -Manifest $manifest -Lock $lock -CurrentTask ([ordered]@{ id = '__complete__'; must_not_modify = @() }) -LockPath $lockInfo.path -LedgerPath $ledgerPath
        foreach ($path in (Get-SnapshotChangedPaths -Before $lock['workspace_snapshot'] -After $finalSnapshot)) {
            Set-ScopeEscalated -Lock $lock -LockPath $lockInfo.path -LedgerPath $ledgerPath -CurrentTask ([ordered]@{ id = '__complete__'; must_not_modify = @() }) -Path $path -Reason 'post_task_mutation'
        }
    }
    $completionEvent = [ordered]@{ timestamp_utc = [DateTime]::UtcNow.ToString('o'); event = 'run_completed'; outcome = $Outcome; session_id = $SessionId; manifest_sha256 = Get-ManifestPathHash -Path ([string]$lock['manifest_path']) }
    if ($lock.Contains('failure_signature') -and $lock.Contains('failure_signature_sha256')) {
        $completionEvent['failure_signature'] = $lock['failure_signature']
        $completionEvent['failure_signature_sha256'] = [string]$lock['failure_signature_sha256']
    }
    Append-LedgerEvent -LedgerPath $ledgerPath -Event $completionEvent
    if ($Outcome -eq 'PASS') {
        Remove-Item -LiteralPath $lockInfo.path -Force -ErrorAction Stop
    }
    else {
        $lock['state'] = $Outcome
        Write-Lock -Path $lockInfo.path -Value $lock
        [void](Write-TerminalHandoffReceipt -Root $root -Lock $lock -LockPath $lockInfo.path)
    }
    [pscustomobject]@{ status = $Outcome }
}

function New-LockArchivePath {
    param([string]$LockPath, [string]$Prefix)
    # A second-resolution stamp alone collided whenever two locks were archived
    # inside the same second, and Copy-Item overwrites silently. The earlier
    # archive was lost, and with it the trust record for a handoff receipt still
    # on disk, which a later Initialize then reported as tampering. Carry a
    # random suffix as run identifiers already do, and refuse to overwrite.
    $directory = Split-Path -Parent $LockPath
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $archivePath = Join-Path $directory ($Prefix + '-' + $stamp + '-' + $suffix + '.json')
    if (Test-Path -LiteralPath $archivePath) {
        Stop-Foreman -Code 'IMMUTABLE' -Message ('Refusing to overwrite an existing lock archive: ' + $archivePath)
    }
    return $archivePath
}

function Invoke-CloseBlocked {
    if (-not $Acknowledge -or [string]::IsNullOrWhiteSpace($WorkspaceRoot)) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'CloseBlocked requires WorkspaceRoot and explicit -Acknowledge.' }
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    $lockInfo = Read-Lock -Root $root
    $state = [string]$lockInfo.value['state']
    if (@('FAIL', 'BLOCKED', 'ESCALATE') -notcontains $state) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'Only a terminal non-PASS lock can be closed.' }
    $archive = New-LockArchivePath -LockPath $lockInfo.path -Prefix 'archived-lock'
    Copy-Item -LiteralPath $lockInfo.path -Destination $archive -ErrorAction Stop
    Remove-Item -LiteralPath $lockInfo.path -Force -ErrorAction Stop
    [pscustomobject]@{ status = 'CLOSED'; archive = $archive }
}

function Invoke-RecoverLock {
    if (-not $Acknowledge -or [string]::IsNullOrWhiteSpace($WorkspaceRoot)) { Stop-Foreman -Code 'ACTIVE_LOCK' -Message 'RecoverLock requires WorkspaceRoot and explicit -Acknowledge.' }
    $root = Get-NormalizedRoot -Path $WorkspaceRoot
    $lockInfo = Read-Lock -Root $root
    $archive = New-LockArchivePath -LockPath $lockInfo.path -Prefix 'abandoned-lock'
    Copy-Item -LiteralPath $lockInfo.path -Destination $archive -ErrorAction Stop
    Remove-Item -LiteralPath $lockInfo.path -Force -ErrorAction Stop
    [pscustomobject]@{ status = 'RECOVERED'; archive = $archive }
}

$mutatingActions = @('Initialize', 'StartTask', 'FinishTask', 'RecordRetry', 'Complete', 'CloseBlocked', 'RecoverLock')
$lifecycleLease = $null
try {
    if ($mutatingActions -contains $Action) {
        $lifecycleLease = Enter-ForemanLifecycleLease -Root $WorkspaceRoot
    }
    switch ($Action) {
        'Initialize' { Invoke-Initialize }
        'Validate' { Invoke-Validate }
        'StartTask' { Invoke-StartTask }
        'FinishTask' { Invoke-FinishTask }
        'RecordRetry' { Invoke-RecordRetry }
        'Complete' { Invoke-Complete }
        'CloseBlocked' { Invoke-CloseBlocked }
        'RecoverLock' { Invoke-RecoverLock }
    }
}
finally {
    Exit-ForemanLifecycleLease -Lease $lifecycleLease
}
