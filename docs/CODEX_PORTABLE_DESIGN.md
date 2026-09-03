# Codex Portable on V: 实施设计

> 状态：2026-09-03 工具和本机 prepare 已完成；实际切换等待全局 Codex 停机
>
> 目标版本：2.29.0
>
> 场景边界：同一块移动 NVMe 在三台 Windows PC 间使用；PC↔Mac 仍使用 Git 和冻结中的 `codesync context` 协议。

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

迁移完成后 `V:\CodexPortable` 是唯一 live Codex 数据。C: 的旧 `.codex` 只作为带时间戳的回滚备份；Dropbox 只保留为备份或未来 conversation transport，不承载 live `CODEX_HOME`。

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
codesync portable verify
codesync portable attach
codesync portable detach
codesync portable rollback --root V:\CodexPortable
```

- `status`、`verify` 只读；
- `prepare` 只创建尚不存在的 portable 结构、launcher 和 manifest，不触碰当前 live home；
- `migrate` 是显式整体迁移，只能在所有 Codex/ChatGPT/app-server writer 退出后运行；
- `attach/detach` 使用 Windows MachineGuid 区分电脑，分别保存和恢复每台机器的用户环境；
- `rollback` 恢复迁移前环境变量和 C: home，V: 数据保留为证据，不自动删除；
- 所有 portable 命令与 `sync`、`context` 的参数和默认行为隔离。

## 4. 设备身份与启动保护

盘符不是身份。manifest 固定记录 Windows Volume GUID；每次 prepare、migrate、verify 和 launcher 启动都必须同时验证：

1. 目标绝对路径位于预期盘符；
2. 当前 Volume GUID 与 manifest 一致；
3. `home`、`sqlite`、`bin` 均已存在；
4. `sqlite_home` 与 `CODEX_SQLITE_HOME` 指向同一目录；
5. `codex.exe` 位于 portable `bin` 且版本符合 manifest。

任何缺失、错误磁盘或路径冲突均 fail closed，禁止自动创建一个新的空 live home。

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

### 5.3 禁止进入 portable/Dropbox transport 的数据

`auth.json`、writer locks、sandbox secrets 和临时文件不迁移；Dropbox 额外禁止任何 SQLite/DB/WAL/SHM/journal、lock、credentials、`.env*` 和 conflicted-copy。

## 6. 提交与回滚

迁移先建立 V: staging 并完整验证，再安装 standalone CLI，最后才把 `C:\Users\<user>\.codex` 原子改名为 `.codex.pre-portable-<timestamp>` 并登记用户环境变量。manifest 记录源/目标、Volume GUID、rollout hash、SQLite 文件、CLI 版本和原环境变量。

第二、第三台电脑只执行 `attach`，各自登录 keyring；`detach` 只恢复本机环境。整体 rollback 要求全局 Codex 停机，只允许源机器执行，并要求其他已登记机器先 detach；它恢复旧环境变量并把 C: 备份改回 `.codex`。portable 数据和 manifest 保留，不立即删除。
