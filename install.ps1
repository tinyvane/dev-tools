# codesync installer for Windows PowerShell 5.1+.
#   irm https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.ps1 | iex
#
# This script:
#   1. Verifies Python >= 3.11 is on PATH.
#   2. Checks for git and gh CLI (warns if missing).
#   3. pip install --user git+https://github.com/tinyvane/dev-tools.git
#   4. Ensures the Python user-base\Scripts directory is on PATH (User scope, persistent).
#
# Idempotent: re-running upgrades codesync in place.

#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$RepoUrl = 'https://github.com/tinyvane/dev-tools.git'
$MinPyMajor = 3
$MinPyMinor = 11

function Section($msg) { Write-Host ""; Write-Host "▸ $msg" -ForegroundColor Cyan }
function Ok($msg)      { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Warn($msg)    { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Err($msg)     { Write-Host "  ✗ $msg" -ForegroundColor Red }
function Detail($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }

# ----------------------------------------------------------------------
# 1. find python
# ----------------------------------------------------------------------
Section "查找 Python (需要 >= $MinPyMajor.$MinPyMinor)"
$Py = $null
foreach ($c in @('python3.13', 'python3.12', 'python3.11', 'python3', 'python')) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        try {
            $v = & $c -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>$null
            if ($v -match '^(\d+)\.(\d+)$') {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -gt $MinPyMajor -or ($major -eq $MinPyMajor -and $minor -ge $MinPyMinor)) {
                    $Py = $c
                    Ok "找到 $c (Python $v)"
                    break
                }
            }
        } catch { }
    }
}

if (-not $Py) {
    Err "未找到 Python >= $MinPyMajor.$MinPyMinor"
    Detail "winget install Python.Python.3.13"
    Detail "或访问 https://www.python.org/downloads/"
    exit 1
}

# ----------------------------------------------------------------------
# 2. dependencies
# ----------------------------------------------------------------------
Section "依赖检查"

if (Get-Command git -ErrorAction SilentlyContinue) {
    Ok "git $((& git --version) -replace 'git version ','')"
} else {
    Err "git 未安装"
    Detail "winget install Git.Git"
    exit 1
}

if (Get-Command gh -ErrorAction SilentlyContinue) {
    $ghVer = (& gh --version | Select-Object -First 1) -replace '^gh version ', '' -replace ' .*', ''
    Ok "gh $ghVer"
} else {
    Warn "gh (GitHub CLI) 未安装 — auto_clone 功能需要它"
    Detail "winget install GitHub.cli"
}

# ----------------------------------------------------------------------
# 3. pip install
# ----------------------------------------------------------------------
Section "安装 codesync"
Detail "$Py -m pip install --user --upgrade git+$RepoUrl"
& $Py -m pip install --user --upgrade "git+$RepoUrl"
if ($LASTEXITCODE -ne 0) {
    Err "pip install 失败"
    exit $LASTEXITCODE
}

# ----------------------------------------------------------------------
# 4. ensure user-base\Scripts is on User PATH
# ----------------------------------------------------------------------
Section "PATH 配置"
$UserBase = (& $Py -m site --user-base).Trim()
$UserScripts = Join-Path $UserBase 'Scripts'

if (-not (Test-Path $UserScripts)) {
    Warn "$UserScripts 不存在（pip 可能装到了别处）"
}

# Update User PATH (persistent, survives reboot) if not already present.
$userPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
$parts = $userPath -split ';' | Where-Object { $_ }
if ($parts -notcontains $UserScripts) {
    $newPath = if ($userPath) { "$UserScripts;$userPath" } else { $UserScripts }
    [System.Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Ok "已加入 User PATH: $UserScripts"
} else {
    Detail "User PATH 已包含 $UserScripts"
}

# Update current session PATH too, so codesync works immediately.
if (($env:Path -split ';') -notcontains $UserScripts) {
    $env:Path = "$UserScripts;$env:Path"
}

# ----------------------------------------------------------------------
# done
# ----------------------------------------------------------------------
Section "完成"
if (Get-Command codesync -ErrorAction SilentlyContinue) {
    $ver = (& codesync --version) -replace 'codesync ', ''
    Ok "codesync $ver 已就绪"
} else {
    Err "codesync 未在 PATH 上 — 请重开 PowerShell 后重试"
}
Write-Host ""
Detail "下一步："
Detail "  1. 重开 PowerShell（让 PATH 在所有新会话里生效）"
Detail "  2. codesync migrate-config  （如果你有 V1 config.local.ps1）"
Detail "  3. codesync sync            （第一次会生成 config.toml 模板）"
