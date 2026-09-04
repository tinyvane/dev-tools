from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from codesync import output, proc


DEFAULT_ROOT = r"V:\CodexPortable"
REGISTRATION_NAME = "portable.json"
INSTALLER_URL = "https://chatgpt.com/codex/install.ps1"
CODEXV_COMMAND = "codexv.cmd"
_CODEXV_MARKER = ":: Managed by codesync portable alias v1"
_SQLITE_FAMILY = re.compile(
    r"^.+_\d+\.sqlite(?:-(?:wal|shm|journal))?$", re.IGNORECASE
)
_ROLLOUT_NAME = re.compile(r"^rollout-.+\.jsonl$", re.IGNORECASE)
_HOME_EXCLUDED_DIRS = {"thread-writer-locks", ".sandbox-secrets", "tmp", ".tmp"}
_HOME_EXCLUDED_FILES = {"auth.json"}


@dataclass(frozen=True)
class PortableLayout:
    root: Path
    bin: Path
    home: Path
    sqlite: Path
    backups: Path
    manifests: Path
    launcher: Path
    registration: Path

    @classmethod
    def from_root(cls, root: str | Path) -> PortableLayout:
        resolved = Path(root).expanduser().absolute()
        return cls(
            root=resolved,
            bin=resolved / "bin",
            home=resolved / "home",
            sqlite=resolved / "sqlite",
            backups=resolved / "backups",
            manifests=resolved / "manifests",
            launcher=resolved / "Start-Codex.ps1",
            registration=resolved / "manifests" / REGISTRATION_NAME,
        )


@dataclass(frozen=True)
class PortableDiagnostic:
    severity: Literal["warning", "error"]
    code: str
    message: str
    path: str | None = None


@dataclass
class PortableReport:
    action: str
    root: str
    mode: str | None = None
    volume_unique_id: str | None = None
    expected_volume_unique_id: str | None = None
    registration_status: str | None = None
    source_home: str | None = None
    sessions_source: str | None = None
    cli_path: str | None = None
    cli_version: str | None = None
    blocking_processes: list[dict[str, object]] = field(default_factory=list)
    diagnostics: list[PortableDiagnostic] = field(default_factory=list)

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


@dataclass(frozen=True)
class RolloutFile:
    session_id: str
    source: Path
    relative_path: Path
    size: int
    sha256: str


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _path_key(path: str | Path) -> str:
    value = os.path.abspath(os.path.normpath(os.fspath(path)))
    if os.name == "nt" and value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(value)


def _copy_io_path(path: str | Path) -> str:
    """Return a Windows extended-length path for staging copy operations."""
    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _copy_mkdir(path: Path, *, exist_ok: bool) -> None:
    os.makedirs(_copy_io_path(path), exist_ok=exist_ok)


def _copy_file(source: Path, target: Path) -> None:
    shutil.copy2(_copy_io_path(source), _copy_io_path(target))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_layout(layout: PortableLayout) -> None:
    if os.name != "nt":
        raise OSError("codesync portable currently supports Windows only")
    if not layout.root.is_absolute() or layout.root == Path(layout.root.anchor):
        raise ValueError("portable root must be an absolute directory below a drive root")
    if layout.root.name.casefold() in {".codex", "sessions", "windows"}:
        raise ValueError("unsafe portable root name")


def _volume_unique_id(path: Path) -> str:
    """Return the stable Windows volume GUID for a path's mount point."""
    if os.name != "nt":
        raise OSError("Windows volume identity is unavailable on this platform")
    root = Path(path.anchor)
    if not root.exists():
        raise FileNotFoundError(f"drive is not mounted: {root}")
    kernel32 = ctypes.windll.kernel32
    volume_path = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumePathNameW(str(root), volume_path, len(volume_path)):
        raise ctypes.WinError()
    volume_name = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetVolumeNameForVolumeMountPointW(
        volume_path.value, volume_name, len(volume_name)
    ):
        raise ctypes.WinError()
    return volume_name.value


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_text(path: Path, value: str) -> None:
    if not path.parent.is_dir():
        raise FileNotFoundError(f"target directory is missing: {path.parent}")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = str(uuid.UUID(value))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed == value.casefold() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_prefix(path: Path, size: int) -> str:
    remaining = size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise OSError(f"file is shorter than its migration baseline: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _is_link(path: Path) -> bool:
    junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(junction and junction(path))


def _blocking_processes() -> list[dict[str, object]]:
    if os.name != "nt":
        return []
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    script = r"""
$items = Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and (
    $_.Name -in @('codex.exe','codex-code-mode-host.exe','ChatGPT.exe') -or
    ($_.Name -eq 'extension-host.exe' -and $_.ExecutablePath -like '*\.codex\*') -or
    ($_.CommandLine -match '(?i)(^|\s)app-server(\s|$)')
  )
} | Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine
@($items) | ConvertTo-Json -Compress
"""
    completed = proc.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=proc.T_QUICK,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "failed to inspect Codex processes")
    parsed = json.loads(completed.stdout.lstrip("\ufeff") or "[]")
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("unexpected process inventory response")
    return [item for item in parsed if isinstance(item, dict)]


def _blocking_process_label(item: dict[str, object]) -> str:
    """Render a blocker without exposing its potentially sensitive command line."""
    process_id = item.get("ProcessId")
    name = str(item.get("Name") or "(unknown process)")
    label = f"PID {process_id}: {name}" if process_id is not None else name
    executable = item.get("ExecutablePath")
    if isinstance(executable, str) and executable.strip():
        label += f" — {executable}"
    return label


def _print_blocking_processes(blockers: list[dict[str, object]]) -> None:
    for item in blockers:
        output.detail(_blocking_process_label(item))
    if blockers:
        output.detail("After confirming the process, stop it with: Stop-Process -Id <PID>")


def _cli_version(executable: Path) -> str:
    completed = proc.run(
        [str(executable), "--version"],
        timeout=proc.T_QUICK,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "codex --version failed")
    match = re.search(r"codex-cli\s+([^\s]+)", completed.stdout)
    if match is None:
        raise ValueError(f"unexpected codex --version output: {completed.stdout!r}")
    return match.group(1)


def _toml_top_level_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = re.match(
            rf"^\s*{re.escape(key)}\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$", line
        )
        if match:
            return match.group(2).replace("\\\\", "\\")
    return None


def _set_portable_config(config_path: Path, sqlite_home: Path) -> None:
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    def replace_top_level(source: str, key: str, value: str) -> str:
        lines = source.splitlines(keepends=True)
        first_table = next(
            (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
            len(lines),
        )
        replacement = f'{key} = "{value}"\n'
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for index in range(first_table):
            if pattern.match(lines[index]):
                lines[index] = replacement
                return "".join(lines)
        lines.insert(0, replacement)
        return "".join(lines)

    normalized = sqlite_home.as_posix()
    text = replace_top_level(text, "sqlite_home", normalized)
    text = replace_top_level(text, "cli_auth_credentials_store", "keyring")
    tomllib.loads(text)
    config_path.write_text(text, encoding="utf-8", newline="\n")


def _powershell_literal(value: str) -> str:
    return value.replace("'", "''")


def _write_launcher(
    layout: PortableLayout,
    volume_id: str,
    expected_version: str | None,
) -> None:
    version_check = ""
    if expected_version:
        expected = _powershell_literal(expected_version)
        version_check = f"""
$reported = (& $exe --version 2>&1 | Out-String).Trim()
if ($reported -notmatch '^codex-cli\\s+{re.escape(expected)}$') {{
    throw "Portable Codex version mismatch. Expected {expected}; got: $reported"
}}
"""
    script = f"""param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CodexArgs
)
$ErrorActionPreference = 'Stop'
$expectedRoot = '{_powershell_literal(str(layout.root))}'
$expectedVolume = '{_powershell_literal(volume_id)}'
$actualRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
if ($actualRoot.TrimEnd('\\') -ine $expectedRoot.TrimEnd('\\')) {{
    throw "Launcher path mismatch. Expected $expectedRoot; got $actualRoot"
}}
$driveLetter = [IO.Path]::GetPathRoot($actualRoot).Substring(0, 1)
$actualVolume = (Get-Volume -DriveLetter $driveLetter -ErrorAction Stop).UniqueId
if ($actualVolume -ine $expectedVolume) {{
    throw "Wrong portable volume. Expected $expectedVolume; got $actualVolume"
}}
$home = Join-Path $actualRoot 'home'
$sqlite = Join-Path $actualRoot 'sqlite'
$bin = Join-Path $actualRoot 'bin'
foreach ($required in @($home, $sqlite, $bin)) {{
    if (-not (Test-Path -LiteralPath $required -PathType Container)) {{
        throw "Required portable directory is missing: $required"
    }}
}}
$exe = Join-Path $bin 'codex.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {{
    throw "Portable codex.exe is missing: $exe"
}}
$config = Join-Path $home 'config.toml'
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {{
    throw "Portable config.toml is missing: $config"
}}
$sqliteLine = Select-String -LiteralPath $config -Pattern '^\\s*sqlite_home\\s*=\\s*["'']([^"'']+)["'']' | Select-Object -First 1
if ($null -eq $sqliteLine) {{ throw 'Portable config.toml has no sqlite_home.' }}
$configuredSqlite = [IO.Path]::GetFullPath($sqliteLine.Matches[0].Groups[1].Value)
if ($configuredSqlite.TrimEnd('\\') -ine $sqlite.TrimEnd('\\')) {{
    throw "sqlite_home mismatch. Expected $sqlite; got $configuredSqlite"
}}
{version_check}
$env:CODEX_HOME = $home
$env:CODEX_SQLITE_HOME = $sqlite
$env:CODEX_INSTALL_DIR = $bin
Write-Host "==> Codex workspace: PORTABLE ($actualRoot)" -ForegroundColor Cyan
& $exe @CodexArgs
exit $LASTEXITCODE
"""
    layout.launcher.write_text(script, encoding="utf-8-sig", newline="\r\n")


def _default_source_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _machine_id() -> str:
    if os.name != "nt":
        raise OSError("Windows MachineGuid is unavailable on this platform")
    import winreg

    access = winreg.KEY_READ
    if hasattr(winreg, "KEY_WOW64_64KEY"):
        access |= winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        access,
    ) as key:
        value = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip().casefold()
    if not value:
        raise ValueError("Windows MachineGuid is empty")
    return value


