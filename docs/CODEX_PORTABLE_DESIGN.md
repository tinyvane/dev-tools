# Codex Portable on V: 实施设计

> 状态：2026-09-05 dual workspace 已完成并通过 verify；v2.31.1 提供每台 PC 的本地 `codexv` 入口
>
> 目标版本：2.31.1
>
> 场景边界：同一块移动 NVMe 在三台 Windows PC 间使用；未插盘时 C: Codex 仍可独立工作，C: 临时会话不回灌 V:；代码始终通过 Git 收敛。

## 1. 权威数据与目录

```text
V:\SyncRepos\
V:\CodexPortable\
├─ bin\
├─ home\
├─ sqlite\
├─ backups\
├─ manifests\
└─ Start-Codex.ps1
```

默认 `dual` 模式下，`V:\CodexPortable` 是跨三台 PC 使用的主要 memory/对话，
`C:\Users\<user>\.codex` 则是每台 PC 永久保留的本机 fallback。两套 home/SQLite 不做自动合并；
代码一致性由 Git/codesync 保证。Dropbox 只保留为备份或未来 conversation transport，不承载 live SQLite。

旧的“V: 是唯一 live home”模型仅作为显式 `--mode exclusive` 兼容路径存在。

## 2. 官方接口及已知边界

- `CODEX_HOME`：CLI、IDE extension、app-server 和 installer 的状态根目录，目录必须预先存在；
- `CODEX_SQLITE_HOME`：CLI/app-server 的 SQLite 根目录；`config.toml` 的 `sqlite_home` 优先；
- `CODEX_INSTALL_DIR`：standalone installer 可见 `codex` 命令的安装目录；package cache 仍在 `CODEX_HOME/packages/standalone`；
- `cli_auth_credentials_store = "keyring"`：凭据进入每台 Windows PC 的 OS credential store，不迁移 `auth.json`。

OpenAI Docs 没有把 Windows Store/ChatGPT desktop app 本体列为 `CODEX_HOME` 的使用者。因此 CLI、IDE extension 和 app-server 是硬验收；Windows App 必须在迁移后通过进程环境、实际写入路径和 `/resume` 实机验证，不能预先承诺共享成功。

## 3. 命令边界

```text
codesync portable status
codesync portable prepare
codesync portable migrate
codesync portable migrate --mode exclusive
codesync portable verify
codesync portable alias
codesync portable attach
codesync portable detach
codesync portable rollback --root V:\CodexPortable
```

- `status`、`verify` 只读；
- `prepare` 只创建尚不存在的 portable 结构、launcher 和 manifest，不触碰当前 live home；
- `migrate` 默认建立 dual workspace，只能在所有 Codex/ChatGPT/app-server writer 退出后运行；
- `alias` 默认 dry run；`--execute` 只在当前 codesync 的 PATH 目录安装或更新受管 `codexv.cmd`，
  `--remove --execute` 只删除受管 shim；同名冲突、错误 Volume GUID 或非 complete dual 均拒绝；
- dual 不登记用户环境，第二/第三台 PC 直接使用 V: launcher；`attach/detach/rollback` 明确不适用；
- exclusive 下 `attach/detach` 使用 Windows MachineGuid 区分电脑，`rollback` 恢复 C: home；
- 所有 portable 命令与 `sync`、`context` 的参数和默认行为隔离。

## 4. 设备身份与启动保护

盘符不是身份。manifest 固定记录 Windows Volume GUID；每次 prepare、migrate、verify 和 launcher 启动都必须同时验证：

1. 目标绝对路径位于预期盘符；
2. 当前 Volume GUID 与 manifest 一致；
3. `home`、`sqlite`、`bin` 均已存在；
4. `sqlite_home` 与 `CODEX_SQLITE_HOME` 指向同一目录；
5. `codex.exe` 位于 portable `bin` 且版本符合 manifest。

任何缺失、错误磁盘或路径冲突均 fail closed，禁止自动创建一个新的空 live home。launcher 还必须
打印 `PORTABLE` 模式，避免用户把 V: 会话误认成本机 C: 会话。

