from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from codesync import cli, portable


SESSION_ID = "11111111-2222-4333-8444-555555555555"
VOLUME_ID = "\\\\?\\Volume{11111111-2222-3333-4444-555555555555}\\"


def _rollout(root: Path, *, suffix: str = "", body: bytes | None = None) -> Path:
    directory = root / "2026" / "09" / "03"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-03T12-00-00{suffix}-{SESSION_ID}.jsonl"
    first = json.dumps({
        "type": "session_meta",
        "payload": {"id": SESSION_ID, "cwd": r"V:\SyncRepos\project"},
    }).encode() + b"\n"
    path.write_bytes(body if body is not None else first)
    return path


def _state_db(home: Path, rollout: Path) -> Path:
    database = home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT)"
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?)",
        (SESSION_ID, str(rollout), r"V:\SyncRepos\project"),
    )
    connection.commit()
    connection.close()
    return database


def _ready_source(path: Path) -> tuple[Path, Path]:
    sessions = path / "sessions"
    sessions.mkdir(parents=True)
    rollout = _rollout(sessions)
    _state_db(path, rollout)
    (path / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    return sessions, rollout


@pytest.fixture
def portable_platform(monkeypatch):
    monkeypatch.setattr(portable, "_validate_layout", lambda _layout: None)
    monkeypatch.setattr(portable, "_volume_unique_id", lambda _path: VOLUME_ID)
    monkeypatch.setattr(portable, "_machine_id", lambda: "machine-one")


def test_prepare_creates_only_portable_scaffold(tmp_path, portable_platform):
    source = tmp_path / "source-home"
    _ready_source(source)
    marker = source / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    root = tmp_path / "CodexPortable"

    result = portable.prepare_portable(str(root), source_home=str(source))

    assert result == 0
    layout = portable.PortableLayout.from_root(root)
    assert all(path.is_dir() for path in (
        layout.bin, layout.home, layout.sqlite, layout.backups, layout.manifests,
    ))
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["status"] == "prepared"
    assert registration["volume_unique_id"] == VOLUME_ID
    assert layout.launcher.is_file()
    inventory = json.loads(Path(registration["inventory_manifest"]).read_text(encoding="utf-8"))
    assert inventory["rollout_count"] == 1
    assert inventory["indexed_rollouts"] == 1
    assert inventory["sqlite_quick_check"] == "ok"
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_prepare_rejects_unrelated_existing_content(tmp_path, portable_platform):
    source = tmp_path / "source"
    _ready_source(source)
    root = tmp_path / "CodexPortable"
    root.mkdir()
    (root / "unrelated.txt").write_text("x", encoding="utf-8")

    assert portable.prepare_portable(str(root), source_home=str(source)) == 1
    assert not (root / "manifests" / portable.REGISTRATION_NAME).exists()


def test_rollout_merge_accepts_identical_and_strict_prefix(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    base = _rollout(first).read_bytes()
    _rollout(second, suffix="-copy", body=base + b'{"type":"event_msg"}\n')
    target = tmp_path / "target"

    merged = portable._merge_rollouts([first, first, second], target)

    assert len(merged) == 1
    assert merged[0].size > len(base)
    assert (target / merged[0].relative_path).read_bytes().endswith(b"}\n")


def test_rollout_merge_rejects_divergence(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    base = _rollout(first).read_bytes()
    _rollout(second, suffix="-copy", body=base + b'{"value":"two"}\n')
    _rollout(first).write_bytes(base + b'{"value":"one"}\n')

    with pytest.raises(ValueError, match="divergent rollout"):
        portable._merge_rollouts([first, second], tmp_path / "target")


def test_copy_home_excludes_live_and_secret_state(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for directory in (
        "sessions", "thread-writer-locks", ".sandbox-secrets", "tmp",
        "plugins/cache/tool/plugin-backup-old",
        "plugins/cache/tool/plugin-install-partial",
    ):
        (source / directory).mkdir(parents=True)
        (source / directory / "item").write_text("x", encoding="utf-8")
    (source / "auth.json").write_text("secret", encoding="utf-8")
    (source / ".env.local").write_text("secret", encoding="utf-8")
    (source / "state_5.sqlite").write_text("db", encoding="utf-8")
    (source / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    target = tmp_path / "target"

    excluded, links = portable._copy_home(source, target)

    assert (target / "config.toml").is_file()
    assert not (target / "sessions").exists()
    assert not (target / "auth.json").exists()
    assert not (target / ".env.local").exists()
    assert not (target / "state_5.sqlite").exists()
    assert not (target / "plugins/cache/tool/plugin-backup-old").exists()
    assert "auth.json" in excluded
    assert links == []


def test_copy_home_retargets_internal_directory_links_after_staging_move(tmp_path):
    source = tmp_path / "source"
    release = source / "plugins" / "cache" / "tool" / "1.0"
    release.mkdir(parents=True)
    (release / "payload.txt").write_text("ok", encoding="utf-8")
    link = release.parent / "latest"
    try:
        portable._create_directory_link(link, release)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")
    stage = tmp_path / "stage-home"
    final = tmp_path / "final-home"

    _excluded, links = portable._copy_home(source, stage)
    stage.rename(final)
    portable._restore_internal_links(final, links)

    assert (final / "plugins" / "cache" / "tool" / "latest" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "ok"


def test_sqlite_index_rewrite_and_quick_check(tmp_path):
    source = tmp_path / "source"
    sessions = source / "sessions"
    sessions.mkdir(parents=True)
    rollout = _rollout(sessions)
    _state_db(source, rollout)
    copied = tmp_path / "sqlite"
    portable._copy_sqlite(source, copied)
    records = portable._scan_rollouts(sessions)

    portable._rewrite_rollout_index(copied, records, tmp_path / "portable-sessions")
    files_before = sorted(path.name for path in copied.iterdir())
    bytes_before = (copied / "state_5.sqlite").read_bytes()
    assert portable._quick_check_sqlite(copied) == ["state_5.sqlite"]
    assert sorted(path.name for path in copied.iterdir()) == files_before
    assert (copied / "state_5.sqlite").read_bytes() == bytes_before
    connection = sqlite3.connect(copied / "state_5.sqlite")
    try:
        indexed_path = connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ?", (SESSION_ID,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert indexed_path == str(
        tmp_path / "portable-sessions" / records[0].relative_path
    )


def test_migrate_dry_run_reports_but_does_not_change_processes(
    tmp_path, portable_platform, monkeypatch, capsys,
):
    source = tmp_path / "source"
    _ready_source(source)
    root = tmp_path / "CodexPortable"
    assert portable.prepare_portable(str(root), source_home=str(source)) == 0
    monkeypatch.setattr(portable, "_blocking_processes", lambda: [{
        "ProcessId": 4242,
        "Name": "codex.exe",
        "ExecutablePath": r"C:\Program Files\OpenAI\Codex\codex.exe",
        "CommandLine": "codex.exe --secret-value must-not-be-rendered",
    }])
    monkeypatch.setattr(portable, "_require_no_blockers", lambda: pytest.fail("no write preflight"))

    assert portable.migrate_portable(str(root), execute=False) == 0
    assert source.is_dir()
    captured = capsys.readouterr()
    assert "PID 4242: codex.exe" in captured.out
    assert r"C:\Program Files\OpenAI\Codex\codex.exe" in captured.out
    assert "Stop-Process -Id <PID>" in captured.out
    assert "secret-value" not in captured.out


def test_require_no_blockers_prints_pid_before_failing(monkeypatch, capsys):
    monkeypatch.setattr(portable, "_blocking_processes", lambda: [{
        "ProcessId": 99,
        "ParentProcessId": 10,
        "Name": "codex-code-mode-host.exe",
        "ExecutablePath": None,
        "CommandLine": "sensitive argument",
    }])

    with pytest.raises(RuntimeError, match=r"codex-code-mode-host\.exe\(99\)"):
        portable._require_no_blockers()

    captured = capsys.readouterr()
    assert "PID 99: codex-code-mode-host.exe" in captured.out
    assert "Stop-Process -Id <PID>" in captured.out
    assert "sensitive argument" not in captured.out


def test_install_cli_enables_official_download_progress(tmp_path, monkeypatch):
    layout = portable.PortableLayout.from_root(tmp_path / "CodexPortable")
    layout.bin.mkdir(parents=True)
    layout.home.mkdir()
    layout.sqlite.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        (layout.bin / "codex.exe").write_bytes(b"fake")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(portable.proc, "run", fake_run)
    monkeypatch.setattr(portable, "_cli_version", lambda _path: "0.153.0")

    assert portable._install_cli(layout) == "0.153.0"
    argv, kwargs = calls[0]
    command = argv[-1]
    assert portable.INSTALLER_URL in command
    assert ".Replace($silent, $visible)" in command
    assert "$visible = '$ProgressPreference = \"Continue\"'" in command
    assert "cannot safely enable download progress" in command
    assert kwargs["capture"] is False
    assert kwargs["stdin_devnull"] is False


def test_migrate_fails_before_copy_when_a_codex_client_is_active(
    tmp_path, portable_platform, monkeypatch,
):
    source = tmp_path / "source"
    _ready_source(source)
    root = tmp_path / "CodexPortable"
    portable.prepare_portable(str(root), source_home=str(source))
    monkeypatch.setattr(
        portable, "_require_no_blockers",
        lambda: (_ for _ in ()).throw(RuntimeError("codex.exe(42)")),
    )

    assert portable.migrate_portable(str(root), execute=True) == 1
    assert source.is_dir()
    assert next((root / "home").iterdir(), None) is None


def test_end_to_end_migrate_and_rollback(tmp_path, portable_platform, monkeypatch):
    source = tmp_path / ".codex"
    sessions = source / "sessions"
    sessions.mkdir(parents=True)
    rollout = _rollout(sessions)
    _state_db(source, rollout)
    (source / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    (source / "memories").mkdir()
    (source / "memories" / "note.md").write_text("memory", encoding="utf-8")
    (source / "auth.json").write_text("secret", encoding="utf-8")
    root = tmp_path / "CodexPortable"
    assert portable.prepare_portable(str(root), source_home=str(source)) == 0

    monkeypatch.setattr(portable, "_require_no_blockers", lambda: None)
    monkeypatch.setattr(
        portable, "_user_environment",
        lambda: {
            "CODEX_HOME": None,
            "CODEX_SQLITE_HOME": None,
            "CODEX_INSTALL_DIR": None,
            "Path": "C:/Windows",
        },
    )
    environment_writes: list[dict[str, str | None]] = []
    monkeypatch.setattr(
        portable, "_write_user_environment",
        lambda values: environment_writes.append(values),
    )

    def fake_install(layout):
        (layout.bin / "codex.exe").write_bytes(b"fake")
        return "0.153.0"

    monkeypatch.setattr(portable, "_install_cli", fake_install)
    monkeypatch.setattr(portable, "_cli_version", lambda _path: "0.153.0")

    assert portable.migrate_portable(str(root), execute=True) == 0
    layout = portable.PortableLayout.from_root(root)
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    backup = Path(registration["source_backup"])
    assert registration["status"] == "complete"
    assert not source.exists()
    assert backup.is_dir()
    assert (layout.home / "memories" / "note.md").read_text(encoding="utf-8") == "memory"
    assert not (layout.home / "auth.json").exists()
    assert portable._toml_top_level_value(
        layout.home / "config.toml", "cli_auth_credentials_store"
    ) == "keyring"
    assert portable._path_key(portable._toml_top_level_value(
        layout.home / "config.toml", "sqlite_home"
    )) == portable._path_key(layout.sqlite)
    connection = sqlite3.connect(layout.sqlite / "state_5.sqlite")
    try:
        indexed = connection.execute("SELECT rollout_path FROM threads").fetchone()[0]
    finally:
        connection.close()
    assert portable._path_key(indexed).startswith(
        portable._path_key(layout.home / "sessions")
    )
    assert environment_writes

    assert portable.detach_portable(str(root), execute=True) == 0
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["machines"]["machine-one"]["status"] == "detached"
    assert portable.attach_portable(str(root), execute=True) == 0
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["machines"]["machine-one"]["status"] == "attached"

    registration["machines"]["machine-two"] = {
        "status": "attached",
        "previous_user_environment": {},
    }
    portable._atomic_json(layout.registration, registration)
    assert portable.rollback_portable(str(root), execute=True) == 1
    registration["machines"]["machine-two"]["status"] = "detached"
    portable._atomic_json(layout.registration, registration)

    assert portable.rollback_portable(str(root), execute=True) == 0
    assert source.is_dir()
    assert not backup.exists()
    assert layout.home.is_dir()
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["status"] == "rolled-back"


def test_migrate_resumes_pending_data_move_and_source_backup(
    tmp_path, portable_platform, monkeypatch,
):
    source = tmp_path / ".codex"
    _ready_source(source)
    root = tmp_path / "CodexPortable"
    assert portable.prepare_portable(str(root), source_home=str(source)) == 0
    layout = portable.PortableLayout.from_root(root)
    monkeypatch.setattr(portable, "_require_no_blockers", lambda: None)
    monkeypatch.setattr(
        portable, "_user_environment",
        lambda: {
            "CODEX_HOME": None, "CODEX_SQLITE_HOME": None,
            "CODEX_INSTALL_DIR": None, "Path": "C:/Windows",
        },
    )
    monkeypatch.setattr(portable, "_write_user_environment", lambda _values: None)

    def fake_install(target_layout):
        (target_layout.bin / "codex.exe").write_bytes(b"fake")
        return "0.153.0"

    monkeypatch.setattr(portable, "_install_cli", fake_install)

    original_rename = Path.rename
    fail_move = {"enabled": True}

    def flaky_rename(path, target):
        if (
            fail_move["enabled"] and path.name == "sqlite"
            and path.parent.name.startswith(".staging-")
        ):
            fail_move["enabled"] = False
            raise OSError("simulated interruption during final data move")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    assert portable.migrate_portable(str(root), execute=True) == 1
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["status"] == "data-move-pending"
    assert any(path.name == "sqlite" for path in root.glob(".staging-*/sqlite"))
    assert any(layout.home.iterdir())

    original_atomic = portable._atomic_json
    fail_backup = {"enabled": True}

    def flaky_atomic(path, value):
        if fail_backup["enabled"] and value.get("status") == "source-backed-up":
            fail_backup["enabled"] = False
            raise OSError("simulated interruption after source backup rename")
        return original_atomic(path, value)

    monkeypatch.setattr(portable, "_atomic_json", flaky_atomic)
    assert portable.migrate_portable(str(root), execute=True) == 1
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["status"] == "backup-pending"
    assert not source.exists()
    assert Path(registration["source_backup"]).is_dir()

    assert portable.migrate_portable(str(root), execute=True) == 0
    registration = json.loads(layout.registration.read_text(encoding="utf-8"))
    assert registration["status"] == "complete"


@pytest.mark.parametrize("action", ["status", "verify"])
def test_portable_parser_read_only_actions(action):
    parser = cli._build_parser()
    args = parser.parse_args(["portable", action, "--root", r"V:\CodexPortable", "--json"])
    assert args.command == "portable"
    assert args.portable_command == action
    assert args.json is True
    assert cli._needs_ssh(args) is False
    assert cli._uses_code_roots(args) is False


@pytest.mark.parametrize("action", ["migrate", "attach", "detach", "rollback"])
def test_portable_parser_write_actions_default_to_dry_run(action):
    parser = cli._build_parser()
    args = parser.parse_args(["portable", action])
    assert args.execute is False
    args = parser.parse_args(["portable", action, "--execute"])
    assert args.execute is True
