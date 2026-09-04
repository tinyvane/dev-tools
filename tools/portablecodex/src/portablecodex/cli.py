from __future__ import annotations

import argparse
import sys

from portablecodex import __version__
from portablecodex import config, output, portable


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portablecodex",
        description="Guided Codex portable workspace management for Windows.",
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit.")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    onboard = commands.add_parser(
        "onboard", help="Guide this PC into a local or existing portable workspace.",
    )
    onboard.add_argument("--root", metavar="PATH")
    onboard.add_argument("--mode", choices=("connect", "initialize"))
    onboard.add_argument("--execute", action="store_true")
    onboard.add_argument("--source-home", metavar="PATH")
    onboard.add_argument("--sessions-source", metavar="PATH")

    context = commands.add_parser(
        "context", help="Inspect conversations, transport, writer locks, and index coverage.",
    )
    context_actions = context.add_subparsers(dest="context_command", required=True)
    for name, description in (
        ("status", "Fast read-only context summary."),
        ("doctor", "Deep read-only validation of every rollout and SQLite index."),
    ):
        action = context_actions.add_parser(name, help=description)
        action.add_argument("--sessions-dir", metavar="PATH")
        action.add_argument("--transport-root", metavar="PATH")
        action.add_argument("--json", action="store_true")

    for name, description in (
        ("status", "Inspect the registered portable layout."),
        ("prepare", "Prepare an empty portable layout and migration inventory."),
        ("migrate", "Build the portable workspace after all Codex writers exit."),
        ("verify", "Deep-check the completed portable workspace."),
        ("alias", "Install or remove this PC's codexv command."),
        ("attach", "Attach this PC to legacy exclusive mode."),
        ("detach", "Detach this PC from legacy exclusive mode."),
        ("rollback", "Roll back a legacy exclusive migration."),
    ):
        action = commands.add_parser(name, help=description)
        action.add_argument("--root", metavar="PATH")
        if name in {"status", "verify"}:
            action.add_argument("--json", action="store_true")
        if name == "prepare":
            action.add_argument("--source-home", metavar="PATH")
            action.add_argument("--sessions-source", metavar="PATH")
        if name == "migrate":
            action.add_argument(
                "--mode", choices=("dual", "exclusive"), default="dual",
            )
        if name == "alias":
            action.add_argument("--remove", action="store_true")
        if name in {"migrate", "alias", "attach", "detach", "rollback"}:
            action.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"portablecodex {__version__}")
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    if args.command != "context":
        try:
            args.root = (
                args.root
                or config.load(include_legacy=False).portable_root
                or portable.DEFAULT_ROOT
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            output.err(f"PortableCodex configuration failed closed: {exc}")
            return 2
    if args.command == "onboard":
        from portablecodex.onboard import run_onboard
        return run_onboard(
            root=args.root,
            mode=args.mode,
            execute=args.execute,
            source_home=args.source_home,
            sessions_source=args.sessions_source,
        )
    if args.command == "context":
        from portablecodex.context_sync import run_context
        return run_context(
            args.context_command,
            sessions_dir=args.sessions_dir,
            transport_root=args.transport_root,
            json_output=args.json,
        )
    from portablecodex.portable import run_portable
    return run_portable(
        args.command,
        root=args.root,
        source_home=getattr(args, "source_home", None),
        sessions_source=getattr(args, "sessions_source", None),
        execute=getattr(args, "execute", False),
        json_output=getattr(args, "json", False),
        migration_mode=getattr(args, "mode", "dual"),
        remove_alias=getattr(args, "remove", False),
    )
