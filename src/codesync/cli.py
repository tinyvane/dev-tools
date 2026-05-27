from __future__ import annotations

import argparse
import sys

from codesync import __version__
from codesync import output


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codesync",
        description="Personal multi-machine git/db sync tool.",
    )
    p.add_argument("--version", action="version", version=f"codesync {__version__}")
    p.add_argument(
        "-U", "--update",
        action="store_true",
        help="Upgrade codesync itself (pip install --upgrade git+https://...) and exit.",
    )
    p.add_argument(
        "--foreground",
        action="store_true",
        help="With --update: run pip synchronously so you see output live (Windows default is detached).",
    )

    sub = p.add_subparsers(dest="command", metavar="<command>")

    p_sync = sub.add_parser("sync", help="Sync all registered git repos.")
    p_sync.add_argument("--push", action="store_true", help="Also push after pulling.")
    p_sync.add_argument("--status", action="store_true", help="Status only, no pull/push.")
    p_sync.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help="Max concurrent git operations (default: auto, ~2x CPU count, capped at 16).",
    )
    p_sync.add_argument(
        "--problems", action="store_true",
        help="In status output, hide clean repos and show only ones needing attention.",
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

    if args.update:
        from codesync.updater import self_update
        return self_update(foreground=args.foreground)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "sync":
        from codesync.sync import run_sync
        return run_sync(
            push=args.push,
            status_only=args.status,
            workers=args.workers,
            problems_only=args.problems,
        )

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
