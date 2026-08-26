from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from codesync import auth, git_ops, output, paths, proc, state as state_mod, trash as trash_mod
from codesync.config import AutoCloneConfig
from codesync.remote_url import parse_github_remote


# ---------- local repo scanning ----------

def _local_repos_by_owner(
    roots: list[Path], owner: str, *, max_workers: int = 1,
) -> tuple[dict[str, Path], bool]:
    entries: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if (entry.is_dir() and (entry / ".git").exists()
                    and git_ops.is_corrupt_repo(entry) is None):
                entries.append(entry)

    def origin_of(entry: Path) -> tuple[Path, git_ops.OriginUrlResult]:
        return entry, git_ops.read_origin_url(entry)

    found: dict[str, Path] = {}
    degraded = False
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        # map yields in input order, preserving the serial scan's deterministic
        # last-write-wins result when duplicate GitHub repo names are present.
        for entry, result in ex.map(origin_of, entries):
            if not result.certain:
                degraded = True
                continue
            if not result.url:
                continue
            parsed = parse_github_remote(result.url)
            if parsed is None:
                continue
            # GitHub logins are case-insensitive — an origin URL with odd casing
            # must not make the repo invisible to the scan.
            if parsed.owner.lower() == owner.lower():
                found[parsed.name] = entry
    return found, degraded


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
    r = proc.run(
        ["gh", "repo", "list", owner, "--limit", _GH_REPO_LIST_LIMIT,
         "--json", "id,name,isFork,isArchived,sshUrl,owner"],
        timeout=proc.T_NET_LONG,
    )
    if r.returncode != 0 or not r.stdout.strip():
        if proc.timed_out(r):
            output.warn(
                f"gh repo list 超时（>{proc.T_NET_LONG}s），本轮跳过所有 GitHub 操作："
                "不 clone、不归档、不移动本地目录"
            )
            return []
        output.warn(f"gh repo list 失败 (exit {r.returncode})，跳过")
        if r.stderr:
            output.detail(r.stderr.strip())
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        output.warn("gh repo list 返回非法 JSON，跳过")
        return []


def _validate_roots(code_roots: list[Path]) -> None:
    """A missing/unreadable root can look exactly like a deliberate bulk delete."""
    for root in code_roots:
        if not root.is_dir():
            raise SystemExit(f"code_root 不存在或不可读，停止 GitHub/垃圾箱操作: {root}")
        try:
            list(root.iterdir())
        except OSError as exc:
            raise SystemExit(f"code_root 扫描失败，停止 GitHub/垃圾箱操作: {root}: {exc}")


