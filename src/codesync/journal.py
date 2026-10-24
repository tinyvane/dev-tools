"""Durable deletion signals, independent of the archived repository."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
from datetime import datetime, timezone

from codesync import output, proc, trash


JOURNAL_REPO = "codesync-journal"
JOURNAL_SCHEMA = 1
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class JournalUnavailable(Exception):
    """The journal could not be read or updated with certainty."""


def _request(args: list[str]):
    try:
        result = proc.run(["gh", *args], timeout=proc.T_NET)
    except (OSError, ValueError) as exc:
        raise JournalUnavailable(f"GitHub 请求无法执行: {exc}") from exc
    if proc.timed_out(result):
        raise JournalUnavailable("GitHub 请求超时")
    return result


def _status(result) -> int | None:
    match = _HTTP_STATUS.search(result.stderr or "")
    return int(match[1]) if match else None


def _failure(result) -> JournalUnavailable:
    return JournalUnavailable((result.stderr or result.stdout or "GitHub 请求失败").strip())


def _json(result):
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError, RecursionError) as exc:
        raise JournalUnavailable("GitHub 返回无效 JSON") from exc


class GhBackend:
    def list_paths(self, owner: str) -> list[str]:
        result = _request(["api", f"repos/{owner}/{JOURNAL_REPO}/git/trees/HEAD?recursive=1"])
        if result.returncode != 0:
            if _status(result) in (404, 409):
                return []
            raise _failure(result)
        raw = _json(result)
        if (not isinstance(raw, dict) or raw.get("truncated")
                or not isinstance(raw.get("tree"), list)):
            raise JournalUnavailable("GitHub tree 不完整或格式无效")
        paths = []
        for item in raw["tree"]:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or not isinstance(item.get("type"), str)):
                raise JournalUnavailable("GitHub tree 条目无效")
            if item["type"] == "blob":
                paths.append(item["path"])
        return sorted(paths)

    def read_json(self, owner: str, path: str) -> dict:
        result = _request(["api", f"repos/{owner}/{JOURNAL_REPO}/contents/{path}"])
        if result.returncode != 0:
            raise _failure(result)
        raw = _json(result)
        try:
            content = "".join(raw["content"].split())
            return json.loads(base64.b64decode(content, validate=True).decode("utf-8"))
        except (KeyError, TypeError, AttributeError, ValueError, RecursionError) as exc:
            raise JournalUnavailable("删除日志内容无法解码") from exc

    def create_json(self, owner: str, path: str, payload: dict, message: str) -> bool:
        try:
            content = base64.b64encode((
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")).decode("ascii")
        except (ValueError, TypeError, RecursionError) as exc:
            raise JournalUnavailable("删除日志内容无法编码") from exc
        args = ["api", "-X", "PUT", f"repos/{owner}/{JOURNAL_REPO}/contents/{path}",
                "-f", f"message={message}", "-f", f"content={content}"]
        created_repo = False
        for attempt in range(3):
            result = _request(args)
            if result.returncode == 0:
                return True
            status = _status(result)
            if status == 422:
                return False
            if status == 404 and not created_repo and attempt < 2:
                created = _request([
                    "repo", "create", f"{owner}/{JOURNAL_REPO}", "--private", "--add-readme",
                    "--description", "codesync deletion journal (managed by codesync; do not clone)",
                ])
                if created.returncode != 0:
                    raise _failure(created)
                created_repo = True
                continue
            if status == 409 and attempt < 2:
                continue
            raise _failure(result)
        raise JournalUnavailable("删除日志写入重试耗尽")

    def delete_path(self, owner: str, path: str, message: str) -> None:
        endpoint = f"repos/{owner}/{JOURNAL_REPO}/contents/{path}"
        result = _request(["api", endpoint])
        if result.returncode != 0:
            if _status(result) == 404:
                return
            raise _failure(result)
        raw = _json(result)
        if not isinstance(raw, dict) or not isinstance(raw.get("sha"), str) or not raw["sha"]:
            raise JournalUnavailable("删除日志缺少 blob SHA")
        result = _request(["api", "-X", "DELETE", endpoint,
                           "-f", f"message={message}", "-f", f"sha={raw['sha']}"])
        if result.returncode != 0:
            raise _failure(result)


backend = GhBackend()


def entry_prefix(repo_id: str) -> str:
    return "deletions/" + hashlib.sha256(repo_id.encode("utf-8")).hexdigest()[:16] + "-"


def deletion_path(repo_id: str, deleted_at: str) -> str:
    try:
        when = datetime.fromisoformat(deleted_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        when = datetime.now(timezone.utc)
    return entry_prefix(repo_id) + when.strftime("%Y%m%d-%H%M%S") + ".json"


def read_entry(owner: str, path: str) -> dict | None:
    entry = backend.read_json(owner, path)
    if not isinstance(entry, dict) or type(entry.get("schema")) is not int:
        return None
    if entry["schema"] != JOURNAL_SCHEMA:
        return None
    if any(not isinstance(entry.get(k), str) or not entry[k]
           for k in ("repo_id", "owner", "original_name", "deleted_at")):
        return None
    remote = entry.get("remote_name")
    if not isinstance(remote, str):
        return None
    try:
        if not path.startswith(entry_prefix(entry["repo_id"])):
            return None
    except UnicodeError:
        return None
    original = entry["original_name"]
    if original in (".", "..") or "/" in original or "\\" in original:
        return None
    if remote and (not remote.startswith(trash.REMOTE_TRASH_PREFIX)
                   or re.fullmatch(r"[A-Za-z0-9._-]+", remote) is None):
        return None
    return entry


def publish_deletion(record: dict) -> bool:
    if record.get("local_only") or any(
        not isinstance(record.get(k), str) or not record[k]
        for k in ("repo_id", "owner", "original_name")
    ):
        return False
    try:
        owner, repo_id = record["owner"], record["repo_id"]
        if any(p.startswith(entry_prefix(repo_id)) for p in backend.list_paths(owner)):
            return True
        payload = {
            "schema": JOURNAL_SCHEMA,
            "repo_id": repo_id,
            "owner": owner,
            "original_name": record["original_name"],
            "remote_name": record.get("remote_name") or "",
            # read_entry rejects an empty deleted_at, so never publish one.
            "deleted_at": record.get("trashed_at") or datetime.now(timezone.utc).isoformat(),
            "deleted_by": socket.gethostname(),
        }
        backend.create_json(owner, deletion_path(repo_id, payload["deleted_at"]), payload,
                            f"Record deletion of {record['original_name']}")
        return True
    except (JournalUnavailable, OSError, ValueError, TypeError) as exc:
        output.warn(f"删除日志发布失败: {exc}")
        return False


def forget(owner: str, repo_id: str) -> bool:
    try:
        for path in backend.list_paths(owner):
            if path.startswith(entry_prefix(repo_id)):
                backend.delete_path(owner, path, f"Forget deletion of {repo_id}")
        return True
    except (JournalUnavailable, OSError, ValueError, TypeError) as exc:
        output.warn(f"删除日志清理失败: {exc}")
        return False
