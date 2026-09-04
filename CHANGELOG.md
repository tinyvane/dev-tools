# Changelog

本文件记录 codesync 的用户可见版本变化。日期使用北京时间。

## [2.31.0] - 2026-09-05

### Added

- 新增 `codesync portable alias [--execute]`：在当前 PC 已加入 PATH 的 codesync 命令目录中原子安装
  `codexv.cmd`，从任意项目目录用 `codexv` 启动 V: portable workspace，同时普通 `codex` 继续使用
  本机 C: fallback。命令默认 dry run，可重复执行更新。
- 新增 `codesync portable alias --remove --execute`。只移除带 codesync 管理标记的 shim；已有同名用户
  命令、PATH 中更早的同名命令、错误 Volume GUID、未完成或非 dual migration 均 fail closed。

### Safety

- `codexv` 不把 portable `bin` 或 `CODEX_*` 写入用户环境；它通过独立 Windows PowerShell 子进程调用
  `Start-Codex.ps1`，保留调用目录、参数和退出码，并继续由 launcher 校验 Volume GUID、目录和 CLI。

## [2.30.1] - 2026-09-05

### Fixed

- Windows `LongPathsEnabled=0` 时，portable dual staging 的目录创建和文件复制现在使用
  extended-length path；`V:\CodexPortable\.dual-staging-*` 下达到或超过 260 字符的插件资源不再
  误报 `[WinError 3] 系统找不到指定的路径`。逻辑路径、Volume GUID、manifest 和 fail-closed
  校验保持不变，也不要求修改系统注册表。
- 保留并验证 `dual-stage-pending` 恢复协议：失败的 staging 会在下次无 writer 重试时整体归档到
  `backups\incomplete-dual-stage-*`，随后从当前 C: 权威源重新建立快照，不复用半成品。

## [2.30.0] - 2026-09-04

### Added

- `portable migrate` 默认采用 `dual` workspace：保留每台 PC 的 C: 本机 Codex 作为无 V 盘时的
  fallback，V: 上的主要 memory/对话只通过 `Start-Codex.ps1` 以进程级环境启动；本机临时会话
  不自动回灌 V:，代码仍通过 Git/codesync 收敛。旧的唯一 live-home 行为保留为显式
  `--mode exclusive`。
- 旧 v2.29 迁移停在 `prepared/data-ready/cli-ready` 时可直接转换为 dual：每次恢复都会从当前 C:
  权威源重新建立静止快照，将旧 V: `home/sqlite/bin` 整体移动到带时间戳 backup 后再换入；目录
  切换意图先写 manifest，中断后可判定并续跑。

### Fixed

- portable 调用官方 Windows installer 时强制 `CODEX_NON_INTERACTIVE=1`，不再在安装成功后等待
  `Start Codex now?` 直至 900 秒超时；真正超时现在明确报告秒数，不再伪装成 installer exit 124。
- dual 完成后删除 installer 写入用户 PATH 的 portable bin，并只在用户变量恰好指向 V: 时清除
  `CODEX_HOME/CODEX_SQLITE_HOME/CODEX_INSTALL_DIR`，不会覆盖其他本机配置。
- `status/verify` 识别 dual mode：C: 与 V: home 同时存在是正常状态，持久化的 V: 环境或 PATH
  反而报错；`attach/detach/rollback` 明确拒绝用于不做全局切换的 dual workspace。

## [2.29.2] - 2026-09-04

### Fixed

- `portable migrate --execute` 调用 OpenAI 官方 Windows 安装器时，现在启用 PowerShell 原生下载
  进度显示，可看到传输字节、总量和百分比，不再长时间只停留在 `Downloading Codex CLI`；官方
  安装器的下载源、SHA-256 校验和 fallback 流程保持不变，无法安全启用进度时会 fail closed。

## [2.29.1] - 2026-09-04

### Changed

- `portable migrate` dry run、执行前阻塞检查及 `portable status/verify` 的普通输出现在逐项显示
  Codex/ChatGPT 阻塞进程的 PID、进程名和可执行路径，并给出 `Stop-Process -Id <PID>` 提示；
  不显示可能含敏感参数的完整命令行，也不会自动终止进程。

## [2.29.0] - 2026-09-03

### Added

- 新增与 `sync/context` 隔离的 Windows `codesync portable` 命令族：`status`、`prepare`、
  `migrate`、`verify`、`attach`、`detach` 和 `rollback`。
