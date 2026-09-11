# Embedded in the launcher so every PC gets the same upgrade behavior.
function Read-PortableRegistration([string]$Root, [string]$VolumeId) {
    foreach ($directory in @($Root, (Join-Path $Root 'manifests'))) {
        $parent = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
        if (-not $parent.PSIsContainer -or
            ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Unsafe portable metadata directory: $directory"
        }
    }
    $path = Join-Path $Root 'manifests\portable.json'
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Unsafe portable registration: $path"
    }
    $record = [IO.File]::ReadAllText($path) | ConvertFrom-Json
    if ($record.schema_version -ne 1 -or $record.root -ine $Root -or
        $record.volume_unique_id -ine $VolumeId -or
        $record.mode -notin @('dual', 'exclusive')) {
        throw "Invalid portable registration: $path"
    }
    return $record
}

function Get-PortableCliVersion([string]$Executable) {
    $reported = (& $Executable --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $reported -notmatch '^codex-cli\s+(\d+\.\d+\.\d+)$') {
        throw "Cannot determine portable Codex version: $reported"
    }
    return $Matches[1]
}

function Assert-PortablePublisher([string]$Executable) {
    # A PowerShell 7 parent can leave incompatible module paths for the PS 5.1
    # launcher. Load this engine's built-in module explicitly, not via PATH.
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    $signature = Get-AuthenticodeSignature -LiteralPath $Executable -ErrorAction Stop
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.GetNameInfo(
            [Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false
        ) -cne 'OpenAI OpCo, LLC') {
        throw 'Portable upgrade requires a valid OpenAI signature; registration was not changed.'
    }
}

function Get-PortableBinaryHash([string]$Executable) {
    $stream = [IO.File]::OpenRead($Executable)
    $digest = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($digest.ComputeHash($stream)) }
    finally { $digest.Dispose(); $stream.Dispose() }
}

function Sync-PortableCliVersion([string]$Root, [string]$VolumeId, [string]$Executable) {
    $record = Read-PortableRegistration $Root $VolumeId
    $expected = [string]$record.expected_cli_version
    if ($record.status -ne 'complete' -or $expected -notmatch '^\d+\.\d+\.\d+$') {
        throw 'Portable registration is incomplete or has an invalid expected version.'
    }
    $installed = Get-PortableCliVersion $Executable
    if ($installed -eq $expected) { return }
    if ([version]$installed -lt [version]$expected) {
        throw "Portable Codex downgrade refused: expected $expected; found $installed"
    }
    # Authenticate the new binary before accepting its version. Unchanged versions
    # avoid the relatively expensive Windows certificate-chain lookup.
    $beforeHash = Get-PortableBinaryHash $Executable
    Assert-PortablePublisher $Executable
    $lockPath = Join-Path $Root 'manifests\upgrade.lock'
    $lockItem = Get-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $lockItem -and ($lockItem.PSIsContainer -or
        ($lockItem.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
        throw "Unsafe upgrade lock: $lockPath"
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ($true) {
        try { $lease = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None'); break }
        catch [IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) { throw 'Another launcher is registering an upgrade; retry codexv.' }
            Start-Sleep -Milliseconds 100
        }
    }
    try {
        $record = Read-PortableRegistration $Root $VolumeId
        if ($record.status -ne 'complete' -or
            [string]$record.expected_cli_version -notmatch '^\d+\.\d+\.\d+$') {
            throw 'Portable registration changed during upgrade; retry codexv.'
        }
        if ($record.expected_cli_version -eq $installed) { return }
        if ([version]$record.expected_cli_version -gt [version]$installed -or
            (Get-PortableCliVersion $Executable) -ne $installed -or
            (Get-PortableBinaryHash $Executable) -ne $beforeHash) {
            throw 'Portable executable or registration changed during upgrade; retry codexv.'
        }
        $backupDir = Join-Path $Root 'backups'
        $backupItem = Get-Item -LiteralPath $backupDir -Force -ErrorAction Stop
        if (-not $backupItem.PSIsContainer -or
            ($backupItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Unsafe portable backup directory: $backupDir"
        }
        $path = Join-Path $Root 'manifests\portable.json'
        $id = [Guid]::NewGuid().ToString('N')
        $backup = Join-Path $backupDir "version-registration-$id.json"
        $temporary = "$path.$id.tmp"
        $previous = $record.expected_cli_version
        $record.expected_cli_version = $installed
        try {
            [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Depth 100))
            [IO.File]::Replace($temporary, $path, $backup)
        } finally {
            if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
        }
        Write-Host "==> Verified OpenAI upgrade: $previous -> $installed (registration backed up)" -ForegroundColor Cyan
    } finally { $lease.Dispose() }
}

function Get-PortableUserPath { return [Environment]::GetEnvironmentVariable('Path', 'User') }
function Set-PortableUserPath([string]$Value) {
    [Environment]::SetEnvironmentVariable('Path', $Value, 'User')
}

function Repair-PortableUserPath([string]$Root, [string]$VolumeId) {
    $record = Read-PortableRegistration $Root $VolumeId
    if ($record.mode -ne 'dual') { return }
    $portableBin = (Join-Path $Root 'bin').TrimEnd('\')
    # Read the current PATH each time: never restore a stale pre-launch snapshot
    # that would overwrite unrelated PATH changes made while Codex was running.
    $current = Get-PortableUserPath
    if ($null -eq $current) { return }
    $kept = @($current.Split(';') | Where-Object {
        $entry = [Environment]::ExpandEnvironmentVariables($_.Trim().Trim('"')).Replace('/', '\').TrimEnd('\')
        $entry -ine $portableBin
    })
    $updated = $kept -join ';'
    if ($updated -cne $current) {
        Set-PortableUserPath $updated
        Write-Host '==> Restored dual PATH: use codex for LOCAL, codexv for PORTABLE.' -ForegroundColor Cyan
    }
}
