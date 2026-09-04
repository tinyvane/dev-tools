from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from portablecodex import config, output, paths


Severity = Literal["warning", "error"]
_ROLLOUT_NAME = re.compile(r"^rollout-.+\.jsonl$", re.IGNORECASE)
_STATE_DB_NAME = re.compile(r"^state_(\d+)\.sqlite$", re.IGNORECASE)
_FORBIDDEN_EXACT = {
    "auth.json",
    "config.toml",
    "history.jsonl",
}
_FORBIDDEN_DIRS = {"thread-writer-locks", ".sandbox-secrets"}
_FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".db-journal",
    ".sqlite",
    ".sqlite3",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite-journal",
    ".lock",
    ".lck",
    ".pid",
    ".tmp",
    ".temp",
    ".pem",
    ".key",
)


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    path: str
    relative_path: str
    cwd: str
    created_at: str | None
    size: int
    line_count: int | None


@dataclass
class ContextReport:
    mode: str
    sessions_dir: str
    link_kind: str
    link_target: str | None
    transport_root: str | None
    transport_connected: bool
    files_seen: int = 0
    session_count: int = 0
    total_bytes: int = 0
    project_count: int = 0
    index_db: str | None = None
    index_rows: int | None = None
    indexed_sessions: int | None = None
    missing_index_count: int = 0
    stale_index_count: int = 0
    path_mismatch_count: int = 0
    duplicate_id_count: int = 0
    writer_lock_files: int = 0
    held_writer_locks: int = 0
    unknown_writer_locks: int = 0
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(item.severity == "error" for item in self.diagnostics)

    @property
    def warning_count(self) -> int:
        return sum(item.severity == "warning" for item in self.diagnostics)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["error_count"] = self.error_count
        result["warning_count"] = self.warning_count
        return result


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(paths.expand(configured)) if configured else Path.home() / ".codex"


def _expanded_path(value: str | None, fallback: Path | None = None) -> Path | None:
    if value is None:
        return fallback
    return Path(paths.expand(value))


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        try:
            return bool(checker(path))
        except OSError:
            return False
    if os.name != "nt":
        return False
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT) and not path.is_symlink()


def _link_details(path: Path) -> tuple[str, str | None]:
    if not os.path.lexists(path):
        return "missing", None
    if path.is_symlink():
        try:
            return "symlink", str(path.resolve(strict=True))
        except OSError:
            return "broken-symlink", None
    if _is_junction(path):
        try:
            return "junction", os.path.realpath(path)
        except OSError:
            return "broken-junction", None
    return "directory" if path.is_dir() else "not-directory", None


def _path_key(value: str | Path) -> str:
    raw = os.fspath(value)
    if os.name == "nt" and raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return os.path.normcase(os.path.abspath(os.path.normpath(raw)))


def _forbidden_reason(relative: Path) -> str | None:
    lowered_parts = [part.casefold() for part in relative.parts]
    name = lowered_parts[-1]
    if any(part in _FORBIDDEN_DIRS for part in lowered_parts[:-1]):
        return "machine-local directory"
    if name in _FORBIDDEN_EXACT or name.startswith(".env"):
        return "machine-local configuration or credentials"
    if "conflicted copy" in name or "冲突副本" in name:
        return "Dropbox conflict copy"
    if "credential" in name and name.endswith(".json"):
        return "credential file"
    if name.endswith(_FORBIDDEN_SUFFIXES):
        return "database, lock, temporary, or key file"
    return None


def _valid_rollout_layout(relative: Path) -> bool:
    parts = relative.parts
    if len(parts) != 4:
        return False
    year, month, day, name = parts
    return (
        len(year) == 4
        and year.isdigit()
        and len(month) == 2
        and month.isdigit()
        and len(day) == 2
        and day.isdigit()
        and bool(_ROLLOUT_NAME.fullmatch(name))
    )


def _valid_session_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if value.casefold() == canonical else None


