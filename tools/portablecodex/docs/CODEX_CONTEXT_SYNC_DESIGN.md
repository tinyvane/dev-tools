# Codex conversation 与 memory 跨机归集设计

> 状态：2026-09-03 D1 只读扫描已完成；memory 阶段冻结
> 当前版本：2.28.0
> CLI 边界：context 诊断归属独立工具 `portablecodex context`；`codesync` 只负责 Git/代码同步。

## 1. 结论

这项能力必须拆成三层，不能把整个 `.codex` 目录直接交给 Dropbox：

1. **conversation transport**：用 Dropbox 归集 `sessions` 下的 rollout JSONL；
2. **local catalog**：每台机器独立维护 Codex 本机线程索引，使外来 JSONL 能被 `/resume` 发现；
3. **semantic memory**：脚本负责确定性提取、去重和证据追踪，LLM 负责语义归纳与冲突合并。

SQLite、WAL/SHM、锁、认证、配置和沙箱秘密都不得同步。原始 conversation 不做语义拼接、不删除、不让 LLM 回写。

## 2. 为什么只做 junction 还不够

`.codex/sessions` 是 conversation 的正文存储，但当前 Codex 还在本机 `state_*.sqlite` 的 `threads` 表维护 `id`、`rollout_path`、`cwd`、标题和更新时间等索引。Dropbox 能把 JSONL 带到另一台机器，却不能安全地同步这个正在写入的 SQLite。

因此 junction 解决“文件到达”，未来的 `portablecodex context reconcile` 解决“本机可发现”。本机状态库只允许在目标 session 已按 5.3 节确认静默、已经备份、schema/version 受支持且事务校验通过时更新；无关 Codex 实例可以继续运行，任何未知版本或字段都 fail closed。

## 3. 新命令，不改 `sync`

```text
portablecodex context status                         # 默认只读；总览目录、云端文件、冲突和本机索引
portablecodex context setup --transport dropbox     # 建目录、首轮复制、校验、备份、junction
portablecodex context reconcile                     # 归集 JSONL，补齐本机 /resume 索引
portablecodex context memory --project <repo>        # 生成项目 memory 草稿
portablecodex context memory --project <repo> --apply# 人工确认后发布规范 memory
portablecodex context doctor                         # 深度检查路径、Dropbox、索引、版本和隐私边界
```

约束：

- `status` 与 `doctor` 永远只读；
- `setup`、`reconcile`、`memory --apply` 是显式写操作，并在执行前列出精确目标与回滚点；
- 不给 `codesync sync` 增加任何 flag，也不在普通 Git 同步中隐式运行 context；
- 允许用户分别调度 Git 同步与 context 归集，失败域互不影响。

## 4. 独立配置

使用新的 `[context]` 段，不复用或修改 `[sync]`：

```toml
[context]
enabled = true
transport = "dropbox"
transport_root = "D:/Dropbox/CodexSessions"
sessions_dir = "C:/Users/yiwang/.codex/sessions"
machine_id = "desktop-main"
memory_mode = "llm-draft"
memory_root = "D:/Dropbox/CodexMemory"
```

这些路径是机器级配置；三台电脑可以有不同盘符。`machine_id` 必须稳定且唯一，不使用主机名作为唯一身份。

## 5. conversation 数据协议

### 5.1 允许同步

- `CodexSessions/YYYY/MM/DD/rollout-*.jsonl`
- 只接受能逐行解析、包含合法 `session_meta.payload.id` 的文件；
- session UUID 是主键，内容 SHA-256 和字节数用于完整性验证。

### 5.2 绝对禁止同步

- `state_*.sqlite`、`*.db` 及 `-wal`/`-shm`/`-journal`；
- `auth.json`、`config.toml`、`.env*`、凭据和 token；
- `thread-writer-locks`、`.sandbox-secrets`、PID/lock/tmp；
- `history.jsonl` 等多机追加时没有冲突协议的全局文件；
- Dropbox 生成的 conflicted-copy 文件。

ignore 规则是第二道防线；第一道防线是只把 `sessions` 目录接入 Dropbox，而不是整个 `.codex`。

### 5.3 活跃会话与冲突

- 活跃 JSONL 只允许其创建机器写入；同一 conversation 不得在两台机器同时 resume；
- 静默判定必须精确到目标 session：writer lock 已释放、目标 JSONL 的大小和 mtime 在观察窗口内稳定，且没有对应 writer。`Ctrl+C` 只可能中断当前 turn，不能单独作为 session 已结束的证据；
- 其他 session、其他终端或 app-server 仍在运行时，不得因“全系统仍有 `codex.exe`”阻塞已静默的目标 session；
- reconcile 遇到正在增长或持锁的文件只观察，不改名、不覆盖；
- 相同 UUID、相同 hash：视为同一份；
- 相同 UUID、严格前缀关系：仅在双方都不活跃时保留较长版本；
- 相同 UUID、内容分叉：两份都隔离保存并报警，绝不自动拼接原始 JSONL；
- v1 不传播删除。归档和删除协议以后用 tombstone 单独设计，避免 Dropbox 删除扩散。

