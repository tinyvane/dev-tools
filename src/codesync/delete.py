"""`codesync delete`: move a repository into local and GitHub trash."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from codesync import git_ops, output, state, trash
from codesync.rename import (
    _ORIGIN_UNAVAILABLE,
    _find_in_roots,
    _is_git_repo,
    _origin_url,
    _parse_remote,
)


def _countdown(name: str) -> bool:
    output.info(f"  5 秒后把 {name} 移入本地和 GitHub 垃圾箱（Ctrl+C 取消）...")
    try:
        for i in range(5, 0, -1):
            output.detail(f"    {i}...")
            time.sleep(1)
    except KeyboardInterrupt:
        output.info("已取消。")
        return False
    return True


def _safe_named_repo(name: str, roots: list[Path]) -> tuple[Path | None, str]:
    """Resolve an immediate child only; never allow traversal or absolute paths."""
    raw = Path(name)
    if not name or raw.is_absolute() or raw.name != name or name in {".", ".."}:
        return None, f"非法 repo 名称（只能是 code_root 下的一层目录名）: {name!r}"
    matches = _find_in_roots(name, roots)
    safe: list[Path] = []
    for match in matches:
        try:
            resolved = match.resolve(strict=True)
            root = match.parent.resolve(strict=True)
        except OSError:
            continue
        if resolved.parent == root:
            safe.append(match)
    if not safe:
        return None, f"在 code_roots 下找不到名为 {name} 的目录。"
    if len(safe) > 1:
        return None, "多个 code_root 下存在同名目录，请 cd 进目标 repo 后使用无参数形式。"
    return safe[0], ""


def _push_before_trash(repo: Path) -> tuple[bool, str]:
    """Commit tracked/untracked work and require a successful push before trashing."""
    dirty = git_ops._is_dirty(repo)
    if dirty:
        committed = git_ops.auto_commit_dirty([repo], skip_names=set(), max_workers=1)
        if repo.name not in committed and git_ops._is_dirty(repo):
            return False, "自动 commit 未完成，repo 仍有未提交改动"
    summary = git_ops.parallel_op([repo], "push", max_workers=1)
    if summary.failed:
        return False, summary.failed[0].detail or "git push 失败"
    if git_ops._is_dirty(repo):
        return False, "push 后 repo 仍有未提交改动"
    return True, ""


def delete_repo(name: str | None, *, yes: bool = False,
                local_only: bool = False) -> int:
    """Move a repo to `.codesync-trash` and rename+archive its GitHub repo.

    local_only leaves GitHub completely untouched — but it is NOT simply
    "skip the remote steps", because doing only that is self-defeating. sync
    computes:

        to_clone   = active ∧ ¬known ∧ ¬local ∧ ¬tombstoned
        to_archive = known ∧ active ∧ ¬local

    so a repo removed locally and left in `Known` gets ARCHIVED on GitHub on
    the next sync, and one dropped from `Known` gets RE-CLONED. The third
    state that means "deliberately absent, leave the remote alone" already
    exists: a Tombstone plus removal from Known.

    Tombstones are keyed by GitHub Repository ID, never by name — name-keyed
    tombstones cannot tell a new repo from a deleted one that shared its name,
    which was the root cause of the v2.9-v2.16 incidents. So even --local-only
    still READS GitHub (`gh repo view`, no mutation) to resolve that ID, and
    fails closed if it cannot. A repo with no GitHub origin gets no tombstone
    and is warned about explicitly.
    """
    from codesync.config import load

    cfg = load()
    try:
        state.load_state()
    except ValueError as exc:
        output.err(f"{exc}；停止垃圾箱操作")
        return 1
    if name is None:
        repo = Path.cwd()
        if not repo.is_dir() or not _is_git_repo(repo):
            output.err(f"当前目录不是 git repo: {repo}")
            return 1
    else:
        repo, msg = _safe_named_repo(name, cfg.code_roots_expanded)
        if repo is None:
            output.err(msg)
            return 1

    repo_name = repo.name
    origin = _origin_url(repo) if _is_git_repo(repo) else None
    if origin is _ORIGIN_UNAVAILABLE:
        output.err("无法可靠读取 origin，远端和本地均保留原状。")
        return 1
    parsed = _parse_remote(origin) if origin else None
    is_github = bool(parsed) and parsed[0].casefold() == "github.com"

    output.section(f"移入垃圾箱: {repo_name}")
    output.detail(f"本地: {repo} -> {repo.parent / trash.LOCAL_TRASH_DIR}")

    identity: trash.RepoIdentity | None = None
    if is_github and parsed:
        _, owner, remote_name = parsed
        status, identity, msg = trash.get_remote_identity(owner, remote_name)
        if status != "ok" or identity is None:
            output.err(f"无法确认 GitHub repo 身份，未做任何更改: {msg or status}")
            return 1
        if identity.owner.casefold() != owner.casefold() or identity.name.casefold() != remote_name.casefold():
            output.err(
                f"origin 已重定向到 {identity.owner}/{identity.name}；为防误操作，只能先修正/迁移该目录。"
            )
            return 1
        if local_only:
            output.detail(
                f"GitHub: {owner}/{remote_name} 保持不变"
                f"（--local-only；仅记录 tombstone，防止下次 sync 重新 clone）"
            )
        else:
            output.detail(
                f"GitHub: {owner}/{remote_name} -> {trash.make_remote_trash_name(remote_name, identity.repo_id)} -> archive"
            )
    elif local_only:
        # No Repository ID means no ID-keyed tombstone, and a name-keyed one is
        # forbidden. Proceed, but say plainly that sync will bring it back.
        output.warn(
            "该目录没有可确认的 GitHub origin，无法记录 tombstone；"
            "若它对应的 GitHub repo 仍然存在，下次 sync 会重新 clone 回来。"
        )
    else:
        output.warn("该目录没有可确认的 GitHub origin，只移动到本地垃圾箱。")

    if not yes and not _countdown(repo_name):
        return 1

    if identity is not None:
        ok, msg = _push_before_trash(repo)
        if not ok:
            output.err(f"删除前同步失败，远端和本地均保留原状: {msg}")
            return 1
        if local_only:
            # Remote untouched. remote_name stays the LIVE name, so `trash
            # restore` has nothing to un-rename and nothing to unarchive — it
            # only needs to put the directory back and lift the tombstone.
            record = {
                "repo_id": identity.repo_id,
                "owner": identity.owner,
                "original_name": identity.name,
                "remote_name": identity.name,
                "trashed_at": datetime.now(timezone.utc).isoformat(),
                "local_only": True,
            }
        else:
            ok, record, msg = trash.trash_remote(identity)
            if not ok or record is None:
                output.err(msg)
                return 1
    else:
        record = {
            "repo_id": "",
            "owner": "",
            "original_name": repo_name,
            "remote_name": "",
        }

    record["original_path"] = str(repo.resolve())
    ok, dest, msg = trash.move_local_to_trash(repo, record)
    if not ok or dest is None:
        output.err(f"本地移动失败: {msg}")
        if identity is not None:
            output.warn("GitHub repo 已进入垃圾箱；本地保留原位，下次 sync 会重试移动。")
        return 1

    repo_id = str(record.get("repo_id") or "")
    if repo_id:
        def remember(s: dict) -> None:
            saved = dict(record)
            saved["local_path"] = str(dest)
            s["Trash"][repo_id] = saved
            s["Repositories"].pop(repo_id, None)
            s["PendingArchives"].pop(repo_id, None)
            s["Tombstones"][repo_id] = saved["trashed_at"]
            s["Known"] = [n for n in s["Known"] if str(n).casefold() != repo_name.casefold()]
        try:
            state.update_state(remember)
        except (OSError, ValueError, TimeoutError) as exc:
            output.warn(f"repo 已安全移入垃圾箱，但状态记录失败，下次 sync 会从远端信号恢复: {exc}")

    output.good(f"已移入本地垃圾箱: {dest}")
    if identity is not None and local_only:
        output.good(f"GitHub 未改动：{identity.owner}/{identity.name} 仍然存在且可用")
        output.detail(
            "已按 Repository ID 记录 tombstone —— 本机下次 sync 不会重新 clone 它。"
        )
        output.detail(
            "其他机器不受影响，仍会正常同步该 repo；本机恢复用 "
            f"`codesync trash restore {repo_name}`。"
        )
    elif identity is not None:
        output.good(f"GitHub 已重命名并 archive: {record['remote_name']}")
        output.detail("其他已升级机器下次 sync 会按 Repository ID 移入各自的 .codesync-trash。")
    return 0
