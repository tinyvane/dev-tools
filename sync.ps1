#Requires -Version 5.1

# ============================================================
# ⚠ DEPRECATED — V1 PowerShell version.
#
# This script is kept for backward compatibility. New machines should use V2:
#   irm https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.ps1 | iex
#
# V2 is cross-platform (Mac/Linux/Windows), distributed via pip, command `codesync`.
# This file will be removed once V2 has been in production use for a few weeks.
#
# Frozen V1 snapshot: https://github.com/tinyvane/dev-tools/releases/tag/v1.0.0
# ============================================================

<#
.SYNOPSIS
    [V1, deprecated] 一键同步所有 git repo（自更新 + 自动注册 + 并发 pull/push）

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

# 可选：GitHub 自动同步（缺则 clone / GitHub archive 则删本地 / rm 本地 + syncp 则归档 GitHub）
# 不需要就把整个 $AutoClone 删掉或保持注释
# $AutoClone = @{
#     Owner            = 'your-github-username'
#     Target           = "$env:USERPROFILE\code"
#     Skip             = @()
#     SkipConfirmation = $false
#     AbortIfShrinkPct = 20
# }

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
                # 用当前进程的 host exe 重启（pwsh 7 / powershell 5.1 都能正确续跑）
                $hostExe = (Get-Process -Id $PID).Path
                if (-not $hostExe) { $hostExe = 'powershell' }
                & $hostExe @relaunchArgs
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
    # 刷新 PATH：pip --user 装到 user-base\Scripts，首次安装不一定在 User PATH 上
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
    $pyUserBase = (& python -m site --user-base 2>$null | Select-Object -First 1)
    if ($pyUserBase) {
        $pyScripts = Join-Path $pyUserBase.Trim() 'Scripts'
        if ((Test-Path $pyScripts) -and ($env:Path -notlike "*$pyScripts*")) {
            $env:Path = "$pyScripts;$env:Path"
        }
    }
    if (-not (Get-Command gita -ErrorAction SilentlyContinue)) {
        throw "gita 安装后仍不在 PATH 中，请检查 $pyScripts 或手动添加到 PATH"
    }
}

# ============================================================
# 3b. GitHub repo 自动同步（clone 缺失 / rm 已 archive / -Push 时 archive 已删本地）
#     state 文件 ~/.gita-known-repos.json 记录上次见过的 repo 集合
# ============================================================
$KnownReposFile = Join-Path $env:USERPROFILE '.gita-known-repos.json'

function Read-KnownRepos {
    if (-not (Test-Path $KnownReposFile)) { return $null }
    try {
        $obj = Get-Content $KnownReposFile -Raw -Encoding UTF8 | ConvertFrom-Json
        return @($obj.Known)
    } catch {
        Write-Host "  状态文件 $KnownReposFile 损坏，按首次运行处理" -ForegroundColor Yellow
        return $null
    }
}

