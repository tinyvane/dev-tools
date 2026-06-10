"""Tests for codesync.delete — local removal + GitHub archive.

gh/git network calls are monkeypatched; the rmtree runs for real against
tmp_path so the filesystem side is exercised.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codesync import config, delete, git_ops


def _init_repo(p: Path, origin: str | None = None) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=p, check=True)
    (p / "f.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(p), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], check=True, capture_output=True)
    if origin:
        subprocess.run(["git", "-C", str(p), "remote", "add", "origin", origin],
                       check=True, capture_output=True)
    return p


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Never hit the network: push always 'succeeds', archive is recorded."""
    monkeypatch.setattr(git_ops, "parallel_op",
                        lambda repos, op, **k: git_ops.OpSummary(op=op, total=len(repos),
                                                                 ok=len(repos), failed=[], elapsed=0.0))


def test_delete_github_repo_archives_and_removes(tmp_path, monkeypatch):
    root = tmp_path / "root"; root.mkdir()
    repo = _init_repo(root / "foo", "git@github.com:me/foo.git")

    archived = {}
    monkeypatch.setattr(delete, "_gh_archive",
                        lambda owner, name: (archived.update({"repo": f"{owner}/{name}"}), (True, ""))[1])
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))

    rc = delete.delete_repo("foo", yes=True)
    assert rc == 0
    assert archived["repo"] == "me/foo"      # archived on GitHub
    assert not repo.exists()                 # local folder gone


def test_delete_non_github_only_removes_local(tmp_path, monkeypatch):
    root = tmp_path / "root"; root.mkdir()
    repo = _init_repo(root / "bar", "git@gitlab.com:me/bar.git")

    called = {"archive": False}
    monkeypatch.setattr(delete, "_gh_archive",
                        lambda o, n: (called.__setitem__("archive", True), (True, ""))[1])
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))

    rc = delete.delete_repo("bar", yes=True)
    assert rc == 0
    assert called["archive"] is False        # non-GitHub → never archived
    assert not repo.exists()


def test_delete_dirty_commits_and_pushes_before_archive(tmp_path, monkeypatch):
    root = tmp_path / "root"; root.mkdir()
    repo = _init_repo(root / "baz", "git@github.com:me/baz.git")
    (repo / "wip.txt").write_text("unsaved", encoding="utf-8")  # make dirty

    order = []
    monkeypatch.setattr(git_ops, "auto_commit_dirty",
                        lambda repos, **k: (order.append("commit"), [])[1])
    monkeypatch.setattr(git_ops, "parallel_op",
                        lambda repos, op, **k: (order.append(f"push"),
                                                git_ops.OpSummary(op=op, total=1, ok=1, failed=[], elapsed=0.0))[1])
    monkeypatch.setattr(delete, "_gh_archive",
                        lambda o, n: (order.append("archive"), (True, ""))[1])
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))

    rc = delete.delete_repo("baz", yes=True)
    assert rc == 0
    # commit + push must happen BEFORE archive (so the archived copy is current)
    assert order == ["commit", "push", "archive"]
    assert not repo.exists()


def test_delete_not_found(tmp_path, monkeypatch):
    root = tmp_path / "root"; root.mkdir()
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))
    assert delete.delete_repo("nonexistent", yes=True) == 1


def test_delete_archive_failure_still_removes_local(tmp_path, monkeypatch):
    """If archive fails (e.g. no access), the local folder is still deleted —
    freeing space is the user's primary intent; the implicit flow retries archive."""
    root = tmp_path / "root"; root.mkdir()
    repo = _init_repo(root / "qux", "git@github.com:me/qux.git")
    monkeypatch.setattr(delete, "_gh_archive", lambda o, n: (False, "no access"))
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))

    rc = delete.delete_repo("qux", yes=True)
    assert rc == 0
    assert not repo.exists()


def test_delete_ambiguous_name_refuses(tmp_path, monkeypatch):
    r1 = tmp_path / "a"; r1.mkdir()
    r2 = tmp_path / "b"; r2.mkdir()
    _init_repo(r1 / "dup", "git@github.com:me/dup.git")
    _init_repo(r2 / "dup", "git@github.com:me/dup.git")
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(r1), str(r2)]))
    # both still exist; ambiguous → refuse without deleting
    assert delete.delete_repo("dup", yes=True) == 1
    assert (r1 / "dup").exists() and (r2 / "dup").exists()
