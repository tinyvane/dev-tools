from __future__ import annotations

import json
import re
import subprocess
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
                capture_output=True, encoding="utf-8", errors="replace",
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

# GitHub is the authority for "which repos exist" — `active` below is derived
# straight from this list, and destructive decisions (clone / local-delete /
# archive) hang off it, NOT off the local SyncRepos count. So this list must be
# COMPLETE: `gh repo list` silently truncates at --limit, and a truncated list
# makes real GitHub repos look absent → they fall into ¬active → a known+local
# one would be deleted locally, and the shrink guard won't catch a small
# truncation (e.g. 205 repos, limit 200 → 2.4% shrink, under the 20% default).
# Set the cap far above any personal account's repo count rather than paginate.
_GH_REPO_LIST_LIMIT = "4000"


def _gh_repo_list(owner: str) -> list[dict]:
    r = subprocess.run(
        ["gh", "repo", "list", owner, "--limit", _GH_REPO_LIST_LIMIT,
         "--json", "name,isFork,isArchived,sshUrl,owner"],
        capture_output=True, encoding="utf-8", errors="replace",
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

def run(ac: AutoCloneConfig, code_roots: list[Path], *, push: bool,
        auto_migrate: bool = True, claude_projects: Path | None = None) -> list[tuple[str, str]]:
    """Returns the list of (old, new) renames auto-migrated from other machines
    (empty unless another machine renamed a repo and `auto_migrate` is on)."""
    output.section("GitHub repo 自动同步")

    if not auth.ensure_gh_authenticated():
        output.detail("跳过 GitHub repo 同步")
        return []

    parsed = _gh_repo_list(ac.owner)
    if not parsed:
        return []

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

    # v2.5.0: pick up repos renamed on ANOTHER machine before computing the
    # clone/delete sets. A rename shows up here as "origin name gone from GitHub,
    # new name appears" — which the naive logic below would read as
    # delete-local + clone-fresh (losing local uncommitted work). Migrating first
    # (mv dir + origin set-url) then re-scanning makes the repo look in-sync.
    migrations: list[tuple[str, str]] = []
    if auto_migrate:
        from codesync import rename as rename_mod
        migrations = rename_mod.detect_and_migrate(
            local_owned, active, ac.owner, claude_projects=claude_projects,
        )
        if migrations:
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
            # Symmetric to the GitHub-shrink guard above, but for the LOCAL side.
            # to_archive fires when a known+active repo is missing locally — the
            # intended signal being "user deleted it locally". But if a LARGE
            # fraction of should-be-local repos vanished at once (code_roots
            # misconfigured, unmounted drive, failed scan, or — pre-v2.6.2 — repos
            # that were never cloned but got seeded into `known`), that's almost
            # certainly not a deliberate bulk delete. Abort before archiving
            # anything rather than mirror a phantom deletion to GitHub.
            should_be_local = [n for n in known_set if n in active_managed]
            if should_be_local:
                missing_pct = len(to_archive) * 100.0 / len(should_be_local)
                if missing_pct > ac.abort_if_local_missing_pct:
                    output.err(
                        f"本地缺失 {missing_pct:.0f}% 的应在本地 repo "
                        f"（{len(to_archive)}/{len(should_be_local)} 个扫不到），"
                        f"超过 {ac.abort_if_local_missing_pct}% 阈值 — 可能 code_roots 配错/"
                        f"盘没挂/扫描异常，abort（不归档任何 repo）"
                    )
                    output.detail(
                        "如确属有意批量删除，把 [auto_clone] abort_if_local_missing_pct "
                        "调高（或设 100）再跑"
                    )
                    raise SystemExit(
                        f"批量归档保护触发（missing={len(to_archive)}, "
                        f"should_be_local={len(should_be_local)}）"
                    )

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
                return migrations

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

    # rm local. rmtree_repo, NOT shutil.rmtree(ignore_errors=True): git marks
    # pack objects read-only and Windows refuses to delete them (WinError 5) —
    # ignore_errors silently left a half-deleted repo behind (.git intact →
    # still scanned as "present" next run). Same fix as `codesync delete`.
    if to_rm_local:
        output.detail("删除本地已归档的 repo:")
        from codesync.git_ops import rmtree_repo
        for name in to_rm_local:
            path = local_managed[name]
            output.detail(f"[{name}] rm -rf {path}")
            ok, msg = rmtree_repo(path)
            if not ok:
                output.warn(f"[{name}] 删除失败: {msg}")

    # archive remote (push mode only)
    if to_archive:
        output.detail("归档 GitHub 上的 repo:")
        for name in to_archive:
            output.detail(f"[{name}] gh repo archive {ac.owner}/{name}")
            _gh_repo_archive(ac.owner, name)

    # update state
    #
    # v2.6.2: `known` now records ONLY repos actually present locally after this
    # run — NOT every active GitHub repo. The old seeding (active_managed.keys()
    # ∪ local) was the root cause of the mass-archive incident: a GitHub repo you
    # never cloned on this machine got written into `known`, and the next push
    # run saw it as known+active+not-local and archived it as a "local deletion".
    #
    # Local-only `known` keeps the clone-vs-archive disambiguation correct:
    #   - active, not local, NOT in known  → genuinely new (or never-cloned) → clone
    #   - active, not local, IS in known   → was local last run, now gone → archive
    # A failed/absent clone simply stays out of `known`, so it's retried (cloned)
    # next run instead of being archived. The deliberate-delete case still works:
    # the repo was in `known` from the prior run when it was local.
    final_local = _local_repos_by_owner(code_roots, ac.owner)
    final_local_managed = [n for n in final_local
                           if n not in fork_set and n not in skip]
    new_known = sorted(set(final_local_managed))
    _save_known(new_known)
    output.detail(f"state 已更新（known={len(new_known)}）")
    return migrations
