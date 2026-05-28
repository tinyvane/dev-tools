# CLAUDE.md

Notes for future Claude sessions working on this repo.

## 项目本质

个人多机 git/db 同步工具。**V1 已 frozen，V2 在 main 分支开发中。**

- V1：`sync.ps1` + `config.local.ps1`（PowerShell only），tag `v1.0.0`
- V2：`codesync` Python 包，通过 `pip install --user git+...` 分发，跨平台

## 关键不变量

1. **包名是 `codesync`，命令是 `codesync`，仓库名是 `dev-tools`** — 不要混淆。仓库名是历史遗留（V1 时叫 dev-tools），改名会破坏 V1 release URL，不要动。
2. **配置文件路径**：`~/.config/codesync/config.toml`（所有平台）。一切 state 文件都在同目录。
3. **写 TOML 字符串永远用 `_toml_str()` 工具函数**（`src/codesync/config.py`），不要手写 `f'"{value}"'`。原因：Windows 路径含 `\U`/`\y` 会被 TOML basic string 当成 escape 炸掉。
4. **subprocess 永远用 list-form**（`subprocess.run(["docker", "exec", ...])`）。不要用 `shell=True`，不要用 `cmd /c`，不要用 `bash -c`。原因：跨平台、避免 shell injection、避免特殊字符解析。
5. **MySQL 密码永远走 `MYSQL_PWD` env var**，不进命令行。`docker exec -e MYSQL_PWD ...` 把 env 透传进容器。这是 V1 时期踩坑修过的 issue。

## 没有任何 Python 第三方依赖（v2.2.0 起）

V1 用 gita 做并发 pull/push 和状态显示。V2 早期还依赖 gita。**v2.2.0 把 gita 彻底踢了**：
- pull/push：`git_ops.py` 用 ThreadPoolExecutor + 直接 `git` 子进程
- 状态显示：`status.py` 直接调 `git rev-parse / status --porcelain / rev-list / stash list`，
  用 `unicodedata.east_asian_width` 处理中文宽度对齐，文字标签替代 cryptic 符号

`pyproject.toml` 的 `dependencies` 现在是空数组。**别再加 gita 进来**，没必要。

## gh CLI 是硬依赖（仅当 auto_clone 启用时）

- `auth.ensure_gh_authenticated()`：探测 + 必要时交互调 `gh auth login --web --git-protocol ssh`
- `github_auto._gh_repo_list()` / `_gh_repo_archive()`：repo 列表/归档操作

如果用户没配 `auto_clone`，这些都不会被调到，gh 也就不必要。install 脚本对 gh 缺失只是 warn。

## 自更新 (--update) 的平台差异

- **Mac/Linux**：`pip install --upgrade --user git+...` 同步跑，覆盖 in-place，用户看到 pip 输出，结束。
- **Windows**（默认 detached）：pip 不能覆盖正在跑的 .exe。要 `subprocess.Popen(... ,
  creationflags=DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP)`，**stdin = DEVNULL，
  stdout/stderr 重定向到 `~/.config/codesync/update.log`**，立即 `sys.exit(0)`。
  没有显式重定向时，DETACHED_PROCESS 会让子进程拿到悬空的继承 handle，pip 写日志就崩
  （v2.2.2 修复，之前是这个 bug 的重灾区）
- **Windows + `--foreground`**：跳过 detach，同步跑 pip，用户实时看输出。
  只在 `codesync.exe` 没被升级或失败排查时用 —— 正常情况会因 .exe 自我覆盖失败

代码在 `src/codesync/updater.py`。任何对 Popen 调用的改动**必须**保留 stdin/stdout/stderr 三个显式
参数；省略任何一个又触发悬空 handle 的老毛病。

### `--user` 何时该传，何时不该传（v2.2.3 起）

