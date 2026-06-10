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

# Note: we deliberately do NOT set $ErrorActionPreference='Stop' here.
# In PS 5.1, that combined with native commands writing to stderr (pip warnings,
# git warnings, etc.) gets wrapped as NativeCommandError and terminates the script
# even when the exe returns exit 0. We check $LASTEXITCODE explicitly where it matters.
$RepoUrl = 'https://github.com/tinyvane/dev-tools.git'
$MinPyMajor = 3
$MinPyMinor = 11

# GitHub mirrors tried (in order) when github.com is unreachable and the user
# didn't set $env:CODESYNC_GH_MIRROR. Public ghproxy-style prefixes; they come
# and go, hence the env-var escape hatch. Form: <mirror>/https://github.com/...
$DefaultMirrors = @('https://ghfast.top', 'https://gh-proxy.com', 'https://mirror.ghproxy.com')
$GhMirror = ''

function Section($msg) { Write-Host ""; Write-Host "▸ $msg" -ForegroundColor Cyan }
function Ok($msg)      { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Warn($msg)    { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Err($msg)     { Write-Host "  ✗ $msg" -ForegroundColor Red }
function Detail($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }

# Reachability probe. Any HTTP response (even an error status) means the host is
# reachable — we only care that TLS completes, not the status code.
function Test-Reachable($url) {
    try {
        [void](Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop)
        return $true
    } catch {
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

# Decide whether to route GitHub through a mirror (for users behind the GFW).
#   $env:CODESYNC_GH_MIRROR set → trust it, no probing.
#   unset + github.com OK       → direct.
#   unset + github.com dead     → first reachable $DefaultMirrors entry.
function Resolve-GhMirror {
    if ($env:CODESYNC_GH_MIRROR) {
        $script:GhMirror = $env:CODESYNC_GH_MIRROR.TrimEnd('/')
        Ok "使用指定 GitHub 镜像: $script:GhMirror"
        return
    }
    # PS 5.1 defaults to TLS 1.0/1.1 for raw .NET calls; force TLS 1.2 so probes
    # to github.com / mirrors don't fail spuriously.
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
    Section "网络探测"
    if (Test-Reachable 'https://github.com/tinyvane/dev-tools') {
        Ok "github.com 直连可用"
        return
    }
    Warn "github.com 直连失败，自动探测国内镜像…"
    foreach ($m in $DefaultMirrors) {
        if (Test-Reachable "$($m.TrimEnd('/'))/https://github.com/tinyvane/dev-tools") {
            $script:GhMirror = $m.TrimEnd('/')
            Ok "使用 GitHub 镜像: $script:GhMirror"
            return
        }
    }
    Warn "所有镜像都探测失败，仍走直连（可能失败）。"
    Detail "可手动指定: `$env:CODESYNC_GH_MIRROR='https://你的镜像'; 重跑安装命令"
}

# The git+ spec pip installs, mirror-rewritten when $GhMirror is set.
function Get-GhGitSpec {
    if ($GhMirror) { "git+$GhMirror/$RepoUrl" } else { "git+$RepoUrl" }
}

# Route pip build deps (setuptools/wheel) through a CN PyPI mirror when behind
# the GFW, via PIP_INDEX_URL. $env:CODESYNC_PIP_INDEX overrides; an already-set
# PIP_INDEX_URL is respected.
function Resolve-PipIndex {
    if ($env:CODESYNC_PIP_INDEX) {
        $env:PIP_INDEX_URL = $env:CODESYNC_PIP_INDEX
        Detail "pip index: $env:PIP_INDEX_URL (CODESYNC_PIP_INDEX)"
    } elseif ($GhMirror -and -not $env:PIP_INDEX_URL) {
        $env:PIP_INDEX_URL = 'https://pypi.tuna.tsinghua.edu.cn/simple'
        Detail "镜像环境：pip 构建依赖走清华 PyPI 镜像（设 CODESYNC_PIP_INDEX 可改）"
    }
}

# ----------------------------------------------------------------------
# 1. find python
# ----------------------------------------------------------------------
# Strategy:
#   1. Try `py` launcher first with explicit minor (bundled with python.org installer;
#      avoids PATH-order issues with WindowsApps\python.exe).
#   2. Try direct python3.X / python3 / python commands on PATH.
#   3. Also probe known install paths directly (handles "winget installed but PATH
#      not yet propagated to this session" — common right after a fresh winget install).
# Validation is purely by running `... -c "print version"`:
#   - Microsoft Store STUB (when real Python not installed) writes nothing to stdout,
#     so the regex fails and we move on.
#   - Real Microsoft Store Python (installed) writes "3.x" and gets accepted, even
#     though its path is inside ...\WindowsApps\... .
Section "查找 Python (需要 >= $MinPyMajor.$MinPyMinor)"
$PyCmd  = $null
$PyArgs = @()

$candidates = [System.Collections.Generic.List[object]]::new()
foreach ($entry in @(
    @{ Cmd = 'py'; Args = @('-3.13') },
    @{ Cmd = 'py'; Args = @('-3.12') },
    @{ Cmd = 'py'; Args = @('-3.11') },
    @{ Cmd = 'py'; Args = @('-3') },
    @{ Cmd = 'python3.13'; Args = @() },
    @{ Cmd = 'python3.12'; Args = @() },
    @{ Cmd = 'python3.11'; Args = @() },
    @{ Cmd = 'python3';    Args = @() },
    @{ Cmd = 'python';     Args = @() }
)) { [void] $candidates.Add($entry) }

# Probe well-known install paths (winget / python.org installer + machine-wide):
$probeRoots = @()
if ($env:LOCALAPPDATA)         { $probeRoots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
if ($env:ProgramFiles)         { $probeRoots += $env:ProgramFiles }
$pf86 = ${env:ProgramFiles(x86)}
if ($pf86)                     { $probeRoots += $pf86 }
foreach ($root in $probeRoots) {
    if (-not (Test-Path $root)) { continue }
    foreach ($dir in (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -match '^Python3(1[1-9]|[2-9]\d)$' })) {
        $exe = Join-Path $dir.FullName 'python.exe'
        if (Test-Path $exe) { [void] $candidates.Add(@{ Cmd = $exe; Args = @() }) }
    }
}

foreach ($c in $candidates) {
    # `py` and `python` go through Get-Command; absolute paths skip that.
    if (-not [System.IO.Path]::IsPathRooted($c.Cmd)) {
        if (-not (Get-Command $c.Cmd -ErrorAction SilentlyContinue)) { continue }
    }
    try {
        # Use --version (writes "Python X.Y.Z") instead of `-c "..."`:
        # PowerShell 5.1 has a long-standing native-command argument quoting bug that
        # mangles double-quoted Python code (the `print(f"...")` form gets reparsed
        # as a syntax error). --version takes no embedded quotes and bypasses it.
        # Capture stderr too since older Pythons sometimes wrote --version to stderr.
        $probe = $c.Args + @('--version')
        $v = & $c.Cmd @probe 2>&1
        if ($v -match 'Python (\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -gt $MinPyMajor -or ($major -eq $MinPyMajor -and $minor -ge $MinPyMinor)) {
                $PyCmd  = $c.Cmd
                $PyArgs = $c.Args
                $shown  = if ($PyArgs) { "$PyCmd $($PyArgs -join ' ')" } else { $PyCmd }
                Ok "找到 $shown (Python $major.$minor)"
                break
            }
        }
    } catch { }
}

if (-not $PyCmd) {
    Err "未找到 Python >= $MinPyMajor.$MinPyMinor"
    Detail "winget install Python.Python.3.13"
    Detail "或访问 https://www.python.org/downloads/"
    Detail "排查：'where.exe python' / 'where.exe py' / 'py -0p' 看看实际状态"
    Detail "如果是 winget 刚装完，把这条 install 命令重跑一遍——本脚本会扫描标准安装目录"
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
# 2.5 GitHub / PyPI mirror resolution (GFW-friendly)
# ----------------------------------------------------------------------
Resolve-GhMirror
Resolve-PipIndex

# ----------------------------------------------------------------------
# 3. pip install
# ----------------------------------------------------------------------
Section "安装 codesync"
$gitSpec = Get-GhGitSpec
$pipArgs = $PyArgs + @('-m', 'pip', 'install', '--user', '--upgrade', $gitSpec)
$shownCmd = if ($PyArgs) { "$PyCmd $($PyArgs -join ' ')" } else { $PyCmd }
Detail "$shownCmd -m pip install --user --upgrade $gitSpec"
& $PyCmd @pipArgs
if ($LASTEXITCODE -ne 0) {
    Err "pip install 失败"
    exit $LASTEXITCODE
}

# ----------------------------------------------------------------------
# 4. ensure user scripts dir is on User PATH
# ----------------------------------------------------------------------
# DO NOT use `python -m site --user-base` here: on Windows it returns the BASE
# (e.g. C:\Users\u\AppData\Roaming\Python), but pip actually installs scripts to
# <base>\PythonXY\Scripts. sysconfig.get_path('scripts', scheme='nt_user') gives
# the correct full path including the Python<MAJOR><MINOR> subdir.
# Note: only single quotes inside the -c code — PS 5.1's native-cmd double-quote
# bug only triggers when the code contains embedded double quotes.
Section "PATH 配置"
$scriptsProbe = $PyArgs + @('-c', "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))")
$UserScripts = (& $PyCmd @scriptsProbe).Trim()

if (-not $UserScripts) {
    Err "无法从 sysconfig 取得 user scripts 目录"
    exit 1
}
if (-not (Test-Path $UserScripts)) {
    Warn "$UserScripts 不存在（codesync.exe 可能装到了别处，请用 pip show codesync 检查）"
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
    # First line only — `codesync --version` may print extra lines (latest-version note).
    $ver = (& codesync --version | Select-Object -First 1) -replace 'codesync ', ''
    Ok "codesync $ver 已就绪"
} else {
    Err "codesync 未在 PATH 上 — 请重开 PowerShell 后重试"
}
Write-Host ""
Detail "下一步："
Detail "  1. 重开 PowerShell（让 PATH 在所有新会话里生效）"
Detail "  2. codesync migrate-config  （如果你有 V1 config.local.ps1）"
Detail "  3. codesync sync            （第一次会生成 config.toml 模板）"
