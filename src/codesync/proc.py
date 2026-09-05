"""Bounded, non-interactive subprocess execution for codesync."""
from __future__ import annotations

import os
import signal
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

_NON_INTERACTIVE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "Never",
}
_TREE_TERMINATED_GIT_OPS = {
    "clone", "fetch", "pull", "push", "ls-remote", "submodule",
}


def _child_environment(
    argv: list[str], env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Return an isolated child environment, forcing Git to stay headless."""
    is_git = Path(argv[0]).name.casefold() in {"git", "git.exe"}
    if env is None and not is_git:
        return None
    child = dict(os.environ if env is None else env)
    if is_git:
        # stdin=DEVNULL only closes terminal input. Git Credential Manager can
        # still open a Windows UI and wait forever, as a real codesync sync
        # demonstrated. Codesync is an unattended batch tool, so both Git's
        # terminal prompt and GCM's GUI/device-flow prompt must be disabled for
        # this child tree. Manual git commands outside codesync are untouched.
        child.update(_NON_INTERACTIVE_GIT_ENV)
    return child


def _terminate_process_tree(child: subprocess.Popen) -> None:
    """Best-effort exact-tree termination for a timed-out child process."""
    if child.poll() is not None:
        return
    if os.name == "nt":
        # Popen.kill() only terminates the direct git.exe. Its fetch,
        # remote-https and credential-manager descendants keep inherited pipe
        # handles open, causing subprocess communicate() to hang even after the
        # advertised timeout. taskkill /T is scoped to this exact child PID.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if child.poll() is None:
        try:
            child.kill()
        except OSError:
            pass


def _needs_tree_timeout(argv: list[str]) -> bool:
    """Whether this Git command may spawn transport/credential descendants."""
    if Path(argv[0]).name.casefold() not in {"git", "git.exe"}:
        return False
    return any(arg in _TREE_TERMINATED_GIT_OPS for arg in argv[1:])


def _timeout_result(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    shown = " ".join(argv[:2])
    return subprocess.CompletedProcess(
        argv,
        TIMEOUT_RC,
        stdout="",
        stderr=f"codesync: 超时 >{timeout}s: {shown}",
    )


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
        "encoding": "utf-8",
        "errors": "replace",
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    child_env = _child_environment(argv, env)
    if child_env is not None:
        kwargs["env"] = child_env
    if not _needs_tree_timeout(argv):
        try:
            return subprocess.run(argv, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired:
            return _timeout_result(argv, timeout)
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(
                argv, NOTFOUND_RC, stdout="", stderr=str(exc),
            )
        except OSError as exc:
            return subprocess.CompletedProcess(
                argv, OSERR_RC, stdout="", stderr=str(exc),
            )

    try:
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen(argv, **kwargs)
        stdout, stderr = child.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(child)
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # taskkill/killpg should have closed every inherited pipe. If an
            # exotic child escaped the tree, do not let cleanup recreate the
            # unbounded wait this module exists to prevent.
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        return _timeout_result(argv, timeout)
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
