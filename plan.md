# codesync (dev-tools V2) — 开发计划

> **当前状态**：V2 主体功能已实现，正在写测试 + 完善文档。V1 已 frozen 为 `v1.0.0` release。

## 目标体验

```bash
# 装：
curl -fsSL https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh | bash   # Mac/Linux/WSL
irm https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.ps1 | iex          # Windows

# 用：
codesync sync                # 拉取所有 repo
codesync sync --push         # 拉取 + 推送
codesync sync --status       # 只看状态
codesync migrate-config      # 一次性迁移 V1 config.local.ps1 → TOML
codesync --update            # 自更新（=claude --update 体验）
codesync -U                  # short form
```

首次跑 `codesync sync` 若 `auto_clone` 已配置且 gh 未登录，会自动调 `gh auth login`，浏览器 Device Flow，等价 `claude auth login` 体验。

## 任务进度

- [x] 1. 骨架：pyproject.toml + src/codesync/
- [x] 2. CLI 路由（argparse）+ 子命令
- [x] 3. 配置 loader（TOML） + V1 PS1 一次性迁移
- [x] 4. 核心 sync 流程（gita 注册/pull/push + gh auth 探测）
- [x] 5. GitHub auto-clone 模块
- [x] 6. DB sync 模块（Docker MySQL via Dropbox）
- [x] 7. 自更新机制（--update / -U，Windows detached subprocess）
- [x] 8. install.sh + install.ps1 双 bootstrap
- [x] 9. README 重写 + sync.ps1 加 deprecation header
- [x] 10. 本地 smoke test + commit + push
- [x] 11. plan.md / CLAUDE.md / README.md
- [x] 12. unit tests（pytest，30 个全过）
- [x] 13. fix smoke-test 发现的 2 个 bug（gita ls 计数 + Python stdout buffering）
- [x] 14. Tag v2.0.0 + GitHub Release（v1.0.0 / v2.0.0 双 release 形成对照）

## 阶段性结论（2026-05-26）

V2 在 main 分支可用，pip install 入口跑通，本机 smoke 通过（133 个 repo 正确注册和列出）。
后续验证由用户在 Mac 上跑 install.sh 完成，问题反馈后再改 install.sh 边界。

## v2.2.2（2026-05-27）— 修复 --update 在 Windows 上的静默失败

V2.2.1 用户在另一台机器跑 `codesync --update` 两次都报"升级已在后台开始"，但
`codesync --version` 始终是旧版。手动同步跑 pip 没问题 → 锁定 root cause 是
`updater.py` 的 detached subprocess 调用：

- `subprocess.Popen(cmd, close_fds=True, creationflags=DETACHED_PROCESS|...)` 没指定
  stdin/stdout/stderr → 继承 parent 的 console handle，但 `DETACHED_PROCESS` 把子进程
  从 console 解绑，继承来的 handle 变成悬空
- pip 一启动就写日志/进度 → 写到坏 handle 触发异常 → pip 静默崩溃，没卸也没装
- 用户两次 `--update` 还会并发两个 detached pip，互相抢锁更乱

### 修法
- [x] **`updater._run_detached_windows()`**：显式把 stdout/stderr 重定向到
  `~/.config/codesync/update.log`（append 模式，多次跑能串起来），stdin = DEVNULL
- [x] **日志加 header**：每次 `--update` 写一行 `=== codesync --update ... ===`，
  含时间戳、cmd，便于事后排查
- [x] **CLI 加 `--foreground` flag**：用户可以 `codesync --update --foreground` 同步跑，
  实时看 pip 输出（Windows 上的 escape hatch；Unix 上本来就是同步）
- [x] **告知日志位置**：detach 前的"升级已在后台开始"提示后面，多打一行日志路径
- [x] 测试 +6 (81 total)：foreground/background 分支、Popen 关键 kwargs、log 文件创建

## v2.2.1（2026-05-27）— 装机 + 迁移工具链的真实场景修复

