from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from portablecodex import config, context_sync


SESSION_ID = "11111111-2222-4333-8444-555555555555"


def _write_rollout(
    sessions: Path,
    *,
    session_id: str = SESSION_ID,
    day: str = "03",
    cwd: str = "/work/repo",
    extra_lines: list[str] | None = None,
) -> Path:
    directory = sessions / "2026" / "09" / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-{day}T12-00-00-{session_id}.jsonl"
    records = [
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": cwd,
                "timestamp": f"2026-09-{day}T12:00:00Z",
            },
        }),
        json.dumps({"type": "response_item", "payload": {"text": "hello"}}),
    ]
    if extra_lines:
        records.extend(extra_lines)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


def _write_index(codex_home: Path, entries: list[tuple[str, Path, str]]) -> Path:
    database = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE threads ("
        "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cwd TEXT NOT NULL, "
        "archived INTEGER NOT NULL DEFAULT 0)"
    )
    connection.executemany(
        "INSERT INTO threads (id, rollout_path, cwd) VALUES (?, ?, ?)",
        [(session_id, str(path), cwd) for session_id, path, cwd in entries],
    )
    connection.commit()
    connection.close()
    return database


@pytest.fixture
def context_home(tmp_path, monkeypatch):
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(context_sync, "_default_codex_home", lambda: sessions.parent)
    monkeypatch.setattr(config, "load_context_config", lambda: None)
    return sessions


