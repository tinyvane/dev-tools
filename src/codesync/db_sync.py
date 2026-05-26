from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from codesync import output, paths
from codesync.config import DbSyncTarget


def _read_state() -> dict:
    f = paths.db_sync_state_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        output.warn(f"状态文件 {f} 损坏，重置")
        return {}


def _save_state(state: dict) -> None:
    paths.ensure_config_dir()
    paths.db_sync_state_file().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _run_docker_capture_to_file(args: list[str], out_path: Path, password: str) -> int:
    """Run `docker exec ...` and pipe stdout to a file.
    Password goes via MYSQL_PWD env (never on command line).
    """
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    with out_path.open("wb") as f:
        r = subprocess.run(args, stdout=f, stderr=subprocess.DEVNULL, env=env)
    return r.returncode


def _run_docker_stdin_from_file(args: list[str], in_path: Path, password: str) -> int:
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    with in_path.open("rb") as f:
        r = subprocess.run(args, stdin=f, stderr=subprocess.DEVNULL, env=env)
    return r.returncode


# ---------- restore (run on every `codesync sync`) ----------

def restore_all(targets: list[DbSyncTarget], *, push_mode: bool) -> None:
    output.section("DB sync (restore)")
    state = _read_state()
    for t in targets:
        dump = Path(paths.expand(t.dump_file))
        if not dump.exists():
            output.detail(f"[{t.name}] Dropbox 上无 dump，跳过")
            continue
        if not _container_running(t.container):
            output.warn(f"[{t.name}] 容器 {t.container} 未运行，跳过")
            continue

        dump_hash = _file_sha256(dump)
        if dump_hash in (state.get(f"{t.name}.LastRestoredHash"),
                         state.get(f"{t.name}.LastPushedHash")):
            output.detail(f"[{t.name}] 已是最新（与本机最近同步一致），跳过")
            continue

        if push_mode:
            output.err(f"[{t.name}] Dropbox 上有更新（来自另一台 PC），但你正在 --push")
            output.err("如果继续，本机数据会被先覆盖再 dump 推回去（=丢失）")
            output.err("建议：先去掉 --push 跑一次 codesync sync 同步好，再 --push")
            raise SystemExit(f"DB sync conflict: 拒绝在 --push 模式下覆盖本机数据 ({t.name})")

        # backup current state
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = paths.db_sync_backup_dir() / f"{t.name}-{ts}.sql"
        output.detail(f"[{t.name}] 备份当前 DB 到 {backup}")
        _run_docker_capture_to_file(
            ["docker", "exec", "-e", "MYSQL_PWD", t.container,
             "mysqldump", f"-u{t.user}",
             "--single-transaction", "--quick", t.database],
            backup, t.password,
        )

        # restore
        output.info(output.hilite(f"  [{t.name}] 恢复 dump（{dump}）...", "cyan"))
        rc = _run_docker_stdin_from_file(
            ["docker", "exec", "-i", "-e", "MYSQL_PWD", t.container,
             "mysql", f"-u{t.user}", t.database],
            dump, t.password,
        )
        if rc != 0:
            output.err(f"[{t.name}] 恢复失败！备份保留在 {backup}")
            continue

        state[f"{t.name}.LastRestoredHash"] = dump_hash
        state[f"{t.name}.LastRestoredAt"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        output.good(f"[{t.name}] 恢复完成")


# ---------- dump (run only on `codesync sync --push`) ----------

def dump_all(targets: list[DbSyncTarget]) -> None:
    output.section("DB sync (dump)")
    state = _read_state()
    for t in targets:
        if not _container_running(t.container):
            output.warn(f"[{t.name}] 容器 {t.container} 未运行，跳过")
            continue

        dump = Path(paths.expand(t.dump_file))
        dump.parent.mkdir(parents=True, exist_ok=True)

        output.info(output.hilite(f"  [{t.name}] dump 到 {dump}...", "cyan"))
        rc = _run_docker_capture_to_file(
            ["docker", "exec", "-e", "MYSQL_PWD", t.container,
             "mysqldump", f"-u{t.user}",
             "--single-transaction", "--quick", t.database],
            dump, t.password,
        )
        if rc != 0:
            output.err(f"[{t.name}] dump 失败")
            continue

        state[f"{t.name}.LastPushedHash"] = _file_sha256(dump)
        state[f"{t.name}.LastPushedAt"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        size = dump.stat().st_size
        size_str = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
        output.good(f"[{t.name}] 完成（{size_str}）")


# ---------- status ----------

def print_status(targets: list[DbSyncTarget]) -> None:
    output.section("DB sync 状态")
    state = _read_state()
    for t in targets:
        lp = state.get(f"{t.name}.LastPushedAt", "-")
        lr = state.get(f"{t.name}.LastRestoredAt", "-")
        output.detail(f"[{t.name}] LastPushed: {lp}  LastRestored: {lr}")
