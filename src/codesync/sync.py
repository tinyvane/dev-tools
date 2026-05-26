from __future__ import annotations

from codesync import config as cfg_mod
from codesync import git_ops, output, shell


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
    # gita ls prints repo names space-separated on a single line.
    count = len((r.stdout or "").split())
    output.detail(f"当前注册 {count} 个 repo")
    return count


def _gita_status():
    shell.run(["gita", "ll"])


def run_sync(push: bool = False, status_only: bool = False,
             workers: int | None = None) -> int:
    # 1. ensure gita is installed (used for `add -r` registration and `ll` display)
    if not shell.ensure_gita():
        output.err("gita 安装失败，请手动 `pip install --user gita` 后重试。")
        return 1

    # 2. load config
    cfg = cfg_mod.load()

    # 3. GitHub auto-clone (only if configured; gh auth happens inside)
    if cfg.auto_clone:
        from codesync import github_auto
        github_auto.run(cfg.auto_clone, cfg.code_roots_expanded, push=push)

    # 4. register repos to gita (so `gita ll` knows about them too)
    _register_code_roots(cfg.code_roots_expanded)

    # 5. status-only mode
    if status_only:
        output.section("repo 状态")
        _gita_status()
        if cfg.db_sync:
            from codesync import db_sync
            db_sync.print_status(cfg.db_sync)
        return 0

    # 6. parallel pull (our own impl, with progress)
    workers = workers or git_ops.default_workers()
    repos = git_ops.find_repos(cfg.code_roots_expanded)
    output.section(f"并发 pull (workers={workers})")
    pull_summary = git_ops.parallel_op(repos, "pull", max_workers=workers)
    git_ops.print_summary(pull_summary)

    # 6b. DB restore
    if cfg.db_sync:
        from codesync import db_sync
        db_sync.restore_all(cfg.db_sync, push_mode=push)

    # 7. push (optional)
    if push:
        output.section(f"并发 push (workers={workers})")
        push_summary = git_ops.parallel_op(repos, "push", max_workers=workers)
        git_ops.print_summary(push_summary)
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

    # Bubble up failure if any repo failed (so CI / shell pipelines can detect it).
    if pull_summary.failed:
        return 2
    if push and 'push_summary' in locals() and push_summary.failed:
        return 2
    return 0
