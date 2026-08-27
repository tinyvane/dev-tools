from __future__ import annotations

import os
import time
from datetime import datetime

from codesync import config as cfg_mod
from codesync import followups, git_ops, git_transport, output, proc, status as status_mod
from codesync.known_hosts import KnownHostsState


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _report_damaged_repos(damaged: list[git_ops.DamagedRepo]) -> None:
    husks = [item.path for item in damaged if item.kind == "husk"]
    incomplete = [item.path for item in damaged if item.kind == "incomplete-clone"]
    if husks:
        output.warn(f"{len(husks)} 个目录的 .git 残缺（疑似删除未完成留下的残骸），已跳过同步:")
        for repo in husks:
            output.warn(f"  {repo.name}  →  移入本地垃圾箱: codesync delete {repo.name}")
            followups.add(
                f"{repo.name} 是半删除残骸",
                f"{repo} 的 .git 不完整；工作区可能仍有用户文件，不会自动删除。",
                [f"codesync delete {repo.name}"],
                "husk",
                identity=str(repo),
            )
    if incomplete:
        output.warn(f"{len(incomplete)} 个目录是未完成的 clone 残骸，已跳过同步:")
        for repo in incomplete:
            output.warn(f"  {repo.name}  →  移入本地 .codesync-trash 后，下轮 sync 会重新 clone")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            trash_target = (
                repo.parent / ".codesync-trash"
                / f"incomplete-clone--{stamp}--{repo.name}"
            )
            move = (
                f'New-Item -ItemType Directory -Force "{trash_target.parent}"; '
                f'Move-Item -LiteralPath "{repo}" -Destination "{trash_target}"'
                if os.name == "nt"
                else f'mkdir -p "{trash_target.parent}" && mv "{repo}" "{trash_target}"'
            )
            followups.add(
                f"{repo.name} 是未完成的 clone 残骸",
                f"{repo} 没有可用分支或工作区文件；先完整移入本地垃圾箱，再重新 clone。",
                [move, "codesync sync"],
                "stale-clone",
                identity=str(repo),
            )


def _safety_countdown(
    net_workers: int,
    local_workers: int,
    mux_enabled: bool,
    mux_reason: str = "",
    known_hosts: KnownHostsState | None = None,
    seconds: int = 10,
) -> bool:
    """Explain network safeguards and allow Ctrl+C before sync writes/network."""
    output.section("同步安全提示")
    output.info("  GitHub SSH 将走官方端点 ssh.github.com:443，不连接 github.com:22")
    output.info("  push 只处理真正 ahead / 有提交的仓库，已同步仓库不会建立 push 连接")
    output.info(
        f"  本次 Git 并发：网络操作 workers={net_workers}，"
        f"本地扫描 workers={local_workers}"
    )
    if mux_enabled:
        output.info("  SSH 连接复用：已启用（一条连接承载全部 GitHub 操作）")
    else:
        output.info(f"  SSH 连接复用：未启用（{mux_reason or '不可用'}）")
    if known_hosts is not None and known_hosts.enabled:
        output.info(f"  GitHub 443 known_hosts：已启用（来源 {known_hosts.source}）")
    else:
        reason = known_hosts.reason if known_hosts is not None else "不可用"
        output.info(f"  GitHub 443 known_hosts：未启用（{reason or '不可用'}）")
    if seconds > 0:
        output.warn(f"{seconds} 秒后开始同步；如不希望继续，请按 Ctrl+C 中断。")
    else:
        output.warn("同步倒计时已关闭，即将开始同步。")
    try:
        for remaining in range(seconds, 0, -1):
            output.detail(f"  {remaining}...")
            time.sleep(1)
    except KeyboardInterrupt:
        output.info("已取消同步，尚未执行 clone / publish / commit / pull / push。")
        return False
    return True


