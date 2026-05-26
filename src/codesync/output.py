from __future__ import annotations

import os
import sys

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()

_COLORS = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "gray": "\x1b[90m",
}


def _wrap(text: str, color: str) -> str:
    if _NO_COLOR:
        return text
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


def section(msg: str) -> None:
    print()
    print(_wrap(f"▸ {msg}", "cyan"))


def info(msg: str) -> None:
    print(msg)


def detail(msg: str) -> None:
    print(_wrap(f"  {msg}", "gray"))


def good(msg: str) -> None:
    print(_wrap(f"  {msg}", "green"))


def warn(msg: str) -> None:
    print(_wrap(f"  ⚠ {msg}", "yellow"))


def err(msg: str) -> None:
    print(_wrap(f"  ✗ {msg}", "red"), file=sys.stderr)


def hilite(msg: str, color: str = "cyan") -> str:
    return _wrap(msg, color)
