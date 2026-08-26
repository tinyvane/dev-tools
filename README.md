# dev-tools

[![test](https://github.com/tinyvane/dev-tools/actions/workflows/test.yml/badge.svg)](https://github.com/tinyvane/dev-tools/actions/workflows/test.yml)

个人多机开发同步工具。一条命令同步所有 git repo（pull/push）、自动 clone GitHub 新 repo、递归同步嵌套 repo / submodule。

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

macOS Homebrew Python 和近代 Debian/Ubuntu/麒麟系统 Python 都是 PEP 668 externally-managed，`install.sh` 自动检测：

- 系统已有 **现代 pipx（≥ 1.0）** → 走 pipx 分支（每个工具单独 venv）
- 否则 → **自建一个专用 venv** 装 codesync，软链到 `~/.local/bin/codesync`（v2.6.0 起，无需 sudo、无需先装 pipx）

> **v2.6.0 起不再自动 apt 装 pipx**：部分发行版（如麒麟）apt 里的 pipx 是 0.12.x 老版本，**不支持从 git URL 安装**（报 `Package cannot be a url`），自带的 pip 也太旧建不了现代 pyproject。自建 venv 绕开了这些坑。若自建 venv 时报缺 `venv` 模块，按提示 `sudo apt install python3-venv`（或 `python3.11-venv`）后重跑即可。

所以**普通情况下你只需要那一行 curl 就够了。**

### Linux / Rocky 新机器准备

安装脚本会先检查 Python 和 git。Rocky 最小化系统常见缺口是 **没有 Python 3.11+** 或 **没有 git**：

```bash
dnf install -y python3.11 python3.11-pip git
python3.11 --version
git --version
```

如果 `python3.11` 包找不到，先启用 CRB / EPEL 后再装：

```bash
dnf install -y dnf-plugins-core
dnf config-manager --set-enabled crb
dnf install -y epel-release
dnf install -y python3.11 python3.11-pip git
```

`codesync` 本体安装不强制要求 GitHub CLI，但要让首次 `codesync sync` 自动 clone GitHub repo，建议提前装 `gh`：

```bash
dnf install -y 'dnf-command(config-manager)'
dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
dnf install -y gh
gh auth login
```

`gh auth login` 交互建议：

- GitHub host 选 `GitHub.com`
- Git protocol 选 `SSH`
- 新机器通常选择生成新的 SSH key 并添加到 GitHub
- SSH key passphrase 是私钥密码；想让 `codesync sync` 自动化不被打断，可以直接回车留空

然后重新跑安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh | bash
```

### 国内 / GitHub 被墙的网络（v2.6.0 起）

安装脚本会**自动探测 github.com**；连不上时自动改走国内镜像（ghfast.top / gh-proxy.com / mirror.ghproxy.com 里第一个通的），并把 pip 构建依赖切到清华 PyPI 镜像。**装完后 `codesync --update` 也走同样的自动探测。**

但**那一行 `curl`/`irm` 本身**是从 `raw.githubusercontent.com` 拉脚本的，这个域名在国内常被墙。所以第一步要用镜像地址拉脚本（脚本拉下来后，后面的 pip clone 它会自己处理）：

**麒麟 / Linux / macOS / WSL**:

```bash
bash -c "$(curl -fsSL https://ghfast.top/https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh)"
```

**Windows (PowerShell)**:

```powershell
irm https://ghfast.top/https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.ps1 | iex
```

镜像前缀可换（`https://gh-proxy.com` / `https://mirror.ghproxy.com`，失效了换一个就行）。想强制指定镜像、或自动探测没选对，可设环境变量后再跑安装命令：

```bash
export CODESYNC_GH_MIRROR=https://ghfast.top      # GitHub 走这个镜像
export CODESYNC_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple   # 可选，覆盖 pip index
```

```powershell
$env:CODESYNC_GH_MIRROR='https://ghfast.top'
$env:CODESYNC_PIP_INDEX='https://pypi.tuna.tsinghua.edu.cn/simple'   # 可选
```

`codesync --update` 同样读这两个环境变量（不设则自动探测）。

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
codesync sync                  # 一条命令做完：clone 缺失 + 发布孤儿 + 自动 commit + rebase pull + push
codesync sync --no-push        # 不执行 push
codesync sync --no-publish     # 跳过"自动发布本地孤儿目录"步骤
codesync sync --no-commit      # 跳过"自动提交脏 repo"步骤
codesync sync --status         # 只看 repo 状态，不操作
codesync sync --status --problems  # 只显示需要关注的 repo（隐藏 clean）
codesync sync --workers 4      # 覆盖网络 Git 并发数
codesync sync --local-workers 16  # 覆盖本地元数据扫描并发数

codesync pull                  # 只拉：自动 commit + rebase pull，不 push / 不 clone / 不发布
codesync push                  # 只推：自动 commit + push，不 pull
codesync pull --no-commit      # 不自动提交脏 repo（pull / push 都支持）

codesync init                  # 重新跑首次配置向导（gh 自动检测 + 写 TOML）
codesync fork-setup            # 给所有本地 fork 自动配 upstream remote（一次性 backfill）
codesync rename foo bar        # 本地目录 + GitHub repo 一起改名
codesync rename bar --local-only   # 只改本地目录名，GitHub 和 origin 不动
codesync delete foo            # 本地完整目录 + GitHub repo 一起移入垃圾箱
codesync delete foo --local-only   # 只把本地目录移入垃圾箱，GitHub repo 保持存活
codesync trash list            # 查看本机 .codesync-trash
codesync trash restore foo     # 本地和 GitHub 一起恢复
codesync trash purge foo       # 输入名称确认后永久清理本地和 GitHub 垃圾
codesync migrate-config        # 一次性把 V1 config.local.ps1 迁移成 TOML

codesync --update              # 自更新（Windows 默认后台跑，日志在 ~/.config/codesync/update.log）
codesync --update --foreground # 同步跑，实时看 pip 输出（排查用）
codesync -U                    # short form of --update
codesync --version
codesync config-path           # 打印配置文件路径
```

从 v2.19.0 起，push 阶段只连接真正比 upstream ahead 的仓库；已同步仓库显示
`无待推送提交` 并跳过网络连接。有提交但尚无 upstream 的新分支仍会尝试首次 push。

### `sync` / `pull` / `push` 怎么选

`codesync sync` 是**唯一会做完整协调**的命令：clone 别的机器新建的 repo、发布本地孤儿目录、
按 Repository ID 处理垃圾箱信号、跟进跨机改名。`pull` 和 `push` 是它的两个子集预设，只搬运
已有仓库的提交，**不 clone、不发布、不归档**。

- `codesync pull` = 自动 commit → rebase pull。**仍然会先 commit**：整条流水线的顺序
  （commit → rebase pull → push）就是为了让你的改动先进 Git 再动历史，`--autostash` 只是兜底。
  不想提交用 `--no-commit`。
- `codesync push` = 自动 commit → push。**不 pull**，所以一个已经和远端分叉的仓库会被 git
  直接拒绝，而不是被"想办法"合并 —— 这是有意的。**要收敛分叉请用 `codesync sync`**。
  codesync 永远不会 force push，也不会引入第三种合并策略。

v2.22.0 起核心顺序是“自动 commit → `git pull --rebase --autostash` → push”。这会把尚未推送的
本地 commit 重放到远端最新之上，让多机同时 auto-commit 产生的分叉能自动收敛。已有未完成的
rebase / merge / cherry-pick / revert 会被跳过，不会自动 abort；codesync 自己发起的 rebase
若冲突则自动回滚。autostash 应用冲突时改动仍在 stash，依提示手动处理。

GitHub SSH remote（如 `git@github.com:owner/repo.git`）在 codesync 进程内会透明走 GitHub 官方
`ssh.github.com:443` 端点，避免批量同步直连 TCP 22。这个设置只传给 codesync 启动的 Git/gh
子进程，不改仓库 remote、不改 `~/.ssh/config`，也不影响 codesync 之外的手动 Git 命令。

443 端点在 OpenSSH 中有独立的 `[ssh.github.com]:443` host key。codesync 会把可信 key 放在只含
这个端点的 `~/.config/codesync/known_hosts`：优先从用户已信任的 `github.com` 条目（含 hashed
known_hosts）派生 —— 这样 GitHub 轮换 host key 时能自愈；派生不到才用既有缓存，新机器再通过
TLS 校验的 GitHub meta API 获取。它不会写
`~/.ssh/known_hosts`，也不会降低 `StrictHostKeyChecking`；要完全自行管理可设
`[sync] github_known_hosts = false`。

实际写同步开始前会显示上述连接保护和本次 worker 数，并倒计时 10 秒。倒计时期间按 `Ctrl+C`
会在 clone、publish、commit、pull、push 之前安全取消；`codesync sync --status` 不倒计时。
POSIX 默认启用 SSH ControlMaster，让并发 GitHub 操作共享一条 TCP 连接，此时网络默认 8 workers；
Windows 或复用不可用时网络默认 4（v2.25.0 之前是 1，导致 Windows 上每个仓库串行握手）。
纯本地元数据扫描按 CPU 自动扩展（最多 32），可分别用 `--workers N` 和 `--local-workers N` 覆盖。

v2.20.0 起所有非交互 git/gh/pip 子进程都有分层 timeout，超时会作为“不确定”处理，不会误判为
repo 不存在或工作区干净。慢网络可在启动前设置 `CODESYNC_TIMEOUT_SCALE=2` 同比放大全部档位；
交互式 `gh auth login --web` 不受 timeout 限制。

**第一次跑** `codesync sync`（v2.2.6 起）：如果配置文件不存在，自动跑 first-run wizard ——
检测 gh 登录（没登就弹浏览器走 OAuth Device Flow）、读出你的 GitHub 用户名、写好 TOML
（默认 `auto_clone.owner = <你的 gh login>`、`target = ~/SyncRepos`）、确认后立刻开 sync
把你 GitHub 名下所有 repo 自动 clone 下来。**装完 codesync 后只需 `codesync sync` 一条命令，
不需要手动编辑任何文件。**

### Repo 垃圾箱（v2.17.0）

`codesync delete foo` 不再执行不可恢复的删除：GitHub 上把 repo 改为
`zz-trash--v1--<时间>--<ID摘要>--foo` 后 archive，本地把整个目录原样移动到
`<code_root>/.codesync-trash/`。原 GitHub 名称 `foo` 随即可以复用；`.env`、ignored 文件、
stash、本地分支和 `.git` 历史都留在垃圾箱中。

另一台运行 **同一最新版** codesync 的机器下次 sync 会按不可变 Repository ID 识别旧 repo，
先把旧本地目录移入自己的 `.codesync-trash`，再处理可能出现的新同名 repo。GitHub repo 转移、
权限变化或列表异常不会被当成删除信号。恢复用 `codesync trash restore foo`；只有
`codesync trash purge foo` 会永久删除。

删除保护和恢复都按不可变 Repository ID 记录。旧 ID 的 tombstone 不会阻止后来复用同名的新 repo；
远端 `zz-trash--v1--...` 名称无论本机是否见过对应 tombstone 都不会被自动 clone。

除 `sync --status` 外，所有 sync 每次都 fresh 检查 main 上的版本。网络不可用、版本未知或本机
落后时 fail closed，只允许只读 status，避免旧客户端误解垃圾箱协议。

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
# - 远端进入 codesync trash → 本地目录移入 .codesync-trash
# - 本地目录删了 + push → 远端改垃圾箱名并 archive
[auto_clone]
owner               = "your-github-username"
target              = "~/SyncRepos"
skip                = []
skip_confirmation   = false
abort_if_shrink_pct = 20   # GitHub 列表骤减保护阈值（防 API 异常误删）

[sync]
# net_workers = 4          # 省略则按 SSH 复用是否生效选择 8 或 4
# local_workers = 16       # 省略则按 CPU 推导，最多 32
countdown_seconds = 10     # 设 0 可跳过倒计时（仍显示同步安全说明）
ssh_multiplex = true
github_known_hosts = true  # false：完全由你管理 ssh.github.com:443 信任
stall_bytes_per_sec = 1000 # HTTP 低于此速度持续 stall_seconds 即中止
stall_seconds = 300        # 设 0 关闭 HTTP/SSH 停滞检测，退回纯时长 timeout
cleanup_stale_packs = true # 清理超过 24 小时的中断传输 tmp_pack_* 残留

[pull]
rebase = true              # false：退回 v2.20.0 的 --ff-only
```

pull / push 这类增量传输用 900 秒兜底，`git clone` 和首次 `gh repo create --push`
用 3600 秒（传的是整个历史，且被杀掉会留下需要人工清理的半成品目录）；真正不再推进的 HTTP/SSH 连接由
low-speed / ServerAlive 在 300 秒内中止 —— 也就是说停滞检测总是先于超时开火，超时只是最后一道网。
（v2.25.0 之前 pull/push 用的是 120 秒，反而比 300 秒的停滞窗口更早，导致停滞检测在这条路径上从未生效，
而且任何超过约 1.8 MB 的传输每轮必然超时。）清理只触碰超过 24 小时的临时 pack，不会删除正在进行的
fetch/clone 文件。

并发默认值：网络操作在有 SSH 连接复用时 8、没有时 4；本地元数据扫描按 CPU 数推导（上限 32）。
Windows OpenSSH 不支持 ControlMaster，所以网络并发在 Windows 上恒为 4（v2.25.0 之前是 1，
141 个仓库要串行握手 15-24 分钟）。用 `--workers N` 或 `[sync].net_workers` 可以覆盖。

> 注：V2.13.0 起移除了 V1 的 Docker MySQL 跨机同步功能 —— codesync 现在是纯 git repo 同步工具。

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
| 跨平台 shell | `proc.run(list)` 统一 UTF-8、timeout，永不用 shell=True | 避免挂死、locale 乱码、cmd 特殊字符和 shell injection |

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
