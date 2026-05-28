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

## v2.3.0（2026-05-28）— "一个命令、感觉不到"：sync 自动 publish + 默认 push

用户诉求（原话）："如无必要勿增实体...希望能一个命令，检查本地所有目录是否建立了 git，
然后新的 mkdir 的目录默认建立 private repo 然后上传，让我'感觉不到'就能把每次本地更新上传、
把本地落后代码更新。"

拒绝了 v2.2 时期讨论的「方向 1 手动 gh repo create」和「方向 2 独立 publish 命令」，
要求全部塞进 `codesync sync` 一条命令。

### `codesync sync` 默认行为（按顺序）

1. auto_clone（GitHub → 本地，已有）
2. **publish orphans（本地 → GitHub，新）**
3. 并发 pull（已有）
4. DB restore（已有）
5. **并发 push（新默认，之前要 --push opt-in）**
6. DB dump（已有，跟随 push）
7. 状态总览（已有）

### publish orphans 逻辑（`src/codesync/publish.py`）

扫 `code_roots/*`：
- 非空目录、无 `.git/` → `git init -b main` + add + commit + `gh repo create --private --source=. --push`
- 有 `.git/` 但无 origin → 同上（跳过 init/commit）
- 有 origin → 跳过（已 tracked）
- 空目录 / 隐藏目录 / `node_modules`/`__pycache__`/`.venv` 等 artifact / 配置 skip 名单 → 跳过
- GitHub 上已存在同名 repo → 跳过 + warn（不覆盖）

安全：列出所有候选 + 5 秒倒计时（Ctrl+C 取消），`[publish] skip_confirmation = true` 可关。

### push 变默认

`--push` 保留但变 no-op（向后兼容）。新增 `--no-push`（纯 pull 模式）和 `--no-publish`。
push 默认后，DB dump、auto_clone 的 archive-on-local-delete 也随之默认触发。

### 新 config 段

```toml
[publish]
skip              = []        # 永不 publish 的目录名
skip_confirmation = false      # true = 跳过 5 秒倒计时
```

wizard 生成的 TOML 含此段。CONFIG_TEMPLATE 注释里也有。

- [x] `publish.py`（find_orphan_candidates / _gh_repo_exists / publish_one / publish_orphans）
- [x] `PublishConfig` dataclass + load + _to_toml + template + wizard
- [x] `sync.run_sync` 重写：publish 阶段 + push 默认 + no_publish/no_push opt-out
- [x] cli.py：--no-push / --no-publish，--push 降级 no-op
- [x] 测试 +18 (134 total)：orphan 识别各分支、gh 存在性、publish_one init/skip/fail 流程、
  PublishConfig round-trip / absent

### 设计取舍

- **5 秒倒计时**而非完全静默：publish 是不可逆操作（GitHub 上真建 repo），保留取消窗口。
  想完全无感的人 `skip_confirmation = true`
- **默认 private**：符合用户偏好
- **空目录不 publish**：`mkdir` 占位但没文件不该建 repo
- **artifact 黑名单**：node_modules 这种不是项目，硬跳过
- **GitHub 已存在不覆盖**：撞名只 warn，避免误操作

## v2.2.9（2026-05-28）— fork 自动配 upstream remote（A + B）

V2.2.8 把 fork 也 auto_clone 下来了，但只配了 origin，**upstream 还要手动 `git remote add`**。
违背了"我用 AI/工具不学 git"的承诺。

为完成 "git fetch upstream + cherry-pick / AI port" 工作流的前置条件，本版本两个改动一起做：

**A. auto_clone clone 完 fork 后自动配 upstream**

`github_auto.run()` 的 clone 循环里，clone 成功后如果 `name in all_forks`（独立于
`include_forks` 的过滤），调用 `fork_setup.add_upstream_for_fork()` 拿 parent SSH URL
并 `git remote add upstream <url>`。失败仅 warn，不阻断 clone。

**B. `codesync fork-setup` 命令补漏老 fork**

`auto_clone` 看到目录已存在就跳过 clone，**老 fork（pre-v2.2.9 时期 clone 的、或者你
手动 clone 的）就吃不到 A 路径的好处**。所以加个一次性 backfill 命令：

```
codesync fork-setup
  扫所有 code_roots/*
  对每个本地 git repo:
    - 已有 upstream → skip
    - origin 不是你 owner → skip
    - origin 是你 owner 但不在 fork 列表 → skip (是自创 repo)
    - 是 fork 但没 upstream → 调 gh api 拿 parent URL → git remote add upstream
  打印汇总（新配 / 已有 / 非fork / 非owned / 失败）
```