def _scan_rollout(
    path: Path,
    relative: Path,
    *,
    deep: bool,
) -> tuple[SessionRecord | None, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    before = path.stat()
    line_count = 0
    try:
        with path.open("rb") as handle:
            first_line = handle.readline()
            line_count = 1 if first_line else 0
            if not first_line:
                raise ValueError("empty rollout")
            first = json.loads(first_line)
            if not isinstance(first, dict) or first.get("type") != "session_meta":
                raise ValueError("first line is not session_meta")
            payload = first.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("session_meta payload is not an object")
            session_id = _valid_session_id(payload.get("id"))
            if session_id is None:
                raise ValueError("session_meta id is not a canonical UUID")
            cwd = payload.get("cwd")
            if not isinstance(cwd, str) or not cwd.strip():
                raise ValueError("session_meta cwd is missing or empty")
            created_at = payload.get("timestamp")
            if created_at is not None and not isinstance(created_at, str):
                diagnostics.append(Diagnostic(
                    "warning", "invalid_timestamp",
                    "session_meta timestamp is not a string", str(path),
                ))

            if not path.name.casefold().endswith(f"{session_id}.jsonl"):
                diagnostics.append(Diagnostic(
                    "error", "filename_id_mismatch",
                    "rollout filename does not end with its session UUID", str(path),
                ))

            if deep:
                for line_count, raw_line in enumerate(handle, 2):
                    if not raw_line.strip():
                        raise ValueError(f"blank JSONL record on line {line_count}")
                    try:
                        decoded = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"invalid JSON on line {line_count}: {exc.msg}"
                        ) from exc
                    if not isinstance(decoded, dict):
                        raise ValueError(f"record on line {line_count} is not an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        diagnostics.append(Diagnostic(
            "error", "invalid_rollout", str(exc), str(path),
        ))
        return None, diagnostics

    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        diagnostics.append(Diagnostic(
            "warning", "changed_during_scan",
            "rollout changed while it was being inspected", str(path),
        ))

    return SessionRecord(
        session_id=session_id,
        path=str(path),
        relative_path=relative.as_posix(),
        cwd=cwd,
        created_at=created_at if isinstance(created_at, str) else None,
        size=after.st_size,
        line_count=line_count if deep else None,
    ), diagnostics


def scan_sessions(
    sessions_dir: Path,
    *,
    deep: bool,
) -> tuple[list[SessionRecord], int, list[Diagnostic]]:
    records: list[SessionRecord] = []
    diagnostics: list[Diagnostic] = []
    files_seen = 0

    def walk_error(exc: OSError) -> None:
        diagnostics.append(Diagnostic(
            "error", "scan_error", str(exc), getattr(exc, "filename", None),
        ))

    try:
        walker = os.walk(sessions_dir, topdown=True, followlinks=False, onerror=walk_error)
        for root, dirnames, filenames in walker:
            root_path = Path(root)
            safe_dirs: list[str] = []
            for dirname in dirnames:
                child = root_path / dirname
                relative = child.relative_to(sessions_dir)
                reason = _forbidden_reason(relative / "placeholder")
                if reason is not None:
                    diagnostics.append(Diagnostic(
                        "error", "forbidden_directory",
                        f"{reason} must not be inside sessions", str(child),
                    ))
                    continue
                if child.is_symlink() or _is_junction(child):
                    diagnostics.append(Diagnostic(
                        "error", "nested_link",
                        "nested symlink/junction is not allowed in sessions", str(child),
                    ))
                    continue
                safe_dirs.append(dirname)
            dirnames[:] = safe_dirs

            for filename in filenames:
                files_seen += 1
                candidate = root_path / filename
                relative = candidate.relative_to(sessions_dir)
                reason = _forbidden_reason(relative)
                if reason is not None:
                    diagnostics.append(Diagnostic(
                        "error", "forbidden_file",
                        f"{reason} must not synchronize", str(candidate),
                    ))
                    continue
                if not _valid_rollout_layout(relative):
                    diagnostics.append(Diagnostic(
                        "error", "unexpected_file",
                        "only YYYY/MM/DD/rollout-*.jsonl is allowed", str(candidate),
                    ))
                    continue
                try:
                    record, found = _scan_rollout(candidate, relative, deep=deep)
                except OSError as exc:
                    record = None
                    found = [Diagnostic("error", "scan_error", str(exc), str(candidate))]
                diagnostics.extend(found)
                if record is not None:
                    records.append(record)
    except OSError as exc:
        diagnostics.append(Diagnostic("error", "scan_error", str(exc), str(sessions_dir)))

    return records, files_seen, diagnostics


