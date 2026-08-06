from __future__ import annotations

import shutil
import subprocess

from codesync import output, proc


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_authenticated() -> bool | None:
    if not gh_available():
        return False
    r = proc.run(
        ["gh", "auth", "status"],
        timeout=proc.T_NET,
    )
    if proc.timed_out(r) or r.returncode in {proc.NOTFOUND_RC, proc.OSERR_RC}:
        return None
    return r.returncode == 0


def gh_username() -> str | None:
    """Returns the currently-active gh user's login, or None if unavailable.
    Uses `gh api user --jq .login` (gh ships its own jq via --jq, no external dep).
    """
    if not gh_available():
        return None
    r = proc.run(
        ["gh", "api", "user", "--jq", ".login"],
        timeout=proc.T_NET,
    )
    if r.returncode != 0:
        return None
    login = r.stdout.strip()
    return login or None


def ensure_gh_authenticated() -> bool:
    """Idempotent: if not authed, kick off interactive `gh auth login`.
    Returns True on success, False otherwise.
    """
    if not gh_available():
        output.err("gh CLI 未安装。")
        output.detail("  Mac:     brew install gh")
        output.detail("  Windows: winget install GitHub.cli")
        output.detail("  装好后重试 codesync sync。")
        return False

    authenticated = gh_authenticated()
    if authenticated is True:
        return True
    if authenticated is None:
        output.err("gh auth status 超时或不可用，跳过 GitHub 操作")
        return False

    output.section("GitHub 认证")
    output.info("  首次使用：启动 `gh auth login`（浏览器走 OAuth Device Flow，等价 claude auth login 体验）")
    output.info("  token 存到 gh 的标准位置（~/.config/gh/），下次不再问。")
    output.info("")

    # INTERACTIVE EXCEPTION: gh auth login must own stdin/stdout/stderr and must
    # never receive a timeout or use proc.run.
    r = subprocess.run(["gh", "auth", "login", "--web", "--git-protocol", "ssh"])
    if r.returncode != 0:
        output.err("gh auth login 失败或被取消。")
        return False

    return gh_authenticated() is True
