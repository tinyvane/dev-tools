# dev-tools

[![test](https://github.com/tinyvane/dev-tools/actions/workflows/test.yml/badge.svg)](https://github.com/tinyvane/dev-tools/actions/workflows/test.yml)

个人多机开发同步工具。一条命令同步所有 git repo（pull/push）、自动 clone GitHub 新 repo、Docker MySQL 跨机同步。

> **V2 是 Python 包，名字叫 `codesync`，跨平台（Mac / Linux / WSL / Windows）。**
> V1 PowerShell 版冻结在 [v1.0.0 release](https://github.com/tinyvane/dev-tools/releases/tag/v1.0.0)，仅供回溯。

## 安装

**macOS / Linux / WSL**:

```bash
curl -fsSL https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh | bash
```

**Windows (PowerShell)**:

```powershell
irm https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.ps1 | iex
```

需要 Python ≥ 3.11 + git。零 Python 第三方依赖（v2.2.0 起）。`auto_clone` 功能额外需要 `gh` CLI（首次跑时会自动调 `gh auth login`，浏览器登 GitHub，之后不再问）。

## 状态符号说明

`codesync sync --status` 用文字标签代替 `gita ll` 的 cryptic 字符：

| 标签 | 含义 |
|---|---|
| `clean` | 工作区干净，与远端同步 |
| `modified` | 工作区有已跟踪文件的改动（未 commit） |
| `untracked` | 有未跟踪的新文件 |
| `mixed` | 既 modified 又 untracked |
| `stash` | 有 `git stash` 里的暂存内容 |
| `ahead N` | 本地比 upstream 多 N 个提交（待 push） |
| `behind N` | 本地比 upstream 少 N 个提交（待 pull） |
| `diverged` | 本地与 upstream 已分叉（既 ahead 又 behind） |
| `no upstream` | 本分支没有配 upstream（如新建本地 repo 还没 push） |
| `error` | 探测 status 出错（如 timeout） |

带 `--problems` 时只显示非 clean 行，clean 的全部隐藏。

## 用法

```bash
codesync sync                  # 拉取所有 repo（+ DB restore 如配置）
codesync sync --push           # 拉取 + 推送（+ DB dump 如配置）
codesync sync --status         # 只看 repo 状态，不操作
codesync sync --status --problems  # 只显示需要关注的 repo（隐藏 clean）
codesync sync --workers 16     # 自定义并发数（默认 ~2×CPU，capped 16）
codesync migrate-config        # 一次性把 V1 config.local.ps1 迁移成 TOML
codesync --update        # 自更新（=pip install --upgrade git+...）
codesync -U              # short form
codesync --version
codesync config-path     # 打印配置文件路径
```

第一次跑 `codesync sync` 会在 `~/.config/codesync/config.toml` 生成模板并提示编辑。

## 配置

`~/.config/codesync/config.toml`（所有平台同路径）：

```toml
# 哪些目录下放着 git repo（递归一层）
code_roots = [
    "~/SyncRepos",
    # "~/code",
    # "D:/projects",
]

# 可选：GitHub repo 自动同步
# - 远端有、本地没 → clone
# - 远端 archived → 删本地
# - 本地删了 + --push → 远端 archive
[auto_clone]
owner               = "your-github-username"
target              = "~/SyncRepos"
skip                = []
skip_confirmation   = false
abort_if_shrink_pct = 20   # GitHub 列表骤减保护阈值（防 API 异常误删）

# 可选：Docker MySQL 跨机同步（dump 走 Dropbox）
# codesync sync       → 检测到 Dropbox dump 更新 → 自动恢复
# codesync sync --push → dump 到 Dropbox
[[db_sync]]
name      = "myproject"
container = "myproject-mysql-dev"
database  = "myproject_db"
user      = "myproject_user"
password  = "dev_pwd"
dump_file = "~/Dropbox/db-sync/myproject.sql"
```

## 技术路径（简述）

> 详见 [`plan.md`](./plan.md) 和 [`CLAUDE.md`](./CLAUDE.md)

| 决策 | 选择 | 理由 |
|---|---|---|
| 语言 | Python ≥ 3.11 | gita 本来就是 Python 包，依赖谱系一致；TOML 标准库可读 |
| 分发 | `pip install --user git+https://...@main` | 不上 PyPI，少一个信任 hop，少一份发版纪律 |
| 配置 | TOML（`~/.config/codesync/config.toml`） | 无代码执行风险，跨平台路径一致 |
| 认证 | 复用 `gh auth login` | Device Flow UX 等价 `claude auth login`，顺带搞定 SSH key |
| 自更新 | `pip install --upgrade` 内部包装 | Windows 上用 detached subprocess 绕过自我覆盖问题 |
| 版本管理 | git tag + GitHub Release | 工业标准，V1 永久可回溯（`v1.0.0`） |
| 跨平台 shell | `subprocess.run(list)` 永不用 shell=True | 避免 cmd 解析特殊字符、避免 shell injection |
| MySQL 密码 | `MYSQL_PWD` env var via `docker exec -e` | V1 时期教训：密码进命令行会泄露 + 被特殊字符炸 |

## 从 V1 升级

```bash
# 1. 装 V2（不会动你的 V1 文件）
curl -fsSL https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh | bash
# 或 irm .../install.ps1 | iex

# 2. 一次性迁移配置
codesync migrate-config        # 读 ~/dev-tools/config.local.ps1 → ~/.config/codesync/config.toml

# 3. 跑一次确认
codesync sync --status

# 4. 用着没问题后可以删掉 V1（仓库和你 PROFILE 里的 sync/syncp/syncs alias）
```

V1 release 永远可以 `git checkout v1.0.0` 拿回。

## 开发

```bash
git clone git@github.com:tinyvane/dev-tools.git
cd dev-tools
pip install --user -e ".[dev]"
pytest tests/
```

## License

MIT