def _state_db(codex_home: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    try:
        for candidate in codex_home.glob("state_*.sqlite"):
            match = _STATE_DB_NAME.fullmatch(candidate.name)
            if match and candidate.is_file():
                candidates.append((int(match.group(1)), candidate))
    except OSError:
        return None
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        details = path.stat()
    except FileNotFoundError:
        return None
    return details.st_size, details.st_mtime_ns, details.st_ino


def _copy_stable_index_snapshot(database: Path, target: Path) -> None:
    """Copy SQLite plus WAL without opening or writing beside the live index."""
    wal = Path(f"{database}-wal")
    target_wal = Path(f"{target}-wal")
    for _attempt in range(3):
        before = (_file_signature(database), _file_signature(wal))
        if before[0] is None:
            raise FileNotFoundError(database)
        shutil.copyfile(database, target)
        if before[1] is not None:
            shutil.copyfile(wal, target_wal)
        elif target_wal.exists():
            target_wal.unlink()
        after = (_file_signature(database), _file_signature(wal))
        if before == after:
            return
    raise OSError("SQLite index changed during all snapshot attempts")


def _probe_writer_lock(path: Path) -> Literal["free", "held", "unknown"]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDWR)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return "held"
                return "unknown"
            else:
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                return "free"

        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return "held"
            return "unknown"
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return "free"
    except OSError:
        return "unknown"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _inspect_writer_locks(report: ContextReport, codex_home: Path) -> None:
    lock_dir = codex_home / "thread-writer-locks"
    if not lock_dir.is_dir():
        return
    try:
        lock_files = [
            item for item in lock_dir.iterdir()
            if item.is_file() and item.name != ".coordination.lock"
        ]
    except OSError as exc:
        report.diagnostics.append(Diagnostic(
            "warning", "writer_locks_unreadable", str(exc), str(lock_dir),
        ))
        return

    report.writer_lock_files = len(lock_files)
    for lock_file in lock_files:
        if _valid_session_id(lock_file.stem) is None:
            report.unknown_writer_locks += 1
            continue
        state = _probe_writer_lock(lock_file)
        if state == "held":
            report.held_writer_locks += 1
        elif state == "unknown":
            report.unknown_writer_locks += 1

    if report.held_writer_locks:
        report.diagnostics.append(Diagnostic(
            "warning", "active_session_writers",
            f"{report.held_writer_locks} target session writer lock(s) are held; "
            "future write operations must skip those sessions",
            str(lock_dir),
        ))
    if report.unknown_writer_locks:
        report.diagnostics.append(Diagnostic(
            "warning", "writer_lock_state_unknown",
            f"{report.unknown_writer_locks} writer lock file(s) could not be classified",
            str(lock_dir),
        ))