- `prepare` 使用 Volume GUID 而非盘符登记移动 NVMe，逐行校验并 hash 全部 rollout，记录本机
  SQLite schema/coverage、CLI 来源与版本，然后生成 portable manifest 和 fail-closed
  `Start-Codex.ps1`。
- `migrate --execute` 在所有 Codex/ChatGPT/app-server 退出后才复制数据；使用 staging，按 UUID/hash/
  严格前缀合并 conversation，迁移单一 SQLite 权威源，重写本机 rollout 索引并运行
  `PRAGMA quick_check`，再通过 OpenAI 官方 standalone installer 安装 portable CLI。
- `attach/detach` 按 Windows MachineGuid 为每台 PC 单独保存和恢复用户级 `CODEX_HOME`、
  `CODEX_SQLITE_HOME`、`CODEX_INSTALL_DIR` 与 PATH；整体 rollback 只允许源机器执行。

### Safety

- portable config 强制 `cli_auth_credentials_store = "keyring"`，不迁移 `auth.json`、writer locks、
  sandbox secrets、`.env*` 或临时文件。
- `migrate` 和 `rollback` 默认只显示 dry run，必须显式给出 `--execute`；中间 phase 持久化，可在
  installer 或环境登记失败后安全续跑。rollback 保留 V: portable 数据为证据。
- `verify` 通过临时 SQLite 快照保持只读，并允许已迁移 rollout 后续合法追加，但要求迁移基线仍为
  严格前缀。Windows Store/ChatGPT app 是否继承 portable 环境必须实机验收，未作无依据保证。
- v2.28 `context` conversation transport 和 memory/LLM 合并继续冻结；现有 `sync/context` 参数与行为不变。

## [2.28.0] - 2026-09-03

### Added

- 新增与 Git `sync` 完全分离的 `codesync context status` 和 `codesync context doctor`：前者快速只读盘点
  Codex rollout、Dropbox junction/symlink 和本机 `/resume` 索引，后者逐行校验全部 JSONL 并运行
  SQLite `quick_check`。两者都支持 `--json`、`--sessions-dir` 和 `--transport-root`。
- 扫描器按 session UUID 检测重复与文件名不一致，核对 rollout 与 `threads.rollout_path`，并将
  SQLite/WAL/SHM、锁、认证、`.env*`、key 和 Dropbox 冲突副本视为禁止同步内容。
- 新增独立 `[context]` 配置段，可为每台机器设置 `sessions_dir` 和 `transport_root`；未配置时
  仍可从 `CODEX_HOME` 和现有 junction/symlink 只读自动检测。
- 读取 `thread-writer-locks` 并非阻塞地探测真正被持有的 session lock，为后续“目标 session 级静默
  判定”提供基线；不再把系统中其他 Codex/app-server 进程视为全局阻塞条件。
- SQLite 索引检查通过稳定的临时快照读取 live DB/WAL，避免只读诊断在 `.codex` 内创建或改动
  `-shm` 等 sidecar。

### Safety

- 2.28.0 只实现 D1 诊断，不复制、改写、合并或删除 conversation，也不读取/生成 memory。

## [2.27.0] - 2026-09-03

### Added

- 所有依赖仓库目录的命令现在先检查 `code_roots`：未配置、目录丢失、路径不是目录或不可访问时，
  会在 Git/SSH/GitHub 及任何仓库操作之前停止，不再把配置故障显示成“发现 0 个 repo”的假成功。
- 交互式终端可在启动检查中直接输入新的一个或多个代码目录；确认后原配置会先备份，再原子更新
  `code_roots`，并在旧 `auto_clone.target` 同样失效时一起修正。其他现有配置项保持不变。
- 非交互运行不会自动修改配置，失败时返回退出码 2，并打印配置文件路径和可执行的修复指引；
  `--version`、`--update`、`config-path`、`init` 和帮助仍不受目录检查影响。

## [2.26.2] - 2026-09-03

### Fixed

- non-fast-forward push 的待办现在明确说明尚未执行自动 rebase、仓库保持 push 前状态，不再复用 pull
  冲突路径的“自动 rebase 已回滚”文案。
- loose branch refs 扫描改为 `存在 / 不存在 / 无法读取` 三态；权限或 IO 错误会作为不确定状态
  fail closed，不再把不可读 repo 误判成可自动移动的 incomplete clone。
