from __future__ import annotations

import argparse
import sys
import tomllib

from codesync import output
from codesync.git_transport import (
    configure_github_ssh_over_443,
    configure_http_stall_detection,
    configure_ssh_command,
)


def _positive_worker_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("worker 数必须是整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("worker 数必须 >= 1")
    return parsed


# Subcommands that can issue a git/gh operation against GitHub, and therefore
# need codesync's GIT_SSH_COMMAND (known_hosts for ssh.github.com:443 plus the
# ServerAlive keepalive) installed. Everything else — --version, --update,
# config-path, migrate-config, plain --help, `trash list` — is local-only and
# must not pay for SSH setup, which can block on an HTTPS host-key probe.
_SSH_COMMANDS = frozenset({
    "sync", "pull", "push", "init", "fork-setup", "rename", "delete",
})

_CODE_ROOT_COMMANDS = frozenset({
    "sync", "pull", "push", "fork-setup", "rename", "delete", "trash",
})


def _needs_ssh(args: argparse.Namespace) -> bool:
    if getattr(args, "version", False) or getattr(args, "update", False):
        return False
    command = getattr(args, "command", None)
    if command == "trash":
        # list reads local manifests only; restore/purge talk to GitHub.
        return getattr(args, "trash_command", None) in {"restore", "purge"}
    return command in _SSH_COMMANDS


def _configure_ssh_if_needed(args: argparse.Namespace) -> None:
    """Install codesync's GIT_SSH_COMMAND, but only for network subcommands.

    This used to run before argparse for every invocation. ensure_github_443_
    known_hosts() can fall through to an HTTPS request to api.github.com, so on
    a machine with no cached host keys and no github.com entry in
    ~/.ssh/known_hosts, `codesync --version`, `config-path` and even `--help`
    each blocked on that request — and since nothing was written on the failure
    path, a blocked network paid it again on every single run. That is worst
    precisely on the GFW-hampered networks this 443 routing exists to serve.

    Multiplexing stays off here: only run_sync re-configures with the user's
    [sync] settings and owns the master connection's lifecycle.
    """
    if not _needs_ssh(args):
        return
    from codesync.config import peek_github_known_hosts_enabled
    configure_ssh_command(
        multiplex_enabled=False,
        known_hosts_enabled=peek_github_known_hosts_enabled(),
    )


def _uses_code_roots(args: argparse.Namespace) -> bool:
    return getattr(args, "command", None) in _CODE_ROOT_COMMANDS


def _can_prompt() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


def _ensure_runtime_config(args: argparse.Namespace) -> bool:
    """Fail before repo/network work when configured roots cannot be scanned."""
    if not _uses_code_roots(args):
        return True

    from codesync import config, paths

    cfg_file = paths.config_file()
    needs_setup = (not cfg_file.exists()) or config.is_template_unedited()
    if needs_setup:
        from codesync.wizard import run_first_run_wizard
        run_first_run_wizard()
        if not cfg_file.exists():
            config.write_template_if_missing()
        if config.is_template_unedited():
            output.warn(f"配置未生成 / 仍是未编辑模板: {cfg_file}")
            output.warn("可以：")
            output.warn("  1. 重跑 `codesync init`（推荐 —— 自动检测 gh 并填配置）")
            output.warn("  2. 或手动编辑该文件后重跑当前命令")
            return False

    try:
        cfg = config.load()
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        output.err(f"配置文件无法读取或解析: {cfg_file}")
        output.detail(f"  {exc}")
        output.detail("请运行 `codesync init` 重新生成，或手动修复该文件。")
        return False

    problems = config.code_root_problems(cfg)
    if not problems:
        return True

    output.section("启动配置检查")
    output.warn("code_roots 配置不可用，未执行任何仓库或网络操作：")
    for problem in problems:
        if problem.expanded is None:
            output.detail(f"  - {problem.reason}")
        else:
            output.detail(f"  - {problem.expanded}（{problem.reason}）")
    output.detail(f"配置文件: {cfg_file}")

    if _can_prompt():
        from codesync.wizard import repair_code_roots
        if repair_code_roots(cfg, problems):
            try:
                repaired = config.load()
            except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
                repaired = None
            if repaired is not None and not config.code_root_problems(repaired):
                output.good("配置检查通过，继续执行当前命令。")
                return True
            output.warn("修复后的配置仍不可用，当前命令已停止。")
            return False

    output.detail("请在交互式终端重跑当前命令完成修复，或手动编辑上述文件。")
    return False


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codesync",
        description="Personal multi-machine Git repository synchronization tool.",
    )
    p.add_argument(
        "--version", action="store_true",
        help="Show the current version and whether it's the latest, then exit.",
    )
    p.add_argument(
        "-U", "--update",
        action="store_true",
        help="Upgrade codesync itself (skips if already latest; pip install --upgrade git+https://...) and exit.",
    )
    p.add_argument(
        "--foreground",
        action="store_true",
        help="With --update: run pip synchronously so you see output live (Windows default is detached).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="With --update: reinstall even if already on the latest version (repair).",
    )

    sub = p.add_subparsers(dest="command", metavar="<command>")

    p_sync = sub.add_parser(
        "sync",
        help="One-command sync: clone, publish, auto-commit, rebase-pull, push.",
    )
    p_sync.add_argument(
        "--push", action="store_true",
        help="(deprecated no-op — push is the default since v2.3.0)",
    )
    p_sync.add_argument(
        "--no-push", action="store_true",
        help="Pull only; don't push local commits (and skip DB dump).",
    )
    p_sync.add_argument(
        "--no-publish", action="store_true",
        help="Don't auto-publish orphan directories (mkdir-but-no-git, or no-origin).",
    )
    p_sync.add_argument(
        "--no-commit", action="store_true",
        help="Don't auto-commit before pull/rebase (default does, except [commit].skip).",
    )
    p_sync.add_argument("--status", action="store_true", help="Status only, no clone/publish/pull/push.")
    p_sync.add_argument(
        "--workers", type=_positive_worker_count, default=None, metavar="N",
        help="Max concurrent network Git operations (overrides [sync].net_workers).",
    )
    p_sync.add_argument(
        "--local-workers", type=_positive_worker_count, default=None, metavar="N",
        help="Max concurrent local Git metadata scans (overrides [sync].local_workers).",
    )
    p_sync.add_argument(
        "--problems", action="store_true",
        help="In status output, hide clean repos and show only ones needing attention.",
    )

    for name, blurb in (
        ("pull", "Pull the latest code for every local repo (no push, no clone, no publish)."),
        ("push", "Commit and push every local repo (no pull — use `sync` to reconcile diverged repos)."),
    ):
        p_op = sub.add_parser(name, help=blurb)
        p_op.add_argument(
            "--no-commit", action="store_true",
            help="Don't auto-commit dirty repos first (default auto-commits, except [commit].skip).",
        )
        p_op.add_argument(
            "--workers", type=_positive_worker_count, default=None, metavar="N",
            help="Max concurrent network Git operations (overrides [sync].net_workers).",
        )
        p_op.add_argument(
            "--local-workers", type=_positive_worker_count, default=None, metavar="N",
            help="Max concurrent local Git metadata scans (overrides [sync].local_workers).",
        )
        p_op.add_argument(
            "--problems", action="store_true",
            help="In the closing status table, show only repos needing attention.",
        )

    sub.add_parser(
        "init",
        help="Run the first-run setup wizard (gh auth + config.toml). Also triggered automatically by `sync` when no config exists.",
    )

    sub.add_parser(
        "fork-setup",
        help="Scan local repos and add 'upstream' remote to forks that don't have one (backfill for forks cloned before v2.2.9).",
    )

    p_rename = sub.add_parser(
        "rename",
        help="Rename a repo locally + on GitHub. `rename <new>` (run in the repo dir) or `rename <old> <new>`.",
    )
    p_rename.add_argument(
        "names", nargs="+", metavar="NAME",
        help="One name (new; old inferred from current dir) or two names (old new).",
    )
    p_rename.add_argument(
        "--local-only", action="store_true",
        help="Rename only the local directory; leave GitHub and origin untouched.",
    )

    p_delete = sub.add_parser(
        "delete",
        help="Move a repo into local .codesync-trash and rename+archive it on GitHub.",
    )
    p_delete.add_argument(
        "name", nargs="?", metavar="NAME",
        help="Immediate child name under code_roots. Omit inside the repo directory.",
    )
    p_delete.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the 5-second confirmation countdown.",
    )
    p_delete.add_argument(
        "--local-only", action="store_true",
        help="Move only the local directory to trash; leave the GitHub repo live "
             "(still reads its Repository ID so sync won't re-clone it).",
    )

    p_trash = sub.add_parser("trash", help="List, restore, or permanently purge repository trash.")
    trash_sub = p_trash.add_subparsers(dest="trash_command", required=True)
    trash_sub.add_parser("list", help="List local .codesync-trash entries.")
    p_restore = trash_sub.add_parser("restore", help="Restore one trashed repo locally and on GitHub.")
    p_restore.add_argument("name", metavar="NAME")
    p_purge = trash_sub.add_parser("purge", help="Permanently delete one trashed repo locally and on GitHub.")
    p_purge.add_argument("name", metavar="NAME")
    p_purge.add_argument("-y", "--yes", action="store_true", help="Skip typed-name confirmation.")

    sub.add_parser(
        "migrate-config",
        help="One-shot migration from V1 config.local.ps1 to TOML.",
    )

    sub.add_parser(
        "config-path",
        help="Print the resolved config file path and exit.",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 output streams. Our output uses ✓ ▸ ⚠ and Chinese text; when
    # stdout is redirected (`codesync sync > log.txt`) Python falls back to the
    # locale encoding — GBK on Chinese Windows, ASCII under a POSIX locale on
    # Kylin/older Linux — and print() raises UnicodeEncodeError. errors=replace
    # keeps even a genuinely non-UTF-8 terminal from crashing us.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # exotic stream (tests, embedders) — keep whatever it is

    # Keep all GitHub SSH traffic spawned by this codesync process on GitHub's
    # official SSH-over-HTTPS endpoint. This is environment-only: repository
    # remotes and the user's ~/.ssh/config are deliberately left unchanged.
    configure_github_ssh_over_443()
    # Defaults for every subcommand: delete/rename push over the network too,
    # and without a stall policy a dead HTTPS connection there hangs for the
    # whole proc.T_NET_LONG backstop (15 minutes). run_sync() re-applies the
    # user's [sync] values. Both of these are pure os.environ writes with no
    # I/O, so they stay pre-parse — unlike configure_ssh_command below.
    configure_http_stall_detection()

    parser = _build_parser()
    args = parser.parse_args(argv)

    # Report the outcome of a prior background --update (v2.12.0), so the user
    # learns whether it finished without having to guess. Best-effort, no network.
    from codesync.updater import report_pending_update
    report_pending_update()

    if args.version:
        from codesync.updater import print_version_cli
        print_version_cli()
        return 0

    if args.update:
        from codesync.updater import self_update
        return self_update(foreground=args.foreground, force=args.force)

    if args.command is None:
        parser.print_help()
        return 0

    # This filesystem-only preflight stays before SSH setup and every repo
    # operation. A moved/unmounted root must not look like a successful empty
    # scan, nor be interpreted as remote-archive intent by a write command.
    if not _ensure_runtime_config(args):
        return 2

    # SSH setup can reach the network; pay that cost only after the local
    # configuration has passed preflight.
    _configure_ssh_if_needed(args)

    if args.command == "sync":
        from codesync.sync import run_sync
        return run_sync(
            status_only=args.status,
            net_workers=args.workers,
            local_workers=args.local_workers,
            problems_only=args.problems,
            no_publish=args.no_publish,
            no_push=args.no_push,
            no_commit=args.no_commit,
        )

    if args.command in {"pull", "push"}:
        # Presets over run_sync, not separate pipelines — repo discovery,
        # nested-repo handling, the write-operation version gate, the safety
        # countdown and the SSH master lifecycle all live there and must not be
        # reimplemented twice.
        from codesync.sync import run_pull, run_push
        runner = run_pull if args.command == "pull" else run_push
        return runner(
            net_workers=args.workers,
            local_workers=args.local_workers,
            problems_only=args.problems,
            no_commit=args.no_commit,
        )

    if args.command == "init":
        from codesync.wizard import run_first_run_wizard
        return 0 if run_first_run_wizard() else 1

    if args.command == "fork-setup":
        from codesync.fork_setup import run_fork_setup
        return run_fork_setup()

    if args.command == "rename":
        from codesync.updater import enforce_up_to_date
        if not enforce_up_to_date():
            return 1
        from codesync.rename import rename_repo
        return rename_repo(args.names, local_only=args.local_only)

    if args.command == "delete":
        from codesync.updater import enforce_up_to_date
        if not enforce_up_to_date():
            return 1
        from codesync.delete import delete_repo
        return delete_repo(
            args.name, yes=args.yes, local_only=args.local_only,
        )

    if args.command == "trash":
        from codesync.updater import enforce_up_to_date
        if args.trash_command != "list" and not enforce_up_to_date():
            return 1
        from codesync.config import load
        from codesync.trash import list_trash, purge_trash, restore_trash
        roots = load().code_roots_expanded
        if args.trash_command == "list":
            return list_trash(roots)
        if args.trash_command == "restore":
            return restore_trash(args.name, roots)
        return purge_trash(args.name, roots, yes=args.yes)

    if args.command == "migrate-config":
        from codesync.config import migrate_from_ps1
        return migrate_from_ps1()

    if args.command == "config-path":
        from codesync.config import config_file_path
        print(config_file_path())
        return 0

    output.err(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