def _default_sessions_source(source_home: Path) -> Path:
    sessions = source_home / "sessions"
    return Path(os.path.realpath(sessions)) if os.path.lexists(sessions) else sessions


def _effective_sqlite_source(source_home: Path) -> Path:
    config_path = source_home / "config.toml"
    configured: object | None = None
    if config_path.is_file():
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        configured = raw.get("sqlite_home")
    if configured is None:
        configured = os.environ.get("CODEX_SQLITE_HOME")
    if configured is None:
        return source_home
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError("effective sqlite_home must be a non-empty path string")
    result = Path(configured).expanduser()
    return result.absolute() if not result.is_absolute() else result


def _home_copy_inventory(source: Path) -> dict[str, object]:
    total_bytes = 0
    file_count = 0
    excluded: list[str] = []
    internal_links: list[dict[str, str]] = []
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for dirname in dirnames:
            child = current_path / dirname
            relative = child.relative_to(source)
            if _excluded_home_path(relative, is_dir=True):
                excluded.append(relative.as_posix() + "/")
                continue
            if _is_link(child):
                resolved = child.resolve(strict=True)
                if not _is_within(resolved, source):
                    raise ValueError(f"external link in CODEX_HOME: {child} -> {resolved}")
                internal_links.append({
                    "path": relative.as_posix(),
                    "target": resolved.relative_to(source.resolve()).as_posix(),
                })
                continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in filenames:
            child = current_path / filename
            relative = child.relative_to(source)
            if _excluded_home_path(relative, is_dir=False):
                excluded.append(relative.as_posix())
                continue
            if _is_link(child):
                raise ValueError(f"file symlink in CODEX_HOME is unsupported: {child}")
            total_bytes += child.stat().st_size
            file_count += 1
    return {
        "file_count": file_count,
        "bytes": total_bytes,
        "excluded_paths": sorted(excluded),
        "internal_links": internal_links,
    }


def _initial_inventory(
    source_home: Path,
    sqlite_source: Path,
    sessions_source: Path,
) -> dict[str, object]:
    from codesync.context_sync import _copy_stable_index_snapshot, _state_db

    rollouts = _scan_rollouts(sessions_source)
    state_database = _state_db(sqlite_source)
    if state_database is None:
        raise FileNotFoundError(f"no state_N.sqlite found in {sqlite_source}")
    with tempfile.TemporaryDirectory(prefix="codesync-portable-inventory-") as temp_dir:
        snapshot = Path(temp_dir) / state_database.name
        _copy_stable_index_snapshot(state_database, snapshot)
        uri = snapshot.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            connection.execute("PRAGMA query_only=ON")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            if not {"id", "rollout_path"}.issubset(columns):
                raise ValueError("unsupported authoritative threads schema")
            index_rows = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT id, rollout_path FROM threads")
            }
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    if quick_check is None or quick_check[0] != "ok":
        raise ValueError(f"authoritative SQLite quick_check failed: {quick_check!r}")
    rollout_by_id = {item.session_id: item for item in rollouts}
    if len(rollout_by_id) != len(rollouts):
        raise ValueError("duplicate session UUIDs exist in the authoritative sessions source")
    rollout_ids = set(rollout_by_id)
    thread_ids = set(index_rows)
    missing = rollout_ids - thread_ids
    mismatched = [
        session_id for session_id in rollout_ids & thread_ids
        if _path_key(os.path.realpath(index_rows[session_id]))
        != _path_key(os.path.realpath(rollout_by_id[session_id].source))
    ]
    if missing:
        raise ValueError(f"{len(missing)} rollout(s) are absent from the /resume index")
    if mismatched:
        raise ValueError(f"{len(mismatched)} rollout path(s) disagree with the /resume index")
    cli_path_value = shutil.which("codex")
    cli_path = Path(cli_path_value) if cli_path_value else None
    cli_version = _cli_version(cli_path) if cli_path is not None else None
    sqlite_files = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in sorted(sqlite_source.iterdir())
        if path.is_file() and _SQLITE_FAMILY.fullmatch(path.name)
    ]
    home_copy = _home_copy_inventory(source_home)
    return {
        "captured_at": _utc_now(),
        "source_home": str(source_home),
        "sqlite_source": str(sqlite_source),
        "sessions_source": str(sessions_source),
        "sessions_is_link": _is_link(source_home / "sessions"),
        "rollout_count": len(rollouts),
        "rollout_bytes": sum(item.size for item in rollouts),
        "rollouts": [
            {
                "id": item.session_id,
                "path": str(item.source),
                "relative_path": item.relative_path.as_posix(),
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in rollouts
        ],
        "home_copy": home_copy,
        "sqlite_files": sqlite_files,
        "state_database": str(state_database),
        "sqlite_user_version": user_version,
        "sqlite_schema_version": schema_version,
        "sqlite_quick_check": "ok",
        "index_rows": len(index_rows),
        "indexed_rollouts": len(thread_ids & rollout_ids),
        "missing_index_rollouts": 0,
        "mismatched_index_rollouts": 0,
        "cli_path": str(cli_path) if cli_path else None,
        "cli_version": cli_version,
        "environment": {
            "CODEX_HOME": os.environ.get("CODEX_HOME"),
            "CODEX_SQLITE_HOME": os.environ.get("CODEX_SQLITE_HOME"),
            "CODEX_INSTALL_DIR": os.environ.get("CODEX_INSTALL_DIR"),
        },
    }


def _registration(layout: PortableLayout) -> dict[str, object] | None:
    return _read_json(layout.registration) if layout.registration.is_file() else None


def _registration_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"registration has no valid {name}")
    return Path(value)


