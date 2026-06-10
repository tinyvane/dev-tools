from __future__ import annotations

import argparse
import sys

from codesync import output


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codesync",
        description="Personal multi-machine git/db sync tool.",
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
        help="One-command sync: clone missing, publish orphans, pull, push (push is default now).",
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
        help="Don't auto-commit dirty repos before push (default auto-commits, except [commit].skip).",
    )
    p_sync.add_argument("--status", action="store_true", help="Status only, no clone/publish/pull/push.")
    p_sync.add_argument(
        "--skip-version-check", action="store_true",
        help="Run even if codesync is outdated (the version gate normally blocks destructive sync; risk is yours).",
    )
    p_sync.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help="Max concurrent git operations (default: auto, ~2x CPU count, capped at 16).",
    )
    p_sync.add_argument(
        "--problems", action="store_true",
        help="In status output, hide clean repos and show only ones needing attention.",
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

    p_delete = sub.add_parser(
        "delete",
        help="Delete a local repo and archive it on GitHub (other machines auto-remove it on next sync). `delete` (in the repo dir) or `delete <name>`.",
    )
    p_delete.add_argument(
        "name", nargs="?", metavar="NAME",
        help="Repo name to find under code_roots. Omit to delete the repo in the current directory.",
    )
    p_delete.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the 5-second confirmation countdown.",
    )

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
    parser = _build_parser()
    args = parser.parse_args(argv)

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

    if args.command == "sync":
        # First-run UX: trigger wizard if either:
        #   (a) config file is missing, OR
        #   (b) config exists but is the unedited template (v2.2.5-era ghost from a
        #       previous failed sync run before the wizard landed).
        # Wizard returns False if it bailed (gh missing, user declined, etc.).
        # Post-wizard, if the config is still missing or still the template, print a
        # clear instruction and exit — don't run sync against an empty/placeholder
        # config (would do nothing useful and confuse the user).
        from codesync import paths
        from codesync.config import is_template_unedited, write_template_if_missing

        cfg_file = paths.config_file()
        needs_setup = (not cfg_file.exists()) or is_template_unedited()
        if needs_setup:
            from codesync.wizard import run_first_run_wizard
            run_first_run_wizard()

            # Re-check after wizard. If it bailed, fall back to writing/keeping the
            # template + telling the user how to proceed.
            if not cfg_file.exists():
                write_template_if_missing()
            if is_template_unedited():
                output.warn(f"配置未生成 / 仍是未编辑模板: {cfg_file}")
                output.warn("可以：")
                output.warn("  1. 重跑 `codesync init`（推荐 —— 自动检测 gh 并填配置）")
                output.warn("  2. 或手动编辑该文件后重跑 `codesync sync`")
                return 1

        from codesync.sync import run_sync
        return run_sync(
            status_only=args.status,
            workers=args.workers,
            problems_only=args.problems,
            no_publish=args.no_publish,
            no_push=args.no_push,
            no_commit=args.no_commit,
            skip_version_check=args.skip_version_check,
        )

    if args.command == "init":
        from codesync.wizard import run_first_run_wizard
        return 0 if run_first_run_wizard() else 1

    if args.command == "fork-setup":
        from codesync.fork_setup import run_fork_setup
        return run_fork_setup()

    if args.command == "rename":
        from codesync.rename import rename_repo
        return rename_repo(args.names)

    if args.command == "delete":
        from codesync.delete import delete_repo
        return delete_repo(args.name, yes=args.yes)

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
