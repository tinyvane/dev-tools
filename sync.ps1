#Requires -Version 5.1

<#
.SYNOPSIS
    一键同步所有 git repo（自更新 + 自动注册 + 并发 pull/push）

.DESCRIPTION
    工作流：
      1. 拉取 tools repo 自身，保证脚本本身是最新的
      2. 如果脚本更新了，用新版本重启
      3. 扫描 config.local.ps1 里配置的代码根目录，把新 repo 注册到 gita
      4. 用 gita 并发 pull 所有 repo
      5. 可选：并发 push 所有有本地提交的 repo

.EXAMPLE
    .\sync.ps1                  # 只拉取
    .\sync.ps1 -Push            # 拉取 + 推送
    .\sync.ps1 -StatusOnly      # 只看状态，不操作
#>

[CmdletBinding()]
param(
    [switch]$Push,           # 拉取之后是否推送
    [switch]$StatusOnly,     # 只显示状态，不做 pull/push
    [switch]$NoSelfUpdate    # 内部使用：跳过自更新（防止递归）
)

$ErrorActionPreference = 'Stop'
$ToolsDir = $PSScriptRoot

function Write-Section([string]$Msg, [string]$Color = 'Cyan') {
    Write-Host ""
    Write-Host "▸ $Msg" -ForegroundColor $Color
}

# ============================================================
# 1. 加载本机配置（不同机器代码路径不同，所以配置不进 git）
# ============================================================
$LocalConfig = Join-Path $ToolsDir 'config.local.ps1'
if (-not (Test-Path $LocalConfig)) {
    @'
# 本机配置（已被 .gitignore 排除，每台机器各自维护）
# 列出所有存放 git repo 的父目录，sync 脚本会递归扫描这些目录
$CodeRoots = @(
    "$env:USERPROFILE\code"
    # "D:\projects"
    # "E:\work\repos"
)
'@ | Set-Content -Path $LocalConfig -Encoding UTF8
    Write-Host "已生成模板 $LocalConfig" -ForegroundColor Yellow
    Write-Host "请编辑后重新运行。" -ForegroundColor Yellow
    exit 1
}
. $LocalConfig

# ============================================================
# 2. 自更新 tools repo 本身
# ============================================================
if (-not $NoSelfUpdate) {
    Write-Section "自更新 tools repo"
    Push-Location $ToolsDir
    try {
        $before = git rev-parse HEAD 2>$null
        git pull --rebase --quiet 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ⚠  自更新失败（可能是网络问题），继续使用当前版本" -ForegroundColor Yellow
        }
        else {
            $after = git rev-parse HEAD
            if ($before -ne $after) {
                Write-Host "  脚本已更新，用新版本重启..." -ForegroundColor Green
                $relaunchArgs = @('-NoProfile', '-File', $PSCommandPath, '-NoSelfUpdate')
                if ($Push) { $relaunchArgs += '-Push' }
                if ($StatusOnly) { $relaunchArgs += '-StatusOnly' }
                & powershell @relaunchArgs
                exit $LASTEXITCODE
            }
            Write-Host "  已是最新版本" -ForegroundColor Gray
        }
    }
    finally { Pop-Location }
}

# ============================================================
# 3. 确保 gita 可用
# ============================================================
if (-not (Get-Command gita -ErrorAction SilentlyContinue)) {
    Write-Section "安装 gita" 'Yellow'
    pip install --user gita
    if ($LASTEXITCODE -ne 0) {
        throw "gita 安装失败，请手动 pip install gita 后重试"
    }
    # 刷新 PATH（pip --user 可能装到新路径）
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
}

# ============================================================
# 4. 扫描并注册新 repo（gita add -r 是幂等的）
# ============================================================
Write-Section "扫描代码目录"
foreach ($root in $CodeRoots) {
    if (Test-Path $root) {
        Write-Host "  扫描 $root" -ForegroundColor Gray
        gita add -r $root 2>$null | Out-Null
    }
    else {
        Write-Host "  跳过不存在的目录 $root" -ForegroundColor DarkGray
    }
}
$repoCount = @(gita ls 2>$null).Count
Write-Host "  当前注册 $repoCount 个 repo" -ForegroundColor Gray

# ============================================================
# 5. 只看状态模式
# ============================================================
if ($StatusOnly) {
    Write-Section "repo 状态"
    gita ll
    exit 0
}

# ============================================================
# 6. 并发 pull
# ============================================================
Write-Section "并发 pull"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
gita pull
$sw.Stop()
Write-Host "  耗时 $([int]$sw.Elapsed.TotalSeconds) 秒" -ForegroundColor Gray

# ============================================================
# 7. 可选：并发 push
# ============================================================
if ($Push) {
    Write-Section "并发 push"
    $sw.Restart()
    gita push
    $sw.Stop()
    Write-Host "  耗时 $([int]$sw.Elapsed.TotalSeconds) 秒" -ForegroundColor Gray
}
else {
    Write-Host ""
    Write-Host "  （如需同时推送，请加 -Push 参数）" -ForegroundColor DarkGray
}

# ============================================================
# 8. 状态总览
# ============================================================
Write-Section "状态总览"
gita ll
