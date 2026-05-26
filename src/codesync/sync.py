from __future__ import annotations

from codesync import config as cfg_mod
from codesync import git_ops, output, status as status_mod


def run_sync(push: bool = False, status_only: bool = False,
             workers: int | None = None, problems_only: bool = False) -> int:
    # 1. load config
    cfg = cfg_mod.load()

    # 2. GitHub auto-clone (only if configured; gh auth happens inside)
    if cfg.auto_clone:
        from codesync import github_auto
        github_auto.run(cfg.auto_clone, cfg.code_roots_expanded, push=push)

    # 3. discover repos
    repos = git_ops.find_repos(cfg.code_roots_expanded)
    output.section("扫描代码目录")
    for root in cfg.code_roots_expanded:
        if root.exists():
            output.detail(f"扫描 {root}")
        else:
            output.detail(f"跳过不存在的目录 {root}")
    output.detail(f"发现 {len(repos)} 个 repo")

    workers = workers or git_ops.default_workers()

    # 4. status-only mode
    if status_only:
        output.section("repo 状态")
        status_mod.print_status(repos, problems_only=problems_only, max_workers=workers)
        if cfg.db_sync:
            from codesync import db_sync
            db_sync.print_status(cfg.db_sync)
        return 0

    # 5. parallel pull
    output.section(f"并发 pull (workers={workers})")
    pull_summary = git_ops.parallel_op(repos, "pull", max_workers=workers)
    git_ops.print_summary(pull_summary)

    # 5b. DB restore
    if cfg.db_sync:
        from codesync import db_sync
        db_sync.restore_all(cfg.db_sync, push_mode=push)

    # 6. push (optional)
    push_summary = None
    if push:
        output.section(f"并发 push (workers={workers})")
        push_summary = git_ops.parallel_op(repos, "push", max_workers=workers)
        git_ops.print_summary(push_summary)
    else:
        output.detail("(如需同时推送，请加 --push)")

    # 6b. DB dump on push
    if push and cfg.db_sync:
        from codesync import db_sync
        db_sync.dump_all(cfg.db_sync)

    # 7. final status summary
    output.section("状态总览")
    status_mod.print_status(repos, problems_only=problems_only, max_workers=workers)
    if cfg.db_sync:
        from codesync import db_sync
        db_sync.print_status(cfg.db_sync)

    # Bubble up failure if any repo failed.
    if pull_summary.failed:
        return 2
    if push_summary is not None and push_summary.failed:
        return 2
    return 0
