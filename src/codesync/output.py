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


# flush=True everywhere: subprocess child writes directly to stdout/stderr and
# would otherwise appear *before* our buffered prints.
def section(msg: str) -> None:
    print(flush=True)
    print(_wrap(f"▸ {msg}", "cyan"), flush=True)


def info(msg: str) -> None:
    print(msg, flush=True)


def detail(msg: str) -> None:
    print(_wrap(f"  {msg}", "gray"), flush=True)


def good(msg: str) -> None:
    print(_wrap(f"  {msg}", "green"), flush=True)


def warn(msg: str) -> None:
    print(_wrap(f"  ⚠ {msg}", "yellow"), flush=True)


def err(msg: str) -> None:
    print(_wrap(f"  ✗ {msg}", "red"), file=sys.stderr, flush=True)


def hilite(msg: str, color: str = "cyan") -> str:
    return _wrap(msg, color)
