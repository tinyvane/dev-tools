"""Durable cross-machine state for repository and trash coordination."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codesync import paths


STATE_SCHEMA_VERSION = 2
TRASH_PROTOCOL_VERSION = 1
_LOCK_TIMEOUT_SECONDS = 10.0
_STALE_LOCK_SECONDS = 300.0


def default_state() -> dict:
    return {
        "SchemaVersion": STATE_SCHEMA_VERSION,
        "TrashProtocolVersion": TRASH_PROTOCOL_VERSION,
        "Known": [],
        "Tombstones": {},
        "Repositories": {},
        "Trash": {},
        "PendingArchives": {},
    }


def load_state() -> dict:
    """Load and migrate the legacy Known/Tombstones-only state in memory."""
    f = paths.known_repos_file()
    if not f.exists():
        return default_state()
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        raise ValueError(f"状态文件损坏: {f}")
    if not isinstance(raw, dict):
        raise ValueError(f"状态文件格式错误: {f}")
    try:
        schema = int(raw.get("SchemaVersion", 1))
        protocol = int(raw.get("TrashProtocolVersion", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"状态文件版本字段错误: {f}") from exc
    if schema > STATE_SCHEMA_VERSION or protocol > TRASH_PROTOCOL_VERSION:
        raise ValueError(
            f"状态由更高版本 codesync 写入（schema={schema}, trash_protocol={protocol}）: {f}"
        )

    state = default_state()
    state.update(raw)
    for key, fallback in (
        ("Known", []),
        ("Tombstones", {}),
        ("Repositories", {}),
        ("Trash", {}),
        ("PendingArchives", {}),
    ):
        if not isinstance(state.get(key), type(fallback)):
            state[key] = fallback.copy()
    state["SchemaVersion"] = STATE_SCHEMA_VERSION
    state["TrashProtocolVersion"] = TRASH_PROTOCOL_VERSION
    return state


def _atomic_write(state: dict) -> None:
    paths.ensure_config_dir()
    target = paths.known_repos_file()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _state_lock():
    paths.ensure_config_dir()
    lock = paths.known_repos_file().with_suffix(".lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _STALE_LOCK_SECONDS:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待状态锁超时: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def update_state(mutator: Callable[[dict], None]) -> dict:
    """Atomically read-modify-write state while preserving concurrent fields."""
    with _state_lock():
        state = load_state()
        mutator(state)
        state["SchemaVersion"] = STATE_SCHEMA_VERSION
        state["TrashProtocolVersion"] = TRASH_PROTOCOL_VERSION
        state["UpdatedAt"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(state)
        return state