def prepare_portable(
    root: str,
    *,
    source_home: str | None = None,
    sessions_source: str | None = None,
) -> int:
    try:
        layout = PortableLayout.from_root(root)
        _validate_layout(layout)
        volume_id = _volume_unique_id(layout.root)
        source = Path(source_home).expanduser().absolute() if source_home else _default_source_home()
        sessions = (
            Path(sessions_source).expanduser().absolute()
            if sessions_source
            else _default_sessions_source(source)
        )
        if not source.is_dir():
            raise FileNotFoundError(f"source CODEX_HOME not found: {source}")
        if not sessions.is_dir():
            raise FileNotFoundError(f"sessions source not found: {sessions}")
        sqlite_source = _effective_sqlite_source(source)
        if not sqlite_source.is_dir():
            raise FileNotFoundError(f"SQLite source not found: {sqlite_source}")
        if _path_key(sqlite_source) != _path_key(source):
            raise ValueError(
                "v2.29 migration requires the authoritative SQLite source to be the "
                "current CODEX_HOME; remove conflicting sqlite_home/CODEX_SQLITE_HOME first"
            )
        if _is_within(layout.root, source) or _is_within(source, layout.root):
            raise ValueError("source CODEX_HOME and portable root must not contain each other")

        existing = _registration(layout)
        if existing is not None:
            if existing.get("volume_unique_id") != volume_id:
                raise ValueError("existing registration belongs to a different volume")
            if _path_key(str(existing.get("root"))) != _path_key(layout.root):
                raise ValueError("existing registration belongs to a different root")
            if existing.get("status") != "prepared":
                output.good(
                    f"Portable layout already progressed to {existing.get('status')}: {layout.root}"
                )
                return 0

        if layout.root.exists():
            allowed = {"bin", "home", "sqlite", "backups", "manifests", "Start-Codex.ps1"}
            unexpected = sorted(
                item.name for item in layout.root.iterdir() if item.name not in allowed
            )
            if unexpected:
                raise ValueError(
                    "portable root contains unexpected entries: " + ", ".join(unexpected)
                )
        inventory = _initial_inventory(source, sqlite_source, sessions)
        estimated_bytes = (
            int(inventory["rollout_bytes"])
            + int(inventory["home_copy"]["bytes"])
            + sum(int(item["size"]) for item in inventory["sqlite_files"])
            + 1024 ** 3
        )
        free_bytes = shutil.disk_usage(Path(layout.root.anchor)).free
        inventory["estimated_required_bytes"] = estimated_bytes
        inventory["target_free_bytes"] = free_bytes
        if free_bytes < estimated_bytes:
            raise OSError(
                f"portable volume needs at least {estimated_bytes} free bytes; has {free_bytes}"
            )
        for directory in (
            layout.root, layout.bin, layout.home, layout.sqlite,
            layout.backups, layout.manifests,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if _is_link(layout.root):
            raise ValueError("portable root must not be a symlink or junction")

        inventory_path = layout.manifests / f"inventory-{_timestamp()}.json"
        _atomic_json(inventory_path, inventory)
        if existing is None:
            registration: dict[str, object] = {
                "schema_version": 1,
                "status": "prepared",
                "mode": None,
                "created_at": _utc_now(),
                "root": str(layout.root),
                "volume_unique_id": volume_id,
                "origin_machine_id": _machine_id(),
                "machines": {},
                "expected_cli_version": None,
                "migration_manifest": None,
                "source_backup": None,
            }
        else:
            registration = existing
        registration.update({
            "source_home": str(source),
            "sessions_source": str(sessions),
            "sqlite_source": str(sqlite_source),
            "inventory_manifest": str(inventory_path),
            "inventory_refreshed_at": _utc_now(),
        })
        _atomic_json(layout.registration, registration)
        _write_launcher(layout, volume_id, None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        output.err(f"Portable prepare failed: {exc}")
        return 1

    output.good(f"Prepared portable layout: {layout.root}")
    output.info(f"Volume identity: {volume_id}")
    output.warn("No live CODEX_HOME, SQLite, credentials, or user environment was changed.")
    return 0


def _valid_rollout_relative(relative: Path) -> bool:
    parts = relative.parts
    if len(parts) != 4:
        return False
    year, month, day, name = parts
    return (
        len(year) == 4 and year.isdigit()
        and len(month) == 2 and month.isdigit() and 1 <= int(month) <= 12
        and len(day) == 2 and day.isdigit() and 1 <= int(day) <= 31
        and bool(_ROLLOUT_NAME.fullmatch(name))
    )


def _scan_rollouts(root: Path) -> list[RolloutFile]:
    records: list[RolloutFile] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            child = current_path / dirname
            if _is_link(child):
                raise ValueError(f"nested link is not allowed in sessions: {child}")
        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(root)
            if not _valid_rollout_relative(relative):
                raise ValueError(f"unexpected sessions content: {path}")
            before = _file_signature(path)
            digest = hashlib.sha256()
            first: object | None = None
            try:
                with path.open("rb") as handle:
                    for line_number, raw_line in enumerate(handle, 1):
                        digest.update(raw_line)
                        if not raw_line.strip():
                            raise ValueError(f"blank JSONL record on line {line_number}")
                        decoded = json.loads(raw_line)
                        if not isinstance(decoded, dict):
                            raise ValueError(f"non-object JSONL record on line {line_number}")
                        if line_number == 1:
                            first = decoded
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid rollout JSONL: {path}: {exc}") from exc
            if first is None:
                raise ValueError(f"empty rollout: {path}")
            payload = first.get("payload") if isinstance(first, dict) else None
            session_id = _canonical_uuid(payload.get("id") if isinstance(payload, dict) else None)
            if first.get("type") != "session_meta" or session_id is None:
                raise ValueError(f"invalid session_meta: {path}")
            if not path.name.casefold().endswith(f"{session_id}.jsonl"):
                raise ValueError(f"rollout filename UUID mismatch: {path}")
            after = _file_signature(path)
            if before != after or after is None:
                raise OSError(f"rollout changed while hashing: {path}")
            records.append(RolloutFile(
                session_id=session_id,
                source=path,
                relative_path=relative,
                size=after[0],
                sha256=digest.hexdigest(),
            ))
    return records


def _is_file_prefix(shorter: Path, longer: Path) -> bool:
    with shorter.open("rb") as left, longer.open("rb") as right:
        while chunk := left.read(1024 * 1024):
            if right.read(len(chunk)) != chunk:
                return False
    return True


def _merge_rollouts(sources: list[Path], target: Path) -> list[RolloutFile]:
    unique_sources: list[Path] = []
    seen_roots: set[str] = set()
    for source in sources:
        key = _path_key(os.path.realpath(source))
        if key not in seen_roots:
            seen_roots.add(key)
            unique_sources.append(source)

    grouped: dict[str, list[RolloutFile]] = {}
    for source in unique_sources:
        for record in _scan_rollouts(source):
            grouped.setdefault(record.session_id, []).append(record)

    selected: list[RolloutFile] = []
    target_paths: dict[str, str] = {}
    for session_id, candidates in sorted(grouped.items()):
        candidates.sort(key=lambda item: (item.size, item.sha256, str(item.source)))
        chosen = candidates[-1]
        for candidate in candidates[:-1]:
            if candidate.size == chosen.size and candidate.sha256 == chosen.sha256:
                continue
            if candidate.size < chosen.size and _is_file_prefix(candidate.source, chosen.source):
                continue
            raise ValueError(
                f"divergent rollout copies for session {session_id}: "
                f"{candidate.source} <> {chosen.source}"
            )
        rel_key = chosen.relative_path.as_posix().casefold()
        other_id = target_paths.get(rel_key)
        if other_id is not None and other_id != session_id:
            raise ValueError(f"rollout target path collision: {chosen.relative_path}")
        target_paths[rel_key] = session_id
        destination = target / chosen.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chosen.source, destination)
        if destination.stat().st_size != chosen.size or _sha256(destination) != chosen.sha256:
            raise OSError(f"rollout copy verification failed: {destination}")
        selected.append(RolloutFile(
            session_id=chosen.session_id,
            source=destination,
            relative_path=chosen.relative_path,
            size=chosen.size,
            sha256=chosen.sha256,
        ))
    return selected


def _excluded_home_path(relative: Path, *, is_dir: bool) -> bool:
    parts = [part.casefold() for part in relative.parts]
    if not parts:
        return False
    if len(parts) == 1:
        name = parts[0]
        if name == "sessions" or name.startswith("sessions.") or name.startswith("sessions-"):
            return True
        if is_dir and name in _HOME_EXCLUDED_DIRS:
            return True
        if not is_dir and (name in _HOME_EXCLUDED_FILES or _SQLITE_FAMILY.fullmatch(name)):
            return True
    if len(parts) >= 2 and parts[:2] == ["packages", "standalone"]:
        return True
    if is_dir and any(
        part.startswith("plugin-backup-") or part.startswith("plugin-install-")
        for part in parts
    ):
        return True
    if is_dir and parts[-1] in {"tmp", ".tmp"}:
        return True
    if not is_dir and (parts[-1].startswith(".env") or "credential" in parts[-1]):
        return True
    return False


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
            r"System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        script = (
            "$ErrorActionPreference='Stop'; "
            f"New-Item -ItemType Junction -Path '{_powershell_literal(str(link))}' "
            f"-Target '{_powershell_literal(str(target))}' | Out-Null"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        completed = proc.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            timeout=proc.T_QUICK,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.strip() or completed.stdout.strip())
    else:
        link.symlink_to(target, target_is_directory=True)


def _copy_home(
    source: Path,
    target: Path,
) -> tuple[list[str], list[dict[str, str]]]:
    excluded: list[str] = []
    internal_links: list[tuple[Path, Path]] = []
    _copy_mkdir(target, exist_ok=False)
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_root = current_path.relative_to(source)
        destination_root = target / relative_root
        _copy_mkdir(destination_root, exist_ok=True)

        safe_dirs: list[str] = []
        for dirname in dirnames:
            child = current_path / dirname
            relative = child.relative_to(source)
            if _excluded_home_path(relative, is_dir=True):
                excluded.append(relative.as_posix() + "/")
                continue
            if _is_link(child):
                resolved = child.resolve(strict=True)
                if not _is_within(resolved, source):
                    raise ValueError(f"external link in CODEX_HOME: {child} -> {resolved}")
                internal_links.append((relative, resolved.relative_to(source.resolve())))
                continue
            _copy_mkdir(target / relative, exist_ok=True)
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs

        for filename in filenames:
            child = current_path / filename
            relative = child.relative_to(source)
            if _excluded_home_path(relative, is_dir=False):
                excluded.append(relative.as_posix())
                continue
            if _is_link(child):
                raise ValueError(f"file symlink in CODEX_HOME is unsupported: {child}")
            _copy_file(child, target / relative)

    links: list[dict[str, str]] = []
    for relative, target_relative in sorted(internal_links, key=lambda item: len(item[0].parts)):
        if not (target / target_relative).is_dir():
            raise ValueError(f"internal link target was not copied: {relative}")
        links.append({"path": relative.as_posix(), "target": target_relative.as_posix()})
    return sorted(excluded), links


def _restore_internal_links(home: Path, links: list[dict[str, str]]) -> None:
    for item in links:
        relative = Path(item["path"])
        target_relative = Path(item["target"])
        link = home / relative
        target = home / target_relative
        if not _is_within(link, home) or not _is_within(target, home):
            raise ValueError(f"internal link escapes portable home: {relative}")
        if not target.is_dir():
            raise FileNotFoundError(f"internal link target is missing: {target}")
        if os.path.lexists(link):
            if not _is_link(link) or _path_key(os.path.realpath(link)) != _path_key(target):
                raise ValueError(f"internal link destination already differs: {link}")
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        _create_directory_link(link, target)