- 清零全仓 38 个 Ruff 告警，并将 `ruff check .` 加入 Windows、macOS、Ubuntu × Python 3.11–3.13
  的 GitHub Actions 矩阵，防止静态质量回退。

## [2.26.1] - 2026-09-03

### Fixed

- `delete --local-only` 先原子持久化 Repository ID tombstone 与 `Known` 摘名，再移动本地目录，
  最后写完整 Trash 记录；移动失败会在源目录仍存在时回滚保护意图，最终状态写入失败则保留远端保护并
  返回失败，不再误报成功，也不会留下下一轮误归档远端的崩溃窗口。无 Repository ID 的明确 404
  路径和无 GitHub origin 的本地目录同样在移动前持久化 `Known` 摘名。
- GitHub repo 不存在只接受明确的 repository 404；DNS 解析失败、缺少命令、403、TLS、超时及其他
  不确定错误一律归为 `unavailable` 并 fail closed，不再被宽泛的 `could not resolve` / `not found`
  子串误判为远端已删除。
- 本地垃圾箱恢复改为目录 rename 成功后才删除 manifest；rename 或 manifest 清理失败时保持或回滚
  可发现的垃圾箱条目，并拒绝从符号链接或对应 code root 之外恢复。
- held repo 的逐项 GitHub 探测会把本轮剩余秒数传入子进程，使整批 60 秒诊断预算成为硬上限，
  不再允许单个默认 120 秒调用越界。
- macOS ControlPath 测试固定使用短 `/tmp` 基址；Windows known-hosts 重试测试避开零 TTL 的时钟
  舍入边界，恢复 Python 3.11–3.13、Windows/macOS/Ubuntu 全矩阵的确定性。

## [2.26.0] - 2026-08-27

### Added

- `sync` 在状态总览之后聚合“需要你处理的事项”，把 rebase/push 分叉、脏 submodule、clone
  目录冲突、远端消失和残骸恢复命令集中到输出末尾，避免被大量 repo 的滚动日志刷走。
- GitHub active 列表中消失的本地 repo 会按本地 origin 逐个确诊 404、转移/改名、archive 或
  网络/权限异常；超过 20 个时停止逐项 API 探测，所有不确定状态仍保持本地不动。

### Changed

- 同 origin 的未完成 clone 空壳会在紧邻移动前再次复核，原子移入同 code root 的
  `.codesync-trash` 后自动重试 clone；不再永久删除可能含 refs、hooks 或自定义 Git 元数据的目录。
- rebase 冲突回滚后补充 ahead/behind 数量；non-fast-forward push 与脏 gitlink 现在给出可直接执行的
  排查、恢复命令。
- `codesync delete --local-only` 在 GitHub 明确返回 404 时允许无 Repository ID 地移入纯本地垃圾箱，
  同时从 `Known` 摘名，避免可见性恢复后错误归档真实远端；仍不写 tombstone，并报告可检测到的未推送
  提交。普通 delete 和网络/权限不确定仍拒绝执行。
- held repo 逐项确诊增加整批 60 秒预算；分叉诊断按当前 upstream fetch，并明确 hard reset 的干净
  工作区前提；脏 submodule 在整仓恢复到 HEAD 前先 stash 保底。
- 待办去重加入 repo 路径 identity，并移到 `run_sync` finally 打印，异常和安全 guard 不再丢结果。

## [2.25.0] - 2026-08-27

### Added

- 新增 `codesync pull` 和 `codesync push` 两个独立命令。`pull` = 自动 commit + rebase pull；
  `push` = 自动 commit + push。两者都**不** clone、不发布孤儿、不触碰归档路径，都支持
  `--no-commit` / `--workers` / `--local-workers` / `--problems`。要收敛已分叉的仓库仍然用
  `codesync sync`：`push` 不 pull，分叉会被 git 直接拒绝，codesync 永远不会 force push。
- **发布孤儿目录前检查超大文件**：目录里有超过 GitHub 单文件 100 MiB 硬上限的文件时，
  **不创建 repo** 并说明原因。此前 `gh repo create --source=. --push` 会先建远端、设好 origin
  再 push，于是留下一个空 GitHub repo + 没有 upstream 的本地 repo，此后每轮 sync 都失败 ——
  而症状显示为"传输超时"，指向网络而非真正原因。
