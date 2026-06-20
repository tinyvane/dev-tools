from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codesync import config, delete, state, trash


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path: Path, remote: str | None = "git@github.com:me/foo.git") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "file.txt").write_text("tracked", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init", "--quiet")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    memory = state.default_state()
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))
    monkeypatch.setattr(state, "load_state", lambda: memory)
    monkeypatch.setattr(delete, "_push_before_trash", lambda repo: (True, ""))
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("ok", trash.RepoIdentity("RID-1", owner, name), ""),
    )
    monkeypatch.setattr(
        trash, "trash_remote",
        lambda ident: (True, {
            "repo_id": ident.repo_id,
            "owner": ident.owner,
            "original_name": ident.name,
            "remote_name": "zz-trash--v1--20260620-120000--abc--" + ident.name,
            "trashed_at": "2026-06-20T12:00:00+00:00",
        }, ""),
    )

    def update(mutator):
        mutator(memory)
        return memory
    monkeypatch.setattr(state, "update_state", update)
    return root, memory


def test_delete_moves_complete_github_repo_to_local_trash(harness):
    root, memory = harness
    repo = _repo(root / "foo")
    (repo / ".env").write_text("SECRET=kept", encoding="utf-8")

    assert delete.delete_repo("foo", yes=True) == 0
    assert not repo.exists()
    entries = trash.iter_local_trash([root])
    assert len(entries) == 1
    dest, manifest = entries[0]
    assert (dest / ".env").read_text(encoding="utf-8") == "SECRET=kept"
    assert manifest["repo_id"] == "RID-1"
    assert memory["Trash"]["RID-1"]["original_name"] == "foo"


def test_delete_local_only_repo_moves_without_gh(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "local", remote=None)
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda *a: pytest.fail("local-only delete must not call GitHub"),
    )
    assert delete.delete_repo("local", yes=True) == 0
    assert not repo.exists()
    assert len(trash.iter_local_trash([root])) == 1


@pytest.mark.parametrize("name", ["..", "../outside", "foo/bar", r"C:\\Users"])
def test_delete_rejects_path_traversal(harness, name):
    root, _ = harness
    outside = root.parent / "outside"
    outside.mkdir(exist_ok=True)
    assert delete.delete_repo(name, yes=True) == 1
    assert outside.exists()


def test_delete_identity_unavailable_fails_closed(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")
    monkeypatch.setattr(trash, "get_remote_identity", lambda *a: ("unavailable", None, "timeout"))
    assert delete.delete_repo("foo", yes=True) == 1
    assert repo.exists()


def test_delete_redirected_identity_fails_closed(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda *a: ("ok", trash.RepoIdentity("RID", "me", "renamed"), ""),
    )
    assert delete.delete_repo("foo", yes=True) == 1
    assert repo.exists()


def test_delete_push_failure_preserves_both_sides(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")
    called = {"remote": False}
    monkeypatch.setattr(delete, "_push_before_trash", lambda p: (False, "push failed"))
    monkeypatch.setattr(
        trash, "trash_remote",
        lambda i: (called.__setitem__("remote", True), None, "")[1:],
    )
    assert delete.delete_repo("foo", yes=True) == 1
    assert repo.exists()
    assert called["remote"] is False


def test_delete_remote_trash_failure_preserves_local(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")
    monkeypatch.setattr(trash, "trash_remote", lambda ident: (False, None, "archive failed"))
    assert delete.delete_repo("foo", yes=True) == 1
    assert repo.exists()


def test_delete_ambiguous_name_refuses(tmp_path, monkeypatch):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    _repo(one / "foo"); _repo(two / "foo")
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(one), str(two)]))
    assert delete.delete_repo("foo", yes=True) == 1
    assert (one / "foo").exists() and (two / "foo").exists()
