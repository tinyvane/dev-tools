"""Local and GitHub trash operations for repositories."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codesync import git_ops, output, proc, state


LOCAL_TRASH_DIR = ".codesync-trash"
REMOTE_TRASH_PREFIX = "zz-trash--v1--"
MANIFEST = ".codesync-trash.json"
_GH_TIMEOUT_SECONDS = proc.T_NET


@dataclass(frozen=True)
class RepoIdentity:
    repo_id: str
    owner: str
    name: str
    is_archived: bool = False


def _gh(args: list[str]) -> tuple[bool, str]:
    result = proc.run(["gh", *args], timeout=_GH_TIMEOUT_SECONDS)
    text = (result.stderr or result.stdout).strip()
    return result.returncode == 0, text


def get_remote_identity(owner: str, name: str) -> tuple[str, RepoIdentity | None, str]:
    """Return (status, identity, error); status is ok/not_found/unavailable."""
    result = proc.run(
        ["gh", "repo", "view", f"{owner}/{name}",
         "--json", "id,name,nameWithOwner,isArchived"],
        timeout=_GH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        low = msg.lower()
        if "not found" in low or "could not resolve" in low:
            return "not_found", None, msg
        return "unavailable", None, msg
    try:
        raw = json.loads(result.stdout)
        full_owner, _ = raw["nameWithOwner"].split("/", 1)
        ident = RepoIdentity(
            repo_id=str(raw["id"]), owner=full_owner, name=str(raw["name"]),
            is_archived=bool(raw.get("isArchived")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return "unavailable", None, f"GitHub 返回无法解析: {exc}"
    return "ok", ident, ""


def make_remote_trash_name(original: str, repo_id: str, *, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(repo_id.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", original).strip("-.") or "repo"
    suffix = f"--{digest}--{safe}"
    return (REMOTE_TRASH_PREFIX + stamp + suffix)[:100].rstrip("-.")


def parse_original_name(remote_name: str) -> str | None:
    if not remote_name.startswith(REMOTE_TRASH_PREFIX):
        return None
    parts = remote_name.split("--", 4)
    return parts[4] if len(parts) == 5 and parts[4] else None


def move_local_to_trash(repo: Path, record: dict) -> tuple[bool, Path | None, str]:
    """Move the complete directory into its root-local hidden trash."""
    source: Path | None = None
    dest: Path | None = None
    try:
        if repo.is_symlink():
            return False, None, f"拒绝自动移动符号链接 repo: {repo}"
        source = repo.resolve(strict=True)
        root = source.parent
        trash_root = root / LOCAL_TRASH_DIR
        trash_root.mkdir(exist_ok=True)
        remote_name = str(record.get("remote_name") or "")
        dirname = remote_name or make_remote_trash_name(source.name, str(record.get("repo_id") or source))
        dest = trash_root / dirname
        if dest.exists():
            return False, None, f"垃圾箱目标已存在: {dest}"
        cwd = Path.cwd().resolve()
        if cwd == source or source in cwd.parents:
            os.chdir(root)
        source.rename(dest)
        manifest = dict(record)
        manifest.setdefault("original_name", source.name)
        manifest.setdefault("original_path", str(source))
        manifest.setdefault("trashed_at", datetime.now(timezone.utc).isoformat())
        (dest / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return True, dest, ""
    except OSError as exc:
        # The directory move and manifest belong to one local transaction. If
        # metadata creation fails, put the complete repo back where it started.
        if source is not None and dest is not None and dest.exists() and not source.exists():
            try:
                dest.rename(source)
            except OSError as rollback_exc:
                return False, dest, f"{exc}；回滚到原路径也失败: {rollback_exc}"
        return False, None, str(exc)


def iter_local_trash(code_roots: list[Path]) -> list[tuple[Path, dict]]:
    entries: list[tuple[Path, dict]] = []
    for root in code_roots:
        trash_root = root / LOCAL_TRASH_DIR
        if not trash_root.is_dir():
            continue
        try:
            children = list(trash_root.iterdir())
        except OSError:
            continue
        for entry in children:
            manifest = entry / MANIFEST
            if not entry.is_dir() or not manifest.is_file():
                continue
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                entries.append((entry, raw))
    return sorted(entries, key=lambda item: str(item[0]).lower())


def _find_trash(name: str, code_roots: list[Path]) -> list[tuple[Path, dict]]:
    needle = name.casefold()
    return [item for item in iter_local_trash(code_roots)
            if needle in {
                item[0].name.casefold(),
                str(item[1].get("original_name", "")).casefold(),
                str(item[1].get("remote_name", "")).casefold(),
            }]


def restore_local_record(record: dict) -> tuple[bool, Path | None, str]:
    source = Path(str(record.get("local_path") or ""))
    target = Path(str(record.get("original_path") or ""))
    if not source.is_dir() or not target.name:
        return False, None, "垃圾箱源目录或原路径无效"
    if target.exists():
        return False, None, f"恢复目标已存在: {target}"
    try:
        manifest = source / MANIFEST
        if manifest.exists():
            manifest.unlink()
        source.rename(target)
    except OSError as exc:
        return False, None, str(exc)
    return True, target, ""


def list_trash(code_roots: list[Path]) -> int:
    entries = iter_local_trash(code_roots)
    output.section("codesync 垃圾箱")
    if not entries:
        output.detail("(空)")
        return 0
    for path, record in entries:
        remote = record.get("remote_name") or "仅本地"
        output.info(f"  {record.get('original_name', path.name)}  ->  {path}")
        output.detail(f"    GitHub: {remote}  ID: {record.get('repo_id', '-')}")
    return 0


def restore_trash(name: str, code_roots: list[Path]) -> int:
    try:
        state.load_state()
    except ValueError as exc:
        output.err(f"{exc}；停止恢复")
        return 1
    matches = _find_trash(name, code_roots)
    if len(matches) != 1:
        output.err("找不到垃圾箱条目。" if not matches else "匹配到多个垃圾箱条目，请使用完整垃圾箱名称。")
        return 1
    source, record = matches[0]
    original = str(record.get("original_name") or "")
    original_path = Path(str(record.get("original_path") or source.parent.parent / original))
    if not original or original_path.exists():
        output.err(f"恢复目标无效或已存在: {original_path}")
        return 1

    owner, remote_name = record.get("owner"), record.get("remote_name")
    # A --local-only delete never touched GitHub: the repo kept its name and was
    # never archived. Identity is still verified below, but there is nothing to
    # unarchive — calling it would fail on a repo that was never archived — and
    # nothing to rename back. All that remains is the directory and the tombstone.
    local_only = bool(record.get("local_only"))
    if owner and remote_name:
        status, ident, msg = get_remote_identity(str(owner), str(remote_name))
        if status != "ok" or ident is None or ident.repo_id != str(record.get("repo_id")):
            output.err(f"无法确认 GitHub 垃圾仓库身份，停止恢复: {msg or status}")
            return 1
        status_old, existing, _ = get_remote_identity(str(owner), original)
        if status_old == "ok" and existing and existing.repo_id != ident.repo_id:
            output.err(f"GitHub 上已存在新的 {owner}/{original}，不能覆盖。")
            return 1
        if not local_only:
            ok, msg = _gh(["repo", "unarchive", f"{owner}/{remote_name}", "--yes"])
            if not ok:
                output.err(f"GitHub unarchive 失败: {msg}")
                return 1
        if not local_only and str(remote_name).casefold() != original.casefold():
            ok, msg = _gh(["repo", "rename", original, "--repo", f"{owner}/{remote_name}", "--yes"])
            if not ok:
                _gh(["repo", "archive", f"{owner}/{remote_name}", "--yes"])
                output.err(f"GitHub 改回原名失败: {msg}")
                return 1

    local_record = dict(record)
    local_record["local_path"] = str(source)
    local_record["original_path"] = str(original_path)
    ok, restored, msg = restore_local_record(local_record)
    if not ok or restored is None:
        output.err(f"本地恢复失败: {msg}")
        return 1

    repo_id = str(record.get("repo_id") or "")
    if repo_id:
        def remember_restore(s: dict) -> None:
            s["Trash"].pop(repo_id, None)
            s["Tombstones"].pop(repo_id, None)
            s["Repositories"][repo_id] = {
                "name": original,
                "path": str(restored),
                "owner": str(owner or ""),
            }
            if not any(str(known).casefold() == original.casefold()
                       for known in s["Known"]):
                s["Known"].append(original)
        state.update_state(remember_restore)
    output.good(f"已恢复: {restored}")
    return 0


def purge_trash(name: str, code_roots: list[Path], *, yes: bool = False) -> int:
    try:
        state.load_state()
    except ValueError as exc:
        output.err(f"{exc}；停止永久清理")
        return 1
    matches = _find_trash(name, code_roots)
    if len(matches) != 1:
        output.err("找不到垃圾箱条目。" if not matches else "匹配到多个垃圾箱条目，请使用完整垃圾箱名称。")
        return 1
    path, record = matches[0]
    original = str(record.get("original_name") or path.name)
    if not yes:
        output.warn(f"永久清理不可恢复。输入 repo 名 {original} 确认：")
        try:
            if input("> ").strip() != original:
                output.info("输入不匹配，已取消。")
                return 1
        except (EOFError, KeyboardInterrupt):
            output.info("已取消。")
            return 1

    owner, remote_name = record.get("owner"), record.get("remote_name")
    if owner and remote_name:
        status, ident, identity_msg = get_remote_identity(str(owner), str(remote_name))
        if (status != "ok" or ident is None
                or ident.repo_id != str(record.get("repo_id"))):
            output.err(f"无法确认 GitHub 垃圾仓库身份，本地垃圾保留: {identity_msg or status}")
            return 1
        ok, msg = _gh(["repo", "delete", f"{owner}/{remote_name}", "--yes"])
        if not ok:
            output.err(f"GitHub 永久删除失败，本地垃圾保留: {msg}")
            return 1
    ok, msg = git_ops.rmtree_repo(path)
    if not ok:
        output.err(f"本地永久清理失败: {msg}")
        return 1
    repo_id = str(record.get("repo_id") or "")
    if repo_id:
        # Purge intentionally keeps the tombstone: the immutable Repository ID
        # was permanently deleted and must not be auto-resurrected. Restore,
        # unlike purge, clears both Trash and Tombstones and backfills live state.
        state.update_state(lambda s: s["Trash"].pop(repo_id, None))
    output.good(f"已永久清理: {original}")
    return 0


def trash_remote(identity: RepoIdentity) -> tuple[bool, dict | None, str]:
    """Rename then archive. Roll the rename back when archive fails."""
    remote_name = make_remote_trash_name(identity.name, identity.repo_id)
    ok, msg = _gh(["repo", "rename", remote_name, "--repo",
                   f"{identity.owner}/{identity.name}", "--yes"])
    if not ok:
        return False, None, f"GitHub 改垃圾箱名称失败: {msg}"
    ok, msg = _gh(["repo", "archive", f"{identity.owner}/{remote_name}", "--yes"])
    if not ok:
        rolled_back, rollback_msg = _gh([
            "repo", "rename", identity.name, "--repo",
            f"{identity.owner}/{remote_name}", "--yes",
        ])
        suffix = "" if rolled_back else f"；回滚名称也失败: {rollback_msg}"
        return False, None, f"GitHub archive 失败: {msg}{suffix}"
    record = {
        "repo_id": identity.repo_id,
        "owner": identity.owner,
        "original_name": identity.name,
        "remote_name": remote_name,
        "trashed_at": datetime.now(timezone.utc).isoformat(),
    }
    return True, record, ""