- `codesync rename <新名> --local-only`：只改本地目录名，GitHub 和 origin 保持不变。
- `codesync delete <名> --local-only`：只把本地目录移入垃圾箱，GitHub repo 保持存活。仍会按
  Repository ID 记录 tombstone（因此仍需只读访问一次 GitHub），否则下次 sync 会重新 clone 回来。

### Changed

- **并发默认值提高**：网络操作在无 SSH 连接复用时从 1 提高到 4，有复用时从 4 提高到 8。
  Windows OpenSSH 不支持 ControlMaster，所以此前 Windows 上**每个仓库都是串行 pull** ——
  按实测 6.6-10.2s 的无复用握手，141 个仓库光握手就要 15-24 分钟。`--workers N` 与
  `[sync].net_workers` 仍可覆盖回旧行为。
- 超时分档从四档改为五档：新增 `T_NET_CLONE=3600s` 专供 `git clone` 与 `gh repo create --push`。
  clone 传的是整个历史而非增量，且被杀掉会留下需要人工清理的半成品目录；死链仍由 300 秒停滞策略
  正常捕获，所以放宽 clone 的墙钟兜底只放过"慢但在推进"的传输。
- `T_NET_LONG` 从 3600 秒收回 900 秒，且 **pull/push 从 `T_NET`（120 秒）移到该档**。此前
  pull/push 在 120 秒超时下运行，而停滞检测窗口是 300 秒 —— 死链在停滞策略开火前就被超时杀掉，
  整层检测在它本该服务的路径上从未生效；同时任何超过约 1.8 MB 的传输每轮必然超时。
- GitHub 443 主机密钥改为**派生优先**（用户 `~/.ssh/known_hosts` 的 `github.com` 条目），缓存降为
  兜底并加 30 天 TTL。此前缓存优先会在 GitHub 轮换 host key 后把过期 key 永久钉死。缓存过期而
  刷新失败时继续供应旧缓存并提示删除路径，绝不因网络错误禁用信任。
- SSH 配置改为按子命令门控：`--version`、`config-path`、`migrate-config`、`--help`、`trash list`
  不再触发 GitHub 主机密钥探测。探测失败写 1 小时负缓存，墙内不再每条命令都付一次超时。

### Fixed

- `git status` 失败的仓库不再显示成灰色 clean，也不再被 `--status --problems` 整行丢弃；
  `--show-stash` 能力探测只缓存确定结论，不再被单个损坏仓库带偏成整轮全红。
- 仓库身份判定改用 `git config --local`：此前"不是仓库"、"半删除残骸"和"仓库没有 origin"
  三者返回完全相同（rc 1、无 stderr），导致 `delete` / `rename` 的 fail-closed 闸门失效。
- 串行重试不再覆盖可操作的失败信息。rebase 冲突且自动回滚失败时，带具体恢复命令的提示会保留到
  最终汇总，而不是被重试路径的"存在未完成的 rebase"覆盖掉。
- `_prepare_control_dir` 先校验 symlink 与属主再 `chmod`，避免对被指向的目录误改权限。
- 测试套件现在跑工作树而非已安装副本（`pythonpath = ["src"]`），Windows 上不再有 13 个夹具报错。

## [2.24.0] - 2026-08-26

### Added

- 新增统一 GitHub remote URL 解析器，精确支持 HTTPS、SSH、SSH 端口、`ssh.github.com:443`
  与 ghproxy 前缀形态，并为跨协议重复 origin 检测生成同一身份键。
- `[sync]` 新增 `stall_bytes_per_sec`（默认 1000）、`stall_seconds`（默认 120）和
  `cleanup_stale_packs`（默认 `true`）；HTTP 使用 Git low-speed 检测，SSH 使用 ServerAlive。
- 扫描可识别 HEAD 已存在但没有 loose/packed 分支 ref 的未完成 clone，并清理超过 24 小时的
  `.git/objects/pack/tmp_pack_*`，保留仍可能在写入的文件。

### Changed

- 仓库身份判定统一读取 `git config --get remote.origin.url` 的原始值，不再读取会应用
  `insteadOf` 的 `git remote get-url`；无 origin 的 rc=1 与超时/启动失败保持严格区分。
- `T_NET_LONG` 从 900 秒提高到 3600 秒，作为大仓库传输的最终兜底；真正停滞由 HTTP low-speed
  与 SSH ServerAlive 在约两分钟内中止，`T_NET` 仍为 120 秒。

