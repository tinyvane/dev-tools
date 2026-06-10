from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from codesync import __repo_url__, __version__, output, paths

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
    import ssl
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # reachable, just a non-2xx status
    except urllib.error.URLError as e:
        # Cert-verification failure means the TLS handshake COMPLETED — the
        # host is reachable; only Python's CA store is stale (common on Kylin /
        # old Debian). The GFW kills the connection DURING the handshake, so a
        # cert error is positive evidence of reachability, not a block.
        return isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError)
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
    """Windows: spawn pip in a hidden background process + redirect stdout/stderr
    to a log file.

    Uses CREATE_NO_WINDOW (NOT DETACHED_PROCESS). DETACHED_PROCESS gives pip no
    console at all, so each console child it spawns (git clone, build-isolation
    pip, wheel compiler) had to allocate its OWN console — that's the flurry of
    windows that flash up and vanish during an update. CREATE_NO_WINDOW instead
    gives pip a *hidden* console its children attach to, so nothing flashes. The
    process still outlives this codesync.exe (child lifetime is independent), so
    pip can replace the running exe.

    stdout/stderr go to a real file and stdin is DEVNULL — required regardless of
    the flag: without explicit handles a background process inherits closed
    console handles and pip crashes on its first log write (the v2.2.2 bug).
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
    for attr in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
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


def print_version_cli() -> None:
    """`codesync --version`: current version + whether it's the latest.

    Prints the FIRST line ("codesync X.Y.Z") immediately, BEFORE the network
    probe — the fresh check can take seconds on a slow/blocked network and a
    blank screen there reads as a hang. The plain first line also keeps
    `codesync --version | awk '{print $2}'`-style parsing working (install.sh
    relies on it).

    The latest-check is FRESH (ttl_hours=0, bypassing the 12h cache) because
    this is a deliberate, infrequent query where accuracy matters more than the
    sync banner's speed — and it warms the cache for the next sync. Fail-open:
    on any network failure just adds a soft note.
    """
    cur = __version__
    output.info(f"codesync {cur}")
    if cur.startswith("0.0.0"):
        output.detail("（源码运行，不检查更新）")
        return
    latest = latest_version(ttl_hours=0)
    if not latest:
        output.detail("（无法检查最新版 — 网络不可达）")
        return
    cur_t, lat_t = _parse_version(cur), _parse_version(latest)
    if cur_t and lat_t and cur_t < lat_t:
        output.info(output.hilite(
            f"  有新版 {latest}，跑 `codesync --update` 升级", "yellow"))
    else:
        output.detail("（已是最新）")


def _update_reachable(timeout: float = 4.0) -> bool:
    """True if github.com or any configured/default mirror is reachable (TLS
    handshake). Update pre-flight (v2.12.0): fail fast instead of spawning a
    doomed background pip when the network is down."""
    if os.environ.get("CODESYNC_GH_MIRROR", "").strip():
        return True  # user-configured mirror — trust it, don't probe
    probe = "https://github.com/tinyvane/dev-tools"
    if _url_ok(probe, timeout=timeout):
        return True
    return any(_url_ok(f"{m}/{probe}", timeout=timeout) for m in _DEFAULT_MIRRORS)


def _write_pending(target: str | None) -> None:
    """Record that a background update was launched, so the next run can verify
    it. Best-effort; never raises."""
    try:
        paths.ensure_config_dir()
        paths.update_pending_file().write_text(
            json.dumps({"target": target,
                        "started_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except OSError:
        pass


def report_pending_update() -> None:
    """Called at the top of every run: if a prior --update left a marker, report
    its outcome by comparing the now-installed __version__ to the recorded
    target. Resolves the "did my background update actually finish?" uncertainty
    without holding the .exe. Never raises.

    - installed >= target (or target unknown)  → success, clear marker
    - installed < target, started < 10 min ago → likely still running, keep marker
    - installed < target, started long ago      → likely failed, warn + clear
    """
    f = paths.update_pending_file()
    if not f.exists():
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            f.unlink()
        except OSError:
            pass
        return

    target = data.get("target")
    cur = __version__
    cur_t = _parse_version(cur)
    tgt_t = _parse_version(target) if target else None

    if (not tgt_t) or (cur_t and cur_t >= tgt_t):
        output.good(f"✓ 上次升级完成，当前 codesync {cur}")
        _safe_unlink(f)
        return

    stale = True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data.get("started_at"))
        stale = age.total_seconds() > 600  # >10 min → almost certainly not still running
    except (ValueError, TypeError):
        stale = True
    if stale:
        output.warn(f"⚠ 上次升级似乎未完成（仍是 {cur}，目标 {target}）。")
        output.detail(f"查日志 {paths.update_log_file()}，或 `codesync --update --foreground` 重试。")
        _safe_unlink(f)
    else:
        output.detail(f"上次升级可能还在后台进行（当前 {cur} → 目标 {target}），稍后再跑确认。")


def _safe_unlink(f) -> None:
    try:
        f.unlink()
    except OSError:
        pass


def self_update(*, foreground: bool = False, force: bool = False) -> int:
    # Skip a pointless reinstall when already on the latest (v2.11.0). Use a
    # FRESH probe (ttl_hours=0) — NOT the 12h cache — because the user explicitly
    # asked to update and a stale cache could wrongly say "already latest" when a
    # newer version exists. --force bypasses the check (reinstall/repair).
    target: str | None = None
    if not force:
        cur = __version__
        if not cur.startswith("0.0.0"):
            latest = latest_version(ttl_hours=0)
            if latest:
                cur_t, lat_t = _parse_version(cur), _parse_version(latest)
                if cur_t and lat_t and cur_t >= lat_t:
                    output.good(f"已是最新版 {cur}，无需升级。")
                    output.detail("要强制重装/修复，加 --force：codesync --update --force")
                    return 0
                output.info(f"发现新版: {cur} → {latest}，开始升级...")
                target = latest
            else:
                # Couldn't read the latest version. Network down? Fail fast
                # rather than spawn a doomed background pip (v2.12.0).
                if not _update_reachable():
                    output.err("网络不通：github.com 和所有镜像都连不上，升级无法进行。")
                    output.detail("检查网络/代理后重试；或设 CODESYNC_GH_MIRROR 指定镜像。")
                    return 1
                output.warn("无法确认最新版（但网络可达），仍尝试升级...")

    if foreground or os.name != "nt":
        # Unix: pip can overwrite in place, no need to detach.
        # Windows + --foreground: user explicitly opted in. Result is shown
        # synchronously, so no pending marker needed.
        return _run_foreground()
    # Windows detached: result is deferred → record the target so the next run
    # can confirm completion (see report_pending_update).
    _write_pending(target)
    return _run_detached_windows()


# ---------- version gate (v2.7.0) ----------
#
# Before any destructive sync (push / archive / local-delete), check whether a
# newer codesync is published on main and refuse to run if this machine is
# behind. Rationale: the v2.6.x mass-archive incident was a multi-machine
# version-skew problem — an old/buggy version on one machine did damage. Gating
# destructive ops on "you're on the latest version" stops that class of bug.
#
# Hard rules:
#  - fail-OPEN: any network/parse failure → proceed (never brick offline use).
#  - source checkouts ("0.0.0+source") and not-installed → skip entirely.
#  - --status (read-only) is exempt; the gate is only wired into write paths.
#  - throttled: the remote lookup is cached for ttl_hours so normal syncs don't
#    pay a network round-trip every run.

# Single-source-of-truth for the version lives in pyproject.toml on main; we read
# it raw (no gh dependency, mirror-aware) rather than hitting the GitHub API.
_RAW_PYPROJECT = (
    "https://raw.githubusercontent.com/tinyvane/dev-tools/main/pyproject.toml"
)


def _parse_version(s: str) -> tuple[int, ...] | None:
    """"2.6.2" -> (2, 6, 2). Drops any +local / pre-release suffix. None if junk."""
    head = s.strip().split("+")[0].split("-")[0]
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$", head)
    if not m:
        return None
    return tuple(int(g) if g else 0 for g in m.groups())


def _fetch_latest_version(timeout: float = 4.0) -> str | None:
    """Fetch the version string from pyproject.toml on main (mirror-aware).
    Returns None on any failure — callers must treat None as 'unknown' and
    fail open."""
    mirror = _gh_mirror()
    url = f"{mirror}/{_RAW_PYPROJECT}" if mirror else _RAW_PYPROJECT
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def _read_check_cache() -> dict:
    try:
        return json.loads(paths.version_check_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_check_cache(latest: str) -> None:
    try:
        paths.ensure_config_dir()
        paths.version_check_file().write_text(
            json.dumps(
                {"latest": latest,
                 "checked_at": datetime.now(timezone.utc).isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # cache is best-effort; a failed write just means we re-probe sooner


def latest_version(*, ttl_hours: int = 12, timeout: float = 4.0) -> str | None:
    """Latest published version, cached for ttl_hours. None if it can't be
    determined (fresh cache miss + network failure) — caller fails open.
    A cache entry within the TTL is used without touching the network; once
    expired we MUST re-probe, and a probe failure returns None (not stale data)
    so we never block on a stale verdict when GitHub is unreachable."""
    cache = _read_check_cache()
    ts, cached = cache.get("checked_at"), cache.get("latest")
    if ts and cached:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
            if age.total_seconds() < ttl_hours * 3600:
                return cached
        except (ValueError, TypeError):
            pass
    latest = _fetch_latest_version(timeout=timeout)
    if latest:
        _write_check_cache(latest)
    return latest


def print_version_status(uc) -> None:
    """Show current + latest version at the top of every run (v2.10.0).
    Cheap and fail-open: uses the cached latest_version (12h TTL); on any
    network failure shows '未知' rather than blocking. `uc` is a
    config.UpdateConfig or None."""
    output.section("codesync 版本")
    cur = __version__
    if cur.startswith("0.0.0"):
        output.detail(f"当前: {cur}（源码运行，不检查更新）")
        return
    if uc is not None and not uc.check:
        output.detail(f"当前: {cur}（更新检查已关闭）")
        return

    ttl = uc.ttl_hours if uc is not None else 12
    latest = latest_version(ttl_hours=ttl)
    if not latest:
        output.detail(f"当前: {cur}")
        output.detail("最新: 未知（无法检查更新）")
        return
    cur_t, lat_t = _parse_version(cur), _parse_version(latest)
    if cur_t and lat_t and cur_t < lat_t:
        output.detail(f"当前: {cur}")
        output.info(output.hilite(
            f"  最新: {latest} —— 有新版，跑 `codesync --update` 升级", "yellow"))
    else:
        # cur >= latest — incl. the case where this machine is AHEAD of a stale
        # 12h cache (just released) or a source build. Don't print a "最新"
        # number that's ≤ current: showing "最新: 2.9.0" while on 2.10.0 reads as
        # if the latest is behind you. One reassuring line instead.
        output.detail(f"当前: {cur}（已是最新）")


def enforce_up_to_date(uc, *, skip: bool) -> bool:
    """Gate for destructive sync. Returns True to proceed, False to abort.
    Never raises. `uc` is a config.UpdateConfig (or None → defaults); `skip` is
    the --skip-version-check flag."""
    if uc is not None and not uc.check:
        return True  # version checking disabled in config
    cur = __version__
    if cur.startswith("0.0.0"):
        return True  # source checkout / not pip-installed → don't gate developers

    ttl = uc.ttl_hours if uc is not None else 12
    latest = latest_version(ttl_hours=ttl)
    if not latest:
        return True  # fail open: couldn't determine the latest version

    cur_t, lat_t = _parse_version(cur), _parse_version(latest)
    if not cur_t or not lat_t or cur_t >= lat_t:
        return True  # up to date (or unparseable → fail open)

    # We are confidently behind.
    if skip:
        output.warn(
            f"⚠ codesync 已过期（本机 {cur} < 最新 {latest}），"
            "--skip-version-check 已跳过拦截，继续运行（风险自负）"
        )
        return True
    block = (uc is None) or uc.block_if_outdated
    if not block:
        output.warn(f"⚠ codesync 有新版（本机 {cur} < 最新 {latest}），建议 `codesync --update`")
        return True

    output.err(f"codesync 已过期：本机 {cur} < 最新 {latest}")
    output.err("多机版本不一致时跑破坏性操作（push / 归档 / 删本地）有风险 — 已拦截。")
    output.err("升级：    codesync --update")
    output.err("仍要用当前版本跑：在 sync 后加 --skip-version-check（风险自负）")
    return False