def _apply_remote_trash_signals(
    parsed: list[dict], local_owned: dict[str, Path], owner: str, sync_state: dict,
    skip: set[str],
) -> list[str]:
    """Move old local copies before clone logic can see a new same-name repo."""
    moved: list[str] = []
    repos_state = sync_state["Repositories"]
    trash_state = sync_state["Trash"]
    local_fold = {n.casefold(): (n, p) for n, p in local_owned.items()}
    skip_fold = {n.casefold() for n in skip}
    active_ids_by_name: dict[str, set[str]] = {}
    for item in parsed:
        if not item.get("isArchived"):
            active_ids_by_name.setdefault(str(item.get("name", "")).casefold(), set()).add(
                str(item.get("id", ""))
            )

    for remote in parsed:
        remote_owner = str(remote.get("owner", {}).get("login", ""))
        if remote_owner.casefold() != owner.casefold() or not remote.get("isArchived"):
            continue
        repo_id = str(remote.get("id") or "")
        remote_name = str(remote.get("name") or "")
        if not repo_id or not remote_name:
            continue
        previous = repos_state.get(repo_id, {})
        original = str(previous.get("name") or trash_mod.parse_original_name(remote_name) or remote_name)
        if original.casefold() in skip_fold:
            continue
        candidate: Path | None = None
        previous_path = previous.get("path")
        if previous_path and Path(str(previous_path)).is_dir():
            candidate = Path(str(previous_path))
        elif original.casefold() in local_fold:
            candidate = local_fold[original.casefold()][1]
        if candidate is None:
            continue

        # If a new active repo already reused the old name, an origin URL alone
        # cannot prove which immutable ID is checked out. Refuse an ambiguous move.
        same_name_active = active_ids_by_name.get(original.casefold(), set()) - {repo_id}
        if same_name_active and not previous:
            output.warn(f"[{original}] 同名新旧 Repository ID 并存但本机缺少旧 ID baseline，暂不移动；先人工核对。")
            continue

        record = {
            "repo_id": repo_id,
            "owner": remote_owner,
            "original_name": original,
            "original_path": str(candidate.resolve()),
            "remote_name": remote_name,
            "trashed_at": remote.get("archivedAt") or _now_iso(),
        }
        ok, dest, msg = trash_mod.move_local_to_trash(candidate, record)
        if not ok or dest is None:
            output.warn(f"[{original}] 垃圾箱移动失败，将在下次 sync 重试: {msg}")
            continue
        record["local_path"] = str(dest)
        trash_state[repo_id] = record
        repos_state.pop(repo_id, None)
        sync_state["Tombstones"][repo_id] = record["trashed_at"]
        sync_state["Known"] = [n for n in sync_state["Known"]
                               if str(n).casefold() != original.casefold()]
        moved.append(original)
        output.good(f"[{original}] 已按 GitHub 垃圾箱信号移动到 {dest}")
    return moved


def _apply_remote_restore_signals(parsed: list[dict], sync_state: dict, skip: set[str]) -> list[str]:
    """Restore local trash when the same immutable ID is active under its old name."""
    restored: list[str] = []
    active_by_id = {str(r.get("id")): r for r in parsed if r.get("id") and not r.get("isArchived")}
    skip_fold = {n.casefold() for n in skip}
    for repo_id, record in list(sync_state["Trash"].items()):
        remote = active_by_id.get(repo_id)
        if remote is None:
            continue
        original = str(record.get("original_name") or "")
        if original.casefold() in skip_fold:
            continue
        if str(remote.get("name", "")).casefold() != original.casefold():
            continue
        ok, path, msg = trash_mod.restore_local_record(record)
        if not ok or path is None:
            output.warn(f"[{original}] GitHub 已恢复，但本地垃圾箱恢复失败: {msg}")
            continue
        sync_state["Trash"].pop(repo_id, None)
        sync_state["Tombstones"].pop(repo_id, None)
        sync_state["Repositories"][repo_id] = {
            "name": original,
            "path": str(path),
            "owner": str(remote.get("owner", {}).get("login", "")),
        }
        if not any(str(name).casefold() == original.casefold()
                   for name in sync_state["Known"]):
            sync_state["Known"].append(original)
        restored.append(original)
        output.good(f"[{original}] 已按 GitHub 恢复信号移回 {path}")
    return restored


# ---------- main entry ----------

