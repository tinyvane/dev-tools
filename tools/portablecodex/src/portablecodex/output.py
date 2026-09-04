from __future__ import annotations

import os
import sys


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ok = True
        for std_handle in (-11, -12):
            handle = kernel32.GetStdHandle(std_handle)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                ok = False
        return ok
    except Exception:
        return False


_NO_COLOR = (
    bool(os.environ.get("NO_COLOR"))
    or not sys.stdout.isatty()
    or not _enable_windows_vt()
)
_COLORS = {
    "reset": "\x1b[0m", "dim": "\x1b[2m", "red": "\x1b[31m",
    "green": "\x1b[32m", "yellow": "\x1b[33m", "cyan": "\x1b[36m",
    "gray": "\x1b[90m",
}


def _wrap(value: str, color: str) -> str:
    if _NO_COLOR:
        return value
    return f"{_COLORS[color]}{value}{_COLORS['reset']}"


def section(message: str) -> None:
    print(flush=True)
    print(_wrap(f"▸ {message}", "cyan"), flush=True)


def info(message: str) -> None:
    print(message, flush=True)


def detail(message: str) -> None:
    print(_wrap(f"  {message}", "gray"), flush=True)


def good(message: str) -> None:
    print(_wrap(f"  {message}", "green"), flush=True)


def warn(message: str) -> None:
    print(_wrap(f"  ⚠ {message}", "yellow"), flush=True)


def err(message: str) -> None:
    print(_wrap(f"  ✗ {message}", "red"), file=sys.stderr, flush=True)


def hilite(message: str, color: str = "cyan") -> str:
    return _wrap(message, color)
