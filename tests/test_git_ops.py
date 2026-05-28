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


def test_parallel_op_mixed(repo_tree: Path, monkeypatch):
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)  # no sleep in tests
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


def test_parallel_op_retry_recovers_transient_failure(repo_tree: Path, monkeypatch):
    """A repo that fails the first pass but succeeds on serial retry ends up OK.
    This is the SSH-throttle case: parallel push fails, serial retry clears it."""
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)
    repos = git_ops.find_repos([repo_tree])
    calls: dict[str, int] = {}

    def fake(repo, op):
        n = calls.get(repo.name, 0)
        calls[repo.name] = n + 1
        if repo.name == "repo-b" and n == 0:
            return git_ops.OpResult(repo=repo, ok=False, code=128, detail="transient ssh")
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "push")

    assert s.total == 2
    assert s.ok == 2          # repo-b recovered on retry
    assert s.failed == []
    assert calls["repo-b"] == 2  # tried, then retried


def test_parallel_op_retry_genuine_failure_still_fails(repo_tree: Path, monkeypatch):
    """A repo that fails both passes stays failed (no access / real conflict)."""
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)
    repos = git_ops.find_repos([repo_tree])

    def fake(repo, op):
        if repo.name == "repo-b":
            return git_ops.OpResult(repo=repo, ok=False, code=1, detail="no access")
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "push")

    assert s.ok == 1
    assert len(s.failed) == 1
    assert s.failed[0].repo.name == "repo-b"


def test_short_err_prefers_fatal_over_trailing_line():
    stderr = (
        "ERROR: Repository not found.\n"
        "fatal: Could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )
    msg = git_ops._short_err(stderr, "")
    assert msg != "and the repository exists."
    assert "Repository not found" in msg or "Could not read" in msg


def test_short_err_skips_From_lines():
    stderr = "From github.com:tinyvane/x\nerror: failed to push some refs\n"
    assert git_ops._short_err(stderr, "") == "error: failed to push some refs"


def test_short_err_fallback_when_no_priority_line():
    assert git_ops._short_err("just some text", "") == "just some text"


def test_default_workers_reasonable():
    n = git_ops.default_workers()
    assert 4 <= n <= 16


# ---------- auto_commit_dirty ----------

def _commit_initial(repo: Path) -> None:
    """Give a repo one commit so it's not in the zero-commit state."""
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], check=True, capture_output=True)


def test_auto_commit_commits_dirty_repo(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo-a")
    _commit_initial(root / "repo-a")
    # make it dirty
    (root / "repo-a" / "new.txt").write_text("change", encoding="utf-8")

    repos = git_ops.find_repos([root])
    committed = git_ops.auto_commit_dirty(repos, skip_names=set())
    assert committed == ["repo-a"]
    # working tree now clean
    assert not git_ops._is_dirty(root / "repo-a")


def test_auto_commit_skips_clean_repo(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo-a")
    _commit_initial(root / "repo-a")  # clean after commit

    repos = git_ops.find_repos([root])
    committed = git_ops.auto_commit_dirty(repos, skip_names=set())
    assert committed == []  # nothing to commit, no empty commit created


def test_auto_commit_respects_skip(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "dev-tools")
    _commit_initial(root / "dev-tools")
    (root / "dev-tools" / "wip.txt").write_text("x", encoding="utf-8")

    repos = git_ops.find_repos([root])
    committed = git_ops.auto_commit_dirty(repos, skip_names={"dev-tools"})
    assert committed == []                      # skipped despite being dirty
    assert git_ops._is_dirty(root / "dev-tools")  # still dirty (untouched)