## 5. 数据迁移协议

### 5.1 conversation

- 不跟随旧 `sessions` junction；将其真实目标作为独立来源；
- rollout 必须位于 `YYYY/MM/DD/rollout-*.jsonl`，首条记录含 canonical session UUID；
- UUID/hash 相同只保留一份；严格前缀仅在全局迁移停机后保留较长版本；同 UUID 内容分叉立即停止；
- staging 与最终目标逐文件复核字节数和 SHA-256。

### 5.2 SQLite 与 memory

- 只迁移当前机器的一套权威 SQLite/WAL/SHM 家族，禁止文件级合并不同机器的数据库；
- staging 中每个主 SQLite 运行 `PRAGMA quick_check`；
- 当前权威 memories 目录与 `memories_*.sqlite` 原样迁移；其他机器旧 memory 只封存为 evidence；
- 不运行 LLM，不做 memory consolidation。

dual 首次快照只以 origin PC 当前 C: 的 SQLite/memory 为权威；之后三台 PC 通过同一块 V: 直接使用
这一套主要状态。未插盘时各机 C: 产生的 memory/会话留在本机，不进入 portable，也不参与 SQLite 合并。

### 5.3 禁止进入 portable/Dropbox transport 的数据

`auth.json`、writer locks、sandbox secrets 和临时文件不迁移；Dropbox 额外禁止任何 SQLite/DB/WAL/SHM/journal、lock、credentials、`.env*` 和 conflicted-copy。

## 6. Dual refresh、提交与恢复

dual 从 `prepared` 或旧 v2.29 的 `data-ready/cli-ready` 开始时，不能信任先前快照仍是最新。每次恢复都：

1. 先持久化 `dual-stage-pending` 与 staging/backup 精确路径；
2. 在全局 Codex 停机时从当前 C: home、sessions source 和单一 SQLite 权威源建立新 staging；Windows
   目录创建与文件复制只在 I/O 边界使用 extended-length path，因此不依赖系统 `LongPathsEnabled`，
   manifest 仍记录可读的规范普通路径；
3. 写入 rollout hash、SQLite inventory/quick_check 和内部链接 manifest；
4. 持久化 `dual-data-move-pending`，再把旧 V: `bin/home/sqlite` 整体 rename 到
   `backups/pre-dual-refresh-*`，将 staging 原子换入；
5. 以 `CODEX_NON_INTERACTIVE=1` 运行官方 installer，完成后再次检查 writer；
6. 写 launcher，并删除 installer 写入用户 PATH 的 V: bin；仅当用户 `CODEX_*` 精确指向本 portable
   路径时才清除，其他配置保持不动；
7. 状态写成 `mode=dual, status=complete`。C: home 始终不改名。

在 staging 或目录换入中断时，登记路径和组合状态必须足以安全续跑；不完整 staging 与旧 V: 数据
只移动进 `backups`，不永久删除。安装失败后下一次执行重新从当前 C: 建快照，避免使用期间新增的
memory/会话遗漏。

exclusive 仍使用 v2.29 原事务：迁移完成后把 C: home 改名为带时间戳 rollback backup，并登记
用户环境。该路径不是默认选择。

## 7. 日常启动边界

- `codex`：本机 C: fallback；不依赖 V:。
- `codexv`：当前 PC 的本地 shim；通过独立 Windows PowerShell 子进程调用下列 launcher，保留 CWD、
  参数和退出码，不持久化 PATH 或 `CODEX_*`。每台新 PC 运行一次 `portable alias --execute`。
- `V:\CodexPortable\Start-Codex.ps1`：校验设备身份并以进程级环境启动 PORTABLE。
- 两种模式可打开同一 Git repo，但各自 conversation/memory 不自动互相出现。
- 同一 repo 的代码修改必须 commit/push；换机器或换盘后通过 Git pull/rebase 收敛，禁止目录复制。
- Windows desktop app 未被官方环境变量适用范围保证，默认视为 LOCAL；portable 仅硬保证 CLI、
  app-server 及从 portable 环境启动且已实测的 IDE 进程。
