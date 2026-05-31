from __future__ import annotations

from codesync import config as cfg_mod
from codesync import git_ops, output, status as status_mod


def run_sync(status_only: bool = False, workers: int | None = None,
             problems_only: bool = False, no_publish: bool = False,
             no_push: bool = False, no_commit: bool = False) -> int:
    """The one-command sync (v2.3.0+).

    Default flow does everything: clone missing GitHub repos, publish local
    orphans, pull, restore DB, push local commits, dump DB. Opt out of pieces
    with no_publish / no_push. status_only short-circuits to a read-only report.

    push is the DEFAULT now (was opt-in via --push pre-v2.3.0). This matches the
    "I want every local change uploaded without thinking about it" workflow.
    """
    do_push = not no_push

    # 1. load config
    cfg = cfg_mod.load()

    # 2. GitHub auto-clone (only if configured; gh auth happens inside).
    #    push mode here controls whether locally-deleted repos get archived on GitHub.
    #    SKIPPED in --status mode: status is strictly read-only (no gh calls, no
    #    clone, no archive). auto_clone clones/archives, which is a write.
    migrations: list[tuple[str, str]] = []
    if cfg.auto_clone and not status_only:
        from codesync import github_auto, rename as rename_mod
        auto_migrate = (cfg.rename is None) or cfg.rename.auto_migrate
        claude_projects = rename_mod._resolve_claude_projects(cfg.rename)
        migrations = github_auto.run(
            cfg.auto_clone, cfg.code_roots_expanded,
            push=do_push, auto_migrate=auto_migrate,
            claude_projects=claude_projects,
        )

    # 2b. Publish local orphans (dirs with no .git, or .git without origin).
    #     Skipped in status-only mode (read-only) and when --no-publish given.
    if not status_only and not no_publish:
        from codesync import publish
        publish.publish_orphans(cfg)

    # 3. discover repos (AFTER publish, so freshly-published repos are included)
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
        db_sync.restore_all(cfg.db_sync, push_mode=do_push)

    # 5c. auto-commit dirty repos (default on; --no-commit / [commit].enabled=false to skip).
    #     Runs AFTER pull (commit lands on top of remote) and BEFORE push (gets pushed).
    commit_enabled = (cfg.commit is None) or cfg.commit.enabled
    if not no_commit and commit_enabled:
        skip_names = set(cfg.commit.skip) if cfg.commit else {"dev-tools"}
        output.section("自动提交本地改动")
        committed = git_ops.auto_commit_dirty(repos, skip_names, max_workers=workers)
        if committed:
            output.detail(f"已 commit {len(committed)} 个 repo（将随 push 上传）")

    # 6. push (default; skip with --no-push)
    push_summary = None
    if do_push:
        output.section(f"并发 push (workers={workers})")
        push_summary = git_ops.parallel_op(repos, "push", max_workers=workers)
        git_ops.print_summary(push_summary)
    else:
        output.detail("(--no-push：跳过推送)")

    # 6b. DB dump on push
    if do_push and cfg.db_sync:
        from codesync import db_sync
        db_sync.dump_all(cfg.db_sync)

    # 6c. Highlight cross-machine renames picked up this run, so the changed repo
    #     name doesn't slip by unnoticed in the scroll-back.
    if migrations:
        output.section("⚠ 检测到其他机器改名（本机已自动迁移）")
        for old, new in migrations:
            output.info(output.hilite(f"  {old}  →  {new}", "yellow"))

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
