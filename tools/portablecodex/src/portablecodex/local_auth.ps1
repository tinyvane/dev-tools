# Embedded in Start-Codex.ps1; no Python dependency on the destination PC.
# Keep CODEX_HOME stable (including its keyring namespace), but resolve encrypted
# secret files to the current Windows user's LOCALAPPDATA, not the removable disk.
function Get-PortableAuthItem([string]$Path) {
    Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function Assert-PortableAuthDirectory([string]$Path) {
    $item = Get-PortableAuthItem $Path
    if ($null -eq $item -or -not $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Expected an ordinary auth directory: $Path"
    }
}

function Get-PortableAuthTarget([string]$Path) {
    $item = Get-PortableAuthItem $Path
    if ($null -eq $item) { return $null }
    if ($item.LinkType -ne 'Junction' -or @($item.Target).Count -ne 1) {
        return $null
    }
    return [IO.Path]::GetFullPath([string]$item.Target[0]).TrimEnd('\')
}

function Write-PortableAuthMarker([string]$Path, $Record) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, ($Record | ConvertTo-Json -Depth 5))
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace($temporary, $Path, [NullString]::Value)
    } else {
        [IO.File]::Move($temporary, $Path)
    }
}

function Read-PortableAuthMarker([string]$Path, [string]$Root, [string]$VolumeId) {
    $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($record.schema_version -ne 1 -or $record.root -ine $Root -or
        $record.volume_id -ine $VolumeId -or $record.targets -isnot [array] -or
        $record.targets.Count -eq 0) {
        throw "Invalid local auth registration: $Path"
    }
    foreach ($entry in $record.targets) {
        if ($entry -isnot [string] -or -not [IO.Path]::IsPathRooted($entry) -or
            $entry -notmatch '\\PortableCodex\\credentials\\[a-f0-9]{64}\\secrets$') {
            throw "Invalid local auth target registration: $Path"
        }
    }
    return $record
}

function Test-PortableOriginalAuth([string]$Root, [string]$Target) {
    $legacy = Join-Path $Root 'backups\legacy-encrypted-secrets'
    if (-not [IO.File]::Exists((Join-Path $legacy 'codex_auth.age')) -or
        [IO.File]::Exists((Join-Path $Target 'codex_auth.age'))) { return $false }
    $registration = Get-Content -LiteralPath (Join-Path $Root 'manifests\portable.json') -Raw |
        ConvertFrom-Json
    $machine = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Cryptography' `
        -Name MachineGuid -ErrorAction Stop).MachineGuid
    $sourceHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
    return ($registration.schema_version -eq 1 -and
        $registration.root -ieq $Root -and
        $registration.origin_machine_id -ieq $machine -and
        $registration.source_home -ieq $sourceHome)
}

function Restore-PortableOriginalAuth([string]$Root, [string]$Target) {
    if (-not (Test-PortableOriginalAuth $Root $Target)) { return }
    $legacy = Join-Path $Root 'backups\legacy-encrypted-secrets'
    Assert-PortableAuthDirectory $legacy
    # Restore the eligibility sentinel last so a partial failure remains retryable.
    foreach ($name in @('mcp_oauth.age', 'local.age', 'codex_auth.age')) {
        $source = Join-Path $legacy $name
        $destination = Join-Path $Target $name
        $item = Get-PortableAuthItem $source
        if ($null -eq $item) { continue }
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Unsafe archived credential path: $source"
        }
        # Never replace credentials already created on this PC.
        if ($null -eq (Get-PortableAuthItem $destination)) {
            $temporary = "$destination.$([Guid]::NewGuid().ToString('N')).tmp"
            [IO.File]::Copy($source, $temporary, $false)
            [IO.File]::Move($temporary, $destination)
        }
    }
    Write-Host 'Original-PC encrypted credentials restored locally; archive retained.'
}

function Get-PortableLocalAuthBase([string]$Root) {
    $localBase = [Environment]::GetFolderPath('LocalApplicationData')
    if (-not [IO.Path]::IsPathRooted($localBase) -or
        [IO.Path]::GetPathRoot($localBase) -ieq [IO.Path]::GetPathRoot($Root)) {
        throw 'LocalApplicationData must be on a local drive outside the portable volume.'
    }
    return $localBase
}