def _copy_sqlite(source_home: Path, target: Path) -> list[dict[str, object]]:
    target.mkdir(parents=True, exist_ok=False)
    source_files = sorted(
        path for path in source_home.iterdir()
        if path.is_file() and _SQLITE_FAMILY.fullmatch(path.name)
    )
    if not source_files:
        raise FileNotFoundError(f"no SQLite state files found in {source_home}")
    signatures = {path: _file_signature(path) for path in source_files}
    for path in source_files:
        shutil.copy2(path, target / path.name)
    if any(_file_signature(path) != signature for path, signature in signatures.items()):
        raise OSError("SQLite state changed while it was being copied")
    return _sqlite_inventory(target)


def _sqlite_inventory(target: Path) -> list[dict[str, object]]:
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(target.iterdir())
        if path.is_file() and _SQLITE_FAMILY.fullmatch(path.name)
    ]


def _latest_state_db(sqlite_root: Path) -> Path:
    candidates: list[tuple[int, Path]] = []
    for path in sqlite_root.glob("state_*.sqlite"):
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name, re.IGNORECASE)
        if match:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        raise FileNotFoundError("no state_N.sqlite found in portable SQLite copy")
    return max(candidates, key=lambda item: item[0])[1]


def _rewrite_rollout_index(
    sqlite_root: Path,
    rollouts: list[RolloutFile],
    final_sessions_root: Path,
) -> None:
    database = _latest_state_db(sqlite_root)
    connection = sqlite3.connect(database, timeout=30)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        if not {"id", "rollout_path"}.issubset(columns):
            raise ValueError("unsupported threads schema in state database")
        indexed = {
            str(row[0]) for row in connection.execute("SELECT id FROM threads")
        }
        session_ids = {record.session_id for record in rollouts}
        missing = sorted(session_ids - indexed)
        if missing:
            raise ValueError(
                f"{len(missing)} rollout session(s) are absent from the authoritative index"
            )
        with connection:
            for record in rollouts:
                final_path = final_sessions_root / record.relative_path
                connection.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = ?",
                    (str(final_path), record.session_id),
                )
    finally:
        connection.close()


def _quick_check_sqlite(sqlite_root: Path) -> list[str]:
    checked: list[str] = []
    for database in sorted(sqlite_root.glob("*_*.sqlite")):
        wal = Path(f"{database}-wal")
        result: tuple[object, ...] | None = None
        for attempt in range(3):
            before = (_file_signature(database), _file_signature(wal))
            with tempfile.TemporaryDirectory(prefix="codesync-portable-sqlite-") as temp_dir:
                snapshot = Path(temp_dir) / database.name
                shutil.copyfile(database, snapshot)
                if before[1] is not None:
                    shutil.copyfile(wal, Path(f"{snapshot}-wal"))
                after = (_file_signature(database), _file_signature(wal))
                if before != after:
                    if attempt < 2:
                        continue
                    raise OSError(f"SQLite changed during all snapshot attempts: {database}")
                uri = snapshot.resolve().as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=30)
                try:
                    connection.execute("PRAGMA query_only=ON")
                    result = connection.execute("PRAGMA quick_check").fetchone()
                finally:
                    connection.close()
                break
        if result is None or result[0] != "ok":
            raise ValueError(f"SQLite quick_check failed for {database}: {result!r}")
        checked.append(database.name)
    if not checked:
        raise ValueError("no SQLite databases were checked")
    return checked