def _inspect_index(
    report: ContextReport,
    records: list[SessionRecord],
    codex_home: Path,
    *,
    deep: bool,
) -> None:
    database = _state_db(codex_home)
    if database is None:
        report.diagnostics.append(Diagnostic(
            "warning", "index_missing", "no state_*.sqlite index found", str(codex_home),
        ))
        return
    report.index_db = str(database)

    try:
        # Even SQLite mode=ro can create or update a -shm file beside a WAL
        # database. Inspect a stable temporary snapshot instead so status and
        # doctor are observably read-only with respect to the Codex home.
        with tempfile.TemporaryDirectory(prefix="codesync-context-") as temp_dir:
            snapshot = Path(temp_dir) / database.name
            _copy_stable_index_snapshot(database, snapshot)
            uri = snapshot.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                connection.execute("PRAGMA query_only=ON")
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)")
                }
                required = {"id", "rollout_path", "cwd"}
                missing_columns = sorted(required - columns)
                if missing_columns:
                    raise ValueError(
                        "threads table is missing columns: " + ", ".join(missing_columns)
                    )
                selected = "id, rollout_path, cwd"
                if "archived" in columns:
                    selected += ", archived"
                rows = list(connection.execute(f"SELECT {selected} FROM threads"))
                if deep:
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    if check is None or check[0] != "ok":
                        raise ValueError(f"SQLite quick_check failed: {check!r}")
            finally:
                connection.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        report.diagnostics.append(Diagnostic(
            "error", "index_unreadable", str(exc), str(database),
        ))
        return

    report.index_rows = len(rows)
    indexed = {str(row[0]): row for row in rows}
    sessions = {record.session_id: record for record in records}
    report.indexed_sessions = len(sessions.keys() & indexed.keys())

    missing_ids = sorted(sessions.keys() - indexed.keys())
    report.missing_index_count = len(missing_ids)
    if missing_ids:
        report.diagnostics.append(Diagnostic(
            "error" if deep else "warning",
            "sessions_missing_from_index",
            f"{len(missing_ids)} rollout(s) are absent from the local threads index",
            str(database),
        ))

    stale = 0
    mismatch = 0
    for session_id, row in indexed.items():
        rollout_path = str(row[1])
        try:
            exists = Path(rollout_path).is_file()
        except OSError:
            exists = False
        if not exists:
            stale += 1
        record = sessions.get(session_id)
        if record is not None and _path_key(record.path) != _path_key(rollout_path):
            mismatch += 1
    report.stale_index_count = stale
    report.path_mismatch_count = mismatch
    if stale:
        report.diagnostics.append(Diagnostic(
            "error" if deep else "warning", "stale_index_paths",
            f"{stale} indexed rollout path(s) do not exist", str(database),
        ))
    if mismatch:
        report.diagnostics.append(Diagnostic(
            "error" if deep else "warning", "index_path_mismatch",
            f"{mismatch} indexed rollout path(s) disagree with scanned files", str(database),
        ))