幂等 + 显式 + 不破坏。跟 `migrate-config` 同类，一次性运维命令。

新模块 `src/codesync/fork_setup.py`：
- `_gh_get_parent_url(owner, name) → str|None`（gh api repos/X/Y --jq .parent.ssh_url）
- `_git_remotes(repo) → dict`（解析 `git remote -v` 输出）
- `_list_user_forks(owner) → set[str]`（gh repo list --fork --json name）
- `_ORIGIN_OWNER_NAME` regex 匹配 SSH/HTTPS origin URL 拆出 owner/name
- `add_upstream_for_fork(repo, owner, name) → (ok, msg)`（被 github_auto 和 fork-setup 复用）
- `run_fork_setup()` 主入口

`cli.py` 加 `fork-setup` 子命令。

- [x] 测试 +16 (116 total)：parent_url happy / failure / null / empty；git_remotes 解析；
  list_user_forks json / 失败 / bad json；add_upstream 成功 / parent 缺失 / git 失败；
  origin URL regex SSH / HTTPS / 带 .git / 带末尾斜杠 4 种

### 不在本版本做的事
- 不在 `codesync sync` 里自动 invoke fork-setup —— sync 不应该跑长 IO（gh api 每个 fork 一次调用）
- 不支持非 GitHub upstream（gh-only）—— 用户场景全是 GitHub fork
- 不去重连 origin 是 https 但用户想用 ssh 之类的格式转换 —— gh API 给的 parent.ssh_url 就是 ssh，不动

## v2.2.8（2026-05-28）— auto_clone 默认包含 fork（include_forks）

V2.2.7 用户实测后反馈：「我有几个 fork 别人的库，有的是想保存别人代码的快照，
有的是 fork 来改的，希望都能像自己创建的 repo 一样跨机器同步。」

原 `github_auto.py` 写死 `not r.get("isFork")` 把所有 fork 排除。改成可配。

### 修法

`AutoCloneConfig` 加字段：`include_forks: bool = True`。

| 值 | 行为 |
|---|---|
| `true`（默认） | fork 跟自己创建的 repo 同等对待：会被 clone、被 status 列出、本地删 + `--push` 时会被 archive 到 GitHub |
| `false` | 跳过所有 fork（pre-v2.2.8 行为；适合 fork 上游只是为了读代码、不想本地堆积的人） |

- `load()`：缺字段时默认 True（personal 用户的常见预期 + opt-out 简单）
- `_to_toml()`：始终输出 `include_forks = <bool>`（明确无歧义）
- `wizard._build_initial_toml()`：新生成的 TOML 显式写 `include_forks = true`
- `github_auto.run()`：按 `ac.include_forks` 分支 `fork_set` 和 `active` 集合构造
- 测试 +4 (100 total)：缺字段 / 显式 false / round-trip / wizard 生成

### 不在本版本做的事（明确 punt 到下一轮）

用户后续会请我做「**把 fork 转成 detach 的 private repo**」—— 不是改 codesync 代码，
而是对他 GitHub 账户的几个具体 fork 做一次性运维操作（clone → 新建 private → push →
删原 fork）。**那部分不进 codesync 主线**，留给下一轮交互式做。

## v2.2.7（2026-05-28）— wizard 自动接管「v2.2.5 留下的空模板」

V2.2.6 用户实测 Mac 后反馈：升级到 v2.2.6 后跑 `codesync sync`，wizard
没触发 —— 因为 v2.2.5 那次跑过一次 `codesync sync`，**已经在磁盘上留下了
一份未编辑的空模板** `config.toml`。v2.2.6 的 wizard 只看「文件不存在」，
看到文件就放过 → sync 拿到空配置跑了个寂寞 → 用户被推回「rm ~/.config/...」
的手动指令，跟「自动化和优雅」直接对立。

修法 (`config.py::is_template_unedited()`)：

- 检 `config.toml` 是否与 `CONFIG_TEMPLATE` **完全一致**（byte-for-byte）
- 一致 → 视为「v2.2.5-era 空模板，用户没动过」→ wizard 该跑
- 任何修改（哪怕加一行注释）→ 视为「用户碰过了」→ wizard 不动它

`cli.py` 的 sync 入口：

```python
needs_setup = (not cfg_file.exists()) or is_template_unedited()
if needs_setup:
    run_first_run_wizard()
    if is_template_unedited():
        # wizard 因故 bail 了（没 gh / 用户拒绝），告诉用户怎么办，不傻跑 sync
        return 1
```