function Save-KnownRepos {
    param([string[]]$Names)
    @{
        Known     = @($Names | Sort-Object -Unique)
        UpdatedAt = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -Path $KnownReposFile -Encoding UTF8
}

function Get-LocalReposByOwner {
    param([string]$Owner)
    $repos = @{}
    foreach ($root in $CodeRoots) {
        if (-not (Test-Path $root)) { continue }
        Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object {
            Test-Path (Join-Path $_.FullName ".git")
        } | ForEach-Object {
            $url = & git -C $_.FullName remote get-url origin 2>$null
            if ($url -and $url -match 'github\.com[:/]([^/]+)/(.+?)(?:\.git)?$') {
                if ($Matches[1] -eq $Owner) {
                    $repos[$Matches[2]] = $_.FullName
                }
            }
        }
    }
    return $repos
}

if ($AutoClone -and $AutoClone.Owner -and $AutoClone.Target) {
    Write-Section "GitHub repo 自动同步"
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Host "  gh CLI 未安装，跳过 GitHub repo 同步" -ForegroundColor Yellow
    }
    else {
        $owner       = $AutoClone.Owner
        $target      = $AutoClone.Target
        $skip        = @($AutoClone.Skip)
        $shrinkPct   = if ($null -ne $AutoClone.AbortIfShrinkPct) { $AutoClone.AbortIfShrinkPct } else { 20 }
        $skipConfirm = [bool]$AutoClone.SkipConfirmation

        # 一次拉全：含 fork、含 archived，本地按字段筛
        # 不能用 2>$null（PS 5.1 在 EAP=Stop 上下文下让 stdout 也吞了），改用 EAP=Continue + 过滤 ErrorRecord
        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $ghOutput = & gh repo list $owner --limit 200 --json name,isFork,isArchived,sshUrl,owner 2>&1
        $ghExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        $rawJson = ($ghOutput | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"

        if ($ghExit -ne 0 -or -not $rawJson) {
            Write-Host "  gh repo list 失败 (exit $ghExit)，跳过" -ForegroundColor Yellow
        }
        else {
            $parsed = ConvertFrom-Json -InputObject $rawJson
            $allOwned = @($parsed | Where-Object { $_.owner.login -eq $owner })
            $forkSet      = @{}; $allOwned | Where-Object { $_.isFork } | ForEach-Object { $forkSet[$_.name] = $true }
            $activeRepos  = @{}; $allOwned | Where-Object { -not $_.isFork -and -not $_.isArchived } | ForEach-Object { $activeRepos[$_.name] = $_.sshUrl }
            $localOwned   = Get-LocalReposByOwner -Owner $owner
            # 排除 fork（fork 不参与自动同步）
            $localManaged = @{}
            $localOwned.GetEnumerator() | Where-Object { -not $forkSet.ContainsKey($_.Key) -and $skip -notcontains $_.Key } | ForEach-Object { $localManaged[$_.Key] = $_.Value }
            $activeManaged = @{}
            $activeRepos.GetEnumerator() | Where-Object { $skip -notcontains $_.Key } | ForEach-Object { $activeManaged[$_.Key] = $_.Value }

            $known     = Read-KnownRepos
            $isFirstRun = ($null -eq $known)
            $knownSet  = @{}; if ($known) { $known | ForEach-Object { $knownSet[$_] = $true } }

            $toClone   = @()
            $toRmLocal = @()
            $toArchive = @()

            if ($isFirstRun) {
                Write-Host "  首次运行（无 state 文件），建立 baseline，不做破坏性操作" -ForegroundColor Cyan
                # 即使首次运行，也支持 clone 缺的（这是用户的 day-1 期望）
                $toClone = @($activeManaged.Keys | Where-Object { -not $localManaged.ContainsKey($_) })
            }
            else {
                # 缩水保护
                if ($known.Count -gt 0) {
                    $shrink = ($known.Count - $activeManaged.Count) * 100.0 / $known.Count
                    if ($shrink -gt $shrinkPct) {
                        Write-Host "  ⚠ GitHub 列表骤减 $([math]::Round($shrink, 1))%（>$shrinkPct%），可能 API 异常，abort" -ForegroundColor Red
                        throw "GitHub 列表骤减保护触发（known=$($known.Count), active=$($activeManaged.Count)）"
                    }
                }
                $toClone   = @($activeManaged.Keys | Where-Object { -not $knownSet.ContainsKey($_) -and -not $localManaged.ContainsKey($_) })
                $toRmLocal = @($knownSet.Keys | Where-Object { $localManaged.ContainsKey($_) -and -not $activeManaged.ContainsKey($_) })
                if ($Push) {
                    $toArchive = @($knownSet.Keys | Where-Object { $activeManaged.ContainsKey($_) -and -not $localManaged.ContainsKey($_) })
                }
            }

            # 摘要 + 5 秒确认
            $destructive = $toRmLocal.Count + $toArchive.Count
            if ($destructive -gt 0) {
                Write-Host ""
                if ($toArchive.Count -gt 0) {
                    Write-Host "  即将归档 GitHub 上 $($toArchive.Count) 个 repo（本地已删除）:" -ForegroundColor Yellow
                    $toArchive | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
                }
                if ($toRmLocal.Count -gt 0) {
                    Write-Host "  即将删除本地 $($toRmLocal.Count) 个 repo（GitHub 已 archive）:" -ForegroundColor Yellow
                    $toRmLocal | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
                }
                Write-Host ""
                if (-not $skipConfirm) {
                    Write-Host "  5 秒后执行（Ctrl+C 取消）..." -ForegroundColor Cyan
                    for ($i = 5; $i -gt 0; $i--) { Write-Host "  $i..." -ForegroundColor Gray; Start-Sleep -Seconds 1 }
                }
            }

            # 执行：clone
            if ($toClone.Count -gt 0) {
                Write-Host "  clone 缺失的 $($toClone.Count) 个 repo:" -ForegroundColor Cyan
                if (-not (Test-Path $target)) { New-Item -Path $target -ItemType Directory -Force | Out-Null }
                foreach ($name in $toClone) {
                    $url = $activeManaged[$name]
                    $dest = Join-Path $target $name
                    if (Test-Path $dest) {
                        Write-Host "    [$name] 目标路径已存在，跳过" -ForegroundColor Yellow
                        continue
                    }
                    Write-Host "    [$name] clone -> $dest" -ForegroundColor Cyan
                    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                    git clone $url $dest 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
                    $ErrorActionPreference = $prevEAP
                }
            }

            # 执行：rm 本地
            if ($toRmLocal.Count -gt 0) {
                Write-Host "  删除本地已归档的 repo:" -ForegroundColor Cyan
                foreach ($name in $toRmLocal) {
                    $path = $localManaged[$name]
                    Write-Host "    [$name] rm -rf $path" -ForegroundColor Cyan
                    Remove-Item -Path $path -Recurse -Force -ErrorAction SilentlyContinue
                    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                    gita rm $name -y 2>&1 | Out-Null
                    $ErrorActionPreference = $prevEAP
                }
            }

            # 执行：archive on GitHub（仅 -Push）
            if ($toArchive.Count -gt 0) {
                Write-Host "  归档 GitHub 上的 repo:" -ForegroundColor Cyan
                foreach ($name in $toArchive) {
                    Write-Host "    [$name] gh repo archive $owner/$name" -ForegroundColor Cyan
                    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                    gh repo archive "$owner/$name" --yes 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
                    $ErrorActionPreference = $prevEAP
                }
            }

            # 更新 state：再扫一次现状，存入
            $finalLocal = Get-LocalReposByOwner -Owner $owner
            $finalLocalManaged = @($finalLocal.Keys | Where-Object { -not $forkSet.ContainsKey($_) -and $skip -notcontains $_ })
            # GitHub 端可能因为 archive 操作变了，重查
            if ($toArchive.Count -gt 0) {
                $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                $ghOut2 = & gh repo list $owner --limit 200 --json name,isFork,isArchived,owner 2>&1
                $ErrorActionPreference = $prevEAP
                $rawJson2 = ($ghOut2 | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
                if ($rawJson2) {
                    $allOwned = @($rawJson2 | ConvertFrom-Json | Where-Object { $_.owner.login -eq $owner })
                    $activeManaged = @{}
                    $allOwned | Where-Object { -not $_.isFork -and -not $_.isArchived -and $skip -notcontains $_.name } | ForEach-Object { $activeManaged[$_.name] = $true }
                }
            }
            $newKnown = @(($activeManaged.Keys + $finalLocalManaged) | Sort-Object -Unique)
            Save-KnownRepos -Names $newKnown
            Write-Host "  state 已更新（known=$($newKnown.Count)）" -ForegroundColor Gray
        }
    }
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
# gita 把状态写到 stderr，PS 5.1 + EAP=Stop 会误判为致命错误。临时降级 EAP。
$prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
gita pull 2>&1 | ForEach-Object { Write-Host $_ }
$ErrorActionPreference = $prevEAP
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
        # 密码走 MYSQL_PWD 透传，不入命令行（避免泄露到进程列表 + cmd 特殊字符解析）
        $env:MYSQL_PWD = $t.Password
        try {
            cmd /c "docker exec -e MYSQL_PWD $cn mysqldump -u$($t.User) --single-transaction --quick $($t.Database) > `"$backup`" 2>NUL" | Out-Null
            # 恢复
            Write-Host "  [$n] 恢复 dump（$dump）..." -ForegroundColor Cyan
            cmd /c "docker exec -i -e MYSQL_PWD $cn mysql -u$($t.User) $($t.Database) < `"$dump`" 2>NUL"
        } finally {
            Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
        }
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
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    gita push 2>&1 | ForEach-Object { Write-Host $_ }
    $ErrorActionPreference = $prevEAP
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
        # 密码走 MYSQL_PWD 透传，不入命令行
        $env:MYSQL_PWD = $t.Password
        try {
            cmd /c "docker exec -e MYSQL_PWD $cn mysqldump -u$($t.User) --single-transaction --quick $($t.Database) > `"$dump`" 2>NUL"
        } finally {
            Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
        }
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