### Fixed

- 修复进程级 GitHub SSH-443 改写被身份扫描再次读回后把端口 `443` 当作 owner，导致 SSH 仓库
  对 auto-clone/Known 隐形、重复 clone、跨协议去重失效及 rename 错误降级为本地-only 的问题。
- 消除已在 `Known` 的 HTTPS 仓库改用 SSH 后被误判为本地删除、进而触发 GitHub 归档的风险。

## [2.23.0] - 2026-08-25

### Added

- `[sync].countdown_seconds` 配置同步开始前的安全倒计时；设为 `0` 时保留说明但立即开始。

### Changed

- repo 状态改用 porcelain v2 合并读取分支、上下游、ahead/behind、stash 与工作区状态，
  现代 Git 每个 repo 的状态探测从 5 个子进程降为 2 个；旧 Git 自动回退原路径。
- GitHub 本地 origin 重扫在每次调用内部按 `local_workers` 并行执行，保持归档门禁与确定性归并语义。
- sync 的 owner 推导与重复-origin 检测共享一次并行 origin 扫描，避免同轮重复启动 git。

## [2.22.0] - 2026-08-25

### Added

- `[pull].rebase` 配置（默认 `true`）；可显式退回 v2.20.0 的 `--ff-only` pull 策略。
- pull 前用纯文件系统标记检查未完成的 rebase / merge / cherry-pick / revert，
  包括 `.git` 为相对 gitdir 文件的 worktree 形态。

### Changed

- sync 核心顺序改为“自动 commit → `pull --rebase --autostash` → push”，本地未推送提交
  会重放到远端最新提交之上，多机同步不再因工具自动 commit 必然分叉。

### Fixed

- codesync 自己发起的 rebase 冲突会自动 `rebase --abort` 回滚；既有未完成操作一律跳过，
  绝不自动 abort 用户手工现场。
- autostash 重放工作区时冲突不再误尝试 abort，明确提示用户改动仍保留在 stash。
- 保留本地新分支尚未发布时的“新分支·待推送”良性降级，不计为 pull 失败。

## [2.21.0] - 2026-08-25

### Added

- codesync 自管仅含 `[ssh.github.com]:443` 的 known_hosts 缓存：优先沿用有效缓存，其次从用户已信任的
  明文或 hashed `github.com` 条目派生，最后通过 TLS 校验的 GitHub meta API 获取；不写用户 SSH 文件。
- `[sync]` 新增 `github_known_hosts`、`ssh_multiplex`、`net_workers`、`local_workers` 配置；
  `--local-workers` 可独立覆盖本地元数据扫描并发。
- POSIX GitHub SSH ControlMaster 连接复用，含 PID 隔离的 ControlPath、路径长度保护、预热和清理。

### Changed

- 本地元数据与网络 Git 操作分开调度；复用生效时网络默认 4 workers，否则保持保守的 1 worker，
  本地扫描按 CPU 自动扩展到最多 32 workers。
- known_hosts 与 ControlMaster 统一由一处组装 `GIT_SSH_COMMAND`；保留用户默认 known_hosts 在前，
  尊重用户自定义命令，Windows 仍启用 GitHub 443 known_hosts 但禁用 ControlMaster。

### Fixed

- 修复 v2.19.0 起 URL 改写到 `ssh.github.com:443` 后，非交互子进程无法确认独立 host key，导致所有
  pull/push 报 `Host key verification failed` 的现网问题。
- 跳过 `@revoked` 与 `@cert-authority` 条目，避免把撤销 key 或 CA 语义错误复制成普通 host key。

## [2.20.0] - 2026-08-05

### Added

- 新增统一 `proc.run` 子进程封装与 30/300/120/900 秒四档 timeout；可用
  `CODESYNC_TIMEOUT_SCALE` 为慢网络同比放大，timeout/命令缺失/OS 错误收敛为 124/127/126。
- 新增 AST 防回归测试，禁止未经审查的 raw `subprocess.run/Popen` 和 import 绕过。

### Changed

- 非交互子进程默认关闭 stdin；git hook/凭据助手不能再等待输入挂死，仍保留 hook 校验，不使用
  `--no-verify`。交互式 `gh auth login --web` 继续继承终端且不设 timeout。