def run(ac: AutoCloneConfig, code_roots: list[Path], *, push: bool,
        auto_migrate: bool = True, claude_projects: Path | None = None,
        local_workers: int = 1) -> list[tuple[str, str]]:
    """Returns the list of (old, new) renames auto-migrated from other machines
    (empty unless another machine renamed a repo and `auto_migrate` is on)."""
    output.section("GitHub repo 自动同步")

    if not auth.ensure_gh_authenticated():
        output.detail("跳过 GitHub repo 同步")
        return []

    _validate_roots(code_roots)
    state_existed = paths.known_repos_file().exists()
    try:
        sync_state = state_mod.load_state()
    except ValueError as exc:
        raise SystemExit(f"{exc}；状态不可信，停止所有 GitHub/垃圾箱操作") from exc

    parsed = _gh_repo_list(ac.owner)
    if not parsed:
        return []

    all_owned = [r for r in parsed
                 if str(r.get("owner", {}).get("login", "")).casefold() == ac.owner.casefold()]
    all_owned_by_name = {str(r.get("name", "")).casefold(): r for r in all_owned}
    skip = set(ac.skip)
    signal_owned = [r for r in all_owned if ac.include_forks or not r.get("isFork")]
    restored_from_trash = _apply_remote_restore_signals(signal_owned, sync_state, skip)
    if restored_from_trash:
        def persist_restores(current: dict) -> None:
            for repo_id, record in sync_state["Repositories"].items():
                if record.get("name") in restored_from_trash:
                    current["Repositories"][repo_id] = record
                    current["Trash"].pop(repo_id, None)
                    current["Tombstones"].pop(repo_id, None)
            current["Known"] = sorted(set(current["Known"]) | set(restored_from_trash), key=str.casefold)
        state_mod.update_state(persist_restores)
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

    local_owned, degraded = _local_repos_by_owner(
        code_roots, ac.owner, max_workers=local_workers,
    )
    degraded_warned = False

    def warn_degraded() -> None:
        nonlocal degraded_warned
        if degraded and not degraded_warned:
            output.warn(
                "本地 origin 扫描退化（超时或 git 不可用）：本轮禁止归档、"
                "垃圾箱本地移动和自动改名，known 只增不减；clone 仍可继续。"
            )
            degraded_warned = True

    warn_degraded()

    # Trash moves must precede rename migration and clone. A newly-created repo
    # may already reuse the old name; immutable Repository IDs disambiguate it.
    moved_to_trash = [] if degraded else _apply_remote_trash_signals(
        signal_owned, local_owned, ac.owner, sync_state, skip,
    )
    if moved_to_trash:
        moved_ids = set(sync_state["Trash"])
        def persist_moves(current: dict) -> None:
            current["Trash"].update(sync_state["Trash"])
            current["Tombstones"].update(sync_state["Tombstones"])
            for repo_id in moved_ids:
                current["Repositories"].pop(repo_id, None)
            moved_fold = {n.casefold() for n in moved_to_trash}
            current["Known"] = [n for n in current["Known"]
                                if str(n).casefold() not in moved_fold]
        state_mod.update_state(persist_moves)
        local_owned, scan_degraded = _local_repos_by_owner(
            code_roots, ac.owner, max_workers=local_workers,
        )
        degraded = degraded or scan_degraded
        warn_degraded()

    # v2.5.0: pick up repos renamed on ANOTHER machine before computing the
    # clone/delete sets. A rename shows up here as "origin name gone from GitHub,
    # new name appears" — which the naive logic below would read as
    # delete-local + clone-fresh (losing local uncommitted work). Migrating first
    # (mv dir + origin set-url) then re-scanning makes the repo look in-sync.
    migrations: list[tuple[str, str]] = []
    if auto_migrate and not degraded:
        from codesync import rename as rename_mod
        migrations = rename_mod.detect_and_migrate(
            local_owned, active, ac.owner, claude_projects=claude_projects,
        )
        if migrations:
            local_owned, scan_degraded = _local_repos_by_owner(
                code_roots, ac.owner, max_workers=local_workers,
            )
            degraded = degraded or scan_degraded
            warn_degraded()

    local_managed = {n: p for n, p in local_owned.items()
                     if n not in fork_set and n not in skip}
    active_managed = {n: url for n, url in active.items() if n not in skip}

    known = list(sync_state.get("Known") or [])
    tombstones = dict(sync_state.get("Tombstones") or {})
    first_run = not state_existed
    known_set = set(known) if known else set()

    # GitHub repo names are case-insensitive-unique. Compare every membership
    # case-folded — otherwise an origin URL whose casing differs from the
    # canonical name reads as "this name deleted locally" + "that name new on
    # GitHub", i.e. a delete + re-clone of the same repo (the flap).
    local_fold = {n.lower() for n in local_managed}
    known_fold = {n.lower() for n in known_set}
    active_fold = {n.lower() for n in active_managed}
    tomb_ids = {str(key) for key in tombstones if str(key)}
    active_canon = {n.lower(): n for n in active_managed}

    def _remote_id(name: str) -> str:
        # Tombstones record intent, not a destructive safety boundary. Missing
        # remote IDs therefore fail open: silently omitting a local repo is
        # harder to detect than cloning one extra copy.
        return str((all_owned_by_name.get(name.casefold()) or {}).get("id") or "")

    to_clone: list[str] = []
    tomb_blocked: list[str] = []
    held_rm: list[tuple[str, str]] = []
    to_archive: list[str] = []
    missing_for_archive: list[str] = []

    if first_run:
        output.detail("首次运行（无 state 文件），建立 baseline，不做破坏性操作")
        to_clone = [n for n in active_managed
                    if n.lower() not in local_fold
                    and _remote_id(n) not in tomb_ids
                    and not n.startswith(trash_mod.REMOTE_TRASH_PREFIX)]
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
                    and _remote_id(n) not in tomb_ids
                    and not n.startswith(trash_mod.REMOTE_TRASH_PREFIX)]
        # A tombstoned repo reappearing in active (most likely unarchived on the
        # web) is NOT auto-resurrected — the deletion intent stays until the
        # user restores it by cloning it back manually.
        tomb_blocked = [n for n in active_managed
                        if _remote_id(n) in tomb_ids and n.lower() not in local_fold]
        # Absence from the list is NOT a delete signal: transfer, permission
        # changes and partial API data are indistinguishable. Only an explicit
        # isArchived record is acted on by _apply_remote_trash_signals above.
        held_rm = [(n, "GitHub 列表中消失但未看到明确 archive 信号")
                   for n in known_set
                   if n in local_managed and n.lower() not in active_fold]
        def truly_missing(name: str) -> bool:
            previous = next((r for r in sync_state["Repositories"].values()
                             if str(r.get("name", "")).casefold() == name.casefold()), None)
            if previous and Path(str(previous.get("path", ""))).exists():
                return False
            return not any((root / name).exists() for root in code_roots)

        if not degraded:
            missing_for_archive = [n for n in known_set
                                   if n.lower() in active_fold
                                   and n.lower() not in local_fold
                                   and truly_missing(n)]
        if push and not degraded:
            to_archive = list(missing_for_archive)
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
        output.warn(f"{len(held_rm)} 个 repo 在 GitHub active 列表中消失，但没有可信垃圾箱信号，保持本地不动：")
        for n, why in held_rm:
            output.detail(f"  - {n}（{why}）: {local_managed[n]}")
        output.detail("  可能是转移、权限变化或列表异常；请人工核对，不会自动删除。")

    # Tombstoned repos that reappeared on GitHub — visible, but never auto-cloned.
    if tomb_blocked:
        output.warn(f"{len(tomb_blocked)} 个曾被删除的 repo 又出现在 GitHub 上（可能被 unarchive），不自动 clone：")
        for n in tomb_blocked:
            output.detail(f"  - {n} —— 想恢复就手动 clone 回 code_roots，下次 sync 自动解除标记")

    # confirm destructive
    destructive = len(to_archive)
    if destructive > 0:
        print()
        if to_archive:
            output.warn(f"即将归档 GitHub 上 {len(to_archive)} 个 repo（本地已删除）:")
            for n in to_archive:
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
                cur = git_ops.origin_url(dest) or ""
                if cur:
                    output.warn(f"[{name}] 目标路径已存在但 origin 指向别处（{cur}）"
                                f"—— 不覆盖；请手动核对内容后改 origin 或改目录名")
                else:
                    output.warn(f"[{name}] 目标路径已存在（非 git repo 或无 origin），跳过")
                continue
            output.detail(f"[{name}] clone -> {dest}")
            r = proc.run(
                ["git", "clone", url, str(dest)],
                timeout=proc.T_NET_CLONE,
                capture=False,
            )
            if r.returncode != 0:
                if proc.timed_out(r):
                    output.warn(
                        f"[{name}] git clone 超时（>{proc.T_NET_CLONE}s）；"
                        f"半成品目录可能保留在 {dest}，请人工核对"
                    )
                else:
                    output.warn(f"[{name}] git clone 失败: {(r.stderr or '').strip()}")
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

    # Move a locally-deleted repo to GitHub trash (push mode only). The local
    # directory is already gone; GitHub retains the complete pushed history.
    deferred_archive: set[str] = set(missing_for_archive)
    if to_archive:
        output.detail("把 GitHub repo 移入垃圾箱:")
        for name in to_archive:
            canon = active_canon[name.lower()]
            remote = all_owned_by_name.get(canon.casefold())
            if remote and remote.get("isArchived"):
                remote = None
            if remote is None:
                deferred_archive.add(name)
                continue
            ident = trash_mod.RepoIdentity(
                repo_id=str(remote.get("id") or ""), owner=ac.owner, name=canon,
            )
            output.detail(f"[{canon}] rename zz-trash + archive")
            ok, record, msg = trash_mod.trash_remote(ident)
            if not ok or record is None:
                output.warn(f"[{canon}] GitHub 垃圾箱操作失败，将在下次 sync 重试: {msg}")
                deferred_archive.add(name)
                continue
            sync_state["Trash"][ident.repo_id] = record
            sync_state["Tombstones"][ident.repo_id] = record["trashed_at"]
            sync_state["Repositories"].pop(ident.repo_id, None)
            sync_state["PendingArchives"].pop(ident.repo_id, None)
            deferred_archive.discard(name)

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
    final_local, final_scan_degraded = _local_repos_by_owner(
        code_roots, ac.owner, max_workers=local_workers,
    )
    degraded = degraded or final_scan_degraded
    warn_degraded()
    final_local_managed = [n for n in final_local
                           if n not in fork_set and n not in skip]
    # A deferred local deletion stays known, otherwise a --no-push run or one
    # transient archive failure would erase the intent and re-clone it next time.
    if degraded:
        new_known = sorted(set(known_set) | set(final_local_managed), key=str.casefold)
    else:
        new_known = sorted(set(final_local_managed) | deferred_archive, key=str.casefold)
    final_records: dict[str, dict] = {}
    for local_name, local_path in final_local.items():
        if local_name not in final_local_managed:
            continue
        remote = all_owned_by_name.get(local_name.casefold())
        if remote and remote.get("id"):
            repo_id = str(remote["id"])
            final_records[repo_id] = {
                "name": local_name,
                "path": str(local_path.resolve()),
                "owner": ac.owner,
            }
    pending: dict[str, dict] = {}
    for name in deferred_archive:
        remote = all_owned_by_name.get(name.casefold())
        if remote and remote.get("isArchived"):
            remote = None
        if remote and remote.get("id"):
            pending[str(remote["id"])] = {"name": name, "since": _now_iso()}

    def persist_final(current: dict) -> None:
        current["Known"] = new_known
        current["Repositories"].update(final_records)
        current["Trash"].update(sync_state["Trash"])
        current["Tombstones"].update(sync_state["Tombstones"])
        for repo_id in final_records:
            current["Tombstones"].pop(repo_id, None)
        current["PendingArchives"] = pending
        for repo_id in current["Trash"]:
            current["Repositories"].pop(repo_id, None)
    saved = state_mod.update_state(persist_final)
    extra = f", trash={len(saved['Trash'])}, pending={len(pending)}"
    output.detail(f"state 已更新（known={len(new_known)}{extra}）")
    return migrations
