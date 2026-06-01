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

## 并发 git op 的重试（v2.3.3）

`git_ops.parallel_op` 第一遍并发跑完后，**失败的 op 自动串行重试一次**
（`max_workers=1` + `_RETRY_DELAY_SEC` 暂停）。原因：16 worker 并发 SSH 到 github.com 会被
**间歇限流**，失败连接报 "Repository not found / access rights"，但 repo 其实完全正常
（手动单推秒过）。串行重试清掉这类假失败；真失败（无权限/冲突）重试仍失败 → 才报。
**别去掉重试**，否则正常 repo 会随机报 push 失败。`_short_err` 优先 `fatal:`/`error:` 行，
别退回截最后一行（会截出 "and the repository exists." 这种废话）。

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

macOS Homebrew Python 和近代 Debian/Ubuntu/麒麟系统 Python 都把自己标成 externally-managed
（stdlib 目录里有个 `EXTERNALLY-MANAGED` 文件），让 `pip install --user` 直接报错。

`install.sh` 的做法（v2.6.0 起，**两档**）：
1. 用 `sysconfig.get_path('stdlib')` 找 stdlib 路径，检 `EXTERNALLY-MANAGED` 文件。
2. 不是 externally-managed → 原 `pip install --user` 路径（`add_dir_to_rc` 写 PATH）。
3. 是 externally-managed：
   - 系统已有 **现代 pipx（major ≥ 1）** → `pipx install --force git+...` + `pipx ensurepath`
     （保留这条经过验证的路径，主要覆盖 macOS Homebrew / 近代 Ubuntu）。
   - 否则 → **自管理 venv**（`install_via_venv`）：`$PY -m venv ~/.local/share/codesync/venv`
     → venv 内 `pip install --upgrade pip setuptools wheel` → `pip install <git-spec>`
     → 软链 `~/.local/bin/codesync` → `add_dir_to_rc ~/.local/bin`。

### 为什么 v2.6.0 砍掉了"apt 自动装 pipx"（v2.2.4–2.5.x 的做法）

老逻辑 pipx 缺失时 `sudo apt/dnf/... install pipx`。**麒麟（和老 Debian）apt 里的 pipx 是
0.12.x（2019 年的版本）**：不支持 `pipx install git+URL`（报 `Package cannot be a url`，得用
老式 `--spec` 语法），自带的 pip 也太旧建不了现代 pyproject。所以 apt 装来的 pipx 反而是坑。

**自管理 venv 是更稳的 PEP 668 路径**：venv 是独立环境（不被 EXTERNALLY-MANAGED 标记），
pip 在里面正常跑；我们还在 venv 内先升级 pip/setuptools/wheel，PEP 517 构建不受 base python
老 pip 影响。无需 sudo、无需 pipx。`codesync --update` 也天然兼容 —— venv 里的 codesync
跑起来 `_in_venv()` 为 True，updater 走"venv 内 `pip install --upgrade`"分支，原地升级、软链不变。

**venv 模块缺失**（Debian/Ubuntu/麒麟把 `python3-venv` 拆成单独包）→ `$PY -m venv` 失败，
`install_via_venv` 显式报错 + 提示 `sudo apt install python3-venv`（或 `python3.11-venv`）。
**不替用户 apt 装 venv 包** —— 跟"不替装 python/gh"一致，留给用户一条明确指令。

**判断现代 pipx 用 major 版本**（`${PIPX_VER%%.*} >= 1`），非数字/空 → 当 0（走 venv）。
别改成"只要 pipx 存在就用" —— 那正是 0.12.x 翻车的原因。

**不要往脚本里加 `--break-system-packages`** 当 fallback —— PEP 668 故意把这门留给"我
明白后果"，长期会污染 system Python；自管理 venv 是干净路径。

## GitHub 镜像 / GFW-friendly 安装（v2.6.0）

国内 / GitHub 被墙的网络下，`install.sh`、`install.ps1`、`updater.py` 三处都会把 GitHub
请求改走镜像。**三处逻辑必须保持一致**（改一处记得同步另两处）：

**镜像决策**（`resolve_gh_mirror` / `Resolve-GhMirror` / `_gh_mirror`）：
1. `CODESYNC_GH_MIRROR` 环境变量设了 → 直接用，**不探测**（尾部 `/` 去掉）。
2. 没设 → 探测 `https://github.com/tinyvane/dev-tools` 通不通。通 → 直连。
3. 不通 → 按顺序探 `DEFAULT_MIRRORS`（`ghfast.top` / `gh-proxy.com` / `mirror.ghproxy.com`），
   用第一个通的。都不通 → 退回直连 + 提示设 `CODESYNC_GH_MIRROR`。

**镜像 URL 形态**：`git+<镜像>/https://github.com/tinyvane/dev-tools.git`（ghproxy 风格前缀，
pip 把 `git+` 剥掉后交给 git，git 走镜像 clone）。**别改成 `url.insteadOf` 全局 git 配置** ——
那会污染用户的 `~/.gitconfig`，env-var 重写是 scoped 的。

