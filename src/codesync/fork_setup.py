"""Configure `upstream` remote for any local fork repo that's missing it.

auto_clone (v2.2.8+) clones forks, but only auto-adds `upstream` for repos
newly cloned by THIS run (v2.2.9+ behavior — see github_auto.py). Forks
that were already on disk (manually cloned earlier, or cloned by an older
codesync that didn't set upstream) miss out.

`codesync fork-setup` is the one-shot cleanup: scan all local repos under
code_roots, identify the ones that are forks-owned-by-you, and add the
upstream remote pointing at the parent repo's SSH URL.

It's a one-time runtime tool (like `migrate-config`) — idempotent, safe to
re-run, and explicit (user invokes it; codesync sync doesn't trigger it).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from codesync import auth, output


_REMOTE_LINE = re.compile(r"^(\S+)\s+(\S+)\s+\((fetch|push)\)\s*$")
# Match GitHub HTTPS or SSH origin URLs; capture owner + name (drop .git suffix).
_ORIGIN_OWNER_NAME = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$")


def _gh_get_parent_url(owner: str, name: str) -> str | None:
    """For a fork at owner/name, return the parent repo's SSH URL, or None.

    One gh API call. Used by github_auto on fresh clone and by run_fork_setup
    on backfill. Both paths tolerate None (the user can manually add upstream).
    """
    r = subprocess.run(
        ["gh", "api", f"repos/{owner}/{name}", "--jq", ".parent.ssh_url"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    # gh's --jq prints "null" (the literal string) when parent is absent.
    if not url or url == "null":
        return None
    return url


def _git_remotes(repo: Path) -> dict[str, str]:
    """Return {remote_name: fetch_url} for the repo. Empty if not a git dir."""
    r = subprocess.run(
        ["git", "-C", str(repo), "remote", "-v"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return {}
    remotes: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = _REMOTE_LINE.match(line)
        if m and m.group(3) == "fetch":
            remotes[m.group(1)] = m.group(2)
    return remotes


def _list_user_forks(owner: str) -> set[str]:
    """Names of repos under `owner` that are forks. Uses gh's --fork filter
    for one-call efficiency. High --limit: gh silently truncates at the cap
    (same pitfall fixed in github_auto v2.7.0) — a fork past the cap would
    silently never get its upstream configured."""
    r = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", "4000", "--fork",
         "--json", "name"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return set()
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return set()
    return {item["name"] for item in data if isinstance(item, dict) and "name" in item}


def add_upstream_for_fork(repo: Path, owner: str, name: str) -> tuple[bool, str]:
    """Add `upstream` remote pointing at the fork's parent.

    Returns (success, message). Safe to call when upstream already exists —
    git reports an error and we propagate via message but don't raise.
    """
    parent_url = _gh_get_parent_url(owner, name)
    if not parent_url:
        return False, "上游 URL 拿不到（gh api 失败或 parent 缺失）"
    r = subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "upstream", parent_url],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if r.returncode == 0:
        return True, parent_url
    return False, (r.stderr or r.stdout).strip()


def run_fork_setup() -> int:
    """Entry point for `codesync fork-setup`. Scans local code_roots and
    backfills `upstream` remote on forks that don't have one.
    """
    from codesync import config as cfg_mod
    cfg = cfg_mod.load()

    output.section("Fork upstream 配置")

    if not auth.ensure_gh_authenticated():
        output.err("gh 未认证，无法查 parent URL。")
        return 1

    # Pick owner from auto_clone if configured; otherwise from gh login.
    owner: str | None = None
    if cfg.auto_clone:
        owner = cfg.auto_clone.owner
    if not owner:
        owner = auth.gh_username()
    if not owner:
        output.err("拿不到 GitHub owner（未配 auto_clone 也没法 gh api user）。")
        return 1

    output.detail(f"GitHub owner: {owner}")

    fork_names = _list_user_forks(owner)
    if not fork_names:
        output.warn("gh 上没找到你的 fork（或者 gh repo list --fork 调用失败）。")
        return 0
    output.detail(f"GitHub 上你有 {len(fork_names)} 个 fork")

    configured = 0
    already_has_upstream = 0
    not_a_fork = 0
    not_user_owned = 0
    failed: list[str] = []

    for root in cfg.code_roots_expanded:
        if not root.exists() or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if not (entry / ".git").exists():
                continue

            remotes = _git_remotes(entry)
            if "upstream" in remotes:
                already_has_upstream += 1
                continue

            origin = remotes.get("origin")
            if not origin:
                continue
            m = _ORIGIN_OWNER_NAME.search(origin)
            if not m:
                continue
            origin_owner, origin_name = m.group(1), m.group(2)

            if origin_owner != owner:
                not_user_owned += 1
                continue
            if origin_name not in fork_names:
                not_a_fork += 1
                continue

            ok, msg = add_upstream_for_fork(entry, owner, origin_name)
            if ok:
                output.detail(f"[{origin_name}] upstream → {msg}")
                configured += 1
            else:
                output.warn(f"[{origin_name}] 配置失败: {msg}")
                failed.append(origin_name)

    output.section("总结")
    output.detail(f"  新配 upstream:  {configured}")
    output.detail(f"  已有 upstream:  {already_has_upstream}")
    output.detail(f"  非 fork 跳过:   {not_a_fork}")
    output.detail(f"  非本人 owned:   {not_user_owned}")
    if failed:
        output.warn(f"  失败 ({len(failed)}): {', '.join(failed)}")
    return 0 if not failed else 2
