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