**PyPI index**：镜像激活时（说明大概率在墙内），pip 构建依赖（setuptools/wheel）从 pypi.org
拉会卡，所以自动把 index 切到清华镜像。`CODESYNC_PIP_INDEX` 覆盖；已设的 `PIP_INDEX_URL` 尊重。
- bash/PS：走 `PIP_INDEX_URL` env var（pip 和 pipx 内部的 pip 都认）。
- updater.py：走 `--index-url` 参数（updater 只用 pip，不用 pipx）。

**探测原语**：reachability 只看 **TLS 能不能握手**（任何 HTTP 状态码都算可达，包括镜像对 HEAD
返回 405）。GFW 对 github 的干扰是在 TLS 层 reset（curl 报 `(35) ssl_error_syscall`），所以
不能只测 TCP connect。bash 用 curl→wget；PS 用 `Invoke-WebRequest -Method Head`；updater 用
`urllib.request.urlopen`（`HTTPError` 也算可达）。`_gh_mirror` 用 `lru_cache` 保证每次 `--update`
最多探一次网。

**bootstrap 的鸡生蛋问题**：那一行 `curl|bash` / `irm|iex` 本身从 `raw.githubusercontent.com`
拉脚本，这个域名常被墙 —— 脚本还没跑起来，没法自愈。所以 README 给国内用户的一行命令用的是
**镜像化的 raw URL**（`https://ghfast.top/https://raw.githubusercontent.com/...`）。脚本一旦跑起来，
后面的 pip clone 由上面的自动探测兜底。

**测试**（`tests/test_updater.py`）：`_isolate_mirror` autouse fixture 把 `_url_ok` 打桩成永远
True（github 可达 → 直连），保证 `_pip_args()` 测试**不碰真网络**。镜像分支的测试显式 patch
`_url_ok` 或设 env，并 `_gh_mirror.cache_clear()`。**加新 updater 测试时记得保持网络隔离。**

## install.sh 写中文标点的坑（v2.2.5 教训）

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

## `codesync sync` 默认做一切（v2.3.0 起，v2.4.0 加 auto-commit）

sync 不再是"只 pull"。默认流程：auto_clone → publish orphans → pull → DB restore →
**auto-commit 脏 repo** → push → DB dump → 状态。**push 和 auto-commit 都是默认开的**。

opt-out：
- `--no-push`：纯 pull，不推、不 DB dump
- `--no-publish`：跳过 orphan 自动发布
- `--no-commit`：跳过自动提交脏 repo
- `--push`：保留但已是 no-op（向后兼容老脚本/肌肉记忆）
- `--status`：只读报告，跳过所有写操作（含 publish/commit）

### auto-commit（v2.4.0，`git_ops.auto_commit_dirty`）
脏 repo（`git status --porcelain` 非空）在 pull 之后 push 之前自动 `git add -A` + commit
（message `chore: auto-commit <ts>`）。clean repo 跳过（不产生空 commit）。
**位置必须在 pull 之后**：commit 落在远端最新之上，避免多机器无谓分叉。
`[commit]` 配置：`enabled`（默认 True）、`skip`（默认 `["dev-tools"]`）。
**dev-tools 默认 skip**：它是 codesync 源码 repo，历史是 curated/tagged 的，不该被垃圾提交污染。
改这块时保留"pull→commit→push"顺序和 skip 默认。

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

**默认 .gitignore（v2.3.1）**：孤儿目录 publish、需要建初始 commit 时，若没 `.gitignore`
就写 `DEFAULT_GITIGNORE`（`.env`/`*.pem`/`id_rsa`/`credentials.*` 等敏感扩展名 + negation 放行
`.env.example`）。**已有 .gitignore 绝不覆盖**。这是"减少误提交"不是"消灭"——
不在列表里的自定义敏感文件名仍会漏。用户知道并接受（拒绝了 fail-closed 全扫方案）。

**按 has_commits 而非 has_git 分流（v2.3.2，重要）**：`publish_one` 必须按"有没有 commit"
决定流程，不能按"有没有 .git"。`git init` 过但 0 commit 的目录（有 .git 但 HEAD 不存在）需要
跟裸目录一样走 add+commit，否则 `gh repo create --source=. --push` 会因无 commit 半残。
`OrphanCandidate.has_commits` 由 `find_orphan_candidates` 用 `_has_commits()` 算出。
**改 publish_one 时别退回到按 has_git 判断**。

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

## Repo 改名（v2.5.0，`src/codesync/rename.py`）

给 repo 改名，本地目录 + GitHub remote 一起改，并让**其他机器在 sync 时自动跟着改**。

### 主动改名：`codesync rename`
- 双形态：`rename <新名>`（在 repo 目录里跑，old = 当前目录名）/ `rename <旧名> <新名>`
  （任意位置跑，去 `code_roots` 找 old；多个 root 撞名 → 报错让用户 cd 进去）
