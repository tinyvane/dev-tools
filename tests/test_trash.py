from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import codesync.github_auto as github_auto
from codesync import git_ops, state, trash


def test_remote_trash_name_is_grouped_unique_and_bounded():
    name = trash.make_remote_trash_name(
        "x" * 150, "RID-123",
        now=datetime(2026, 6, 20, 12, 34, 56, tzinfo=timezone.utc),
    )
    assert name.startswith("zz-trash--v1--20260620-123456--")
    assert len(name) <= 100


def test_move_local_to_trash_preserves_all_files(tmp_path):
    repo = tmp_path / "foo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".env").write_text("secret", encoding="utf-8")
    ok, dest, msg = trash.move_local_to_trash(repo, {
        "repo_id": "RID", "original_name": "foo", "remote_name": "zz-trash--v1--x--id--foo",
    })
    assert ok, msg
    assert dest is not None and (dest / ".env").read_text(encoding="utf-8") == "secret"
    assert not repo.exists()
    assert (dest / trash.MANIFEST).is_file()


def test_move_local_to_trash_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    ok, dest, msg = trash.move_local_to_trash(link, {"repo_id": "RID"})
    assert not ok and dest is None
    assert target.exists() and "符号链接" in msg


def test_parse_original_name():
    assert trash.parse_original_name("zz-trash--v1--20260620-120000--abcd1234--foo") == "foo"
    assert trash.parse_original_name("foo") is None


def test_remote_archive_failure_rolls_name_back(monkeypatch):
    calls = []
    def fake_gh(args):
        calls.append(args)
        if args[:2] == ["repo", "archive"]:
            return False, "archive failed"
        return True, ""
    monkeypatch.setattr(trash, "_gh", fake_gh)
    ok, record, msg = trash.trash_remote(trash.RepoIdentity("RID", "me", "foo"))
    assert not ok and record is None
    assert "archive failed" in msg
    assert calls[-1][:3] == ["repo", "rename", "foo"]


def test_restore_local_record_moves_back_and_removes_manifest(tmp_path):
    source = tmp_path / trash.LOCAL_TRASH_DIR / "trashed"
    source.mkdir(parents=True)
    (source / trash.MANIFEST).write_text("{}", encoding="utf-8")
    (source / ".env").write_text("kept", encoding="utf-8")
    target = tmp_path / "foo"
    ok, restored, msg = trash.restore_local_record({
        "local_path": str(source), "original_path": str(target),
    })
    assert ok, msg
    assert restored == target and (target / ".env").is_file()
    assert not (target / trash.MANIFEST).exists()


def _trash_entry(root: Path, *, repo_id: str = "RID-x", original: str = "foo") -> tuple[Path, dict]:
    remote_name = f"zz-trash--v1--20260620-120000--hash--{original}"
    source = root / trash.LOCAL_TRASH_DIR / remote_name
    source.mkdir(parents=True)
    record = {
        "repo_id": repo_id,
        "owner": "me",
        "original_name": original,
        "original_path": str(root / original),
        "remote_name": remote_name,
        "local_path": str(source),
        "trashed_at": "2026-06-20T12:00:00+00:00",
    }
    (source / trash.MANIFEST).write_text(
        json.dumps(record), encoding="utf-8",
    )
    return source, record


def _memory_with_trash(record: dict) -> dict:
    memory = state.default_state()
    repo_id = record["repo_id"]
    memory["Trash"][repo_id] = dict(record)
    memory["Tombstones"][repo_id] = record["trashed_at"]
    return memory


def _stub_state(monkeypatch, memory: dict) -> None:
    monkeypatch.setattr(state, "load_state", lambda: memory)

    def update(mutator):
        mutator(memory)
        return memory

    monkeypatch.setattr(state, "update_state", update)


def _stub_remote_restore(monkeypatch, record: dict) -> None:
    def identity(owner, name):
        return "ok", trash.RepoIdentity(record["repo_id"], owner, name), ""

    monkeypatch.setattr(trash, "get_remote_identity", identity)
    monkeypatch.setattr(trash, "_gh", lambda args: (True, ""))


def test_restore_trash_clears_tombstone_and_backfills_state(tmp_path, monkeypatch):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    _stub_remote_restore(monkeypatch, record)

    assert trash.restore_trash("foo", [tmp_path]) == 0
    assert memory["Trash"] == {}
    assert memory["Tombstones"] == {}
    assert memory["Repositories"]["RID-x"] == {
        "name": "foo", "path": str(tmp_path / "foo"), "owner": "me",
    }
    assert memory["Known"] == ["foo"]


def test_restore_paths_agree(tmp_path, monkeypatch):
    cli_root = tmp_path / "cli"
    sync_root = tmp_path / "sync"
    _, cli_record = _trash_entry(cli_root)
    _, sync_record = _trash_entry(sync_root)
    cli_memory = _memory_with_trash(cli_record)
    sync_memory = _memory_with_trash(sync_record)
    _stub_state(monkeypatch, cli_memory)
    _stub_remote_restore(monkeypatch, cli_record)

    assert trash.restore_trash("foo", [cli_root]) == 0
    restored = github_auto._apply_remote_restore_signals(
        [{
            "id": "RID-x", "name": "foo", "isArchived": False,
            "owner": {"login": "me"},
        }],
        sync_memory,
        set(),
    )
    assert restored == ["foo"]

    cli_memory["Repositories"]["RID-x"]["path"] = "<restored>"
    sync_memory["Repositories"]["RID-x"]["path"] = "<restored>"
    keys = ("Trash", "Tombstones", "Repositories", "Known")
    assert {key: cli_memory[key] for key in keys} == {
        key: sync_memory[key] for key in keys
    }


def test_purge_keeps_tombstone(tmp_path, monkeypatch):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("ok", trash.RepoIdentity("RID-x", owner, name), ""),
    )
    monkeypatch.setattr(trash, "_gh", lambda args: (True, ""))
    monkeypatch.setattr(git_ops, "rmtree_repo", lambda path: (True, ""))

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 0
    assert memory["Trash"] == {}
    assert memory["Tombstones"] == {"RID-x": record["trashed_at"]}