def _directory_is_empty(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is None


def _installer_command() -> str:
    """Fetch the official installer and enable its native transfer progress."""
    return (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        f"$installer = [string](irm '{INSTALLER_URL}' -UseBasicParsing); "
        "$silent = '$ProgressPreference = \"SilentlyContinue\"'; "
        "$visible = '$ProgressPreference = \"Continue\"'; "
        "if (-not $installer.Contains($silent)) { "
        "throw 'Official Codex installer changed; cannot safely enable download progress.' }; "
        "$installer = $installer.Replace($silent, $visible); "
        "& ([scriptblock]::Create($installer))"
    )


def _install_cli(layout: PortableLayout) -> str:
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    environment = os.environ.copy()
    environment.update({
        "CODEX_HOME": str(layout.home),
        "CODEX_SQLITE_HOME": str(layout.sqlite),
        "CODEX_INSTALL_DIR": str(layout.bin),
        "CODEX_NON_INTERACTIVE": "1",
    })
    module_paths = [
        str(Path(environment.get("ProgramFiles", r"C:\Program Files")) / "WindowsPowerShell/Modules"),
        str(Path(environment.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/Modules"),
    ]
    existing_modules = environment.get("PSModulePath")
    if existing_modules:
        module_paths.append(existing_modules)
    environment["PSModulePath"] = os.pathsep.join(module_paths)
    completed = proc.run(
        [
            str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", _installer_command(),
        ],
        env=environment,
        timeout=proc.T_NET_LONG,
        capture=False,
        stdin_devnull=True,
    )
    if completed.returncode != 0:
        if proc.timed_out(completed):
            raise OSError(
                f"official Codex installer timed out after {proc.T_NET_LONG}s"
            )
        raise OSError(f"official Codex installer exited {completed.returncode}")
    executable = layout.bin / "codex.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"installer did not create {executable}")
    return _cli_version(executable)


def _user_environment() -> dict[str, str | None]:
    if os.name != "nt":
        raise OSError("Windows user environment registry is unavailable")
    import winreg

    result: dict[str, str | None] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        for name in ("CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_INSTALL_DIR", "Path"):
            try:
                result[name] = str(winreg.QueryValueEx(key, name)[0])
            except FileNotFoundError:
                result[name] = None
    return result


def _write_user_environment(values: dict[str, str | None]) -> None:
    if os.name != "nt":
        raise OSError("Windows user environment registry is unavailable")
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as key:
        for name, value in values.items():
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                value_type = winreg.REG_EXPAND_SZ if "%" in value else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, value_type, value)
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass


def _activate_user_environment(layout: PortableLayout) -> dict[str, str | None]:
    previous = _user_environment()
    old_path = previous.get("Path") or ""
    components = [item for item in old_path.split(os.pathsep) if item]
    components = [item for item in components if _path_key(item) != _path_key(layout.bin)]
    new_path = os.pathsep.join([str(layout.bin), *components])
    _write_user_environment({
        "CODEX_HOME": str(layout.home),
        "CODEX_SQLITE_HOME": str(layout.sqlite),
        "CODEX_INSTALL_DIR": str(layout.bin),
        "Path": new_path,
    })
    return previous


def _remove_portable_user_environment(layout: PortableLayout) -> None:
    """Keep local Codex as the default and remove installer-created V: state."""
    current = _user_environment()
    changes: dict[str, str | None] = {}
    portable_values = {
        "CODEX_HOME": layout.home,
        "CODEX_SQLITE_HOME": layout.sqlite,
        "CODEX_INSTALL_DIR": layout.bin,
    }
    for name, portable_path in portable_values.items():
        value = current.get(name)
        if value and _path_key(value) == _path_key(portable_path):
            changes[name] = None

    old_path = current.get("Path")
    if old_path is not None:
        components = [item for item in old_path.split(os.pathsep) if item]
        filtered = [
            item for item in components
            if _path_key(item) != _path_key(layout.bin)
        ]
        new_path = os.pathsep.join(filtered)
        if new_path != old_path:
            changes["Path"] = new_path
    if changes:
        _write_user_environment(changes)


def _machine_records(registration: dict[str, object]) -> dict[str, object]:
    records = registration.get("machines")
    if records is None:
        records = {}
        registration["machines"] = records
    if not isinstance(records, dict):
        raise ValueError("registration machines field must be an object")
    return records


def _codesync_command_dir() -> Path:
    command = shutil.which("codesync")
    if not command:
        raise FileNotFoundError("cannot locate the installed codesync command on PATH")
    directory = Path(command).absolute().parent
    if not directory.is_dir():
        raise FileNotFoundError(f"codesync command directory is missing: {directory}")
    return directory


def _batch_value(value: Path) -> str:
    rendered = str(value)
    unsafe = ('"', "%", "!", "^", "&", "|", "<", ">", "\r", "\n")
    if any(character in rendered for character in unsafe):
        raise ValueError(
            f"portable launcher path cannot be represented safely in cmd: {rendered}"
        )
    return rendered


def _codexv_content(layout: PortableLayout) -> str:
    launcher = _batch_value(layout.launcher)
    return "\r\n".join((
        "@echo off",
        _CODEXV_MARKER,
        "setlocal",
        f'set "CODEXV_LAUNCHER={launcher}"',
        'if not exist "%CODEXV_LAUNCHER%" (',
        "  echo codexv: portable launcher not found: %CODEXV_LAUNCHER% 1>&2",
        "  echo codexv: insert the registered portable drive and retry. 1>&2",
        "  exit /b 1",
        ")",
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        '-NoProfile -ExecutionPolicy Bypass -File "%CODEXV_LAUNCHER%" %*',
        "exit /b %ERRORLEVEL%",
        "",
    ))


def _is_managed_codexv(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return _CODEXV_MARKER in path.read_text(encoding="utf-8").splitlines()[:3]
    except (OSError, UnicodeError):
        return False


def configure_portable_alias(root: str, *, execute: bool, remove: bool) -> int:
    layout = PortableLayout.from_root(root)
    try:
        _validate_layout(layout)
        command_dir = _codesync_command_dir()
        if _is_within(command_dir, layout.root):
            raise ValueError("codexv cannot be installed inside the portable workspace")
        alias = command_dir / CODEXV_COMMAND
        output.section("Codex portable alias")
        output.info(f"action:  {'remove' if remove else 'install'}")
        output.info(f"command: {alias}")
        output.info(f"target:  {layout.launcher}")

        if remove:
            if not os.path.lexists(alias):
                output.good("No local codexv command is installed.")
                return 0
            if not _is_managed_codexv(alias):
                raise ValueError(f"refusing to remove unmanaged command: {alias}")
            if not execute:
                output.warn("Dry run only. Re-run with --remove --execute.")
                return 0
            alias.unlink()
            output.good("Removed the local codexv command.")
            return 0

        registration = _registration(layout)
        if registration is None or registration.get("status") != "complete":
            raise ValueError("portable migration is not complete")
        if registration.get("mode") != "dual":
            raise ValueError("codexv is only used by a completed dual workspace")
        actual_volume = _volume_unique_id(layout.root)
        expected_volume = str(registration.get("volume_unique_id") or "")
        if actual_volume.casefold() != expected_volume.casefold():
            raise ValueError("portable volume identity mismatch")
        if not layout.launcher.is_file():
            raise FileNotFoundError(f"portable launcher is missing: {layout.launcher}")

        desired = _codexv_content(layout)
        resolved = shutil.which("codexv")
        if resolved and _path_key(resolved) != _path_key(alias):
            raise ValueError(f"another codexv command shadows the install target: {resolved}")
        if os.path.lexists(alias):
            if not _is_managed_codexv(alias):
                raise ValueError(f"refusing to overwrite unmanaged command: {alias}")
            if alias.read_bytes().decode("utf-8") == desired:
                output.good("Local codexv command is already up to date.")
                return 0
        if not execute:
            output.warn("Dry run only. Re-run with --execute to install codexv.")
            return 0
        _atomic_text(alias, desired)
        if alias.read_bytes().decode("utf-8") != desired:
            raise OSError(f"codexv verification failed after installation: {alias}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        output.err(f"Portable alias failed closed: {exc}")
        return 1
    output.good("Installed local command: codexv")
    output.info("Run `codexv` from any project directory to use portable state.")
    return 0


def _attach_current_machine(
    layout: PortableLayout,
    registration: dict[str, object],
) -> str:
    machine_id = _machine_id()
    records = _machine_records(registration)
    current = records.get(machine_id)
    if not isinstance(current, dict) or current.get("status") == "detached":
        current = {
            "status": "pending",
            "previous_user_environment": _user_environment(),
            "prepared_at": _utc_now(),
        }
        records[machine_id] = current
        _atomic_json(layout.registration, registration)
    elif current.get("status") not in {"pending", "attached"}:
        raise ValueError(f"unsupported machine attachment phase: {current.get('status')}")
    _activate_user_environment(layout)
    current["status"] = "attached"
    current["attached_at"] = _utc_now()
    _atomic_json(layout.registration, registration)
    return machine_id


def attach_portable(root: str, *, execute: bool) -> int:
    layout = PortableLayout.from_root(root)
    try:
        _validate_layout(layout)
        registration = _registration(layout)
        if registration is None or registration.get("status") != "complete":
            raise ValueError("portable migration is not complete")
        if registration.get("mode") == "dual":
            raise ValueError(
                "dual mode does not use attach; run Start-Codex.ps1 from the portable drive"
            )
        if _volume_unique_id(layout.root).casefold() != str(
            registration.get("volume_unique_id") or ""
        ).casefold():
            raise ValueError("portable volume identity mismatch")
        machine_id = _machine_id()
        output.section("Attach this Windows PC to Codex Portable")
        output.info(f"machine: {machine_id}")
        output.info(f"root:    {layout.root}")
        if not execute:
            output.warn("Dry run only. Re-run after every Codex client exits with --execute.")
            return 0
        _require_no_blockers()
        _attach_current_machine(layout, registration)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        output.err(f"Portable attach failed closed: {exc}")
        return 1
    output.good("This PC now points to the portable Codex home.")
    output.warn("Open a new terminal, then run `codex login` for this PC's keyring.")
    return 0


def detach_portable(root: str, *, execute: bool) -> int:
    layout = PortableLayout.from_root(root)
    try:
        _validate_layout(layout)
        registration = _registration(layout)
        if registration is None or registration.get("status") != "complete":
            raise ValueError("portable migration is not complete")
        if registration.get("mode") == "dual":
            raise ValueError("dual mode never attaches the user environment; detach is unnecessary")
        if _volume_unique_id(layout.root).casefold() != str(
            registration.get("volume_unique_id") or ""
        ).casefold():
            raise ValueError("portable volume identity mismatch")
        machine_id = _machine_id()
        records = _machine_records(registration)
        machine = records.get(machine_id)
        if not isinstance(machine, dict) or machine.get("status") not in {
            "pending", "attached",
        }:
            raise ValueError("this machine is not attached to the portable home")
        previous = machine.get("previous_user_environment")
        if not isinstance(previous, dict):
            raise ValueError("this machine has no environment rollback snapshot")
        output.section("Detach this Windows PC from Codex Portable")
        output.info(f"machine: {machine_id}")
        output.info(f"root:    {layout.root}")
        if not execute:
            output.warn("Dry run only. Re-run after every Codex client exits with --execute.")
            return 0
        _require_no_blockers()
        _write_user_environment({
            str(name): value if isinstance(value, str) else None
            for name, value in previous.items()
        })
        machine["status"] = "detached"
        machine["detached_at"] = _utc_now()
        _atomic_json(layout.registration, registration)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        output.err(f"Portable detach failed closed: {exc}")
        return 1
    output.good("This PC's previous Codex environment was restored.")
    return 0


def _migration_manifest_path(layout: PortableLayout) -> Path:
    candidate = layout.manifests / f"migration-{_timestamp()}.json"
    if not candidate.exists():
        return candidate
    return layout.manifests / f"migration-{_timestamp()}-{uuid.uuid4().hex[:8]}.json"


def _require_no_blockers() -> None:
    blockers = _blocking_processes()
    if blockers:
        _print_blocking_processes(blockers)
        summary = ", ".join(
            f"{item.get('Name')}({item.get('ProcessId')})" for item in blockers[:12]
        )
        raise RuntimeError(
            f"all Codex clients must exit for whole-home migration; blockers: {summary}"
        )


def _populate_data_stage(
    layout: PortableLayout,
    source_home: Path,
    sqlite_source: Path,
    sessions_source: Path,
    stage: Path,
    manifest: dict[str, object],
) -> None:
    stage.mkdir()
    stage_home = stage / "home"
    stage_sqlite = stage / "sqlite"
    excluded, internal_links = _copy_home(source_home, stage_home)
    rollouts = _merge_rollouts(
        [_default_sessions_source(source_home), sessions_source],
        stage_home / "sessions",
    )
    _copy_sqlite(sqlite_source, stage_sqlite)
    _rewrite_rollout_index(stage_sqlite, rollouts, layout.home / "sessions")
    checked = _quick_check_sqlite(stage_sqlite)
    _set_portable_config(stage_home / "config.toml", layout.sqlite)
    manifest.update({
        "rollouts": [
            {
                "id": item.session_id,
                "relative_path": item.relative_path.as_posix(),
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in rollouts
        ],
        "sqlite_files": _sqlite_inventory(stage_sqlite),
        "sqlite_quick_check": checked,
        "excluded_home_paths": excluded,
        "internal_home_links": internal_links,
        "data_ready_at": _utc_now(),
    })


def _finish_initial_data_move(
    layout: PortableLayout,
    registration: dict[str, object],
    migration_manifest: dict[str, object],
) -> None:
    stage = _registration_path(registration.get("staging_dir"), "staging_dir")
    for staged, final in ((stage / "home", layout.home), (stage / "sqlite", layout.sqlite)):
        if staged.is_dir():
            if final.exists():
                if not _directory_is_empty(final):
                    raise ValueError(f"refusing to replace non-empty portable target: {final}")
                final.rmdir()
            staged.rename(final)
        elif not final.is_dir() or _directory_is_empty(final):
            raise ValueError(f"neither staged nor completed portable data exists: {final}")
    if stage.exists():
        stage.rmdir()
    raw_links = migration_manifest.get("internal_home_links", [])
    if not isinstance(raw_links, list) or not all(
        isinstance(item, dict) for item in raw_links
    ):
        raise ValueError("migration manifest internal links are invalid")
    _restore_internal_links(layout.home, raw_links)
    registration["status"] = "data-ready"
    registration["data_moved_at"] = _utc_now()
    _atomic_json(layout.registration, registration)


def _dual_registered_path(
    layout: PortableLayout,
    registration: dict[str, object],
    field: str,
    *,
    parent: Path,
    prefix: str,
) -> Path:
    path = _registration_path(registration.get(field), field)
    if path.parent != parent or not path.name.startswith(prefix):
        raise ValueError(f"unsafe registered {field}: {path}")
    return path


def _archive_incomplete_dual_stage(
    layout: PortableLayout,
    registration: dict[str, object],
) -> None:
    stage = _dual_registered_path(
        layout, registration, "dual_staging_dir",
        parent=layout.root, prefix=".dual-staging-",
    )
    if stage.exists():
        archive = layout.backups / f"incomplete-dual-stage-{_timestamp()}-{uuid.uuid4().hex[:8]}"
        stage.rename(archive)
        archived = registration.setdefault("dual_incomplete_stages", [])
        if not isinstance(archived, list):
            raise ValueError("dual_incomplete_stages must be a list")
        archived.append(str(archive))
    registration["status"] = "dual-refresh-required"
    _atomic_json(layout.registration, registration)


def _start_dual_refresh(
    layout: PortableLayout,
    registration: dict[str, object],
    source_home: Path,
    sqlite_source: Path,
    sessions_source: Path,
    actual_volume: str,
) -> dict[str, object]:
    if (
        not source_home.is_dir() or not sqlite_source.is_dir()
        or not sessions_source.is_dir()
    ):
        raise FileNotFoundError("source home, SQLite, or sessions source is missing")
    attempt_id = str(uuid.uuid4())
    stage = layout.root / f".dual-staging-{attempt_id}"
    backup = layout.backups / f"pre-dual-refresh-{_timestamp()}-{attempt_id[:8]}"
    manifest: dict[str, object] = {
        "schema_version": 2,
        "mode": "dual",
        "migration_id": attempt_id,
        "started_at": _utc_now(),
        "root": str(layout.root),
        "volume_unique_id": actual_volume,
        "source_home": str(source_home),
        "sessions_source": str(sessions_source),
        "rollouts": [],
        "sqlite_files": [],
        "sqlite_quick_check": [],
        "excluded_home_paths": [],
    }
    registration.update({
        "mode": "dual",
        "status": "dual-stage-pending",
        "dual_staging_dir": str(stage),
        "dual_refresh_backup": str(backup),
    })
    _atomic_json(layout.registration, registration)
    output.info("==> Refreshing portable data from the current local fallback")
    output.detail(f"staging: {stage}")
    _populate_data_stage(
        layout, source_home, sqlite_source, sessions_source, stage, manifest
    )
    manifest_path = _migration_manifest_path(layout)
    _atomic_json(manifest_path, manifest)
    registration["migration_manifest"] = str(manifest_path)
    registration["status"] = "dual-data-move-pending"
    _atomic_json(layout.registration, registration)
    return manifest


def _finish_dual_data_move(
    layout: PortableLayout,
    registration: dict[str, object],
    manifest: dict[str, object],
) -> None:
    stage = _dual_registered_path(
        layout, registration, "dual_staging_dir",
        parent=layout.root, prefix=".dual-staging-",
    )
    backup = _dual_registered_path(
        layout, registration, "dual_refresh_backup",
        parent=layout.backups, prefix="pre-dual-refresh-",
    )
    output.info("==> Archiving the previous portable snapshot")
    output.detail(f"backup:  {backup}")
    backup.mkdir(parents=True, exist_ok=True)

    saved_bin = backup / "bin"
    if not os.path.lexists(saved_bin):
        if not os.path.lexists(layout.bin):
            raise FileNotFoundError(f"portable bin is missing before refresh: {layout.bin}")
        layout.bin.rename(saved_bin)
    if not layout.bin.exists():
        layout.bin.mkdir()
    elif not layout.bin.is_dir() or next(layout.bin.iterdir(), None) is not None:
        raise ValueError(f"portable bin is not empty during dual refresh: {layout.bin}")

    for name, final in (("home", layout.home), ("sqlite", layout.sqlite)):
        staged = stage / name
        saved = backup / name
        if staged.is_dir():
            if final.exists():
                if saved.exists():
                    raise ValueError(f"both current and backup {name} exist during refresh")
                final.rename(saved)
            elif not saved.is_dir():
                raise ValueError(f"neither current nor backup {name} exists during refresh")
            staged.rename(final)
        elif not final.is_dir() or not saved.is_dir():
            raise ValueError(f"dual {name} move cannot be resumed safely")
    if stage.exists():
        stage.rmdir()
    raw_links = manifest.get("internal_home_links", [])
    if not isinstance(raw_links, list) or not all(
        isinstance(item, dict) for item in raw_links
    ):
        raise ValueError("dual migration manifest internal links are invalid")
    _restore_internal_links(layout.home, raw_links)
    backups = registration.setdefault("dual_refresh_backups", [])
    if not isinstance(backups, list):
        raise ValueError("dual_refresh_backups must be a list")
    if str(backup) not in backups:
        backups.append(str(backup))
    registration["status"] = "dual-data-ready"
    registration["dual_data_moved_at"] = _utc_now()
    _atomic_json(layout.registration, registration)
    output.info("==> Refreshed portable data is ready")


def _migrate_dual(
    layout: PortableLayout,
    registration: dict[str, object],
    source_home: Path,
    sqlite_source: Path,
    sessions_source: Path,
    actual_volume: str,
    status: str,
) -> None:
    if status == "complete":
        if registration.get("mode") != "dual":
            raise ValueError("completed exclusive migration cannot be converted in place")
        return
    if status in {"backup-pending", "source-backed-up"}:
        raise ValueError(
            f"cannot select dual mode after exclusive cutover reached {status}"
        )

    if status == "dual-stage-pending":
        _archive_incomplete_dual_stage(layout, registration)
        status = "dual-refresh-required"
    if status == "dual-data-move-pending":
        manifest_path = _registration_path(
            registration.get("migration_manifest"), "migration_manifest"
        )
        _finish_dual_data_move(layout, registration, _read_json(manifest_path))
        status = "dual-data-ready"

    if status not in {
        "prepared", "data-ready", "cli-ready", "dual-data-ready",
        "dual-refresh-required",
    }:
        raise ValueError(f"unsupported dual migration phase: {status}")

    manifest = _start_dual_refresh(
        layout, registration, source_home, sqlite_source, sessions_source, actual_volume
    )
    _finish_dual_data_move(layout, registration, manifest)
    output.info("==> Installing the portable Codex CLI non-interactively")
    version = _install_cli(layout)
    _require_no_blockers()
    _write_launcher(layout, actual_volume, version)
    output.info("==> Restoring local Codex as the default command")
    _remove_portable_user_environment(layout)
    registration.update({
        "mode": "dual",
        "expected_cli_version": version,
        "status": "complete",
        "completed_at": _utc_now(),
        "source_backup": None,
        "machines": {},
    })
    _atomic_json(layout.registration, registration)


def migrate_portable(
    root: str,
    *,
    execute: bool,
    mode: Literal["dual", "exclusive"] = "dual",
) -> int:
    layout = PortableLayout.from_root(root)
    try:
        _validate_layout(layout)
        registration = _registration(layout)
        if registration is None:
            raise FileNotFoundError("run `codesync portable prepare` first")
        expected_volume = str(registration.get("volume_unique_id") or "")
        actual_volume = _volume_unique_id(layout.root)
        if not expected_volume or actual_volume.casefold() != expected_volume.casefold():
            raise ValueError("portable volume identity mismatch")
        source_home = _registration_path(registration.get("source_home"), "source_home")
        sqlite_source = _registration_path(
            registration.get("sqlite_source"), "sqlite_source"
        )
        sessions_source = _registration_path(
            registration.get("sessions_source"), "sessions_source"
        )
        status = str(registration.get("status") or "")
        registered_mode = registration.get("mode")
        if registered_mode is None and status in {
            "backup-pending", "source-backed-up", "complete",
            "rollback-pending", "rollback-home-restored", "rolled-back",
        }:
            registered_mode = "exclusive"
        if registered_mode not in {None, "dual", "exclusive"}:
            raise ValueError(f"unsupported registered portable mode: {registered_mode}")
        if registered_mode is not None and registered_mode != mode:
            raise ValueError(
                f"portable migration mode is already {registered_mode}; requested {mode}"
            )
        if mode == "dual" and status in {"backup-pending", "source-backed-up"}:
            raise ValueError(
                f"cannot select dual mode after exclusive cutover reached {status}"
            )

        output.section("Codex Portable migration")
        output.info(f"mode:     {mode}")
        output.info(f"source:   {source_home}")
        output.info(f"sessions: {sessions_source}")
        output.info(f"target:   {layout.root}")
        output.info(f"volume:   {actual_volume}")
        output.info(f"phase:    {status}")
        if not execute:
            blockers = _blocking_processes()
            if blockers:
                output.warn(
                    f"Migration is currently blocked by {len(blockers)} Codex/ChatGPT process(es)."
                )
                _print_blocking_processes(blockers)
            else:
                output.good("No blocking Codex/ChatGPT processes detected.")
            if mode == "dual":
                output.detail("C: remains the local fallback; V: is used via Start-Codex.ps1.")
            output.warn("Dry run only. Re-run after every Codex client exits with --execute.")
            return 0

        _require_no_blockers()
        if mode == "exclusive":
            registration["mode"] = "exclusive"
            _atomic_json(layout.registration, registration)
        migration_manifest: dict[str, object]
        manifest_value = registration.get("migration_manifest")
        if isinstance(manifest_value, str) and Path(manifest_value).is_file():
            migration_manifest = _read_json(Path(manifest_value))
        else:
            migration_manifest = {
                "schema_version": 1,
                "migration_id": str(uuid.uuid4()),
                "started_at": _utc_now(),
                "root": str(layout.root),
                "volume_unique_id": actual_volume,
                "source_home": str(source_home),
                "sessions_source": str(sessions_source),
                "rollouts": [],
                "sqlite_files": [],
                "sqlite_quick_check": [],
                "excluded_home_paths": [],
            }

        if mode == "dual" and status != "data-move-pending":
            _migrate_dual(
                layout, registration, source_home, sqlite_source,
                sessions_source, actual_volume, status,
            )
            status = "complete"

        if status == "prepared":
            if (
                not source_home.is_dir() or not sqlite_source.is_dir()
                or not sessions_source.is_dir()
            ):
                raise FileNotFoundError("source home, SQLite, or sessions source is missing")
            if not _directory_is_empty(layout.home) or not _directory_is_empty(layout.sqlite):
                raise ValueError("portable home/sqlite must be empty before migration")
            stage = layout.root / f".staging-{migration_manifest['migration_id']}"
            if stage.exists():
                raise FileExistsError(f"previous staging directory requires review: {stage}")
            _populate_data_stage(
                layout, source_home, sqlite_source, sessions_source,
                stage, migration_manifest,
            )
            manifest_path = _migration_manifest_path(layout)
            _atomic_json(manifest_path, migration_manifest)
            registration["migration_manifest"] = str(manifest_path)
            registration["staging_dir"] = str(stage)
            registration["status"] = "data-move-pending"
            _atomic_json(layout.registration, registration)
            status = "data-move-pending"

        if status == "data-move-pending":
            _finish_initial_data_move(layout, registration, migration_manifest)
            status = "data-ready"

        if mode == "dual":
            _migrate_dual(
                layout, registration, source_home, sqlite_source,
                sessions_source, actual_volume, status,
            )
            status = "complete"

        if mode == "exclusive" and status == "data-ready":
            version = _install_cli(layout)
            registration["expected_cli_version"] = version
            registration["status"] = "cli-ready"
            _write_launcher(layout, actual_volume, version)
            _atomic_json(layout.registration, registration)
            status = "cli-ready"

        if mode == "exclusive" and status == "cli-ready":
            if not source_home.is_dir():
                raise FileNotFoundError(f"source home vanished before backup: {source_home}")
            backup = source_home.with_name(
                f"{source_home.name}.pre-portable-{_timestamp()}"
            )
            if backup.exists():
                raise FileExistsError(f"backup target already exists: {backup}")
            registration["source_backup"] = str(backup)
            registration["status"] = "backup-pending"
            _atomic_json(layout.registration, registration)
            status = "backup-pending"

        if mode == "exclusive" and status == "backup-pending":
            backup = _registration_path(registration.get("source_backup"), "source_backup")
            if source_home.is_dir() and not backup.exists():
                source_home.rename(backup)
            elif source_home.exists() or not backup.is_dir():
                raise ValueError("source/backup state is inconsistent with pending backup intent")
            registration["status"] = "source-backed-up"
            registration["source_backed_up_at"] = _utc_now()
            _atomic_json(layout.registration, registration)
            status = "source-backed-up"

        if mode == "exclusive" and status == "source-backed-up":
            _attach_current_machine(layout, registration)
            registration["status"] = "complete"
            registration["completed_at"] = _utc_now()
            _atomic_json(layout.registration, registration)
            status = "complete"

        if status != "complete":
            raise ValueError(f"unsupported migration phase: {status}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        output.err(f"Portable migration failed closed: {exc}")
        return 1

    output.good("Portable migration completed.")
    if mode == "dual":
        output.info(f"Local fallback: {registration.get('source_home')}")
        output.info(f"Portable launch: {layout.launcher}")
        output.warn("Local and portable conversations remain intentionally separate.")
    else:
        output.info(f"Rollback source: {registration.get('source_backup')}")
        output.warn("Open a new terminal before validation; existing processes keep old environment.")
    return 0


def _forbidden_dropbox_files(root: Path) -> list[str]:
    forbidden: list[str] = []
    suffixes = (
        ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
        ".db", ".db-wal", ".db-shm", ".db-journal", ".lock", ".key", ".pem",
    )
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            if dirname.casefold() in {"thread-writer-locks", ".sandbox-secrets", "tmp", ".tmp"}:
                forbidden.append(str(current_path / dirname))
                dirnames.remove(dirname)
        for filename in filenames:
            lowered = filename.casefold()
            if (
                lowered == "auth.json" or lowered.startswith(".env")
                or lowered.endswith(suffixes) or "credential" in lowered
                or "conflicted copy" in lowered or "冲突副本" in lowered
            ):
                forbidden.append(str(current_path / filename))
    return forbidden


def build_portable_report(root: str, *, deep: bool) -> PortableReport:
    layout = PortableLayout.from_root(root)
    _validate_layout(layout)
    report = PortableReport(action="verify" if deep else "status", root=str(layout.root))
    try:
        report.volume_unique_id = _volume_unique_id(layout.root)
    except OSError as exc:
        report.diagnostics.append(PortableDiagnostic("error", "volume_unavailable", str(exc)))
        return report

    registration = _registration(layout)
    if registration is None:
        report.diagnostics.append(PortableDiagnostic(
            "error", "registration_missing", "portable layout has not been prepared",
            str(layout.registration),
        ))
        return report
    report.expected_volume_unique_id = str(registration.get("volume_unique_id") or "")
    report.registration_status = str(registration.get("status") or "")
    raw_mode = registration.get("mode")
    if raw_mode in {"dual", "exclusive"}:
        report.mode = str(raw_mode)
    elif report.registration_status in {
        "cli-ready", "backup-pending", "source-backed-up", "complete",
        "rollback-pending", "rollback-home-restored", "rolled-back",
    }:
        report.mode = "exclusive"
    else:
        report.mode = "unselected"
    report.source_home = str(registration.get("source_home") or "")
    report.sessions_source = str(registration.get("sessions_source") or "")
    if report.volume_unique_id.casefold() != report.expected_volume_unique_id.casefold():
        report.diagnostics.append(PortableDiagnostic(
            "error", "volume_mismatch", "mounted drive is not the registered portable volume",
            str(layout.root),
        ))
    for name, path in (("bin", layout.bin), ("home", layout.home), ("sqlite", layout.sqlite)):
        if not path.is_dir():
            report.diagnostics.append(PortableDiagnostic(
                "error", f"{name}_missing", f"required {name} directory is missing", str(path),
            ))
    if not layout.launcher.is_file():
        report.diagnostics.append(PortableDiagnostic(
            "error", "launcher_missing", "Start-Codex.ps1 is missing", str(layout.launcher),
        ))

    executable = layout.bin / "codex.exe"
    if executable.is_file():
        report.cli_path = str(executable)
        try:
            report.cli_version = _cli_version(executable)
        except (OSError, ValueError) as exc:
            report.diagnostics.append(PortableDiagnostic(
                "error", "cli_unusable", str(exc), str(executable),
            ))
    elif report.registration_status in {
        "cli-ready", "source-backed-up", "dual-data-ready", "complete",
    }:
        report.diagnostics.append(PortableDiagnostic(
            "error", "cli_missing", "portable codex.exe is missing", str(executable),
        ))

    expected_version = registration.get("expected_cli_version")
    if expected_version and report.cli_version != expected_version:
        report.diagnostics.append(PortableDiagnostic(
            "error", "cli_version_mismatch",
            f"expected {expected_version}, found {report.cli_version}", str(executable),
        ))

    config_path = layout.home / "config.toml"
    if config_path.is_file():
        sqlite_value = _toml_top_level_value(config_path, "sqlite_home")
        auth_value = _toml_top_level_value(config_path, "cli_auth_credentials_store")
        if sqlite_value is None or _path_key(sqlite_value) != _path_key(layout.sqlite):
            report.diagnostics.append(PortableDiagnostic(
                "error", "sqlite_home_mismatch", "config sqlite_home does not match portable sqlite",
                str(config_path),
            ))
        if auth_value != "keyring":
            report.diagnostics.append(PortableDiagnostic(
                "error", "credential_store_unsafe", "credential store must be keyring",
                str(config_path),
            ))
        if (layout.home / "auth.json").exists():
            report.diagnostics.append(PortableDiagnostic(
                "error", "portable_auth_json", "auth.json must not be on the portable drive",
                str(layout.home / "auth.json"),
            ))
    elif report.registration_status in {
        "data-ready", "cli-ready", "source-backed-up",
        "dual-data-move-pending", "dual-data-ready", "complete",
    }:
        report.diagnostics.append(PortableDiagnostic(
            "error", "config_missing", "portable config.toml is missing", str(config_path),
        ))

    try:
        report.blocking_processes = _blocking_processes()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report.diagnostics.append(PortableDiagnostic(
            "warning", "process_inventory_failed", str(exc),
        ))
    if report.blocking_processes:
        report.diagnostics.append(PortableDiagnostic(
            "warning", "active_codex_clients",
            f"{len(report.blocking_processes)} Codex/ChatGPT process(es) block portable maintenance",
        ))

    if report.registration_status == "complete":
        try:
            user_env = _user_environment()
            if report.mode == "dual":
                for name, expected in {
                    "CODEX_HOME": layout.home,
                    "CODEX_SQLITE_HOME": layout.sqlite,
                    "CODEX_INSTALL_DIR": layout.bin,
                }.items():
                    value = user_env.get(name)
                    if value and _path_key(value) == _path_key(expected):
                        report.diagnostics.append(PortableDiagnostic(
                            "error", "dual_environment_leak",
                            f"dual mode must not persist user {name} to portable storage",
                            value,
                        ))
                path_value = user_env.get("Path") or ""
                if any(
                    _path_key(item) == _path_key(layout.bin)
                    for item in path_value.split(os.pathsep) if item
                ):
                    report.diagnostics.append(PortableDiagnostic(
                        "error", "dual_path_leak",
                        "dual mode must not persist portable bin in user PATH",
                        str(layout.bin),
                    ))
                source = Path(str(registration.get("source_home") or ""))
                if not source.is_dir():
                    report.diagnostics.append(PortableDiagnostic(
                        "error", "local_fallback_missing",
                        "dual mode local fallback CODEX_HOME is missing", str(source),
                    ))
            else:
                machine_id = _machine_id()
                machines = _machine_records(registration)
                machine = machines.get(machine_id)
                if not isinstance(machine, dict) or machine.get("status") != "attached":
                    report.diagnostics.append(PortableDiagnostic(
                        "error", "machine_not_attached",
                        "this Windows installation is not attached to the portable home",
                        machine_id,
                    ))
                expected_env = {
                    "CODEX_HOME": str(layout.home),
                    "CODEX_SQLITE_HOME": str(layout.sqlite),
                    "CODEX_INSTALL_DIR": str(layout.bin),
                }
                for name, expected in expected_env.items():
                    if _path_key(user_env.get(name) or "") != _path_key(expected):
                        report.diagnostics.append(PortableDiagnostic(
                            "error", "user_environment_mismatch",
                            f"user {name} does not point to portable storage", expected,
                        ))
        except OSError as exc:
            report.diagnostics.append(PortableDiagnostic(
                "error", "user_environment_unreadable", str(exc),
            ))

    if deep and report.registration_status == "complete":
        manifest_value = registration.get("migration_manifest")
        if not isinstance(manifest_value, str) or not Path(manifest_value).is_file():
            report.diagnostics.append(PortableDiagnostic(
                "error", "migration_manifest_missing", "migration manifest is missing",
            ))
        else:
            manifest = _read_json(Path(manifest_value))
            rollouts = manifest.get("rollouts")
            if not isinstance(rollouts, list):
                raise ValueError("migration manifest has no rollout inventory")
            for item in rollouts:
                if not isinstance(item, dict):
                    raise ValueError("invalid rollout manifest item")
                path = layout.home / "sessions" / str(item["relative_path"])
                baseline_size = item.get("size")
                baseline_hash = item.get("sha256")
                if (
                    not path.is_file() or not isinstance(baseline_size, int)
                    or path.stat().st_size < baseline_size
                    or _sha256_prefix(path, baseline_size) != baseline_hash
                ):
                    report.diagnostics.append(PortableDiagnostic(
                        "error", "rollout_hash_mismatch", "portable rollout differs from manifest",
                        str(path),
                    ))
            try:
                _quick_check_sqlite(layout.sqlite)
            except (OSError, ValueError, sqlite3.Error) as exc:
                report.diagnostics.append(PortableDiagnostic(
                    "error", "sqlite_quick_check_failed", str(exc), str(layout.sqlite),
                ))
            sessions_source = Path(str(registration.get("sessions_source") or ""))
            if sessions_source.is_dir():
                forbidden = _forbidden_dropbox_files(sessions_source)
                for path in forbidden:
                    report.diagnostics.append(PortableDiagnostic(
                        "error", "forbidden_transport_content",
                        "machine-local or secret content exists in conversation transport", path,
                    ))

        if report.mode != "dual":
            source = Path(str(registration.get("source_home") or ""))
            backup = Path(str(registration.get("source_backup") or ""))
            if source.exists():
                report.diagnostics.append(PortableDiagnostic(
                    "error", "old_home_still_live", "old CODEX_HOME still exists", str(source),
                ))
            if not backup.is_dir():
                report.diagnostics.append(PortableDiagnostic(
                    "error", "rollback_backup_missing", "C: rollback backup is missing", str(backup),
                ))
    return report


def _print_report(report: PortableReport) -> None:
    output.section(f"Codex Portable {report.action}")
    output.info(f"root:     {report.root}")
    output.info(f"volume:   {report.volume_unique_id or '(unavailable)'}")
    output.info(f"expected: {report.expected_volume_unique_id or '(not registered)'}")
    output.info(f"mode:     {report.mode or '(unselected)'}")
    output.info(f"phase:    {report.registration_status or '(not prepared)'}")
    output.info(f"CLI:      {report.cli_path or '(not installed)'}")
    if report.cli_version:
        output.detail(f"codex-cli {report.cli_version}")
    output.info(f"blockers: {len(report.blocking_processes)}")
    _print_blocking_processes(report.blocking_processes)
    for item in report.diagnostics:
        message = f"[{item.code}] {item.message}"
        if item.path:
            message += f" ({item.path})"
        (output.err if item.severity == "error" else output.warn)(message)
    output.info(
        f"result: {report.error_count} error(s), {report.warning_count} warning(s)"
    )


def report_portable(root: str, *, deep: bool, json_output: bool) -> int:
    try:
        report = build_portable_report(root, deep=deep)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if json_output:
            print(json.dumps({
                "action": "verify" if deep else "status",
                "root": root,
                "error_count": 1,
                "warning_count": 0,
                "diagnostics": [{
                    "severity": "error", "code": "portable_error", "message": str(exc),
                }],
            }, ensure_ascii=False, indent=2))
        else:
            output.err(f"Portable inspection failed: {exc}")
        return 1
    if json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 1 if report.error_count else 0


def rollback_portable(root: str, *, execute: bool) -> int:
    layout = PortableLayout.from_root(root)
    try:
        _validate_layout(layout)
        registration = _registration(layout)
        if registration is None or registration.get("status") not in {
            "complete", "rollback-pending", "rollback-home-restored",
        }:
            raise ValueError("only a completed portable migration can be rolled back")
        if registration.get("mode") == "dual":
            raise ValueError("dual mode retained the local home; no local cutover exists to roll back")
        if _volume_unique_id(layout.root).casefold() != str(
            registration.get("volume_unique_id") or ""
        ).casefold():
            raise ValueError("portable volume identity mismatch")
        source = _registration_path(registration.get("source_home"), "source_home")
        backup = _registration_path(registration.get("source_backup"), "source_backup")
        machine_id = _machine_id()
        if registration.get("origin_machine_id") != machine_id:
            raise ValueError("whole-home rollback is allowed only on the origin machine")
        machines = registration.get("machines")
        if not isinstance(machines, dict):
            raise ValueError("machine attachment records are missing")
        active_others = [
            key for key, value in machines.items()
            if key != machine_id and isinstance(value, dict)
            and value.get("status") in {"pending", "attached"}
        ]
        if active_others:
            raise ValueError("detach other registered machines before whole-home rollback")
        machine = machines.get(machine_id)
        previous = machine.get("previous_user_environment") if isinstance(machine, dict) else None
        if not isinstance(previous, dict):
            raise ValueError("origin machine environment backup is missing")
        status = str(registration.get("status"))
        if status == "complete":
            if source.exists():
                raise FileExistsError(f"refusing to overwrite existing old home: {source}")
            if not backup.is_dir():
                raise FileNotFoundError(f"rollback backup not found: {backup}")
        elif status == "rollback-pending":
            if not (
                (not source.exists() and backup.is_dir())
                or (source.is_dir() and not backup.exists())
            ):
                raise ValueError("source/backup state is inconsistent with rollback intent")
        elif not source.is_dir() or backup.exists():
            raise FileNotFoundError(f"partially restored old home is missing: {source}")
        output.section("Codex Portable rollback")
        output.info(f"restore: {backup} -> {source}")
        output.warn(f"portable evidence will be retained at {layout.root}")
        if not execute:
            output.warn("Dry run only. Re-run after every Codex client exits with --execute.")
            return 0
        _require_no_blockers()
        if status == "complete":
            registration["status"] = "rollback-pending"
            _atomic_json(layout.registration, registration)
            status = "rollback-pending"
        if status == "rollback-pending":
            if backup.is_dir() and not source.exists():
                backup.rename(source)
            registration["status"] = "rollback-home-restored"
            _atomic_json(layout.registration, registration)
        _write_user_environment({
            str(name): value if isinstance(value, str) else None
            for name, value in previous.items()
        })
        if isinstance(machine, dict):
            machine["status"] = "detached"
            machine["detached_at"] = _utc_now()
        registration["status"] = "rolled-back"
        registration["rolled_back_at"] = _utc_now()
        _atomic_json(layout.registration, registration)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        output.err(f"Portable rollback failed closed: {exc}")
        return 1
    output.good("Portable migration rolled back; V: evidence was retained.")
    return 0


def run_portable(
    action: Literal[
        "status", "prepare", "migrate", "verify", "attach", "detach", "rollback",
        "alias",
    ],
    *,
    root: str = DEFAULT_ROOT,
    source_home: str | None = None,
    sessions_source: str | None = None,
    execute: bool = False,
    json_output: bool = False,
    migration_mode: Literal["dual", "exclusive"] = "dual",
    remove_alias: bool = False,
) -> int:
    if action == "status":
        return report_portable(root, deep=False, json_output=json_output)
    if action == "prepare":
        return prepare_portable(
            root, source_home=source_home, sessions_source=sessions_source
        )
    if action == "migrate":
        return migrate_portable(root, execute=execute, mode=migration_mode)
    if action == "verify":
        return report_portable(root, deep=True, json_output=json_output)
    if action == "attach":
        return attach_portable(root, execute=execute)
    if action == "detach":
        return detach_portable(root, execute=execute)
    if action == "rollback":
        return rollback_portable(root, execute=execute)
    if action == "alias":
        return configure_portable_alias(root, execute=execute, remove=remove_alias)
    output.err(f"Unknown portable action: {action}")
    return 2
