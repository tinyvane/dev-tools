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

# 可选：本地 Docker MySQL 跨 PC 同步配置（不需要就保持注释）
# 工作流：sync.ps1 自动恢复 Dropbox 上更新的 dump；sync.ps1 -Push 自动 dump 到 Dropbox
# $DbSyncTargets = @(
#     @{
#         Name      = 'jx-perf'
#         Container = 'jx-perf-mysql-dev'
#         Database  = 'jx_perf'
#         User      = 'jx_perf'
#         Password  = 'dev_pwd'
#         DumpFile  = 'D:\dropbox\db-sync\jx-perf.sql'
#     }
# )
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
    if ($DbSyncTargets) {
        $stateFile = Join-Path $env:USERPROFILE '.gita-db-sync.json'
        if (Test-Path $stateFile) {
            $obj = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Section "DB sync 状态"
            foreach ($t in $DbSyncTargets) {
                $n = $t.Name
                $lp = $obj."$n.LastPushedAt";   if (-not $lp) { $lp = '-' }
                $lr = $obj."$n.LastRestoredAt"; if (-not $lr) { $lr = '-' }
                Write-Host ("  [{0}] LastPushed: {1}  LastRestored: {2}" -f $n, $lp, $lr) -ForegroundColor Gray
            }
        }
    }
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
# 6b. DB sync — 从 Dropbox 恢复（如果 dump 比本机最近同步的新）
#     state 文件 ~/.gita-db-sync.json 记录本机 LastPushedHash / LastRestoredHash
# ============================================================
$DbStateFile = Join-Path $env:USERPROFILE '.gita-db-sync.json'

function Read-DbState {
    if (-not (Test-Path $DbStateFile)) { return @{} }
    try {
        $obj = Get-Content $DbStateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $h = @{}
        $obj.PSObject.Properties | ForEach-Object { $h[$_.Name] = $_.Value }
        return $h
    } catch {
        Write-Host "  状态文件 $DbStateFile 损坏，重置" -ForegroundColor Yellow
        return @{}
    }
}

function Save-DbState($state) {
    $state | ConvertTo-Json | Set-Content -Path $DbStateFile -Encoding UTF8
}

function Test-DbContainer($name) {
    $running = docker ps --filter "name=$name" --format '{{.Names}}' 2>$null
    return [bool]$running
}

if ($DbSyncTargets) {
    Write-Section "DB sync (restore)"
    $state = Read-DbState
    foreach ($t in $DbSyncTargets) {
        $n = $t.Name; $dump = $t.DumpFile; $cn = $t.Container
        if (-not (Test-Path $dump)) {
            Write-Host "  [$n] Dropbox 上无 dump，跳过" -ForegroundColor DarkGray
            continue
        }
        if (-not (Test-DbContainer $cn)) {
            Write-Host "  [$n] 容器 $cn 未运行，跳过" -ForegroundColor Yellow
            continue
        }
        $dumpHash = (Get-FileHash $dump -Algorithm SHA256).Hash
        if ($dumpHash -eq $state["$n.LastRestoredHash"] -or $dumpHash -eq $state["$n.LastPushedHash"]) {
            Write-Host "  [$n] 已是最新（与本机最近同步一致），跳过" -ForegroundColor Gray
            continue
        }
        # 检测到 Dropbox 有新 dump
        if ($Push) {
            Write-Host ""
            Write-Host "  ⚠  [$n] Dropbox 上有更新（来自另一台 PC），但你正在 -Push" -ForegroundColor Red
            Write-Host "  ⚠  如果继续，本机数据会被先覆盖再 dump 推回去（=丢失）" -ForegroundColor Red
            Write-Host "  ⚠  建议：先去掉 -Push 跑一次 sync.ps1 同步好，再 -Push" -ForegroundColor Red
            Write-Host ""
            throw "DB sync conflict: 拒绝在 -Push 模式下覆盖本机数据"
        }
        # 备份现状
        $backupDir = Join-Path $env:USERPROFILE '.gita-db-sync-backups'
        if (-not (Test-Path $backupDir)) { New-Item -Path $backupDir -ItemType Directory | Out-Null }
        $backup = Join-Path $backupDir "$n-$(Get-Date -Format 'yyyyMMdd-HHmmss').sql"
        Write-Host "  [$n] 备份当前 DB 到 $backup" -ForegroundColor Gray
        cmd /c "docker exec $cn mysqldump -u$($t.User) -p$($t.Password) --single-transaction --quick $($t.Database) > `"$backup`" 2>NUL" | Out-Null
        # 恢复
        Write-Host "  [$n] 恢复 dump（$dump）..." -ForegroundColor Cyan
        cmd /c "docker exec -i $cn mysql -u$($t.User) -p$($t.Password) $($t.Database) < `"$dump`""
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [$n] 恢复失败！备份保留在 $backup" -ForegroundColor Red
            continue
        }
        $state["$n.LastRestoredHash"] = $dumpHash
        $state["$n.LastRestoredAt"] = (Get-Date).ToString('o')
        Save-DbState $state
        Write-Host "  [$n] 恢复完成" -ForegroundColor Green
    }
}

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
# 7b. DB sync — dump 到 Dropbox（仅 -Push 模式下）
# ============================================================
if ($Push -and $DbSyncTargets) {
    Write-Section "DB sync (dump)"
    $state = Read-DbState
    foreach ($t in $DbSyncTargets) {
        $n = $t.Name; $dump = $t.DumpFile; $cn = $t.Container
        if (-not (Test-DbContainer $cn)) {
            Write-Host "  [$n] 容器 $cn 未运行，跳过" -ForegroundColor Yellow
            continue
        }
        $dumpDir = Split-Path $dump -Parent
        if (-not (Test-Path $dumpDir)) { New-Item -Path $dumpDir -ItemType Directory -Force | Out-Null }
        Write-Host "  [$n] dump 到 $dump..." -ForegroundColor Cyan
        cmd /c "docker exec $cn mysqldump -u$($t.User) -p$($t.Password) --single-transaction --quick $($t.Database) > `"$dump`""
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [$n] dump 失败" -ForegroundColor Red
            continue
        }
        $state["$n.LastPushedHash"] = (Get-FileHash $dump -Algorithm SHA256).Hash
        $state["$n.LastPushedAt"] = (Get-Date).ToString('o')
        Save-DbState $state
        $size = (Get-Item $dump).Length
        $sizeStr = if ($size -lt 1MB) { '{0:N1} KB' -f ($size / 1KB) } else { '{0:N1} MB' -f ($size / 1MB) }
        Write-Host "  [$n] 完成（$sizeStr）" -ForegroundColor Green
    }
}

# ============================================================
# 8. 状态总览
# ============================================================
Write-Section "状态总览"
gita ll
if ($DbSyncTargets) {
    Write-Section "DB sync 状态"
    $state = Read-DbState
    foreach ($t in $DbSyncTargets) {
        $n = $t.Name
        $lp = $state["$n.LastPushedAt"]; if (-not $lp) { $lp = '-' }
        $lr = $state["$n.LastRestoredAt"]; if (-not $lr) { $lr = '-' }
        Write-Host ("  [{0}] LastPushed: {1}  LastRestored: {2}" -f $n, $lp, $lr) -ForegroundColor Gray
    }
}
