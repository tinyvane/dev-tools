# Changelog

本文件记录 codesync 的用户可见版本变化。日期使用北京时间。

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