def build_report(
    *,
    mode: Literal["status", "doctor"],
    sessions_override: str | None = None,
    transport_override: str | None = None,
) -> ContextReport:
    configured = config.load_context_config()
    codex_home = _default_codex_home()
    sessions_value = sessions_override or (configured.sessions_dir if configured else None)
    sessions_dir = _expanded_path(sessions_value, codex_home / "sessions")
    assert sessions_dir is not None
    transport_value = transport_override or (
        configured.transport_root if configured else None
    )

    link_kind, link_target = _link_details(sessions_dir)
    transport = _expanded_path(transport_value)
    if transport is None and link_target is not None:
        transport = Path(link_target)
    connected = False
    if transport is not None and sessions_dir.exists() and transport.exists():
        connected = _path_key(os.path.realpath(sessions_dir)) == _path_key(
            os.path.realpath(transport)
        )

    report = ContextReport(
        mode=mode,
        sessions_dir=str(sessions_dir),
        link_kind=link_kind,
        link_target=link_target,
        transport_root=str(transport) if transport is not None else None,
        transport_connected=connected,
    )

    if link_kind == "missing":
        report.diagnostics.append(Diagnostic(
            "error", "sessions_missing", "sessions directory does not exist", str(sessions_dir),
        ))
        return report
    if link_kind in {"broken-symlink", "broken-junction", "not-directory"}:
        report.diagnostics.append(Diagnostic(
            "error", "sessions_unusable", f"sessions path is {link_kind}", str(sessions_dir),
        ))
        return report
    if transport is None:
        report.diagnostics.append(Diagnostic(
            "warning", "transport_not_configured",
            "no transport root is configured or detectable", str(sessions_dir),
        ))
    elif not transport.exists():
        report.diagnostics.append(Diagnostic(
            "error", "transport_missing", "transport root does not exist", str(transport),
        ))
    elif not connected:
        report.diagnostics.append(Diagnostic(
            "error", "transport_disconnected",
            "sessions directory does not resolve to the transport root", str(transport),
        ))
    elif link_kind == "directory" and transport_value is not None:
        report.diagnostics.append(Diagnostic(
            "warning", "transport_without_link",
            "sessions is the transport directory itself, not a symlink/junction", str(sessions_dir),
        ))

    records, files_seen, diagnostics = scan_sessions(
        sessions_dir, deep=(mode == "doctor")
    )
    report.files_seen = files_seen
    report.session_count = len(records)
    report.total_bytes = sum(record.size for record in records)
    report.project_count = len({record.cwd.casefold() for record in records})
    report.diagnostics.extend(diagnostics)

    grouped: dict[str, int] = {}
    for record in records:
        grouped[record.session_id] = grouped.get(record.session_id, 0) + 1
    duplicate_ids = sorted(key for key, count in grouped.items() if count > 1)
    report.duplicate_id_count = len(duplicate_ids)
    if duplicate_ids:
        report.diagnostics.append(Diagnostic(
            "error", "duplicate_session_ids",
            f"{len(duplicate_ids)} session UUID(s) occur in multiple rollout files",
            str(sessions_dir),
        ))

    _inspect_index(report, records, codex_home, deep=(mode == "doctor"))
    _inspect_writer_locks(report, codex_home)
    return report


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{value} B"


def _print_report(report: ContextReport) -> None:
    output.section(f"Codex context {report.mode}")
    output.info(f"sessions:  {report.sessions_dir}")
    if report.link_target:
        output.detail(f"{report.link_kind} -> {report.link_target}")
    else:
        output.detail(f"path type: {report.link_kind}")
    output.info(f"transport: {report.transport_root or '(not configured)'}")
    output.detail("connected" if report.transport_connected else "not connected")
    output.info(
        f"rollouts:  {report.session_count}/{report.files_seen} valid · "
        f"{_format_bytes(report.total_bytes)} · {report.project_count} cwd(s)"
    )
    if report.index_db is None:
        output.info("index:     not found")
    else:
        output.info(
            f"index:     {report.indexed_sessions or 0}/{report.session_count} indexed · "
            f"{report.index_rows or 0} row(s)"
        )
        output.detail(report.index_db)
    output.info(
        f"writers:   {report.held_writer_locks} held / "
        f"{report.writer_lock_files} lock file(s)"
    )

    if not report.diagnostics:
        output.good("No context transport or index problems detected.")
        return
    for item in report.diagnostics:
        detail = f"[{item.code}] {item.message}"
        if item.path:
            detail += f" ({item.path})"
        (output.err if item.severity == "error" else output.warn)(detail)
    output.info(
        f"result: {report.error_count} error(s), {report.warning_count} warning(s)"
    )


def run_context(
    action: Literal["status", "doctor"],
    *,
    sessions_dir: str | None = None,
    transport_root: str | None = None,
    json_output: bool = False,
) -> int:
    try:
        report = build_report(
            mode=action,
            sessions_override=sessions_dir,
            transport_override=transport_root,
        )
    except (OSError, UnicodeError, ValueError, TypeError, sqlite3.Error) as exc:
        if json_output:
            print(json.dumps({
                "mode": action,
                "error_count": 1,
                "warning_count": 0,
                "diagnostics": [{
                    "severity": "error",
                    "code": "context_config_error",
                    "message": str(exc),
                    "path": str(paths.config_file()),
                }],
            }, ensure_ascii=False, indent=2))
        else:
            output.err(f"Codex context 配置或扫描失败: {exc}")
        return 1

    if json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 1 if report.error_count else 0