def _has_network_work(cfg: cfg_mod.Config) -> bool:
    """Whether this run will issue any network Git command (gates the prewarm).

    Deliberately cheap: auto_clone always does a remote inventory, and every
    discovered repo gets pulled. any_repo short-circuits at the first hit
    instead of building and sorting the full list, which step 3 rebuilds
    anyway — deliberately, since step 3 must run AFTER auto-clone and publish
    so freshly created repos are included. The orphan scan is NOT consulted —
    it spawns a `git config --get` per directory, and paying that just to
    decide whether to open one SSH connection would reintroduce exactly the
    duplicated scanning this exists to remove.
    """
    if cfg.auto_clone is not None:
        return True
    return git_ops.any_repo(cfg.code_roots_expanded)


def run_sync(status_only: bool = False, net_workers: int | None = None,
             local_workers: int | None = None,
             problems_only: bool = False, no_publish: bool = False,
             no_push: bool = False, no_commit: bool = False,
             no_pull: bool = False, no_clone: bool = False,
             ) -> int:
    """Run sync and always close any SSH master created during the run."""
    followups.clear()
    mux_state: list[git_transport.SshMultiplexState] = []
    try:
        return _run_sync(
            status_only=status_only,
            net_workers=net_workers,
            local_workers=local_workers,
            problems_only=problems_only,
            no_publish=no_publish,
            no_push=no_push,
            no_commit=no_commit,
            no_pull=no_pull,
            no_clone=no_clone,
            _mux_state=mux_state,
        )
    finally:
        try:
            if mux_state:
                git_transport.close_github_master(mux_state[0])
        finally:
            # Always surface work already discovered, including when _run_sync
            # exits through a safety guard or an unexpected exception.
            followups.print_followups()


def run_pull(net_workers: int | None = None, local_workers: int | None = None,
             problems_only: bool = False, no_commit: bool = False) -> int:
    """`codesync pull` — bring every local repo up to date, and nothing else.

    Deliberately a preset over run_sync rather than a second pipeline: repo
    discovery, nested/embedded handling, the damaged-repo report, the safety
    countdown and the SSH master lifecycle are all subtle and must not be
    reimplemented twice.

    Auto-commit still runs. sync's whole ordering rationale is commit -> rebase
    pull -> push precisely so user work enters git BEFORE history changes; a
    pull that rebases over an uncommitted tree is what --autostash is papering
    over. --no-commit opts out. Publishing orphans and cloning new GitHub repos
    are sync's job, not this command's — "拉取最新代码" is about repos you
    already have.
    """
    return run_sync(
        net_workers=net_workers, local_workers=local_workers,
        problems_only=problems_only, no_commit=no_commit,
        no_push=True, no_publish=True, no_clone=True,
    )


def run_push(net_workers: int | None = None, local_workers: int | None = None,
             problems_only: bool = False, no_commit: bool = False) -> int:
    """`codesync push` — commit and upload local work, without pulling.

    Auto-commit stays on: without it a dirty repo has nothing to push and the
    command would silently do nothing. No pull means a repo that has diverged
    from its remote will be REJECTED by git rather than reconciled — that is
    intentional. `codesync sync` is the command that reconciles; this one never
    force-pushes and never introduces a second merge strategy.
    """
    return run_sync(
        net_workers=net_workers, local_workers=local_workers,
        problems_only=problems_only, no_commit=no_commit,
        no_pull=True, no_publish=True, no_clone=True,
    )