wizard bail 后还是空模板的话，**不再无声 fallback 到 sync**（v2.2.6 的隐患），
而是给出明确指令：跑 `codesync init` 重试 wizard，或手动编辑后再 sync。

- [x] `is_template_unedited()` byte-for-byte 比对
- [x] sync 入口检测 + 失败时干净退出（非 0）
- [x] 测试 +4 (96 total)：fresh template / missing / user-edited / wizard-written

### 设计取舍

- 用 byte-for-byte 而非「结构性是否为空」：简单、误判风险最低。template 改的话
  老用户那台 is_template_unedited 会返回 False（视为"用户碰过"，不重跑 wizard），
  这种 false negative 是安全的（用户能手动 `codesync init`）。
- 不在 `load()` 里查 is_template_unedited：保持 `load()` 纯，wizard 触发逻辑集中在 cli.py。

## v2.2.6（2026-05-28）— 首次运行不再让用户自己编辑 TOML

V2.2.5 用户在 Mac 装完，跑 `codesync sync`，看到的是：

```
⚠ 配置文件不存在，已生成模板: /Users/yiwang/.config/codesync/config.toml
⚠ 请编辑后重新运行 `codesync sync`。
```

用户反馈：「难道不应该帮我配置好需要同步的内容么？或者让我登陆 GitHub，
然后帮我把 GitHub 名下所有的 repos pull 下来？」

完全合理 —— 装机一行 + 首跑还要手 vi `config.toml` 不算「优雅」。

### 修法：first-run wizard

新模块 `src/codesync/wizard.py`：

1. 检 gh CLI 是否装 —— 没装 → fall back 到原模板逻辑（让用户手编辑）
2. 没认证 → 调 `ensure_gh_authenticated()`（已有逻辑，浏览器 Device Flow）
3. `gh api user --jq .login` 拿 owner
4. 打印拟生成的配置（owner、code_roots=~/SyncRepos、auto_clone.target=~/SyncRepos、db_sync 留空）
5. 提示用户确认（默认 Y；EOF / 空输入 → Y；显式 n → bail）
6. 写 TOML，返回 True 让 `codesync sync` 继续执行 —— auto_clone 会拉齐所有 repo

`codesync sync` 在 config 不存在时**自动 invoke wizard**。新增 `codesync init` 子命令
显式触发同一个 wizard（适合用户想重置的场景，或者纯做配置不立刻 sync）。

- [x] `wizard.run_first_run_wizard()` + `_prompt_yes()` + `_build_initial_toml()`
- [x] `auth.gh_username()`（`gh api user --jq .login`）
- [x] `cli.py`：`init` 子命令 + `sync` 首跑自动 invoke
- [x] 测试 +8 (92 total)：gh 缺失 / 认证失败 / 用户名拿不到 / Y / 空输入 / EOF / 显式 N / 生成的 TOML 可解析
- [x] db_sync 故意留空 —— 大部分用户没 Docker MySQL，需要的人手动加

### 不做：

- 不在 wizard 里问 db_sync。这玩意儿涉及 docker container 名字、密码、Dropbox 路径，
  问起来啰嗦且大部分用户用不上。TOML 模板里有注释示例，自助加即可。
- 不让用户选 code_roots 路径。默认 `~/SyncRepos` 跨平台一致，想要别的位置就改 TOML。
- 不自动跑 `gh auth login`。直接复用 `ensure_gh_authenticated()`，如果未登 gh 会弹浏览器
  —— 这条路径在更早版本就跑通过。

## v2.2.5（2026-05-28）— 修 macOS bash 3.2 兼容（`set -u` + 变量后跟中文标点）

V2.2.4 真实 Mac 跑日志：

```
⚠ pipx 未装。在 externally-managed Python 上，pipx 是装 Python 应用的标准方式。
bash: line 128: installer_label?: unbound variable
```

错误出在脚本里这一行：

```bash
detail "检测到 $installer_label。"
```

`$installer_label` 是已赋值的（`installer_label="Homebrew"`），但 macOS 自带的 **bash 3.2.57**（Apple 2007 年的 fork，因 GPL v3 至今不升级）在 `set -u` 下解析 `$var<UTF-8>` 时
有 bug：它把 `。`（U+3002, UTF-8 `\xe3\x80\x82`）的首字节 `\xe3` 当成变量名延伸字符，
得到一个 `installer_label?`（`?` 是 bash 错误信息里的不可打印字符占位符）的伪变量名，
触发 unbound variable 错误。bash 5.x（Linux/WSL/Homebrew bash）和 zsh 都没这个 bug，
所以 CI 矩阵（Ubuntu）和我本机（Git Bash 5.2）都没暴露。

