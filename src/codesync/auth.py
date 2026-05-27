from __future__ import annotations

import shutil
import subprocess

from codesync import output


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_authenticated() -> bool:
    if not gh_available():
        return False
    r = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def gh_username() -> str | None:
    """Returns the currently-active gh user's login, or None if unavailable.
    Uses `gh api user --jq .login` (gh ships its own jq via --jq, no external dep).
    """
    if not gh_available():
        return None
    r = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True,
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

    if gh_authenticated():
        return True

    output.section("GitHub 认证")
    output.info("  首次使用：启动 `gh auth login`（浏览器走 OAuth Device Flow，等价 claude auth login 体验）")
    output.info("  token 存到 gh 的标准位置（~/.config/gh/），下次不再问。")
    output.info("")

    # gh auth login is interactive; let it own stdin/stdout/stderr.
    r = subprocess.run(["gh", "auth", "login", "--web", "--git-protocol", "ssh"])
    if r.returncode != 0:
        output.err("gh auth login 失败或被取消。")
        return False

    return gh_authenticated()