def _run_sync(status_only: bool = False, net_workers: int | None = None,
              local_workers: int | None = None,
              problems_only: bool = False, no_publish: bool = False,
              no_push: bool = False, no_commit: bool = False,
              no_pull: bool = False, no_clone: bool = False,
              _mux_state: list[git_transport.SshMultiplexState] | None = None,
              ) -> int:
    """The one-command sync (v2.3.0+).

    Default flow does everything: clone missing GitHub repos, publish local
    orphans, auto-commit, rebase-pull, push local commits. Opt out of pieces with
    no_publish / no_push. status_only short-circuits to a read-only report.

    push is the DEFAULT now (was opt-in via --push pre-v2.3.0). This matches the
    "I want every local change uploaded without thinking about it" workflow.
    """
    do_push = not no_push

    # 1. load config
    cfg = cfg_mod.load()

    # 1a. Show current + latest version up front, on every run incl. --status
    #     (v2.10.0). Cheap/fail-open; the gate below reuses the cached lookup.
    from codesync.updater import print_version_status
    print_version_status(cfg.update)

    # 1b. Version gate (v2.7.0): refuse to run destructive sync on an outdated
    #     codesync. Read-only --status is exempt. Fails open on network errors;
    #     Strict and fresh for every write-capable sync; no config/CLI bypass.
    if not status_only:
        from codesync.updater import enforce_up_to_date
        if not enforce_up_to_date(cfg.update):
            return 1

    sync_cfg = cfg.sync or cfg_mod.SyncConfig()
    pull_cfg = cfg.pull or cfg_mod.PullConfig()
    stall_enabled = sync_cfg.stall_bytes_per_sec > 0 and sync_cfg.stall_seconds > 0
    # Called unconditionally: cli.main() already installed the defaults, so
    # skipping here would leave a `stall_bytes_per_sec = 0` config unable to
    # switch the policy off.
    git_transport.configure_http_stall_detection(
        bytes_per_sec=sync_cfg.stall_bytes_per_sec,
        seconds=sync_cfg.stall_seconds,
    )
    transport = git_transport.configure_ssh_command(
        multiplex_enabled=sync_cfg.ssh_multiplex,
        known_hosts_enabled=sync_cfg.github_known_hosts,
        stall_seconds=sync_cfg.stall_seconds if stall_enabled else 0,
    )
    mux = transport.mux
    if _mux_state is not None:
        _mux_state.append(mux)

    resolved_net_workers = (
        net_workers
        or sync_cfg.net_workers
        or git_ops.default_net_workers(multiplexed=mux.enabled)
    )
    resolved_local_workers = (
        local_workers
        or sync_cfg.local_workers
        or git_ops.default_local_workers()
    )

    # Put the confirmation before auto-clone/publish as well as pull/push: once
    # the countdown begins, Ctrl+C still guarantees no sync mutation occurred.
    if not status_only and not _safety_countdown(
        resolved_net_workers,
        resolved_local_workers,
        mux.enabled,
        mux.reason,
        transport.known_hosts,
        seconds=sync_cfg.countdown_seconds,
    ):
        return 130

    # mux.enabled first: prewarm_github_master returns immediately when
    # multiplexing is off (always, on Windows — no ControlMaster), so scanning
    # the roots to decide whether to prewarm was pure waste there.
    if not status_only and mux.enabled and _has_network_work(cfg):
        git_transport.prewarm_github_master(mux, timeout=proc.T_NET)

    # 2. GitHub auto-clone (only if configured; gh auth happens inside).
    #    push mode here controls whether locally-deleted repos get archived on GitHub.
    #    SKIPPED in --status mode: status is strictly read-only (no gh calls, no
    #    clone, no archive). auto_clone clones/archives, which is a write.
    #    no_clone additionally skips it for the standalone pull/push commands:
    #    github_auto is what clones new repos AND what archives locally-deleted
    #    ones, and neither belongs in "just move commits for what I already
    #    have". Keeping it out also means those commands can never trip the
    #    archive path.
    migrations: list[tuple[str, str]] = []
    if cfg.auto_clone and not status_only and not no_clone:
        from codesync import github_auto, rename as rename_mod
        auto_migrate = (cfg.rename is None) or cfg.rename.auto_migrate
        claude_projects = rename_mod._resolve_claude_projects(cfg.rename)
        migrations = github_auto.run(
            cfg.auto_clone, cfg.code_roots_expanded,
            push=do_push, auto_migrate=auto_migrate,
            claude_projects=claude_projects,
            local_workers=resolved_local_workers,
        )
    elif cfg.auto_clone is None and not status_only and not no_clone:
        # Silent feature-absence reads as success: a config without [auto_clone]
        # (e.g. generated by V1 migrate-config) syncs fine but never clones repos
        # created on other machines — and nothing ever said so. One dim line, not
        # a warn, so deliberate gh-free workflows aren't nagged. (v2.14.0)
        output.detail("（未配置 [auto_clone]，其他机器新建的 repo 不会自动克隆到本机 —— `codesync init` 可补）")

    # 2b. Publish local orphans (dirs with no .git, or .git without origin).
    #     Skipped in status-only mode (read-only) and when --no-publish given.
    if not status_only and not no_publish:
        from codesync import publish
        publish.publish_orphans(cfg)

    # 3. discover repos (AFTER publish, so freshly-published repos are included)
    toplevel = git_ops.find_repos(cfg.code_roots_expanded)
    output.section("扫描代码目录")
    for root in cfg.code_roots_expanded:
        if root.exists():
            output.detail(f"扫描 {root}")
        else:
            output.detail(f"跳过不存在的目录 {root}")
    output.detail(f"发现 {len(toplevel)} 个 repo")

    # One authoritative top-level origin scan feeds the two read-only consumers
    # below. github_auto and publish deliberately keep their independent scans.
    origins = git_ops.scan_origins(
        toplevel, max_workers=resolved_local_workers,
    )

    # 3a. damaged .git directories: half-deleted husks and interrupted clones.
    #     Surface each once with the recovery hint for its specific shape.
    damaged = git_ops.find_corrupt_repos(cfg.code_roots_expanded)
    _report_damaged_repos(damaged)

    # 3b. discover nested repos (v2.8.0). EMBEDDED repos sync as independent
    #     repos (third-party = pull-only); PROPER submodules get a submodule
    #     update after pull. The outer repo's auto-commit excludes the nested
    #     path so a moving gitlink isn't baked into the superproject.
    recurse = (cfg.submodules is None) or cfg.submodules.recurse
    sub_skip = tuple(cfg.submodules.skip) if cfg.submodules else ()
    embedded: list[git_ops.NestedRepo] = []
    submodule_parents: list = []
    if recurse:
        owners = git_ops.my_owners(cfg, toplevel, origins=origins)
        nested = git_ops.find_nested_repos(toplevel, owners, skip=sub_skip)
        embedded = [n for n in nested if not n.is_submodule]
        submodule_parents = [r for r in toplevel if (r / ".gitmodules").exists()]
        if embedded or submodule_parents:
            n_push = sum(1 for e in embedded if e.pushable)
            n_pull = len(embedded) - n_push
            output.detail(
                f"嵌套 repo：{len(embedded)} 个嵌入式（{n_push} 可同步 / "
                f"{n_pull} 第三方 pull-only），{len(submodule_parents)} 个含 submodule"
            )

    # Repos for each phase:
    #   pull  : every repo (top-level + all embedded; third-party pulled too)
    #   commit/push : top-level + embedded that are mine (pull-only ones skipped)
    embedded_all = [e.path for e in embedded]
    embedded_pushable = [e.path for e in embedded if e.pushable]
    pull_repos = toplevel + embedded_all
    push_repos = toplevel + embedded_pushable

    # Interrupted transfers leave tmp_pack_* files behind. Check every scanned
    # top-level/embedded repo plus damaged top-level leftovers. Only files older
    # than 24h are eligible so concurrent sync or manual fetch stays untouched.
    if not status_only and sync_cfg.cleanup_stale_packs:
        cleanup_repos = list(dict.fromkeys(
            toplevel + [item.path for item in damaged] + embedded_all
        ))
        cleanup = git_ops.cleanup_stale_packs(cleanup_repos)
        if cleanup.before_count:
            output.detail(
                f"过期 tmp_pack 清理前 {cleanup.before_count} 个/"
                f"{_format_bytes(cleanup.before_bytes)}；清理后 "
                f"{cleanup.after_count} 个/{_format_bytes(cleanup.after_bytes)}；"
                f"释放 {_format_bytes(cleanup.freed_bytes)}"
            )
    # outer-repo → nested rel paths to keep out of the outer's auto-commit
    exclude_map: dict = {}
    for e in embedded:
        exclude_map.setdefault(e.outer, set()).add(e.rel)

    # 3c. duplicate-origin advisory (v2.14.0): the same remote checked out into
    #     2+ top-level folders (old date-prefixed clone + canonical clone) wastes
    #     disk and risks editing the wrong copy. Top-level only — embedded repos
    #     sharing their outer's origin is a separate (known) shape. Read-only:
    #     report, never auto-fix.
    dup_origins = git_ops.find_duplicate_origins(
        toplevel, max_workers=resolved_local_workers, origins=origins,
    )
    if dup_origins:
        output.warn(f"{len(dup_origins)} 个 origin 被多个本地目录共用（同一 repo 克隆了多份，建议保留一份）:")
        for origin_key, repo_paths in sorted(dup_origins.items()):
            names = ", ".join(p.name for p in repo_paths)
            output.detail(f"  {origin_key}  ←  {names}")

    # 4. status-only mode
    if status_only:
        output.section("repo 状态")
        status_mod.print_status(
            pull_repos,
            problems_only=problems_only,
            max_workers=resolved_local_workers,
        )
        return 0

    # 5. auto-commit dirty repos (default on; --no-commit / [commit].enabled=false to skip).
    #    Record user work before any history operation; the following rebase
    #    replays these local commits on the remote tip before ordinary push.
    commit_enabled = (cfg.commit is None) or cfg.commit.enabled
    if not no_commit and commit_enabled:
        skip_names = set(cfg.commit.skip) if cfg.commit else {"dev-tools"}
        output.section("自动提交本地改动")
        # Commit top-level + my embedded repos (not third-party pull-only ones);
        # exclude_map keeps nested gitlinks out of the outer repos' commits.
        committed = git_ops.auto_commit_dirty(
            push_repos,
            skip_names,
            max_workers=resolved_local_workers,
            exclude_map=exclude_map,
        )
        if committed:
            output.detail(f"已 commit {len(committed)} 个 repo（将随 push 上传）")

    # 5b. parallel pull (top-level + all embedded, third-party included)
    pull_summary = None
    if not no_pull:
        output.section(f"并发 pull (workers={resolved_net_workers})")
        pull_summary = git_ops.parallel_op(
            pull_repos,
            "pull",
            max_workers=resolved_net_workers,
            rebase=pull_cfg.rebase,
        )
        git_ops.print_summary(pull_summary)
    else:
        output.detail("(跳过 pull)")

    # 5c. proper submodules: check out recorded commits after the parent's pull.
    #     Skipped when we did not pull — there is no new parent commit whose
    #     recorded submodule SHAs would need checking out, and doing it anyway
    #     would move submodule worktrees during a push-only run.
    if submodule_parents and not no_pull:
        git_ops.update_submodules(
            submodule_parents, max_workers=resolved_net_workers,
        )

    # 6. push (default; skip with --no-push). Top-level + my embedded repos.
    push_summary = None
    if do_push:
        output.section(f"并发 push (workers={resolved_net_workers})")
        push_summary = git_ops.parallel_op(
            push_repos, "push", max_workers=resolved_net_workers,
        )
        git_ops.print_summary(push_summary)
    else:
        output.detail("(--no-push：跳过推送)")

    # 6c. Highlight cross-machine renames picked up this run, so the changed repo
    #     name doesn't slip by unnoticed in the scroll-back.
    if migrations:
        output.section("⚠ 检测到其他机器改名（本机已自动迁移）")
        for old, new in migrations:
            output.info(output.hilite(f"  {old}  →  {new}", "yellow"))

    # 7. final status summary
    output.section("状态总览")
    status_mod.print_status(
        pull_repos,
        problems_only=problems_only,
        max_workers=resolved_local_workers,
    )

    # Bubble up failure if any repo failed.
    if pull_summary is not None and pull_summary.failed:
        return 2
    if push_summary is not None and push_summary.failed:
        return 2
    return 0
