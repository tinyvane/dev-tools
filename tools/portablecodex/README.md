# PortableCodex

`portablecodex` 管理移动硬盘上的 Codex workspace，同时保留每台 PC 正常的 C: 本机 workspace。
它是 `dev-tools` 仓库里的独立安装包，不在运行时 import 或依赖 `codesync`。

```powershell
python -m pip install --user `
  'git+https://github.com/tinyvane/dev-tools.git#subdirectory=tools/portablecodex'
portablecodex onboard
```

如果移动盘上已有本仓库，也可以不依赖 GitHub 下载，直接安装 V: 上的子项目：

```powershell
& 'V:\SyncRepos\dev-tools\tools\portablecodex\Setup-PortableCodex.ps1'
```

该脚本检查 Python、从 V: 本地源码安装、确保 Python user `Scripts` 在当前及未来 PowerShell 的 PATH
中，然后直接进入 onboarding；不需要先安装 `codesync`。如不希望脚本维护 PATH，也可手动执行
`python -m pip install --user --upgrade ...` 和 `python -m portablecodex onboard`。

向导会自动区分两种安全操作：

- `connect`：连接已有且完整的 V: workspace，并为当前 PC 安装 `codexv`。它不会导入或合并这台
  PC 的本机 SQLite。
- `initialize`：从当前 PC 的权威 Codex 状态创建第一份 portable workspace。已有完整 workspace 时
  明确拒绝覆盖，并要求全部 Codex writer 先退出。

交互式 PowerShell 直接运行：

```powershell
portablecodex onboard
```

向导展示本机 Codex 路径、session 数、memory 是否存在、V: phase 和登记的 Volume GUID，只有输入
`y` 才执行。自动化或非交互终端必须同时明确意图与执行：

```powershell
portablecodex onboard --root 'V:\CodexPortable' --mode connect --execute
```

日常入口有意保持不同：

```powershell
codex       # LOCAL：当前 PC 的 C: workspace
codexv      # PORTABLE：V: 共享 sessions 和 memory
```

第二、第三台 PC 请让同一移动盘仍挂载为 `V:`，安装本工具后运行 `portablecodex onboard`；检测到
complete dual workspace 时会推荐 `connect`，不会把该 PC 的本机历史自动灌入 V:。如确需导入零散
本机历史，必须另行审查 UUID、前缀关系和 SQLite 权威源，不能把两个 SQLite 文件直接合并。

认证使用每台 PC 的 Windows keyring；CLI 0.153.4 还会写入依赖本机密钥的 `secrets/*.age`。
从 0.1.1 起，launcher 将 `home/secrets` 映射至本机
`%LOCALAPPDATA%\PortableCodex\credentials\<workspace-id>\secrets`，每台电脑/用户保留自己的登录，
V: 仍共用 sessions、memory 和 SQLite。不要直接运行 V: bin 中的 CLI，请使用 `codexv`。

升级旧工作区：退出所有 V: Codex，运行 `portablecodex alias --execute`，再运行 `codexv`。
已有加密文件整体保留到 V: `backups/legacy-encrypted-secrets`；只有原 MachineGuid 和原用户路径均
匹配时，才将缺失的旧凭据复制回该 PC 本机目录，绝不覆盖已有登录。原 OS keyring 密钥仍需存在；
密钥已丢失/登录已过期时需在对应 PC 重新登录。其他 PC 首次使用各自登录，随后切盘无需互相覆盖。
启动器共享锁防止活跃进程期间切换凭据；未知链接、损坏登记或旧进程阻塞均拒绝操作。

`auth.json`、`secrets` 永远不会随 home 迁移到移动盘。完整事务与恢复
协议见 [CODEX_PORTABLE_DESIGN.md](docs/CODEX_PORTABLE_DESIGN.md)。
