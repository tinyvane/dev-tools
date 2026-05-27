from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from codesync import auth, output, paths
from codesync.config import AutoCloneConfig


# ---------- state file ----------

def _read_known() -> list[str] | None:
    f = paths.known_repos_file()
    if not f.exists():
        return None
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        return list(obj.get("Known") or [])
    except (json.JSONDecodeError, OSError):
        output.warn(f"状态文件 {f} 损坏，按首次运行处理")
        return None


def _save_known(names: list[str]) -> None:
    f = paths.known_repos_file()
    paths.ensure_config_dir()
    f.write_text(
        json.dumps(
            {"Known": sorted(set(names)), "UpdatedAt": datetime.now(timezone.utc).isoformat()},
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------- local repo scanning ----------

_GH_URL_RE = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$")


def _local_repos_by_owner(roots: list[Path], owner: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if not (entry / ".git").exists():
                continue
            r = subprocess.run(
                ["git", "-C", str(entry), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                continue
            m = _GH_URL_RE.search(r.stdout.strip())
            if not m:
                continue
            if m.group(1) == owner:
                found[m.group(2)] = entry
    return found


# ---------- gh interactions ----------

def _gh_repo_list(owner: str) -> list[dict]:
    r = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", "200",
         "--json", "name,isFork,isArchived,sshUrl,owner"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        output.warn(f"gh repo list 失败 (exit {r.returncode})，跳过")
        if r.stderr:
            output.detail(r.stderr.strip())
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        output.warn("gh repo list 返回非法 JSON，跳过")
        return []


def _gh_repo_archive(owner: str, name: str) -> bool:
    r = subprocess.run(["gh", "repo", "archive", f"{owner}/{name}", "--yes"])
    return r.returncode == 0


# ---------- main entry ----------

def run(ac: AutoCloneConfig, code_roots: list[Path], *, push: bool) -> None:
    output.section("GitHub repo 自动同步")

    if not auth.ensure_gh_authenticated():
        output.detail("跳过 GitHub repo 同步")
        return

    parsed = _gh_repo_list(ac.owner)
    if not parsed:
        return

    all_owned = [r for r in parsed if r.get("owner", {}).get("login") == ac.owner]
    # Independently of the exclusion logic below, keep a set of all forks-you-own —
    # used after clone (v2.2.9+) to auto-configure the `upstream` remote.
    all_forks: set[str] = {r["name"] for r in all_owned if r.get("isFork")}
    # ac.include_forks (default True, v2.2.8+) controls whether forks-you-own are
    # treated as auto_clone-managed repos:
    #   True  → forks behave just like own repos (cloned, tracked, archived on
    #           local-delete --push)
    #   False → forks excluded entirely (pre-v2.2.8 behavior; useful when you fork
    #           upstream just to read code and don't want clutter locally)
    # Archived repos are always skipped from active regardless of include_forks.
    if ac.include_forks:
        fork_set: set[str] = set()
        active = {r["name"]: r["sshUrl"]
                  for r in all_owned if not r.get("isArchived")}
    else:
        fork_set = {r["name"] for r in all_owned if r.get("isFork")}
        active = {r["name"]: r["sshUrl"]
                  for r in all_owned if not r.get("isFork") and not r.get("isArchived")}

    local_owned = _local_repos_by_owner(code_roots, ac.owner)
    skip = set(ac.skip)
    local_managed = {n: p for n, p in local_owned.items()
                     if n not in fork_set and n not in skip}
    active_managed = {n: url for n, url in active.items() if n not in skip}

    known = _read_known()
    first_run = known is None
    known_set = set(known) if known else set()

    to_clone: list[str] = []
    to_rm_local: list[str] = []
    to_archive: list[str] = []

    if first_run:
        output.detail("首次运行（无 state 文件），建立 baseline，不做破坏性操作")
        to_clone = [n for n in active_managed if n not in local_managed]
    else:
        if len(known_set) > 0:
            shrink = (len(known_set) - len(active_managed)) * 100.0 / len(known_set)
            if shrink > ac.abort_if_shrink_pct:
                output.err(
                    f"GitHub 列表骤减 {shrink:.1f}%（>{ac.abort_if_shrink_pct}%），可能 API 异常，abort"
                )
                raise SystemExit(
                    f"GitHub 列表骤减保护触发（known={len(known_set)}, active={len(active_managed)}）"
                )
        to_clone = [n for n in active_managed
                    if n not in known_set and n not in local_managed]
        to_rm_local = [n for n in known_set
                       if n in local_managed and n not in active_managed]
        if push:
            to_archive = [n for n in known_set
                          if n in active_managed and n not in local_managed]

    # confirm destructive
    destructive = len(to_rm_local) + len(to_archive)
    if destructive > 0:
        print()
        if to_archive:
            output.warn(f"即将归档 GitHub 上 {len(to_archive)} 个 repo（本地已删除）:")
            for n in to_archive:
                output.detail(f"  - {n}")
        if to_rm_local:
            output.warn(f"即将删除本地 {len(to_rm_local)} 个 repo（GitHub 已 archive）:")
            for n in to_rm_local:
                output.detail(f"  - {n}")
        print()
        if not ac.skip_confirmation:
            output.info("  5 秒后执行（Ctrl+C 取消）...")
            try:
                for i in range(5, 0, -1):
                    output.detail(f"{i}...")
                    time.sleep(1)
            except KeyboardInterrupt:
                output.info("已取消")
                return

    # clone
    if to_clone:
        output.detail(f"clone 缺失的 {len(to_clone)} 个 repo:")
        target = Path(paths.expand(ac.target))
        target.mkdir(parents=True, exist_ok=True)
        # Lazy import: fork_setup imports auth which is fine, but keeping it lazy
        # mirrors the rest of this module and avoids cycles if structure shifts.
        from codesync.fork_setup import add_upstream_for_fork
        for name in to_clone:
            url = active_managed[name]
            dest = target / name
            if dest.exists():
                output.warn(f"[{name}] 目标路径已存在，跳过")
                continue
            output.detail(f"[{name}] clone -> {dest}")
            r = subprocess.run(["git", "clone", url, str(dest)])
            if r.returncode != 0:
                output.warn(f"[{name}] git clone 失败")
                continue
            # v2.2.9+: for fresh clone of a fork, auto-configure `upstream` so the
            # user's "fetch from upstream + cherry-pick" workflow is ready out of
            # the box. Best-effort; failure here just logs a warning (user can
            # run `codesync fork-setup` later or add manually).
            if name in all_forks:
                ok, msg = add_upstream_for_fork(dest, ac.owner, name)
                if ok:
                    output.detail(f"[{name}] upstream → {msg}")
                else:
                    output.warn(f"[{name}] upstream 未配置: {msg}（可后续 `codesync fork-setup` 补）")

    # rm local
    if to_rm_local:
        output.detail("删除本地已归档的 repo:")
        for name in to_rm_local:
            path = local_managed[name]
            output.detail(f"[{name}] rm -rf {path}")
            shutil.rmtree(path, ignore_errors=True)

    # archive remote (push mode only)
    if to_archive:
        output.detail("归档 GitHub 上的 repo:")
        for name in to_archive:
            output.detail(f"[{name}] gh repo archive {ac.owner}/{name}")
            _gh_repo_archive(ac.owner, name)

    # update state
    final_local = _local_repos_by_owner(code_roots, ac.owner)
    final_local_managed = [n for n in final_local
                           if n not in fork_set and n not in skip]
    if to_archive:
        # GitHub side may have changed; re-fetch
        parsed2 = _gh_repo_list(ac.owner)
        all_owned2 = [r for r in parsed2 if r.get("owner", {}).get("login") == ac.owner]
        active_managed = {
            r["name"]: True for r in all_owned2
            if not r.get("isFork") and not r.get("isArchived") and r["name"] not in skip
        }

    new_known = sorted(set(list(active_managed.keys()) + final_local_managed))
    _save_known(new_known)
    output.detail(f"state 已更新（known={len(new_known)}）")