`_pip_args()` 内部用 `_in_venv()`（即 `sys.prefix != sys.base_prefix`）判断：
- venv 外（system / pip --user 装的 codesync）→ 传 `--user`，避免要 root
- venv 内（pipx 装的、stdlib venv 装的）→ **不传 `--user`**，否则 pip 直接拒
  ("Can not perform a '--user' install. User site-packages are not visible in this virtualenv")

pipx 把 codesync 装到 `~/.local/share/pipx/venvs/codesync/`，`sys.executable` 是 venv 里的
python，所以这套检测同时覆盖 pipx 和手动 venv。**任何改 `_pip_args()` 的人都得保留这个分支。**

## PEP 668 (externally-managed) 安装路径

macOS Homebrew Python 和近代 Debian/Ubuntu 系统 Python 都把自己标成 externally-managed
（stdlib 目录里有个 `EXTERNALLY-MANAGED` 文件），让 `pip install --user` 直接报错。
PEP 668 推荐 pipx。

`install.sh` 的做法：
1. 用 `sysconfig.get_path('stdlib')` 找 stdlib 路径
2. 检 `EXTERNALLY-MANAGED` 文件
3. 在就走 pipx 分支（`pipx install --force git+...`），不在保持原 pip --user 路径
4. pipx 分支不写自己的 `~/.zshrc` 段，靠 `pipx ensurepath` —— 别叠加
5. **pipx 缺失时自动装**（v2.2.4 起）：按 uname + 包管理器选命令：
   - macOS + brew → `brew install pipx`
   - Linux + apt-get/dnf/yum/pacman → `sudo <pkg-mgr> install pipx`
   - 都没 → exit 1 + 打印手动指令
   带 5 秒倒计时让用户能 Ctrl+C 取消。
   不替用户装 brew —— 那条边界太深，要写 `/usr/local` 或 `/opt/homebrew`。

**不要往脚本里加 `--break-system-packages`** 当 fallback —— PEP 668 故意把这门留给"我
明白后果"，长期会污染 system Python；pipx 是干净路径。

## install.sh 写中文标点的坑（v2.2.5 教训）

macOS 自带 bash 是 3.2.57（Apple 因 GPL v3 不升级）。`set -euo pipefail` + `$var<UTF-8>`
组合在 3.2 下会 misparse：bash 3.2 把 UTF-8 lead byte 误当成变量名字符，触发 unbound
variable。

**铁律**：install.sh 里变量名后**紧跟非 ASCII 字符**（中文标点、中文字、emoji）时
必须用 `${var}` 大括号。变量后跟空格、ASCII 标点不需要：

```bash
detail "$var 后面有空格"        # 安全
detail "${var}。后面是中文标点"   # 必须大括号
detail "${var}中文紧跟"           # 必须大括号
```

CI 矩阵跑 Ubuntu/macOS/Windows，但 macOS runner 通常装的是 bash 5.x（Homebrew），跟用户
真实环境（system /bin/bash 3.2）不一样，所以 CI 测不出这个。修这类问题靠真实 Mac 跑过。

## First-run wizard（v2.2.6 起，v2.2.7 改进）

`codesync sync` 在两种情况下自动 invoke `wizard.run_first_run_wizard()`：

1. `config.toml` 不存在
2. `config.toml` 存在但跟 `CONFIG_TEMPLATE` byte-for-byte 一致（即 v2.2.5-era
   "已生成空模板请编辑" 的残留，用户从未编辑）—— 见 `is_template_unedited()`

任何用户编辑（哪怕添加一个空格或注释）都让 `is_template_unedited()` 返回 False，
wizard 不再去碰它 —— 用户有意识编辑的配置不会被自动覆盖。

wizard 触发后如果还是 bail（gh 没装 / 用户拒绝 / username 拿不到），sync 入口
显式 `return 1` 并打印「跑 codesync init 或手动编辑」的指令。**不再** silently
继续跑 sync against 空配置（v2.2.6 的隐患 —— 装出来个无声的"什么都不做"）。

