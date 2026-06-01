from __future__ import annotations

import functools
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

from codesync import __repo_url__, output, paths

# GitHub mirrors tried (in order) when github.com is unreachable and the user
# didn't set CODESYNC_GH_MIRROR. Same list the install scripts use. Public
# ghproxy-style prefixes; they come and go, hence the env-var escape hatch.
_DEFAULT_MIRRORS = (
    "https://ghfast.top",
    "https://gh-proxy.com",
    "https://mirror.ghproxy.com",
)


# Install command. `--upgrade` so we go forward, never backward.
# `--user` only OUTSIDE a venv — inside a venv (incl. pipx-managed installs on
# PEP 668 externally-managed Pythons like Homebrew's) pip rejects --user with
# "Can not perform a '--user' install. User site-packages are not visible in
# this virtualenv". The canonical in-venv check is sys.prefix != sys.base_prefix;
# works for both stdlib venv and pipx.
def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _url_ok(url: str, timeout: float = 6.0) -> bool:
    """True if the URL responds at all (any HTTP status). We only care that
    TLS completes — behind the GFW, github.com fails at the TLS layer."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # reachable, just a non-2xx status
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _gh_mirror() -> str:
    """Mirror prefix to route GitHub through, or "" for direct.
    CODESYNC_GH_MIRROR wins; otherwise probe github.com and fall back to the
    first reachable DEFAULT_MIRRORS entry. Cached for the process lifetime so
    the probe runs at most once per --update."""
    env = os.environ.get("CODESYNC_GH_MIRROR", "").strip().rstrip("/")
    if env:
        return env
    if _url_ok("https://github.com/tinyvane/dev-tools"):
        return ""
    for m in _DEFAULT_MIRRORS:
        if _url_ok(f"{m}/https://github.com/tinyvane/dev-tools"):
            return m
    return ""


def _pip_args() -> list[str]:
    mirror = _gh_mirror()
    args = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if not _in_venv():
        args.append("--user")
    # Behind the GFW, pypi.org (for the setuptools/wheel build deps) is slow;
    # route pip's index through a CN mirror when a GitHub mirror is active.
    # CODESYNC_PIP_INDEX overrides explicitly.
    index = os.environ.get("CODESYNC_PIP_INDEX", "").strip()
    if not index and mirror:
        index = "https://pypi.tuna.tsinghua.edu.cn/simple"
    if index:
        args += ["--index-url", index]
    spec = f"git+{__repo_url__}.git@main"
    if mirror:
        spec = f"git+{mirror}/{__repo_url__}.git@main"
    args.append(spec)
    return args


def _log_header(reason: str) -> str:
    return (
        f"\n{'=' * 60}\n"
        f"codesync --update {reason}\n"
        f"started: {datetime.now().isoformat(timespec='seconds')}\n"
        f"cmd:     {' '.join(_pip_args())}\n"
        f"{'=' * 60}\n"
    )


def _run_foreground() -> int:
    """Synchronous pip install — user sees output live.
    Safe on Mac/Linux (pip can overwrite in place). On Windows, may fail if
    pip tries to replace the running codesync.exe — that's exactly when you
    should use the default (detached) mode instead.
    """
    output.section("codesync 自更新（前台模式）")
    cmd = _pip_args()
    output.detail(" ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode == 0:
        output.good("升级完成。下次跑 codesync 即为新版。")
        return 0
    output.err(f"升级失败 (pip exit {r.returncode})。详见上方 pip 输出。")
    return r.returncode


def _run_detached_windows() -> int:
    """Windows: spawn pip detached + redirect stdout/stderr to a log file.

    The previous version passed no stdout/stderr to Popen, which under
    DETACHED_PROCESS made pip inherit closed console handles and crash
    silently on its first log write. We now point pip at a real file
    (append mode so multiple runs accumulate) and give it /dev/null stdin.
    """
    output.section("codesync 自更新")
    cmd = _pip_args()
    output.detail(" ".join(cmd))

    log = paths.update_log_file()
    paths.ensure_config_dir()
    # Append a header so successive runs are distinguishable.
    with open(log, "a", encoding="utf-8") as f:
        f.write(_log_header("(background)"))

    creationflags = 0
    for attr in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= getattr(subprocess, attr, 0)

    logf = open(log, "ab")
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
        )
    finally:
        logf.close()

    output.good("升级已在后台开始。pip 输出写到:")
    output.detail(f"  {log}")
    output.detail("几秒后跑 `codesync --version` 验证；失败请查日志，或用 `codesync --update --foreground` 同步重试。")
    return 0


def self_update(*, foreground: bool = False) -> int:
    if foreground or os.name != "nt":
        # Unix: pip can overwrite in place, no need to detach.
        # Windows + --foreground: user explicitly opted in.
        return _run_foreground()
    return _run_detached_windows()
