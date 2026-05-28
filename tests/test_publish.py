"""Tests for codesync.publish — orphan detection + publish flow.

find_orphan_candidates is tested against a real tmp_path tree (cheap, no
network). publish_one / _gh_repo_exists are tested with mocked subprocess."""
from __future__ import annotations

import subprocess
from pathlib import Path

from codesync import publish
from codesync.publish import OrphanCandidate, find_orphan_candidates


def _make_dir(parent: Path, name: str, *, files: list[str] | None = None,
              git: bool = False) -> Path:
    d = parent / name
    d.mkdir()
    for f in (files or []):
        (d / f).write_text("x", encoding="utf-8")
    if git:
        (d / ".git").mkdir()
    return d


# ---------- find_orphan_candidates ----------

def test_finds_dir_without_git(tmp_path, monkeypatch) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, "new-project", files=["main.py"])

    cands = find_orphan_candidates([root], skip=set())
    assert len(cands) == 1
    assert cands[0].name == "new-project"
    assert cands[0].has_git is False


def test_skips_empty_dir(tmp_path) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, "empty-dir")  # no files

    cands = find_orphan_candidates([root], skip=set())
    assert cands == []


def test_skips_hidden_dir(tmp_path) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, ".hidden", files=["x"])

    cands = find_orphan_candidates([root], skip=set())
    assert cands == []


def test_skips_never_publish_names(tmp_path) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, "node_modules", files=["pkg.json"])
    _make_dir(root, "__pycache__", files=["x.pyc"])

    cands = find_orphan_candidates([root], skip=set())
    assert cands == []


def test_skips_user_skip_list(tmp_path) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, "playground", files=["test.py"])

    cands = find_orphan_candidates([root], skip={"playground"})
    assert cands == []


def test_git_repo_without_origin_is_candidate(tmp_path, monkeypatch) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    d = _make_dir(root, "local-only-repo", files=["a.py"], git=True)

    # git remote get-url origin → fails (no origin)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="no origin"))
    cands = find_orphan_candidates([root], skip=set())
    assert len(cands) == 1
    assert cands[0].name == "local-only-repo"
    assert cands[0].has_git is True


def test_git_repo_with_origin_is_not_candidate(tmp_path, monkeypatch) -> None:
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, "tracked-repo", files=["a.py"], git=True)

    # git remote get-url origin → succeeds
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, stdout="git@github.com:me/tracked-repo.git\n", stderr=""))
    cands = find_orphan_candidates([root], skip=set())
    assert cands == []


def test_mixed_tree(tmp_path, monkeypatch) -> None:
    """A realistic mix: 1 new dir, 1 empty, 1 node_modules, 1 tracked repo, 1 orphan repo."""
    root = tmp_path / "SyncRepos"
    root.mkdir()
    _make_dir(root, "brand-new", files=["x.py"])              # candidate (no git)
    _make_dir(root, "empty")                                   # skip (empty)
    _make_dir(root, "node_modules", files=["p.json"])          # skip (artifact)
    _make_dir(root, "tracked", files=["a"], git=True)          # skip (has origin)
    _make_dir(root, "orphan-repo", files=["b"], git=True)      # candidate (no origin)

    def fake_run(cmd, **kw):
        # only git repos call `git remote get-url origin`
        if "tracked" in " ".join(str(c) for c in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="git@github.com:me/tracked.git\n", stderr="")
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="no origin")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cands = find_orphan_candidates([root], skip=set())
    names = sorted(c.name for c in cands)
    assert names == ["brand-new", "orphan-repo"]


def test_nonexistent_root_skipped(tmp_path) -> None:
    cands = find_orphan_candidates([tmp_path / "does-not-exist"], skip=set())
    assert cands == []


# ---------- _gh_repo_exists ----------

def test_gh_repo_exists_true(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout='{"name":"x"}', stderr=""))
    assert publish._gh_repo_exists("me", "x") is True


def test_gh_repo_exists_false(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found"))
    assert publish._gh_repo_exists("me", "x") is False


# ---------- publish_one ----------

def test_publish_one_bails_if_repo_exists(monkeypatch, tmp_path) -> None:
    c = OrphanCandidate(path=tmp_path, name="foo", has_git=False, reason="")
    # _gh_repo_exists → True
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: True)
    ok, msg = publish.publish_one(c, "me")
    assert ok is False
    assert "已有" in msg


def test_publish_one_init_commit_create_flow(monkeypatch, tmp_path) -> None:
    """No .git → must run init, add, diff(check staged), commit, then gh repo create."""
    c = OrphanCandidate(path=tmp_path, name="foo", has_git=False, reason="")
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: False)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        joined = " ".join(str(x) for x in cmd)
        # `git diff --cached --quiet` → return 1 (means: there ARE staged changes)
        if "diff" in cmd and "--cached" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = publish.publish_one(c, "me")
    assert ok is True
    # Verify the sequence touched init, add, commit, and gh repo create
    flat = [" ".join(str(x) for x in c) for c in calls]
    assert any("init" in f for f in flat)
    assert any("add" in f for f in flat)
    assert any("commit" in f for f in flat)
    assert any("gh repo create" in f for f in flat)


def test_publish_one_bails_when_nothing_to_commit(monkeypatch, tmp_path) -> None:
    """If `git add .` stages nothing, `git diff --cached --quiet` returns 0 → bail."""
    c = OrphanCandidate(path=tmp_path, name="foo", has_git=False, reason="")
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: False)

    def fake_run(cmd, **kw):
        if "diff" in cmd and "--cached" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")  # nothing staged
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = publish.publish_one(c, "me")
    assert ok is False
    assert "无可提交" in msg


def test_publish_one_existing_git_skips_init(monkeypatch, tmp_path) -> None:
    """has_git=True → skip init/commit, go straight to gh repo create."""
    c = OrphanCandidate(path=tmp_path, name="foo", has_git=True, reason="")
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: False)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(" ".join(str(x) for x in cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = publish.publish_one(c, "me")
    assert ok is True
    # init should NOT have been called
    assert not any("init" in f for f in calls)
    assert any("gh repo create" in f for f in calls)


def test_publish_one_reports_gh_create_failure(monkeypatch, tmp_path) -> None:
    c = OrphanCandidate(path=tmp_path, name="foo", has_git=True, reason="")
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: False)

    def fake_run(cmd, **kw):
        if "gh" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="name already taken")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = publish.publish_one(c, "me")
    assert ok is False
    assert "gh repo create" in msg