`codesync init` 子命令也跑同一个 wizard，给"想重置配置"的场景。

- 检 gh 装、调 `ensure_gh_authenticated()`、`gh api user --jq .login` 拿 owner
- 默认值：`code_roots = ["~/SyncRepos"]`，`auto_clone.owner = <gh-login>`，
  `auto_clone.target = ~/SyncRepos`，db_sync 留空
- 默认是 Y（直接回车 = Yes；EOF / piped stdin 也算 Y —— 自动化场景兜底）

`codesync init` 子命令也跑同一个 wizard，给"想重置配置但不想立刻 sync"用。

### 何时 fall back 到旧的「写模板让用户编辑」路径

wizard 返回 False 的情况：
- gh CLI 未装 → 让用户先装 gh
- `ensure_gh_authenticated()` 失败（用户取消了浏览器登录、token 出错）
- `gh api user` 拿不到 login（网络问题）
- 用户显式输 n / no

False 时 `codesync sync` 继续往下跑 → `config.load()` 看到 config 还是不存在 →
走老逻辑写空模板 + 提示编辑（v2.2.5 之前的行为）。这是有意保留的 escape hatch，
gh-free 工作流仍能用。

### 不在 wizard 里做的事

- 不问 db_sync 配置（docker container / 密码 / Dropbox path —— 啰嗦且大部分用户不需要）
- 不让用户选 code_roots 路径（默认 ~/SyncRepos 跨平台一致，想要别的改 TOML）
- 不让用户多选 owner（一台机器一个 codesync owner 是常态；多 owner 改 TOML）

## `codesync sync` 默认做一切（v2.3.0 起）

sync 不再是"只 pull"。默认流程：auto_clone → publish orphans → pull → DB restore →
push → DB dump → 状态。**push 是默认了**（之前要 `--push`）。

opt-out：
- `--no-push`：纯 pull，不推、不 DB dump
- `--no-publish`：跳过 orphan 自动发布
- `--push`：保留但已是 no-op（向后兼容老脚本/肌肉记忆）
- `--status`：只读报告，跳过所有写操作（含 publish）

### publish orphans（`src/codesync/publish.py`）

扫 `code_roots/*`，把"本地有但 GitHub 没有"的目录推上去（auto_clone 的反向）：
- 无 `.git/` 的非空目录 → init + commit + `gh repo create --private --source=. --push`
- 有 `.git/` 无 origin → 同上跳过 init/commit
- 跳过：空目录、隐藏目录、`NEVER_PUBLISH_NAMES`（node_modules 等）、`[publish] skip` 名单、
  GitHub 已存在同名

安全：候选列表 + 5 秒倒计时（`[publish] skip_confirmation = true` 可关）。每个候选失败独立
（撞名 / push reject）只 warn，不中断其他。

**改 `publish_one` / `find_orphan_candidates` 时注意**：空目录判断、artifact 黑名单、
GitHub 存在性检查这三个 guard 是防误建 repo 的，别拆。

**默认 .gitignore（v2.3.1）**：no-git 孤儿目录 publish 时，`git init` 前若没 `.gitignore`
就写 `DEFAULT_GITIGNORE`（`.env`/`*.pem`/`id_rsa`/`credentials.*` 等敏感扩展名 + negation 放行
`.env.example`）。**已有 .gitignore 绝不覆盖**。has-git 分支不写。这是"减少误提交"不是"消灭"——
不在列表里的自定义敏感文件名仍会漏。用户知道并接受（拒绝了 fail-closed 全扫方案）。

## Fork upstream 配置（v2.2.9 起）

Fork repo 需要俩 remote：`origin`（你的 fork）和 `upstream`（原 repo）。auto_clone
只配 origin；upstream 由 `fork_setup` 模块管。