- **按 origin 分三档**（关键，别合并）：
  1. `github.com` → 全套：`gh repo rename` + 本地 mv + `git remote set-url`
  2. 非 GitHub（gitlab 等）→ **拒绝碰 remote**，降级成只改本地目录名
  3. 无 origin / 无 .git（孤儿）→ 只改本地目录名
- **执行顺序铁律**：脏/未 push → 5 秒倒计时(Ctrl+C 取消)后 auto-commit + push →
  `gh repo rename` → 本地 mv → `set-url`。GitHub rename 放最前(唯一会真失败的步骤，
  撞名/无权限)，失败就 abort、本地一概不动。后两步几乎不失败。
- **Windows 坑**：1 参形态在 repo 目录里跑，Windows 不能 rename 进程 CWD 所在的目录
  （句柄占用）。`_move_dir` 先 `os.chdir` 到父目录再 rename。改这函数别去掉这段。
- guard：新名非法/同名/GitHub 已存在同名/本地目标目录已存在 → 全部拒绝。

### 被动迁移：B 机 sync 时自动跟改（`detect_and_migrate`）
挂在 `github_auto.run()` 里，**必须跑在 to_clone/to_rm_local 计算之前**。原因：A 把
`foo`→`bar` 后，B 看到的是"foo 从 GitHub 列表消失、bar 是新的" —— 朴素逻辑会
**删本地 foo + 重 clone bar**（丢 B 上未提交改动）。先迁移(mv + set-url)再 re-scan，
repo 就显得 in-sync，删/clone 都不触发。

- 检测**白嫖 auto_clone 已经拉的 `gh repo list`**：本地 repo 的 origin 名不在 active 集
  → 可疑 → 补一次 `gh api repos/<owner>/<旧名> --jq .name`（走 GitHub 301 重定向解析新名）。
  **正常情况额外 api = 0 次**（所有名都命中 active），只对 mismatch 触发，所以不违反
  "sync 要快、别 per-repo gh api" 的不变量。
- 分流：解析出新名且在 active 集 → 改名迁移；404 → repo 真删了，不管；解析出的名不在
  active 集 → 不猜，跳过。
- 目录只在"目录名 == 旧 repo 名"（clone/publish 约定）时才 mv；否则只更新 origin。
- 迁移结果在 sync 末尾用黄色高亮 banner 再打一遍（`sync.py` 的 `migrations`），避免在
  长 sync 里被刷走。
- `[rename] auto_migrate`（默认 True）可关。`github_auto.run` 返回 `list[(old,new)]`。

### 跟着改 Claude 对话目录（v2.5.1）
Claude Code 把每个 repo 的对话 transcript 存在 `~/.claude/projects/<目录名>/`，目录名是
repo **绝对路径**把 `: / \` 全换成 `-`（`_claude_project_dirname`，
`C:\Users\me\SyncRepos\foo` → `C--Users-me-SyncRepos-foo`）。repo 一改路径，这目录名就对不上，
Claude 当成新空 project，历史失联。所以**任何一次本地目录物理移动**都跟着幂等改这个对话目录。

- 绑定点：`rename_repo` 三档（github/非 github/孤儿，只要本地目录动了）+ `detect_and_migrate`
  里**真 mv 了工作目录**那一支（只更 origin 没动目录的不碰）。
- **幂等**（`_rename_claude_project`）：源在、目标不在 → 改；目标已在 → 跳过；源不在 → 跳过。
  大小写不敏感匹配实际目录名（`_find_ci`，Windows 大小写不敏感 + transcript 名大小写敏感）。
- **为什么幂等是对的**：用户常把 `~/.claude/projects` 做成 junction 指向 Dropbox（**共享存储**），
  一台机器改名 Dropbox 会传播到其他机器。幂等保证"另一台 sync 时目标已存在 → 跳过"，
  也兜底 Dropbox 还没传到时本机补一刀，最终收敛。
- **best-effort**：`_rename_claude_project` 绝不抛异常 —— 对话目录改名失败不能拖垮 repo 改名本身。
- **故意不动 `~/.claude.json`** 里按绝对路径（正斜杠 key，如 `C:/Users/me/syncrepos/foo`）存的
  per-project 条目（allowedTools/MCP 配置/trust-dialog 标志/上次会话统计）。理由：它**不在 Dropbox
  里**（机器本地）、是 Claude 正在写的活文件（旁路改有 race，毁的是中心配置）、且代价小且自愈
  （新路径生成新条目，顶多重点一次信任/重批权限，对话历史不丢）。也不动老 transcript **内部**
  写死的旧路径（纯历史上下文）。
- 配置 `[rename] sync_claude_projects`（默认 True）、`claude_projects_dir`（默认 `~/.claude/projects`）。
  关掉 → 只改 repo，不碰对话目录。

### 故意不做
- 多机同时把同一 repo 改成**不同**新名 → 后 sync 的那台发现 canonical 不在预期内 →
  跳过不猜，不做自动 merge（极罕见）。
- 不支持非 GitHub remote 的远端改名（gh 硬依赖，和 fork/publish 一致）。

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