- [x] **install.ps1** Python 探测重写：
  - 加 `py` launcher 优先尝试（`py -3.13` / `-3.12` / `-3.11` / `-3`），是 Windows 上最可靠的 Python 入口
  - 用 `--version` 输出做版本判定，绕开 PowerShell 5.1 native-command 双引号 bug
    （原来的 `-c 'import sys; print(f"{...}")'` 在 PS 5.1 下会被错误转义导致 python 报语法错误）
  - 直接扫 `%LocalAppData%\Programs\Python\Python3*\python.exe`，兜底"winget 刚装完 PATH 没传播"
  - 用 `sysconfig.get_path('scripts', scheme='nt_user')` 取真实 user-scripts 路径
    （原来用 `site --user-base` 拼 `\Scripts` 缺失 `\Python313\` 层，导致 PATH 加了不存在的目录）
  - 移除 script-wide `$ErrorActionPreference='Stop'`：pip 写到 stderr 的 warning（如
    "Ignoring invalid distribution"）会被包成 NativeCommandError 把脚本拽停
- [x] **migrate-config 过滤 dev-tools/codesync 自身**：
  - V1 用户常把 `~/dev-tools` 也写进 `$CodeRoots`（V1 是从源码目录跑的）
  - V2 是 pip 安装的，`codesync --update` 自己升级，源码目录不需要进 code_roots
  - `filter_codesync_self_dirs()` 检测 `sync.ps1`（V1 marker）或 `src/codesync/__init__.py`（V2 marker），
    迁移时自动剔除并打印 notice；保守策略 — 不存在的路径不动，叫 dev-tools 但没 marker 的也不动
- [x] **repo 改 public**：原 private repo 让 `raw.githubusercontent.com` 拒绝匿名 GET → install 一行命令 404
  改 public 后 install URL 真正可用，pip git+ 也不再需要 token
- [x] 测试新增 6 个 (75 total)：`filter_codesync_self_dirs` 的 keep/drop 各种组合

## v2.2.0（2026-05-27）— 自实现 status + 删 gita 依赖

- [x] 18. 自实现 status 显示（替代 `gita ll`）
  - `status.py`：每个 repo 跑 `git rev-parse / status --porcelain / rev-list / stash list` 探测
  - CJK 宽度对齐用 `unicodedata.east_asian_width`，中文名 repo 不再让后续列错位
  - 文字标签 (`clean` / `modified` / `untracked` / `mixed` / `stash` / `ahead N` / `behind N` /
    `diverged` / `no upstream` / `error`) 替代 gita 的 cryptic 字符
  - 顶部加汇总 + 一行 legend，clean 行视觉 dim，problems 行高亮
  - 新 flag `--problems`：隐藏所有 clean，只看需要关注的
- [x] 同时删 gita 依赖：pull/push 早已自实现，status 替换后 gita 不再被任何代码调用，
  pyproject `dependencies` 变成空数组；删 `shell.py`（ensure_gita 等死代码）和 github_auto 里
  最后一处 `gita rm`

测试 69（+28 v.s. v2.1.0）：覆盖 visual_width、pad_visual、truncate_visual（CJK），
RepoStatus.label 各种状态组合，compute_status 用 tmp_path 真实 git init 验证 dirty/untracked/mixed。

## v2.1.0（2026-05-27）— CI + 单源版本号 + 自实现 parallel

- [x] 15. GitHub Actions matrix CI（ubuntu/macos/windows × Python 3.11/3.12/3.13）
- [x] 16. 版本号单源化：`__version__` 用 `importlib.metadata.version("codesync")` 读 pyproject.toml
- [x] 17. 自实现 parallel pull/push（`git_ops.py`）：ThreadPoolExecutor + 每完成一个 repo 打印 `[X/Y] ✓/✗ name`，
       不再依赖 gita pull 输出的解析；新加 `--workers N` flag（默认 ~2×CPU，capped 16）；
       任一 repo pull/push 失败导致整体退出码 2（CI/pipeline 可识别）

测试覆盖：41 个 pytest（test_git_ops.py 用 tmp_path + git init 真实构造小型 repo 树测 find_repos，
parallel_op 用 mock 测；symlink 测试在 Windows 自动 skip）。

## 关键技术决策（决策日志）

### 路径选择：Python 重写 vs PowerShell 跨平台改造
**选 Python**（路径 A）。理由：
- gita 本来就是 Python 包，依赖谱系一致
- `pip install --user git+https://...` 是天然的跨平台分发
- `--update` 实现简洁（`pip install --upgrade`），等价 claude code 体验
- TOML 配置比 PS1 脚本配置更安全（无代码注入）

