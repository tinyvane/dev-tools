# Changelog

本文件记录 codesync 的用户可见版本变化。日期使用北京时间。

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