## 6. 本机 `/resume` 索引重建

适配器优先级：

1. Codex 提供的官方导入/迁移接口；
2. 能被验证为会重建索引的 Codex 原生命令；
3. 最后才是版本锁定的 SQLite adapter。

SQLite adapter 的硬门槛：

- 待导入/修复的目标 session writer lock 已释放，JSONL 通过稳定窗口，且没有对应 writer；不得以全系统 Codex 进程是否清零代替该判定；
- 备份数据库以及同代 WAL/SHM，并记录 hash；
- 校验 Codex 版本、SQLite user/schema version 和 `threads` 表字段集合；
- 从 JSONL 的 `session_meta` 提取 id/cwd/source/provider/sandbox/approval 等，不猜测必填字段；
- 单事务插入缺失记录，不覆盖已有 session；
- `PRAGMA integrity_check`、行数差异和抽样 `codex resume <id>` 验证全部通过；
- 任一步失败立即回滚并保留诊断包。

路径迁移使用可配置映射，例如 `C:/Users/yiwang/SyncRepos -> V:/SyncRepos`。只改本机索引与本机 JSONL 中明确的 CWD 元数据，不改对话正文。

## 7. memory：脚本与 LLM 的职责边界

memory 合并需要 LLM，但不能让 LLM 单独承担数据工程。

### 脚本负责的确定性工作

- 按 session UUID、项目根路径、Git remote 和时间窗口发现材料；
- 解析 user/assistant/tool/result，过滤噪声与秘密；
- 提取已修改文件、提交 SHA、测试结果、明确决策、未完成项及对应 event offset；
- hash 去重、增量游标、token 预算切片；
- 生成不可变 evidence pack，并验证 LLM 输出引用的 session/offset 确实存在。

### LLM 负责的语义工作

- 把多个 conversation 中的同义结论合并；
- 区分“计划”“尝试失败”“已验证完成”；
- 对互相冲突的决定按证据和时间排序，无法裁决时并列保留；
- 把细节压缩成可复用的项目约束、操作手册和未决问题。

### 三层产物

```text
CodexMemory/<project-id>/
  evidence/<session-id>.json          # 脚本生成，不可变
  drafts/<machine-id>-<timestamp>.md  # LLM 候选，不互相覆盖
  MEMORY.md                           # 人工确认后的规范 memory
  manifest.json                       # 输入 hash、模型、prompt 版本、输出 hash
```

默认 `memory` 只生成 draft；只有 `--apply` 才更新规范 memory。规范 memory 更新前保留旧版，且每条结论必须能追溯到 session id。秘密扫描失败时禁止把 evidence 发给远端 LLM。

## 8. 三台 PC 的建议运行方式

1. 每台机器安装同一版 dev-tools，并各自配置本机路径和唯一 `machine_id`；
2. 启动 Codex 前运行 `portablecodex context reconcile`；
3. 工作中由 Dropbox 传输 append-only JSONL，但不在另一台机器同时 resume 同一 session；
4. 结束工作并退出 Codex 后再运行一次 reconcile；
5. 按项目定期运行 `context memory`，审阅 draft 后 `--apply`；
6. Git 代码同步仍独立运行 `codesync sync`，两条流水线不互相调用。

## 9. 实现阶段

- [x] D0：确定三层架构、CLI 边界、数据边界与 LLM 职责。
- [x] D1：只读 scanner/status/doctor；覆盖 Windows/macOS 路径、JSONL 容错、禁止内容、索引差异与 writer-lock 测试。
- [ ] D2：Dropbox setup、备份、junction/symlink 与可逆卸载；实现 session 级 writer-lock + 稳定窗口判定，拒绝目标活跃 writer 但不等待无关 Codex 进程。
- [ ] D3：conversation reconcile、prefix/divergence 检测与 manifest。
- [ ] D4：Codex 本机索引 adapter；先探测官方接口，再实现版本锁定 fallback。
- [ ] D5：evidence pack、秘密过滤与 LLM draft；默认不 apply。
- [ ] D6：三机并发、Dropbox 延迟/冲突、断电恢复和跨版本验收。

## 10. 最低验收标准

- 另一台机器收到的新 session 能出现在 `codex resume --all`，并能按 UUID 打开；
- 任意时刻云端不存在 SQLite/WAL/SHM、认证、配置或锁文件；
- 活跃会话、未知 schema、Dropbox 未完成下载或内容分叉时 fail closed；
- 原始 JSONL 永不由 LLM 修改，冲突永不静默覆盖；
- memory 每条发布结论可追溯，失败/计划不会被写成已完成；
- `codesync sync` 的 parser、配置、行为和测试快照没有任何变化。
