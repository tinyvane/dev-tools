"""Bounded, non-interactive subprocess execution for codesync."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


TIMEOUT_RC = 124
NOTFOUND_RC = 127
OSERR_RC = 126


def _timeout_scale() -> float:
    try:
        scale = float(os.environ.get("CODESYNC_TIMEOUT_SCALE", "1.0"))
    except ValueError:
        return 1.0
    return scale if scale > 0 else 1.0


_TIMEOUT_SCALE = _timeout_scale()
T_QUICK = max(1, round(30 * _TIMEOUT_SCALE))
T_LOCAL = max(1, round(300 * _TIMEOUT_SCALE))
T_NET = max(1, round(120 * _TIMEOUT_SCALE))
# Wall-clock backstop for unbounded transfers: clone, fetch/pull, push, and the
# full `gh repo list`. It is only a final safety net — a genuinely dead
# connection is caught far sooner by the HTTP low-speed policy and SSH
# ServerAlive (git_transport.DEFAULT_STALL_SECONDS, 300s), which is why this
# does not need to be tight. 900s at the 12-15 KB/s the stall comment describes
# is roughly 11-13 MB of legitimate transfer, while the stall detector still
# fires 3x sooner on a link that has actually died. It was 3600s, which only
# helped repositories large enough to be worth cloning by hand and made the
# worst-case unattended hang an hour.
# MUST stay strictly above git_transport.DEFAULT_STALL_SECONDS: if this fires
# first, the stall policy becomes unreachable dead code.
T_NET_LONG = max(1, round(900 * _TIMEOUT_SCALE))
# First-time whole-repository transfers: `git clone`, and `gh repo create
# --source=. --push`. These move the ENTIRE history rather than an increment, so
# they legitimately outlast an incremental pull by a wide margin — and unlike a
# pull, a killed clone leaves a half-finished directory the user has to clean up
# by hand. A dead connection is still caught in ~300s by the low-speed / SSH
# ServerAlive policy (clone runs with capture=False, so git gets real sideband
# progress and the byte counter actually moves), which is what makes a generous
# wall-clock backstop safe here: it only extends SLOW-but-progressing transfers.
T_NET_CLONE = max(1, round(3600 * _TIMEOUT_SCALE))


def run(
    argv: list[str],
    *,
    timeout: int,
    cwd: str | Path | None = None,
    capture: bool = True,
    stdin_devnull: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run one command and convert launch/timeout errors into return codes."""
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
            argv,
            TIMEOUT_RC,
            stdout="",
            stderr=f"codesync: 超时 >{timeout}s: {shown}",
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            argv, NOTFOUND_RC, stdout="", stderr=str(exc),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            argv, OSERR_RC, stdout="", stderr=str(exc),
        )


def timed_out(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == TIMEOUT_RC