def test_status_reports_valid_rollout_and_index_without_writing(context_home):
    rollout = _write_rollout(context_home)
    database = _write_index(context_home.parent, [(SESSION_ID, rollout, "/work/repo")])
    rollout_before = rollout.read_bytes()
    database_before = database.read_bytes()
    codex_entries_before = sorted(path.name for path in context_home.parent.iterdir())

    report = context_sync.build_report(
        mode="status",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.session_count == 1
    assert report.files_seen == 1
    assert report.indexed_sessions == 1
    assert report.transport_connected is True
    assert report.error_count == 0
    assert {item.code for item in report.diagnostics} == {"transport_without_link"}
    assert rollout.read_bytes() == rollout_before
    assert database.read_bytes() == database_before
    assert sorted(path.name for path in context_home.parent.iterdir()) == codex_entries_before


def test_index_is_read_from_a_temporary_snapshot(context_home, monkeypatch):
    rollout = _write_rollout(context_home)
    database = _write_index(context_home.parent, [(SESSION_ID, rollout, "/work/repo")])
    copied: list[tuple[Path, Path]] = []
    real_copyfile = context_sync.shutil.copyfile

    def recording_copy(source, target):
        copied.append((Path(source), Path(target)))
        return real_copyfile(source, target)

    monkeypatch.setattr(context_sync.shutil, "copyfile", recording_copy)
    report = context_sync.build_report(
        mode="status",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.error_count == 0
    assert copied
    assert copied[0][0] == database
    assert copied[0][1].parent != database.parent


def test_doctor_deep_validates_every_jsonl_line(context_home):
    rollout = _write_rollout(context_home, extra_lines=["{broken-json"])
    _write_index(context_home.parent, [(SESSION_ID, rollout, "/work/repo")])

    report = context_sync.build_report(
        mode="doctor",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.session_count == 0
    assert report.error_count == 1
    diagnostic = next(item for item in report.diagnostics if item.code == "invalid_rollout")
    assert "line 3" in diagnostic.message


@pytest.mark.parametrize(
    "relative",
    [
        "2026/09/03/state_5.sqlite-wal",
        "2026/09/03/auth.json",
        "2026/09/03/.env.local",
        "2026/09/03/private.key",
        "2026/09/03/rollout-conflicted copy.jsonl",
        "thread-writer-locks/session.lock",
    ],
)
def test_forbidden_machine_local_files_are_errors(context_home, relative):
    candidate = context_home / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("secret", encoding="utf-8")

    report = context_sync.build_report(
        mode="status",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.error_count >= 1
    assert any(item.code.startswith("forbidden_") for item in report.diagnostics)


def test_status_warns_but_doctor_fails_when_index_is_missing_session(context_home):
    _write_rollout(context_home)
    _write_index(context_home.parent, [])

    status = context_sync.build_report(
        mode="status",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )
    doctor = context_sync.build_report(
        mode="doctor",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert status.error_count == 0
    assert status.missing_index_count == 1
    assert doctor.error_count == 1
    assert doctor.missing_index_count == 1


def test_duplicate_session_ids_fail_closed(context_home):
    first = _write_rollout(context_home, day="02")
    second = _write_rollout(context_home, day="03")
    _write_index(context_home.parent, [(SESSION_ID, first, "/work/repo")])
    assert first != second

    report = context_sync.build_report(
        mode="status",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.duplicate_id_count == 1
    assert any(item.code == "duplicate_session_ids" for item in report.diagnostics)
    assert report.error_count == 1


def test_index_path_mismatch_is_visible(context_home):
    _write_rollout(context_home)
    wrong = context_home / "2026" / "09" / "03" / "wrong.jsonl"
    _write_index(context_home.parent, [(SESSION_ID, wrong, "/work/repo")])

    report = context_sync.build_report(
        mode="doctor",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.stale_index_count == 1
    assert report.path_mismatch_count == 1
    assert report.error_count == 2


def test_writer_locks_are_scoped_and_reported(context_home, monkeypatch):
    rollout = _write_rollout(context_home)
    _write_index(context_home.parent, [(SESSION_ID, rollout, "/work/repo")])
    lock_dir = context_home.parent / "thread-writer-locks"
    lock_dir.mkdir()
    (lock_dir / f"{SESSION_ID}.lock").touch()
    (lock_dir / ".coordination.lock").touch()
    monkeypatch.setattr(context_sync, "_probe_writer_lock", lambda _path: "held")

    report = context_sync.build_report(
        mode="status",
        sessions_override=str(context_home),
        transport_override=str(context_home),
    )

    assert report.writer_lock_files == 1
    assert report.held_writer_locks == 1
    assert report.error_count == 0
    assert any(item.code == "active_session_writers" for item in report.diagnostics)


def test_run_context_json_is_machine_readable(context_home, capsys):
    rollout = _write_rollout(context_home)
    _write_index(context_home.parent, [(SESSION_ID, rollout, "/work/repo")])

    result = context_sync.run_context(
        "status",
        sessions_dir=str(context_home),
        transport_root=str(context_home),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["session_count"] == 1
    assert payload["error_count"] == 0


def test_context_config_read_is_non_creating(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config.paths, "config_file", lambda: config_path)
    monkeypatch.setattr(
        config.paths, "legacy_codesync_config_file", lambda: tmp_path / "legacy.toml",
    )
    assert config.load_context_config() is None
    assert not config_path.exists()


def test_context_config_round_trip_preserves_windows_paths(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config.paths, "config_file", lambda: config_path)
    expected = config.ContextConfig(
        sessions_dir=r"C:\Users\me\.codex\sessions",
        transport_root=r"D:\Dropbox\CodexSessions",
    )
    config_path.write_text(
        config._to_toml(config.Config(context=expected)), encoding="utf-8"
    )

    assert config.load_context_config() == expected
    assert config.load().context == expected


def test_context_config_reads_legacy_codesync_without_writing(tmp_path, monkeypatch):
    current = tmp_path / "portablecodex.toml"
    legacy = tmp_path / "codesync.toml"
    monkeypatch.setattr(config.paths, "config_file", lambda: current)
    monkeypatch.setattr(config.paths, "legacy_codesync_config_file", lambda: legacy)
    legacy.write_text(
        "[context]\n"
        'sessions_dir = "D:\\\\Dropbox\\\\CodexSessions"\n'
        'transport_root = "D:\\\\Dropbox\\\\CodexSessions"\n',
        encoding="utf-8",
    )

    loaded = config.load_context_config()
    assert loaded is not None
    assert loaded.sessions_dir == r"D:\Dropbox\CodexSessions"
    assert not current.exists()


def test_remember_root_preserves_legacy_context(tmp_path, monkeypatch):
    current = tmp_path / "portablecodex.toml"
    legacy = tmp_path / "codesync.toml"
    monkeypatch.setattr(config.paths, "config_file", lambda: current)
    monkeypatch.setattr(config.paths, "legacy_codesync_config_file", lambda: legacy)
    legacy.write_text(
        "[context]\ntransport_root = 'D:\\Dropbox\\CodexSessions'\n",
        encoding="utf-8",
    )

    config.remember_root(r"V:\CodexPortable")

    loaded = config.load(include_legacy=False)
    assert loaded.portable_root == r"V:\CodexPortable"
    assert loaded.context is not None
    assert loaded.context.transport_root == r"D:\Dropbox\CodexSessions"