### 分发：PyPI vs GitHub-direct
**GitHub-direct**。`pip install --user git+https://github.com/tinyvane/dev-tools.git@main`。
- 个人工具不需要 PyPI 的发版纪律
- 不引入第三方信任 hop
- `--update` 内部跑同一条命令，永远从 main 拉最新

### 命令名
**`codesync`**。`sync` 撞 Unix `sync(8)`，`dt` 含义不清，`codesync` 长但语义清楚。

### 配置位置
**`~/.config/codesync/config.toml`**（所有平台一致，包括 Windows）。
- `Path.home()` 在 Windows 是 `$env:USERPROFILE`
- 同目录还放 state 文件：`known-repos.json`、`db-sync-state.json`、`db-sync-backups/`

### TOML 字符串引号
**写入用 literal string（单引号）**，因为 Windows 路径含 `\U` 会被 TOML basic string 当成 Unicode escape 报错。
工具函数 `_toml_str()` 在 `config.py`：含 `'` 才退回 basic string（带反斜杠转义）。

### GitHub 认证
**复用 `gh auth login`**，不自己注册 OAuth App。
- gh 内部就是 Device Flow，UX 等价
- 顺带搞定 SSH key 上传 / git credential helper
- gh 已经是 auto-clone 的依赖，不引入新依赖

### `--update` 在 Windows 的处理
**Detached subprocess + 立即 exit**。
- Windows 上 pip 不能边跑边覆盖自己（.exe 被当前进程持有）
- spawn 一个 `subprocess.Popen` 后立即 `sys.exit(0)`
- 用户看到「升级在后台开始」的提示，下次重跑就是新版
- Claude Code 在 Windows 上也是这套（这是公开惯例）

### 版本管理：V1 / V2 共存
**Git tag + GitHub Release**。
- V1 终态 tag `v1.0.0` 已推到 GitHub Releases
- V2 占据 main 分支
- sync.ps1 留在 main 加 deprecation header，V2 稳定后 PR 删除（tag 永久保留）

## 不做（明确放弃的设计）

- ❌ **不上 PyPI**：trust hop 和发版纪律的成本不抵收益
- ❌ **不写 sync 的 wrapper alias 到 .zshrc/$PROFILE**：`codesync sync` 已经短到没必要再短
- ❌ **不打包成单文件二进制**（PyInstaller/Nuitka）：用户已经在装 Python 跑 gita，没必要再装一份
- ❌ **不支持 Linux 自动装 gh / python**：每个发行版命令不同，错误率高，install 脚本只提示

## 文件总览（V2 实现）

```
dev-tools/
  pyproject.toml                    # 包元数据
  install.sh / install.ps1          # bootstrap
  plan.md                           # 本文件
  CLAUDE.md                         # 给 Claude 看的项目笔记
  README.md                         # V2 用户面文档
  sync.ps1                          # V1，加了 deprecation header（暂留）
  config.local.ps1                  # V1 配置（暂留供 migrate-config 读取）
  src/codesync/
    __init__.py                     # __version__, __repo_url__
    __main__.py                     # python -m codesync 入口
    cli.py                          # argparse 路由
    config.py                       # TOML loader + V1 迁移
    sync.py                         # 主同步流程
    github_auto.py                  # GitHub auto-clone
    db_sync.py                      # Docker MySQL via Dropbox
    auth.py                         # gh auth 探测/触发
    updater.py                      # --update 实现
    paths.py                        # 配置目录、state 文件路径
    shell.py                        # subprocess 包装、gita 安装
    output.py                       # 颜色化输出
  tests/
    test_config_migration.py
    test_toml_quoting.py
    test_cli.py
```