**两条触发路径**：
1. **自动**：`auto_clone.run()` clone 完一个 fork 后，立刻调 `add_upstream_for_fork()`
2. **手动 backfill**：`codesync fork-setup` 子命令，扫所有本地 repo、识别 fork、补 upstream

**判断"是否是 fork"的来源**：`gh repo list <owner> --fork --json name` 一次拿全。auto_clone
里 `all_forks` 集合包含所有 fork 名（独立于 `include_forks` 的过滤逻辑）。

**拿 parent URL**：`gh api repos/<owner>/<name> --jq .parent.ssh_url`，per-fork 一次调用。
gh's --jq 在 parent 缺失时打印字面 "null"，`_gh_get_parent_url` 显式判空。

**故意不做**：
- 不在 `codesync sync` 里自动 invoke fork-setup —— sync 应该快，gh api per-fork 会拖慢
- 不支持非 GitHub fork（gh 是硬依赖）
- 不重写 origin URL 格式（用户配的是 https 就是 https，gh 给的 ssh 就是 ssh）

## V1 → V2 配置迁移

`codesync migrate-config` 在 `src/codesync/config.py::migrate_from_ps1()`：
- 在常见位置（`~/dev-tools/`、`~/SyncRepos/dev-tools/`、`~/code/dev-tools/`、cwd）找 `config.local.ps1`
- regex + 手写括号匹配解析 `$CodeRoots`、`$AutoClone`、`$DbSyncTargets`
- **自动剔除 `code_roots` 里指向 codesync 自身源码的项**（`filter_codesync_self_dirs`，靠
  `sync.ps1` 或 `src/codesync/__init__.py` marker 识别）。V1 用户常误把 `~/dev-tools` 也算成
  一个 code_root，V2 不需要 —— 工具是 pip 装的，靠 `--update` 升级。保守策略：路径不存在
  /目录名叫 dev-tools 但没 marker → 不动，避免误删用户的"碰巧叫 dev-tools 的别的 repo"
- 输出新 `config.toml`，旧 `.ps1` 不动
- 如果新 TOML 已存在，备份到 `.toml.bak`

**parser 是 best-effort**，不是完整 PowerShell parser。复杂表达式（`"$env:USERPROFILE\..."`）目前依赖字面字符串匹配。如果用户 V1 配置写得花，可能解析不全 —— 这是已知边界，不修，因为这是一次性迁移。

## 测试

```bash
# 装 dev deps
pip install --user -e ".[dev]"

# 跑测试
pytest tests/
```

测试覆盖（见 `tests/`）：
- `test_config_migration.py`：核心 V1 PS1 解析
- `test_toml_quoting.py`：Windows 路径在 TOML 中正确转义
- `test_cli.py`：argparse 路由

## 版本号是单源的

`pyproject.toml` 的 `version = "..."` 是唯一的真实来源。
`src/codesync/__init__.py` 通过 `importlib.metadata.version("codesync")` 读取。

升级版本时只需改 `pyproject.toml`，然后：
1. `git tag -a v2.1.0 -m "..." && git push origin v2.1.0`
2. （可选）GitHub Release：`gh release create v2.1.0 --title "..." --notes "..."`

源码 checkout 但没 `pip install -e .` 时（罕见），`__version__` 回退到 `"0.0.0+source"`。

## 故意没做的事

- 不发 PyPI（trust hop + 发版纪律 vs 收益不成比例）
- 不打包二进制（用户必有 Python 跑 gita）
- 不在 install 脚本里自动装 gh/python（每个 OS 命令不同，错误率高）
- 不写 `sync` / `syncp` shell alias（`codesync sync` 已经够短）
- 不实现 plugin 系统、subcommand 自定义（YAGNI）

## 调试常用命令

```bash
codesync config-path         # 看 TOML 在哪
codesync --version           # 看版本
pip show codesync            # 看 pip 视角的版本和位置
python -c "from codesync import config; print(config.load())"   # 看解析后的 config dataclass
```