- GitHub、本地 Git、publish、rename、fork setup、垃圾箱和前台 updater 操作全部使用分层 timeout；
  clone 仍显示进度，超时半成品不自动删除。

### Fixed

- 修复 delete 前置 push 失败读取不存在的 `OpResult.message` 而抛裸 traceback；现在返回 detail 并
  保持远端、本地原状。
- 修复 tombstone 写入用 Repository ID、读取却按 repo 名比较导致保护恒不生效；同名新 ID 不受旧
  tombstone 污染，垃圾箱前缀 repo 永不自动 clone，所有 restore 入口恢复完整状态。
- GitHub repo list 或本地 origin 扫描超时不再被解释为“远端/本地不存在”；危险动作 fail-closed，
  degraded 扫描禁止归档/本地移动/自动改名且 Known 只增不减。
- GitHub 存在性、origin、commit/staged/reset 等不确定检查不再放行 publish、rename、delete 或
  嵌套 gitlink commit。

## [2.19.1] - 2026-07-22

### Changed

- `codesync sync` 默认使用单 worker，降低短时间内并发建立 Git/SSH 连接的风险；仍可用 `--workers N` 显式调整。
- 写同步开始前显示 SSH 443、ahead-only push 和 worker 数提示，并倒计时 10 秒；期间按 `Ctrl+C` 会在 clone/publish/pull/commit/push 之前安全取消。只读 `sync --status` 不等待。

## [2.19.0] - 2026-07-22

### Security

- codesync 进程内把 GitHub SSH remote 透明改写到 GitHub 官方 `ssh.github.com:443` 端点，避免大量同步连接直连 TCP 22；不修改仓库 remote 或用户 `~/.ssh/config`。

### Changed

- push 阶段先比较当前分支与 upstream，只对真正 ahead 的仓库发起网络 push；有提交但尚无 upstream 的分支仍保留首次 push，空仓库和已同步仓库直接跳过。

## [2.18.1] - 2026-07-10

### Documentation

- 补充 Linux / Rocky 新机器安装提示：Python 3.11、git、GitHub CLI、SSH 登录选择和 SSH key passphrase 建议。

## [2.17.0] - 2026-06-20

### Added

- 仓库垃圾箱协议：GitHub repo 先重命名为 `zz-trash--v1--<时间>--<ID摘要>--<原名>` 再 archive，释放原名称。
- 本地完整目录移动到 `<code_root>/.codesync-trash/`，保留 `.env`、ignored 文件、stash、分支和完整 `.git`。
- 使用 GitHub Repository ID 跨机识别旧仓库；另一台最新版客户端下次 sync 自动执行相同移动。
- `codesync trash list|restore|purge`，恢复同样按 Repository ID 校验，永久清理要求明确确认。
- `known-repos.json` schema/protocol 版本、Repository ID/path baseline、pending archive 和 trash manifest。

### Changed

- `codesync delete` 不再永久删除；远端重命名+archive 与本地移动组成 fail-closed 事务。
- 写能力的 `sync`、`delete`、`trash restore/purge` 每次 fresh 检查版本；无法确认或不是最新版时禁止操作。
- GitHub 列表中“消失”不再等同 archive；只有明确 `isArchived=true` 才触发本地垃圾箱移动。
- 状态文件改为跨进程锁保护、同目录临时文件 + `fsync` + `os.replace` 原子更新。

### Fixed

- 拒绝 `delete ..`、绝对路径和带分隔符名称，封堵 code root 外路径穿越。
- archive 失败或 `--no-push` 不再丢失本地删除意图并错误重新 clone。
- canonical/Repository ID 无法确认时不再继续远端操作。
- `rmtree_repo` 不再吞掉只读重试失败，并验证删除后置条件。
- 损坏 repo、缺失 code root、权限变化和 API 列表异常不再被解释为删除授权。
- 冻结的 V1 `sync.ps1` 禁止旧式本地删除和远端归档，避免旧客户端误解 v2.17 协议。
- 修复长期 CI 隔离问题：测试不再篡改全局 `os.name`，临时 Git repo 明确配置本地提交身份。

## [2.16.0] - 2026-06-12

- 增加 `delete --purge` 和半删除 `.git` 残骸检测。`--purge` 已在 2.17.0 被安全的 `trash purge` 流程取代。

## [2.15.0] - 2026-06-11

- 增加 tombstone、dirty/ahead 删除保护和 GitHub 改名重定向防护。