function Open-PortableLocalAuth([string]$Root, [string]$VolumeId) {
    $localBase = Get-PortableLocalAuthBase $Root
    Assert-PortableAuthDirectory $Root
    foreach ($part in @('home', 'manifests', 'backups')) {
        Assert-PortableAuthDirectory (Join-Path $Root $part)
    }
    $digest = [Security.Cryptography.SHA256]::Create()
    try {
        $identity = ($VolumeId + '|' + $Root).ToLowerInvariant()
        $key = [BitConverter]::ToString($digest.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($identity))).Replace('-', '').ToLowerInvariant()
    } finally { $digest.Dispose() }
    $target = $localBase
    Assert-PortableAuthDirectory $target
    foreach ($part in @('PortableCodex', 'credentials', $key, 'secrets')) {
        $target = Join-Path $target $part
        if ($null -eq (Get-PortableAuthItem $target)) {
            [void][IO.Directory]::CreateDirectory($target)
        }
        Assert-PortableAuthDirectory $target
    }
    $link = Join-Path $Root 'home\secrets'
    $marker = Join-Path $Root 'manifests\local-auth.json'
    $lockPath = Join-Path $Root 'manifests\local-auth.lock'
    foreach ($path in @($marker, $lockPath)) {
        $item = Get-PortableAuthItem $path
        if ($null -ne $item -and ($item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
            throw "Unsafe auth metadata path: $path"
        }
    }
    if (-not [IO.File]::Exists($lockPath)) {
        try {
            $created = [IO.File]::Open($lockPath, 'CreateNew', 'Write', 'None')
            $created.Dispose()
        } catch {
            if (-not [IO.File]::Exists($lockPath)) { throw }
        }
    }
    # Shared lease lasts for the entire Codex process, preventing another launcher
    # from switching the directory underneath a live login/session.
    $lease = [IO.File]::Open($lockPath, 'Open', 'Read', 'Read')
    try {
        $record = $null
        if ([IO.File]::Exists($marker)) {
            $record = Read-PortableAuthMarker $marker $Root $VolumeId
        }
        $currentTarget = Get-PortableAuthTarget $link
        if ($null -ne $record -and $record.targets -contains $target -and
            $currentTarget -ieq $target -and
            -not (Test-PortableOriginalAuth $Root $target)) { return $lease }
    } catch { $lease.Dispose(); throw }
    $lease.Dispose()
    try {
        $exclusive = [IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None')
    } catch {
        throw 'Another codexv is using portable credentials. Exit it and retry.'
    }
    try {
        # Also guard older launchers/direct CLI processes which do not hold a lease.
        # Fail closed if a Codex executable cannot be identified. Never terminate it.
        $blockers = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -in @('codex.exe', 'codex-code-mode-host.exe') -and (
                -not $_.ExecutablePath -or
                $_.ExecutablePath.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase))
        })
        if ($blockers.Count) {
            $labels = $blockers | ForEach-Object {
                "PID $($_.ProcessId): $($_.Name) [$($_.ExecutablePath)]"
            }
            throw "Exit portable Codex before isolating credentials: $($labels -join '; ')"
        }
        # Reread under exclusive ownership, including after an interrupted attempt.
        if ([IO.File]::Exists($marker)) {
            $record = Read-PortableAuthMarker $marker $Root $VolumeId
        }
        $item = Get-PortableAuthItem $link
        $currentTarget = Get-PortableAuthTarget $link
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
            ($null -eq $record -or $null -eq $currentTarget -or
             $record.targets -notcontains $currentTarget)) {
            throw "Refusing to replace an unmanaged secrets link: $link"
        }
        $legacy = Join-Path $Root 'backups\legacy-encrypted-secrets'
        if ($null -ne $item -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Assert-PortableAuthDirectory $link
            if ($null -ne (Get-PortableAuthItem $legacy)) {
                throw "Legacy credential backup already exists; manual review required: $legacy"
            }
        }
        $targets = @($target)
        if ($null -ne $record) { $targets += @($record.targets) }
        Write-PortableAuthMarker $marker @{
            schema_version = 1; root = $Root; volume_id = $VolumeId
            targets = @($targets | Select-Object -Unique)
        }
        if ($null -ne $item) {
            if ($null -ne $currentTarget) {
                # Delete only the validated junction, NEVER recurse into its target.
                [IO.Directory]::Delete($link, $false)
            } else {
                # Same-volume whole-directory rename preserves old encrypted bytes.
                [IO.Directory]::Move($link, $legacy)
                Write-Host "Previous encrypted credentials preserved: $legacy"
            }
        }
        Restore-PortableOriginalAuth $Root $target
        [void](New-Item -ItemType Junction -Path $link -Target $target -ErrorAction Stop)
        if ((Get-PortableAuthTarget $link) -ine $target) {
            throw 'Local credential junction verification failed.'
        }
    } finally { $exclusive.Dispose() }
    $lease = [IO.File]::Open($lockPath, 'Open', 'Read', 'Read')
    if ((Get-PortableAuthTarget $link) -ine $target) {
        $lease.Dispose()
        throw 'Credential target changed concurrently; retry codexv.'
    }
    Write-Host "==> Credentials: LOCAL ($target)" -ForegroundColor Cyan
    return $lease
}
