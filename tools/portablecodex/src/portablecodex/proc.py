"""Bounded, non-interactive subprocess execution for PortableCodex."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


TIMEOUT_RC = 124
NOTFOUND_RC = 127
OSERR_RC = 126


def _timeout_scale() -> float:
    raw = os.environ.get(
        "PORTABLECODEX_TIMEOUT_SCALE",
        os.environ.get("CODESYNC_TIMEOUT_SCALE", "1.0"),
    )
    try:
        scale = float(raw)
    except ValueError:
        return 1.0
    return scale if scale > 0 else 1.0


_TIMEOUT_SCALE = _timeout_scale()
T_QUICK = max(1, round(30 * _TIMEOUT_SCALE))
T_LOCAL = max(1, round(300 * _TIMEOUT_SCALE))
T_NET = max(1, round(120 * _TIMEOUT_SCALE))
T_NET_LONG = max(1, round(900 * _TIMEOUT_SCALE))


def run(
    argv: list[str],
    *,
    timeout: int,
    cwd: str | Path | None = None,
    capture: bool = True,
    stdin_devnull: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise TypeError("argv must be a list[str]")
    if not argv:
        raise ValueError("argv must not be empty")
    kwargs: dict = {
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if capture:
        kwargs["capture_output"] = True
    if stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    if env is not None:
        kwargs["env"] = dict(env)
    try:
        return subprocess.run(argv, **kwargs)
    except subprocess.TimeoutExpired:
        shown = " ".join(argv[:2])
        return subprocess.CompletedProcess(
            argv, TIMEOUT_RC, stdout="", stderr=f"portablecodex: timeout >{timeout}s: {shown}",
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, NOTFOUND_RC, stdout="", stderr=str(exc))
    except OSError as exc:
        return subprocess.CompletedProcess(argv, OSERR_RC, stdout="", stderr=str(exc))


def timed_out(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == TIMEOUT_RC
