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


def _read_tombstones() -> dict[str, str]:
    """Tombstones: repo name → ISO timestamp of when this machine deleted it on
    the cross-machine delete signal (or archived it / `codesync delete`d it).
    A tombstoned name is never auto-cloned again, even if it reappears in the
    active list (e.g. the user unarchived it on the web) — without this, any
    transient reappearance resurrects a deliberately-deleted repo on every
    machine (the claude-hub delete→re-clone flap). Cleared automatically when
    the repo is found locally again (user manually cloned it back = restore)."""
    f = paths.known_repos_file()
    if not f.exists():
        return {}
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        t = obj.get("Tombstones") or {}
        return dict(t) if isinstance(t, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(names: list[str], tombstones: dict[str, str] | None = None) -> None:
    f = paths.known_repos_file()
    paths.ensure_config_dir()
    f.write_text(
        json.dumps(
            {
                "Known": sorted(set(names)),
                "Tombstones": dict(sorted((tombstones or {}).items())),
                "UpdatedAt": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def add_tombstone(name: str) -> None:
    """Record a deliberate delete (used by `codesync delete`) so a later
    unarchive/reappearance on GitHub doesn't auto-clone the repo back here."""
    known = _read_known() or []
    t = _read_tombstones()
    t[name] = datetime.now(timezone.utc).isoformat()
    _save_state(known, t)


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
            # GitHub logins are case-insensitive — an origin URL with odd casing
            # must not make the repo invisible to the scan.
            if m.group(1).lower() == owner.lower():
                found[m.group(2)] = entry
    return found


# Patchable seams (tests stub these; run() never shells out through them
# unmocked in the suite). dirty/ahead are read-only local git calls.

def _repo_dirty(path: Path) -> bool:
    from codesync import git_ops
    return git_ops._is_dirty(path)


def _repo_ahead(path: Path) -> int:
    from codesync.rename import _ahead_count
    return _ahead_count(path)


def _rmtree(path: Path) -> tuple[bool, str]:
    from codesync.git_ops import rmtree_repo
    return rmtree_repo(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    tombstones = _read_tombstones()
    first_run = known is None
    known_set = set(known) if known else set()

    # GitHub repo names are case-insensitive-unique. Compare every membership
    # case-folded — otherwise an origin URL whose casing differs from the
    # canonical name reads as "this name deleted locally" + "that name new on
    # GitHub", i.e. a delete + re-clone of the same repo (the flap).
    local_fold = {n.lower() for n in local_managed}
    known_fold = {n.lower() for n in known_set}
    active_fold = {n.lower() for n in active_managed}
    tomb_fold = {n.lower() for n in tombstones}
    active_canon = {n.lower(): n for n in active_managed}

    to_clone: list[str] = []
    tomb_blocked: list[str] = []
    to_rm_local: list[str] = []
    held_rm: list[tuple[str, str]] = []
    to_archive: list[str] = []

    if first_run:
        output.detail("首次运行（无 state 文件），建立 baseline，不做破坏性操作")
        to_clone = [n for n in active_managed if n.lower() not in local_fold]
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
                    if n.lower() not in known_fold
                    and n.lower() not in local_fold
                    and n.lower() not in tomb_fold]
        # A tombstoned repo reappearing in active (most likely unarchived on the
        # web) is NOT auto-resurrected — the deletion intent stays until the
        # user restores it by cloning it back manually.
        tomb_blocked = [n for n in active_managed
                        if n.lower() in tomb_fold and n.lower() not in local_fold]
        to_rm_local = [n for n in known_set
                       if n in local_managed and n.lower() not in active_fold]
        # The delete signal must never destroy work that exists ONLY here: a
        # repo with uncommitted or unpushed changes is held back (warned every
        # run until the user resolves it), not deleted.
        if to_rm_local:
            deletable: list[str] = []
            for n in to_rm_local:
                p = local_managed[n]
                why = []
                if _repo_dirty(p):
                    why.append("未提交改动")
                if _repo_ahead(p) > 0:
                    why.append("未推送 commit")
                if why:
                    held_rm.append((n, "、".join(why)))
                else:
                    deletable.append(n)
            to_rm_local = deletable
        if push:
            to_archive = [n for n in known_set
                          if n.lower() in active_fold and n.lower() not in local_fold]
            # Symmetric to the GitHub-shrink guard above, but for the LOCAL side.
            # to_archive fires when a known+active repo is missing locally — the
            # intended signal being "user deleted it locally". But if a LARGE
            # fraction of should-be-local repos vanished at once (code_roots
            # misconfigured, unmounted drive, failed scan, or — pre-v2.6.2 — repos
            # that were never cloned but got seeded into `known`), that's almost
            # certainly not a deliberate bulk delete. Abort before archiving
            # anything rather than mirror a phantom deletion to GitHub.
            should_be_local = [n for n in known_set if n.lower() in active_fold]
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

    # Delete signals held back because local-only work would be lost. Printed
    # outside the countdown — these are NOT acted on, only surfaced (and they
    # re-surface every run until resolved).
    if held_rm:
        output.warn(f"{len(held_rm)} 个 repo 收到删除信号（GitHub 已归档/消失），但本地有改动，先不删：")
        for n, why in held_rm:
            output.detail(f"  - {n}（{why}）: {local_managed[n]}")
        output.detail("  确认不需要后手动删除该目录；想保留就先备份/恢复远端再 push。")

    # Tombstoned repos that reappeared on GitHub — visible, but never auto-cloned.
    if tomb_blocked:
        output.warn(f"{len(tomb_blocked)} 个曾被删除的 repo 又出现在 GitHub 上（可能被 unarchive），不自动 clone：")
        for n in tomb_blocked:
            output.detail(f"  - {n} —— 想恢复就手动 clone 回 code_roots，下次 sync 自动解除标记")

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
                # The dir exists but didn't scan as this repo → its origin points
                # somewhere else (or it's not a git repo). Say WHICH, so the user
                # can fix it instead of seeing this skip forever (the stale-origin
                # folder trap: pulls an old/archived repo, never gets new code).
                r = subprocess.run(
                    ["git", "-C", str(dest), "remote", "get-url", "origin"],
                    capture_output=True, encoding="utf-8", errors="replace",
                )
                cur = r.stdout.strip() if r.returncode == 0 else ""
                if cur:
                    output.warn(f"[{name}] 目标路径已存在但 origin 指向别处（{cur}）"
                                f"—— 不覆盖；请手动核对内容后改 origin 或改目录名")
                else:
                    output.warn(f"[{name}] 目标路径已存在（非 git repo 或无 origin），跳过")
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
        for name in to_rm_local:
            path = local_managed[name]
            output.detail(f"[{name}] rm -rf {path}")
            ok, msg = _rmtree(path)
            if not ok:
                output.warn(f"[{name}] 删除失败: {msg}")
            else:
                # Tombstone: this machine acted on the delete signal — never
                # auto-clone this name back, even if it reappears in active.
                tombstones[name] = _now_iso()

    # archive remote (push mode only)
    if to_archive:
        output.detail("归档 GitHub 上的 repo:")
        for name in to_archive:
            canon = active_canon[name.lower()]
            output.detail(f"[{canon}] gh repo archive {ac.owner}/{canon}")
            if _gh_repo_archive(ac.owner, canon):
                # This machine originated the delete (local folder gone) — pin
                # the intent so a later unarchive doesn't re-clone it here.
                tombstones[canon] = _now_iso()

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
    # A tombstoned repo found locally again = the user restored it (manual
    # clone) — the delete intent is withdrawn, clear the tombstone.
    final_fold = {n.lower() for n in final_local}
    tombstones = {n: ts for n, ts in tombstones.items() if n.lower() not in final_fold}
    _save_state(new_known, tombstones)
    extra = f", tombstones={len(tombstones)}" if tombstones else ""
    output.detail(f"state 已更新（known={len(new_known)}{extra}）")
    return migrations
