from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from codesync import trash


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
