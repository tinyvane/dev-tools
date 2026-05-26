from __future__ import annotations

import time

from codesync import config as cfg_mod
from codesync import output, shell


def _register_code_roots(roots) -> int:
    """Run `gita add -r` for each existing code root. Returns total registered count."""
    output.section("扫描代码目录")
    for root in roots:
        if root.exists():
            output.detail(f"扫描 {root}")
            shell.run(["gita", "add", "-r", str(root)], capture=True)
        else:
            output.detail(f"跳过不存在的目录 {root}")
    r = shell.run(["gita", "ls"], capture=True)
    count = len([ln for ln in (r.stdout or "").splitlines() if ln.strip()])
    output.detail(f"当前注册 {count} 个 repo")
    return count


def _gita_pull():
    output.section("并发 pull")
    t0 = time.monotonic()
    shell.run(["gita", "pull"])
    output.detail(f"耗时 {int(time.monotonic() - t0)} 秒")


def _gita_push():
    output.section("并发 push")
    t0 = time.monotonic()
    shell.run(["gita", "push"])
    output.detail(f"耗时 {int(time.monotonic() - t0)} 秒")


def _gita_status():
    shell.run(["gita", "ll"])


def run_sync(push: bool = False, status_only: bool = False) -> int:
    # 1. ensure gita
    if not shell.ensure_gita():
        output.err("gita 安装失败，请手动 `pip install --user gita` 后重试。")
        return 1

    # 2. load config (auto-generates template + exits on first run)
    cfg = cfg_mod.load()

    # 3. GitHub auto-clone (only if configured; gh auth happens inside)
    if cfg.auto_clone:
        from codesync import github_auto
        github_auto.run(cfg.auto_clone, cfg.code_roots_expanded, push=push)

    # 4. register repos to gita
    _register_code_roots(cfg.code_roots_expanded)

    # 5. status-only mode
    if status_only:
        output.section("repo 状态")
        _gita_status()
        if cfg.db_sync:
            from codesync import db_sync
            db_sync.print_status(cfg.db_sync)
        return 0

    # 6. pull
    _gita_pull()

    # 6b. DB restore
    if cfg.db_sync:
        from codesync import db_sync
        db_sync.restore_all(cfg.db_sync, push_mode=push)

    # 7. push (optional)
    if push:
        _gita_push()
    else:
        output.detail("(如需同时推送，请加 --push)")

    # 7b. DB dump on push
    if push and cfg.db_sync:
        from codesync import db_sync
        db_sync.dump_all(cfg.db_sync)

    # 8. summary
    output.section("状态总览")
    _gita_status()
    if cfg.db_sync:
        from codesync import db_sync
        db_sync.print_status(cfg.db_sync)
    return 0