修法：变量后紧跟非 ASCII 字符时显式用 `${var}` 大括号定界。脚本里只有这一处中招，其他地方
变量后都跟空格（ASCII 安全分界符）。

- [x] line 128 改 `${installer_label}。`
- [x] 加内联注释（防止后人无知拆掉大括号）
- [x] bump 2.2.5

### 未来 install.sh 写法守则
变量后**紧跟非 ASCII 字符**时必须用 `${var}` 大括号定界。变量后跟空格、ASCII 标点不需要。
最稳的做法是养成所有 `$var` 都写 `${var}` 的习惯，但脚本里已存在的安全用法不动。

## v2.2.4（2026-05-28）— install.sh 自动装 pipx

V2.2.3 让脚本在 PEP 668 Python 上检测出问题、提示用户**手动**装 pipx。
真实用户反馈："普通用户不会知道要先 brew install pipx，一行命令的承诺就破了。"

修法：脚本检测到缺 pipx 时，按 OS 自动选包管理器跑安装命令：

| OS / 包管理器 | 命令 |
|---|---|
| macOS + brew | `brew install pipx` |
| Debian/Ubuntu | `sudo apt-get update && sudo apt-get install -y pipx` |
| Fedora/RHEL | `sudo dnf install -y pipx` |
| 老 RHEL/CentOS | `sudo yum install -y pipx` |
| Arch | `sudo pacman -S --noconfirm python-pipx` |

- [x] 5 秒倒计时给用户取消窗口（Ctrl+C 可中断）—— 和 auto_clone 的破坏性确认套路一致
- [x] 装失败时 fall back 到打印手动指令 + exit 1
- [x] macOS 没装 brew：不替用户装 brew（太深），直接提示 brew 一键装命令 + exit 1
- [x] 其他未识别的 Linux 发行版：提示 https://pipx.pypa.io/stable/installation/ + exit 1

注意：脚本是 `curl ... | bash` 形式跑的，bash 的 stdin 是管道但 stderr/stdout/terminal 是真的，
所以 sudo 弹密码会正常出现（sudo 从 `/dev/tty` 读，不依赖 stdin）。

## v2.2.3（2026-05-27）— macOS Homebrew Python（PEP 668）支持

V2.2.2 用户在 Mac 上跑 `install.sh` 报错：

```
error: externally-managed-environment
× This environment is externally managed
```

Homebrew 装的 Python 标记自己是 "externally managed"（[PEP 668](https://peps.python.org/pep-0668/)），
拒绝 `pip install --user`。PEP 668 推荐的替代是 [pipx](https://pipx.pypa.io)：
每个 Python 应用一个 venv，跟 system Python 隔离。

### 修法

- [x] **install.sh 加 PEP 668 检测**：用 `sysconfig.get_path('stdlib')` 找 stdlib 目录，
  检测 `EXTERNALLY-MANAGED` 文件是否存在。在就走 pipx 分支，不在保持原 pip --user 路径。
- [x] **install.sh pipx 分支**：要求 pipx 已装（不自动 brew install，太激进），
  跑 `pipx install --force git+...`（`--force` 让 install 等价于 install-or-upgrade，
  幂等）；`pipx ensurepath` 管 PATH（写入 ~/.zshrc / ~/.bashrc，自家逻辑，不跟我们的
  marker 段冲突）。
- [x] **updater.py 适配 venv**：pipx 装的 codesync `sys.executable` 是 venv 里的 python，
  `pip install --user` 在 venv 里会被拒（"User site-packages are not visible in this
  virtualenv"）。新增 `_in_venv()`（`sys.prefix != sys.base_prefix`），venv 里跑 pip
  不传 `--user`，venv 外保持原行为。同时支持 stdlib venv 和 pipx。
- [x] 测试 +3 (84 total)：`_pip_args` 在 venv 内/外的分支、base_prefix 缺失的回退。

### 设计取舍

- 不自动装 pipx：每个 OS 命令不同（`brew install pipx` / `apt install pipx` / 别的），
  错误率高 + 需要 sudo。脚本只检测 + 提示用户怎么装。
- 不让用户用 `--break-system-packages`：那是 PEP 668 故意留给"我知道我在干什么"的逃生口，
  长期会污染 system Python。pipx 是干净路径。

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
