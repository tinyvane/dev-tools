"""Tests for git_ops: repo discovery and parallel runner skeleton."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codesync import git_ops


def _init_repo(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=p, check=True)


@pytest.fixture
def repo_tree(tmp_path: Path) -> Path:
    """Build a layout:
        tmp/root/
          repo-a/.git
          repo-b/.git
          not-a-repo/    (no .git)
          file.txt       (not a directory)
    """
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo-a")
    _init_repo(root / "repo-b")
    (root / "not-a-repo").mkdir()
    (root / "file.txt").write_text("hi")
    return root


def test_find_repos_single_root(repo_tree: Path):
    repos = git_ops.find_repos([repo_tree])
    names = [r.name for r in repos]
    assert names == ["repo-a", "repo-b"]


def test_find_repos_multiple_roots(tmp_path: Path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _init_repo(root_a / "x")
    _init_repo(root_b / "y")

    repos = git_ops.find_repos([root_a, root_b])
    names = sorted(r.name for r in repos)
    assert names == ["x", "y"]


def test_find_repos_skips_missing_roots(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    _init_repo(real / "r")
    missing = tmp_path / "does-not-exist"

    repos = git_ops.find_repos([missing, real])
    assert [r.name for r in repos] == ["r"]


def test_find_repos_skips_files(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "i-am-a-file").write_text("nope")
    _init_repo(root / "actual-repo")

    assert [r.name for r in git_ops.find_repos([root])] == ["actual-repo"]


def test_find_repos_dedupes_symlinks(tmp_path: Path):
    """If two roots point at the same actual dir, don't double-count."""
    real = tmp_path / "real"
    real.mkdir()
    _init_repo(real / "x")

    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    repos = git_ops.find_repos([real, link])
    assert len(repos) == 1


def test_parallel_op_empty():
    summary = git_ops.parallel_op([], "pull")
    assert summary.total == 0
    assert summary.ok == 0
    assert summary.failed == []


def test_parallel_op_all_success(repo_tree: Path):
    """Mock _run_one so no real network is hit. Verify summary math + ordering tolerance."""
    repos = git_ops.find_repos([repo_tree])
    assert len(repos) == 2

    def fake(repo, op):
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "pull")

    assert s.total == 2
    assert s.ok == 2
    assert s.failed == []


def test_parallel_op_mixed(repo_tree: Path):
    repos = git_ops.find_repos([repo_tree])

    def fake(repo, op):
        if repo.name == "repo-b":
            return git_ops.OpResult(repo=repo, ok=False, code=1, detail="boom")
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "pull")

    assert s.total == 2
    assert s.ok == 1
    assert len(s.failed) == 1
    assert s.failed[0].repo.name == "repo-b"
    assert s.failed[0].detail == "boom"


def test_default_workers_reasonable():
    n = git_ops.default_workers()
    assert 4 <= n <= 16
