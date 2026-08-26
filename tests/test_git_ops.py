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
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=p, check=True)


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
    _commit_initial(root / "repo-a")
    _init_repo(root / "repo-b")
    _commit_initial(root / "repo-b")
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
    _commit_initial(root_a / "x")
    _init_repo(root_b / "y")
    _commit_initial(root_b / "y")

    repos = git_ops.find_repos([root_a, root_b])
    names = sorted(r.name for r in repos)
    assert names == ["x", "y"]


def test_find_repos_skips_missing_roots(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    _init_repo(real / "r")
    _commit_initial(real / "r")
    missing = tmp_path / "does-not-exist"

    repos = git_ops.find_repos([missing, real])
    assert [r.name for r in repos] == ["r"]


def test_find_repos_skips_files(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "i-am-a-file").write_text("nope")
    _init_repo(root / "actual-repo")
    _commit_initial(root / "actual-repo")

    assert [r.name for r in git_ops.find_repos([root])] == ["actual-repo"]


def test_find_repos_dedupes_symlinks(tmp_path: Path):
    """If two roots point at the same actual dir, don't double-count."""
    real = tmp_path / "real"
    real.mkdir()
    _init_repo(real / "x")
    _commit_initial(real / "x")

    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    repos = git_ops.find_repos([real, link])
    assert len(repos) == 1


def test_find_repos_skips_corrupt_husk(tmp_path: Path):
    """A .git dir that lost HEAD (half-deleted leftover: only objects/ survives
    a delete that skipped read-only pack files) is not an operable repo —
    find_repos excludes it, find_corrupt_repos surfaces it."""
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "good")
    _commit_initial(root / "good")
    husk = root / "husk"
    (husk / ".git" / "objects" / "pack").mkdir(parents=True)
    (husk / ".git" / "objects" / "pack" / "x.pack").write_bytes(b"\x00")

    assert [r.name for r in git_ops.find_repos([root])] == ["good"]
    assert [(r.path.name, r.kind) for r in git_ops.find_corrupt_repos([root])] == [
        ("husk", "husk"),
    ]
    assert git_ops.is_corrupt_repo(husk) == "husk"
    assert git_ops.is_corrupt_repo(root / "good") is None


def test_gitlink_file_is_not_corrupt(tmp_path: Path):
    """A .git FILE (worktree / embedded-checkout gitlink) must never be judged
    corrupt — the HEAD check only applies to .git directories."""
    d = tmp_path / "linked"
    d.mkdir()
    (d / ".git").write_text("gitdir: ../somewhere/.git/worktrees/linked\n", encoding="utf-8")
    assert git_ops.is_corrupt_repo(d) is None


def test_incomplete_clone_is_excluded_and_classified(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    repo = root / "interrupted"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    assert git_ops.is_corrupt_repo(repo) == "incomplete-clone"
    assert git_ops.find_repos([root]) == []
    assert [(r.path, r.kind) for r in git_ops.find_corrupt_repos([root])] == [
        (repo, "incomplete-clone"),
    ]


def test_empty_loose_refs_with_packed_refs_is_normal(tmp_path: Path):
    repo = tmp_path / "packed"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "packed-refs").write_text("# pack-refs\n", encoding="utf-8")
    assert git_ops.is_corrupt_repo(repo) is None


def test_loose_branch_ref_is_normal(tmp_path: Path):
    repo = tmp_path / "loose"
    heads = repo / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (heads / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    assert git_ops.is_corrupt_repo(repo) is None


def test_cleanup_stale_tmp_packs_keeps_recent_files(tmp_path: Path):
    import os

    root = tmp_path / "root"
    pack = root / "repo" / ".git" / "objects" / "pack"
    pack.mkdir(parents=True)
    old = pack / "tmp_pack_old"
    recent = pack / "tmp_pack_recent"
    old.write_bytes(b"old-pack")
    recent.write_bytes(b"recent")
    now = 2_000_000.0
    os.utime(old, (now - 86_401, now - 86_401))
    os.utime(recent, (now - 86_399, now - 86_399))

    result = git_ops.cleanup_stale_packs([root / "repo"], now=now)

    assert result.before_count == 1
    assert result.after_count == 0
    assert result.freed_bytes == len(b"old-pack")
    assert not old.exists()
    assert recent.exists()


def test_cleanup_stale_tmp_packs_ignores_unreadable_directory(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    pack = repo / ".git" / "objects" / "pack"
    pack.mkdir(parents=True)
    original = Path.iterdir

    def fake_iterdir(path):
        if path == pack:
            raise OSError("unreadable")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    assert git_ops.cleanup_stale_packs([repo]).before_count == 0


def test_parallel_op_empty():
    summary = git_ops.parallel_op([], "pull")
    assert summary.total == 0
    assert summary.ok == 0
    assert summary.failed == []


def test_parallel_op_all_success(repo_tree: Path):
    """Mock _run_one so no real network is hit. Verify summary math + ordering tolerance."""
    repos = git_ops.find_repos([repo_tree])
    assert len(repos) == 2

    def fake(repo, op, *, rebase=True):
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "pull")

    assert s.total == 2
    assert s.ok == 2
    assert s.failed == []


def test_parallel_op_mixed(repo_tree: Path, monkeypatch):
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)  # no sleep in tests
    repos = git_ops.find_repos([repo_tree])

    def fake(repo, op, *, rebase=True):
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

    def fake(repo, op, *, rebase=True):
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

    def fake(repo, op, *, rebase=True):
        if repo.name == "repo-b":
            return git_ops.OpResult(repo=repo, ok=False, code=1, detail="no access")
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "push")

    assert s.ok == 1
    assert len(s.failed) == 1
    assert s.failed[0].repo.name == "repo-b"


def test_parallel_op_passes_pull_strategy_to_worker(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    seen: list[bool] = []

    def fake(repo, op, *, rebase=True):
        seen.append(rebase)
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    monkeypatch.setattr(git_ops, "_run_one", fake)

    summary = git_ops.parallel_op([repo], "pull", max_workers=1, rebase=False)

    assert summary.ok == 1
    assert seen == [False]


# ---------- pull of a local branch not yet on the remote (v2.18.0) ----------
# A brand-new local branch whose upstream is configured but never pushed: in the
# commit→pull→push flow, pull can't find the ref ("no such ref was fetched"),
# then the push pass creates it. Must show dim "新分支·待推送", not a red ✗.


def _fake_git_result(cmd, returncode=0, *, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        cmd, returncode, stdout=stdout, stderr=stderr,
    )


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_run_one_pull_rebase_argv_is_default(monkeypatch, tmp_path: Path):
    repo = _fake_repo(tmp_path)
    calls: list[tuple[list[str], int]] = []

    def fake_run(cmd, *, timeout, **kwargs):
        calls.append((cmd, timeout))
        return _fake_git_result(cmd)

    monkeypatch.setattr(git_ops.proc, "run", fake_run)

    result = git_ops._run_one(repo, "pull")

    assert result.ok is True
    assert calls == [(
        ["git", "-C", str(repo), "pull", "--rebase", "--autostash", "--quiet"],
        git_ops._OP_TIMEOUT_SEC,
    )]


def test_run_one_pull_ff_only_compatibility_argv(monkeypatch, tmp_path: Path):
    repo = _fake_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout, **kwargs):
        calls.append(cmd)
        return _fake_git_result(cmd)

    monkeypatch.setattr(git_ops.proc, "run", fake_run)

    result = git_ops._run_one(repo, "pull", rebase=False)

    assert result.ok is True
    assert calls == [[
        "git", "-C", str(repo), "pull", "--ff-only", "--quiet",
    ]]


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ],
)
def test_in_progress_operation_with_git_directory(
    tmp_path: Path, marker: str, expected: str,
):
    repo = _fake_repo(tmp_path)
    marker_path = repo / ".git" / marker
    if marker.startswith("rebase-"):
        marker_path.mkdir()
    else:
        marker_path.write_text("head\n", encoding="utf-8")

    assert git_ops.in_progress_operation(repo) == expected


def test_in_progress_operation_clean_git_directory(tmp_path: Path):
    repo = _fake_repo(tmp_path)
    assert git_ops.in_progress_operation(repo) is None


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ],
)
def test_in_progress_operation_resolves_relative_gitdir_file(
    tmp_path: Path, marker: str, expected: str,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    actual = tmp_path / "actual"
    actual.mkdir()
    (repo / ".git").write_text("gitdir: ../actual\n", encoding="utf-8")
    marker_path = actual / marker
    if marker.startswith("rebase-"):
        marker_path.mkdir()
    else:
        marker_path.write_text("head\n", encoding="utf-8")

    assert git_ops.in_progress_operation(repo) == expected


def test_pull_guard_skips_existing_rebase_without_pull_or_abort(
    monkeypatch, tmp_path: Path,
):
    repo = _fake_repo(tmp_path)
    (repo / ".git" / "rebase-merge").mkdir()
    monkeypatch.setattr(
        git_ops.proc,
        "run",
        lambda *args, **kwargs: pytest.fail("guarded repo must spawn no subprocess"),
    )

    result = git_ops._run_one(repo, "pull")

    assert result.ok is False
    assert "rebase" in result.detail
    assert "已跳过" in result.detail


def test_our_rebase_conflict_is_aborted_and_reported(
    monkeypatch, tmp_path: Path,
):
    repo = _fake_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout, **kwargs):
        calls.append(cmd)
        if cmd[-2:] == ["rebase", "--abort"]:
            (repo / ".git" / "rebase-merge").rmdir()
            return _fake_git_result(cmd)
        (repo / ".git" / "rebase-merge").mkdir()
        return _fake_git_result(cmd, 1, stderr="CONFLICT (content): merge conflict")

    monkeypatch.setattr(git_ops.proc, "run", fake_run)

    result = git_ops._run_one(repo, "pull")

    assert result.ok is False
    assert "已回滚" in result.detail
    assert calls[-1] == ["git", "-C", str(repo), "rebase", "--abort"]


def test_rebase_abort_failure_reports_manual_recovery(
    monkeypatch, tmp_path: Path,
):
    repo = _fake_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout, **kwargs):
        calls.append(cmd)
        if cmd[-2:] == ["rebase", "--abort"]:
            return _fake_git_result(cmd, 2, stderr="abort failed")
        (repo / ".git" / "rebase-apply").mkdir()
        return _fake_git_result(cmd, 1, stderr="CONFLICT (content): merge conflict")

    monkeypatch.setattr(git_ops.proc, "run", fake_run)

    result = git_ops._run_one(repo, "pull")

    assert result.ok is False
    assert "rebase 中间态" in result.detail
    assert "rebase --abort" in result.detail
    assert calls[-1] == ["git", "-C", str(repo), "rebase", "--abort"]


def test_autostash_apply_conflict_never_aborts(monkeypatch, tmp_path: Path):
    repo = _fake_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout, **kwargs):
        calls.append(cmd)
        return _fake_git_result(
            cmd,
            0,  # rebase itself succeeded; only the post-rebase autostash apply failed
            stderr="Applying autostash resulted in conflicts.\nYour changes are safe in the stash.",
        )

    monkeypatch.setattr(git_ops.proc, "run", fake_run)

    result = git_ops._run_one(repo, "pull")

    assert result.ok is False
    assert "autostash" in result.detail
    assert "stash" in result.detail
    assert all(cmd[-2:] != ["rebase", "--abort"] for cmd in calls)


def test_rebase_pull_keeps_unpushed_branch_benign_downgrade(
    monkeypatch, tmp_path: Path,
):
    repo = _fake_repo(tmp_path)

    def fake_run(cmd, *, timeout, **kwargs):
        return _fake_git_result(
            cmd,
            1,
            stderr=(
                "Your configuration specifies to merge with the ref 'refs/heads/topic' "
                "from the remote, but no such ref was fetched."
            ),
        )

    monkeypatch.setattr(git_ops.proc, "run", fake_run)
    monkeypatch.setattr(git_ops, "_upstream_missing_on_remote", lambda _repo: True)

    result = git_ops._run_one(repo, "pull", rebase=True)

    assert result.ok is True
    assert result.skipped is True
    assert result.detail == "新分支·待推送"

def _make_clone_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Bare remote + a working repo with `main` pushed. Returns (remote, work)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    work = tmp_path / "work"
    _init_repo(work)
    _commit_initial(work)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)
    # Point the bare remote's HEAD at main so clones check it out (git's default
    # init.defaultBranch may be master → clone would otherwise land on no branch).
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True, capture_output=True)
    return remote, work


def _add_unpushed_branch(work: Path, name: str = "feat") -> None:
    """Create a local branch with upstream config but never push it to the remote."""
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", name], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", f"branch.{name}.remote", "origin"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", f"branch.{name}.merge", f"refs/heads/{name}"],
                   check=True, capture_output=True)


def test_upstream_missing_on_remote_true_for_unpushed_branch(tmp_path: Path):
    _, work = _make_clone_with_remote(tmp_path)
    _add_unpushed_branch(work)
    assert git_ops._upstream_missing_on_remote(work) is True


def test_upstream_missing_on_remote_false_for_tracked_branch(tmp_path: Path):
    """On `main`, which tracks origin/main (exists) → not the benign case."""
    _, work = _make_clone_with_remote(tmp_path)
    assert git_ops._upstream_missing_on_remote(work) is False


def test_upstream_missing_on_remote_false_without_upstream(tmp_path: Path):
    """A branch with no upstream config is a different problem — keep the error."""
    work = tmp_path / "lonely"; _init_repo(work); _commit_initial(work)
    assert git_ops._upstream_missing_on_remote(work) is False


def test_run_one_pull_skips_unpushed_local_branch(tmp_path: Path):
    _, work = _make_clone_with_remote(tmp_path)
    _add_unpushed_branch(work)
    res = git_ops._run_one(work, "pull")
    assert res.skipped is True
    assert res.ok is True
    assert res.code == 0
    assert res.detail == "新分支·待推送"


def test_run_one_pull_ff_only_real_divergence_not_skipped(tmp_path: Path):
    """A divergent branch (ff-only impossible) is a genuine failure — never silenced."""
    remote, work = _make_clone_with_remote(tmp_path)
    # Second clone advances origin/main.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True, capture_output=True)
    (other / "b.txt").write_text("remote change", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "remote"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True, capture_output=True)
    # Local main diverges with its own commit (no fetch yet).
    (work / "a.txt").write_text("local change", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "local"], check=True, capture_output=True)

    res = git_ops._run_one(work, "pull", rebase=False)
    assert res.skipped is False
    assert res.ok is False


def test_needs_push_false_when_tracked_branch_is_synchronized(tmp_path: Path):
    _, work = _make_clone_with_remote(tmp_path)
    assert git_ops._needs_push(work) is False


def test_run_one_push_skips_synchronized_repo(tmp_path: Path):
    _, work = _make_clone_with_remote(tmp_path)
    res = git_ops._run_one(work, "push")
    assert res.ok is True
    assert res.skipped is True
    assert res.detail == "无待推送提交"


def test_run_one_pushes_when_tracked_branch_is_ahead(tmp_path: Path):
    remote, work = _make_clone_with_remote(tmp_path)
    (work / "ahead.txt").write_text("new commit", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "ahead.txt"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "ahead"], check=True)

    assert git_ops._needs_push(work) is True
    res = git_ops._run_one(work, "push")

    assert res.ok is True
    assert res.skipped is False
    local_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    assert remote_head == local_head


def test_needs_push_true_for_committed_branch_without_upstream(tmp_path: Path):
    _, work = _make_clone_with_remote(tmp_path)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "new-local"], check=True)
    assert git_ops._needs_push(work) is True


def test_needs_push_false_for_unborn_repository(tmp_path: Path):
    work = tmp_path / "empty"
    _init_repo(work)
    assert git_ops._needs_push(work) is False


def test_needs_push_fails_open_on_detection_error(tmp_path: Path):
    with patch.object(git_ops.subprocess, "run", side_effect=OSError("probe failed")):
        assert git_ops._needs_push(tmp_path) is True


def test_parallel_op_skipped_counts_as_ok_not_failed(repo_tree: Path, monkeypatch):
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)
    repos = git_ops.find_repos([repo_tree])

    def fake(repo, op, *, rebase=True):
        if repo.name == "repo-b":
            return git_ops.OpResult(repo=repo, ok=True, code=0, detail="新分支·待推送", skipped=True)
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="")

    with patch.object(git_ops, "_run_one", side_effect=fake):
        s = git_ops.parallel_op(repos, "pull")

    assert s.ok == 2
    assert s.failed == []


def test_execute_pass_renders_skipped_dim_not_red(repo_tree: Path, monkeypatch, capsys):
    repos = git_ops.find_repos([repo_tree])[:1]

    def fake(repo, op, *, rebase=True):
        return git_ops.OpResult(repo=repo, ok=True, code=0, detail="新分支·待推送", skipped=True)

    monkeypatch.setattr(git_ops, "_run_one", fake)
    git_ops._execute_pass(repos, "pull", max_workers=1)
    out = capsys.readouterr().out
    assert "新分支·待推送" in out
    assert "✗" not in out


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


def test_default_worker_pools_separate_local_and_network_concurrency():
    assert 4 <= git_ops.default_local_workers() <= 32
    assert git_ops.default_net_workers(multiplexed=False) == 1
    assert git_ops.default_net_workers(multiplexed=True) == 4


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


def _embed_inner_repo(superproject: Path, inner_name: str) -> Path:
    """Embed a nested git repo as a gitlink inside `superproject` and return it.

    Mimics the accidental "git repo cloned into a subfolder of another git repo"
    layout (the AutoResearchClaw case): the superproject records a gitlink, not
    the inner files.
    """
    inner = superproject / inner_name
    _init_repo(inner)
    (inner / "code.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(inner), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(inner), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "inner init"], check=True, capture_output=True)
    # Record the gitlink in the superproject (git adds nested repos as gitlinks).
    subprocess.run(["git", "-C", str(superproject), "add", inner_name],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(superproject), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "add gitlink"], check=True, capture_output=True)
    return inner


def test_auto_commit_no_false_commit_for_dirty_submodule(tmp_path: Path):
    """A superproject dirty ONLY because an embedded repo's worktree changed must
    NOT be reported as a commit failure, and must not create an empty commit."""
    root = tmp_path / "root"
    root.mkdir()
    sup = root / "super"
    _init_repo(sup)
    _commit_initial(sup)
    inner = _embed_inner_repo(sup, "inner")

    # Dirty the inner worktree but DON'T commit it — gitlink sha stays the same,
    # so `git add -A` in the superproject can't stage anything.
    (inner / "code.py").write_text("print('v2')\n", encoding="utf-8")

    assert git_ops._is_dirty(sup)              # superproject sees ` M inner`
    repos = git_ops.find_repos([root])
    committed = git_ops.auto_commit_dirty(repos, skip_names=set())
    assert committed == []                     # no commit attempted/made


def test_dirty_submodules_detects_gitlink(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    sup = root / "super"
    _init_repo(sup)
    _commit_initial(sup)
    inner = _embed_inner_repo(sup, "inner")
    (inner / "code.py").write_text("print('v2')\n", encoding="utf-8")

    assert git_ops._dirty_submodules(sup) == ["inner"]


def test_dirty_submodules_empty_for_plain_changes(tmp_path: Path):
    """Ordinary modified/untracked files are not gitlinks — none reported."""
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo-a")
    _commit_initial(root / "repo-a")
    (root / "repo-a" / "new.txt").write_text("change", encoding="utf-8")

    assert git_ops._dirty_submodules(root / "repo-a") == []


# ---------- duplicate-origin detection (v2.14.0) ----------

@pytest.mark.parametrize("url,expected", [
    ("git@github.com:Me/Foo.git", "github.com/me/foo"),
    ("https://github.com/me/foo", "github.com/me/foo"),
    ("https://github.com/me/foo.git", "github.com/me/foo"),
    ("https://ghfast.top/https://github.com/me/foo.git", "github.com/me/foo"),
    ("ssh://git@ssh.github.com:443/Me/Foo.git", "github.com/me/foo"),
    ("git@gitlab.com:me/bar.git", "git@gitlab.com:me/bar"),
])
def test_normalize_origin(url, expected):
    assert git_ops._normalize_origin(url) == expected


def test_find_duplicate_origins_flags_same_remote_different_forms(
    tmp_path: Path, monkeypatch,
):
    """ssh-form and https-form of the SAME repo in two folders → one dup group."""
    a = tmp_path / "old-dated-clone"
    b = tmp_path / "foo"
    c = tmp_path / "unique"
    urls = {
        a: "git@github.com:me/foo.git",
        b: "https://github.com/me/foo",
        c: "git@github.com:me/unique.git",
    }

    def fake_run(cmd, *, timeout):
        repo = Path(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, urls[repo], "")

    monkeypatch.setattr(git_ops.proc, "run", fake_run)

    dup = git_ops.find_duplicate_origins([a, b, c])
    assert list(dup.keys()) == ["github.com/me/foo"]
    assert [p.name for p in dup["github.com/me/foo"]] == ["foo", "old-dated-clone"]


def test_four_github_origin_forms_share_one_duplicate_key(tmp_path: Path):
    repos = [tmp_path / name for name in ("https", "ssh", "ssh443", "proxy")]
    origins = dict(zip(repos, [
        "https://github.com/me/foo.git",
        "git@github.com:me/foo.git",
        "ssh://git@ssh.github.com:443/me/foo.git",
        "https://ghfast.top/https://github.com/me/foo.git",
    ]))
    assert git_ops.find_duplicate_origins(repos, origins=origins) == {
        "github.com/me/foo": sorted(repos, key=lambda p: p.name.lower()),
    }


def test_find_duplicate_origins_ignores_unique_and_originless(tmp_path: Path):
    a = tmp_path / "a"; _init_repo(a)
    subprocess.run(["git", "-C", str(a), "remote", "add", "origin",
                    "git@github.com:me/a.git"], check=True, capture_output=True)
    b = tmp_path / "no-origin"; _init_repo(b)
    assert git_ops.find_duplicate_origins([a, b]) == {}
    assert git_ops.find_duplicate_origins([]) == {}


def test_scan_origins_omits_unreadable_origins(monkeypatch, tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"

    def fake_run(cmd, *, timeout):
        if Path(cmd[2]) == a:
            return subprocess.CompletedProcess(
                cmd, 0, "git@github.com:me/a.git\n", "",
            )
        return subprocess.CompletedProcess(cmd, 2, "", "no origin")

    monkeypatch.setattr(git_ops.proc, "run", fake_run)
    assert git_ops.scan_origins([a, b], max_workers=2) == {
        a: "git@github.com:me/a.git",
    }


def test_origin_url_reads_stored_config_without_insteadof(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, "git@github.com:me/foo.git\n", "",
        )

    monkeypatch.setattr(git_ops.proc, "run", fake_run)
    assert git_ops.origin_url(tmp_path) == "git@github.com:me/foo.git"
    assert calls == [[
        "git", "-C", str(tmp_path), "config", "--local", "--get-all",
        "remote.origin.url",
    ]]


def test_origin_url_rc_one_empty_is_certainly_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        git_ops.proc, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", ""),
    )
    result = git_ops.read_origin_url(tmp_path)
    assert result.url is None
    assert result.certain is True


def test_precomputed_origins_avoid_duplicate_scan_subprocesses(
    monkeypatch, tmp_path: Path,
):
    a = tmp_path / "a"
    b = tmp_path / "b"
    origins = {
        a: "git@github.com:Me/shared.git",
        b: "https://github.com/me/shared",
    }
    monkeypatch.setattr(
        git_ops.proc, "run",
        lambda *args, **kwargs: pytest.fail("precomputed origins must spawn no git"),
    )

    duplicates = git_ops.find_duplicate_origins(
        [a, b], origins=origins,
    )
    assert duplicates == {"github.com/me/shared": [a, b]}


def test_precomputed_origins_avoid_owner_scan_subprocesses(
    monkeypatch, tmp_path: Path,
):
    from codesync.config import Config
    a = tmp_path / "a"
    origins = {a: "git@github.com:TINYVANE/a.git"}
    monkeypatch.setattr(
        git_ops.proc, "run",
        lambda *args, **kwargs: pytest.fail("precomputed origins must spawn no git"),
    )

    assert git_ops.my_owners(Config(), [a], origins=origins) == {"tinyvane"}


def test_my_owners_configured_owner_short_circuits_origins(monkeypatch, tmp_path: Path):
    from codesync.config import AutoCloneConfig, Config

    class ExplodingOrigins(dict):
        def get(self, *args, **kwargs):
            pytest.fail("configured owner must not inspect origins")

    cfg = Config(auto_clone=AutoCloneConfig(owner="Mine", target="~/x"))
    monkeypatch.setattr(
        git_ops.proc, "run",
        lambda *args, **kwargs: pytest.fail("configured owner must spawn no git"),
    )

    assert git_ops.my_owners(
        cfg, [tmp_path / "a"], origins=ExplodingOrigins(),
    ) == {"mine"}


# ---------- rmtree_repo (shared safe deletion, v2.13.1) ----------

def test_rmtree_repo_removes_readonly_git_objects(tmp_path: Path):
    """git marks pack objects read-only; Windows refuses to delete them, so a
    plain rmtree(ignore_errors=True) silently left half a repo behind (the
    github_auto cross-machine delete path). rmtree_repo must remove everything."""
    import os, stat as stat_mod
    repo = tmp_path / "victim"
    _init_repo(repo)
    _commit_initial(repo)  # creates real .git objects (read-only on Windows)
    # Belt and braces: force one explicitly read-only file like a pack object.
    ro = repo / ".git" / "objects" / "fake.pack"
    ro.parent.mkdir(parents=True, exist_ok=True)
    ro.write_text("x", encoding="utf-8")
    os.chmod(ro, stat_mod.S_IREAD)

    ok, msg = git_ops.rmtree_repo(repo)
    assert ok, msg
    assert not repo.exists()


def test_rmtree_repo_rejects_false_success_when_tree_remains(tmp_path: Path, monkeypatch):
    repo = tmp_path / "victim"
    repo.mkdir()
    monkeypatch.setattr(git_ops.shutil, "rmtree", lambda *a, **k: None)
    ok, msg = git_ops.rmtree_repo(repo)
    assert not ok
    assert "仍存在" in msg


def test_update_submodules_timeout_does_not_raise(tmp_path: Path, monkeypatch, capsys):
    """A hung submodule clone raises TimeoutExpired inside subprocess.run —
    update_submodules' 'Never raises' contract must hold (it used to kill sync)."""
    parent = tmp_path / "p"
    _init_repo(parent)

    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)
    monkeypatch.setattr(subprocess, "run", fake_run)

    git_ops.update_submodules([parent])  # must not raise
    assert "超时" in capsys.readouterr().out


# ---------- nested repo discovery (v2.8.0) ----------

def _set_origin(repo: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url],
                   check=True, capture_output=True)


def test_origin_owner_parses_ssh_and_https(tmp_path: Path):
    a = tmp_path / "a"; _init_repo(a); _set_origin(a, "git@github.com:tinyvane/foo.git")
    b = tmp_path / "b"; _init_repo(b); _set_origin(b, "https://github.com/OtherOrg/bar.git")
    assert git_ops._origin_owner(a) == "tinyvane"
    assert git_ops._origin_owner(b) == "OtherOrg"


def test_origin_owner_handles_ghproxy_mirror(tmp_path: Path):
    """ghproxy-style prefix must not fool owner extraction (anchors on github.com/)."""
    a = tmp_path / "a"; _init_repo(a)
    _set_origin(a, "https://ghfast.top/https://github.com/aiming-lab/AutoResearchClaw.git")
    assert git_ops._origin_owner(a) == "aiming-lab"


def test_origin_owner_none_without_origin(tmp_path: Path):
    a = tmp_path / "a"; _init_repo(a)
    assert git_ops._origin_owner(a) is None


def test_gitmodules_paths_parsing(tmp_path: Path):
    repo = tmp_path / "r"; _init_repo(repo)
    (repo / ".gitmodules").write_text(
        '[submodule "backend"]\n\tpath = backend\n\turl = git@github.com:x/b.git\n'
        '[submodule "frontend"]\n\tpath = frontend\n\turl = git@github.com:x/f.git\n',
        encoding="utf-8",
    )
    assert git_ops._gitmodules_paths(repo) == {"backend", "frontend"}
    # repo with no .gitmodules → empty
    plain = tmp_path / "p"; _init_repo(plain)
    assert git_ops._gitmodules_paths(plain) == set()


def test_walk_nested_git_skips_artifact_dirs(tmp_path: Path):
    outer = tmp_path / "outer"; _init_repo(outer)
    _init_repo(outer / "inner")                       # real nested repo (depth 1)
    _init_repo(outer / "node_modules" / "pkg")        # must be pruned
    found = {p.name for p in git_ops._walk_nested_git(outer, max_depth=3)}
    assert "inner" in found
    assert "pkg" not in found


def test_find_nested_repos_classifies_embedded_vs_submodule(tmp_path: Path):
    root = tmp_path / "root"; root.mkdir()
    sup = root / "super"; _init_repo(sup); _commit_initial(sup)

    # embedded repo owned by me (pushable)
    mine = _embed_inner_repo(sup, "mine"); _set_origin(mine, "git@github.com:tinyvane/mine.git")
    # embedded repo owned by a third party (pull-only)
    theirs = _embed_inner_repo(sup, "theirs"); _set_origin(theirs, "https://github.com/aiming-lab/x.git")
    # a registered submodule path
    (sup / ".gitmodules").write_text(
        '[submodule "sub"]\n\tpath = sub\n\turl = git@github.com:other/sub.git\n', encoding="utf-8")
    sub = _embed_inner_repo(sup, "sub"); _set_origin(sub, "git@github.com:other/sub.git")

    nested = git_ops.find_nested_repos([sup], owners={"tinyvane"})
    by_rel = {n.rel: n for n in nested}

    assert by_rel["mine"].is_submodule is False and by_rel["mine"].pushable is True
    assert by_rel["theirs"].is_submodule is False and by_rel["theirs"].pushable is False
    assert by_rel["sub"].is_submodule is True  # registered in .gitmodules
    assert by_rel["mine"].outer == sup


def test_find_nested_repos_respects_skip(tmp_path: Path):
    root = tmp_path / "root"; root.mkdir()
    sup = root / "super"; _init_repo(sup); _commit_initial(sup)
    _embed_inner_repo(sup, "keep")
    _embed_inner_repo(sup, "drop")
    nested = git_ops.find_nested_repos([sup], owners=set(), skip=("drop",))
    assert {n.rel for n in nested} == {"keep"}


def test_my_owners_prefers_auto_clone(tmp_path: Path):
    from codesync.config import AutoCloneConfig, Config
    cfg = Config(auto_clone=AutoCloneConfig(owner="TinyVane", target="~/x"))
    assert git_ops.my_owners(cfg, []) == {"tinyvane"}  # lowercased


def test_my_owners_derives_from_toplevel_when_no_autoclone(tmp_path: Path):
    from codesync.config import Config
    a = tmp_path / "a"; _init_repo(a); _set_origin(a, "git@github.com:tinyvane/a.git")
    owners = git_ops.my_owners(Config(), [a])
    assert owners == {"tinyvane"}


def test_auto_commit_excludes_nested_gitlink_from_outer(tmp_path: Path):
    """When an embedded repo gets a NEW commit (gitlink moves), the outer's
    auto-commit must NOT bake in the moved pointer (exclude_map)."""
    root = tmp_path / "root"; root.mkdir()
    sup = root / "super"; _init_repo(sup); _commit_initial(sup)
    inner = _embed_inner_repo(sup, "inner")

    # Inner gets a new commit → its gitlink sha changes → super sees ` M inner`.
    (inner / "code.py").write_text("print('v2')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(inner), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(inner), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "inner v2"], check=True, capture_output=True)
    # Also a genuine outer change that SHOULD be committed.
    (sup / "outer.txt").write_text("real change", encoding="utf-8")

    assert git_ops._is_dirty(sup)
    committed = git_ops.auto_commit_dirty(
        [sup], skip_names=set(), exclude_map={sup: {"inner"}},
    )
    assert committed == ["super"]
    # The gitlink must still be unstaged/uncommitted (pointer not baked in).
    assert git_ops._dirty_submodules(sup) == ["inner"]
    # The real file change made it in.
    tracked = subprocess.run(["git", "-C", str(sup), "ls-files", "outer.txt"],
                             capture_output=True, text=True)
    assert "outer.txt" in tracked.stdout


def test_commit_timeout_is_warned_not_raised(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(git_ops, "_is_dirty", lambda path: True)

    def fake_run(cmd, **kwargs):
        op = cmd[3]
        if op == "diff":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if op == "commit":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == []
    captured = capsys.readouterr()
    assert "超时" in captured.out + captured.err


def test_reset_failure_skips_commit_to_protect_gitlink(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "outer"
    repo.mkdir()
    calls = []
    monkeypatch.setattr(git_ops, "_is_dirty", lambda path: True)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[3] == "reset":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    committed = git_ops.auto_commit_dirty(
        [repo], skip_names=set(), exclude_map={repo: {"inner"}},
    )
    assert committed == []
    assert not any(cmd[3] == "commit" for cmd in calls)
    captured = capsys.readouterr()
    assert "gitlink 撤销暂存失败" in captured.out + captured.err


def test_is_dirty_timeout_counts_as_dirty(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_ops._is_dirty(tmp_path) is True


def test_staged_check_timeout_skips_commit(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []
    monkeypatch.setattr(git_ops, "_is_dirty", lambda path: True)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[3] == "diff":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == []
    assert not any(cmd[3] == "commit" for cmd in calls)
    captured = capsys.readouterr()
    assert "无法确认暂存状态" in captured.out + captured.err


# Verbatim `git pull --rebase --autostash` output captured from a real diverged
# + dirty repository (git 2.x). Note it contains BOTH "autostash" and
# "CONFLICT": a text-matching discriminator misroutes this to the autostash
# branch and leaves the repository stranded mid-rebase forever, because the
# next sync's pre-guard then skips it.
_REAL_REBASE_CONFLICT_OUTPUT = """Created autostash: 69f197d
Rebasing (1/2)Auto-merging f.txt
CONFLICT (add/add): Merge conflict in f.txt
error: could not apply f1c6fba... init
hint: Resolve all conflicts manually, mark them as resolved with
hint: "git add/rm <conflicted_files>", then run "git rebase --continue".
Could not apply f1c6fba... init
"""

# Verbatim output of a rebase that SUCCEEDED but could not re-apply the stash.
# Here no rebase is in progress and the work lives in a stash entry.
_REAL_AUTOSTASH_CONFLICT_OUTPUT = """Updating 335fb38..c8ed094
Created autostash: af6804e
Fast-forward
 f.txt | 2 +-
Applying autostash resulted in conflicts.
Your changes are safe in the stash.
You can run "git stash pop" or "git stash drop" at any time.
"""


def _stub_pull(monkeypatch, output: str, rc: int, in_progress: str | None):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--abort":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, rc, output, "")

    monkeypatch.setattr(git_ops.proc, "run", fake_run)
    states = iter([None, in_progress])  # pre-guard sees clean, post-failure sees state
    monkeypatch.setattr(git_ops, "in_progress_operation", lambda repo: next(states))
    return calls


def test_real_rebase_conflict_is_rolled_back_not_mistaken_for_autostash(
    monkeypatch, tmp_path,
):
    calls = _stub_pull(monkeypatch, _REAL_REBASE_CONFLICT_OUTPUT, 1, "rebase")

    res = git_ops._run_one(tmp_path / "repo", "pull", rebase=True)

    assert res.ok is False
    assert "已回滚" in res.detail
    assert any(argv[-1] == "--abort" for argv in calls), "stranded rebase must be aborted"


def test_real_autostash_conflict_is_not_aborted_and_names_the_stash(
    monkeypatch, tmp_path,
):
    calls = _stub_pull(monkeypatch, _REAL_AUTOSTASH_CONFLICT_OUTPUT, 0, None)

    res = git_ops._run_one(tmp_path / "repo", "pull", rebase=True)

    assert res.ok is False
    assert "stash" in res.detail
    assert not any(argv[-1] == "--abort" for argv in calls), "nothing to abort here"


def test_git_init_with_uncommitted_work_is_never_called_damaged(tmp_path):
    """A freshly `git init`-ed repo has the SAME .git fingerprint as an
    interrupted clone: HEAD present, no refs, no packed-refs. Judging it damaged
    excludes it from the scan and tells the user to delete a directory that may
    hold their only copy of that work. The empty-worktree requirement is what
    keeps the two apart — publish supports this state on purpose."""
    repo = tmp_path / "init-with-work"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "main.py").write_text("print('my only copy')\n", encoding="utf-8")

    assert git_ops.is_corrupt_repo(repo) is None
    assert git_ops.find_repos([tmp_path]) == [repo]


def test_interrupted_clone_has_an_empty_worktree_and_is_flagged(tmp_path):
    repo = tmp_path / "half-cloned"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    assert git_ops.is_corrupt_repo(repo) == "incomplete-clone"
    assert git_ops.find_repos([tmp_path]) == []


def test_packed_refs_only_repo_is_healthy(tmp_path):
    """Fresh clones keep every ref in packed-refs with an empty refs/heads."""
    repo = tmp_path / "packed"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "packed-refs").write_text("# pack-refs with: peeled\n", encoding="utf-8")

    assert git_ops.is_corrupt_repo(repo) is None


def test_origin_with_several_urls_reports_the_one_git_fetches_from(monkeypatch, tmp_path):
    """`git remote set-url --add` yields several remote.origin.url values.
    Git fetches from the FIRST; plain `git config --get` returns the LAST, which
    would identify the repo as one codesync never actually syncs with."""
    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(
            argv, 0,
            "https://github.com/a/first.git\nhttps://github.com/b/second.git\n", "",
        )

    monkeypatch.setattr(git_ops.proc, "run", fake_run)
    result = git_ops.read_origin_url(tmp_path)

    assert "--get-all" in captured[0]
    assert result.url == "https://github.com/a/first.git"
    assert result.certain is True


# ---------- read_origin_url three-state, against REAL git ----------
#
# These deliberately do not stub proc.run. The whole point of the --local flag
# is that git itself answers differently for "repo with no origin" and "not a
# repository", and a stub would just re-encode whatever we already believe.

def test_real_repo_without_origin_is_certainly_originless(tmp_path: Path):
    """MUST stay certain=True.

    github_auto._local_repos_by_owner turns certain=False into degraded=True,
    which suppresses archiving, remote-trash moves and rename migration for the
    whole run. Misclassifying the ordinary "local repo, no origin" shape would
    pin degraded on forever and silently disable those features.
    """
    repo = tmp_path / "no-origin"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)

    result = git_ops.read_origin_url(repo)

    assert result.url is None
    assert result.certain is True


def test_not_a_repository_is_uncertain_not_originless(tmp_path: Path):
    """Without --local this returned certain=True, defeating delete/rename's
    _ORIGIN_UNAVAILABLE abort."""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = git_ops.read_origin_url(plain)

    assert result.url is None
    assert result.certain is False


def test_half_deleted_husk_is_uncertain(tmp_path: Path):
    """A .git dir with no HEAD is the Windows half-delete leftover. git cannot
    read it, so codesync must not claim to know it has no origin."""
    husk = tmp_path / "husk"
    (husk / ".git" / "objects").mkdir(parents=True)

    result = git_ops.read_origin_url(husk)

    assert result.certain is False


def test_gitlink_repo_still_reads_its_origin(tmp_path: Path):
    """A submodule/worktree checkout has .git as a FILE, not a directory.

    --local must not regress this shape: git_ops derives a nested repo's
    `pushable` from its origin owner, so a spurious "unreadable" here would
    silently downgrade the user's own embedded repos to pull-only.
    """
    outer = tmp_path / "outer"
    inner_real = tmp_path / "inner-real"
    for path in (outer, inner_real):
        path.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    # Emulate a gitlink: worktree dir whose .git is a file pointing at a gitdir.
    inner = outer / "inner"
    inner.mkdir()
    gitdir = inner_real / ".git"
    subprocess.run(
        ["git", "-C", str(gitdir.parent), "remote", "add", "origin",
         "https://github.com/me/inner.git"],
        check=True,
    )
    (inner / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    result = git_ops.read_origin_url(inner)

    assert result.url == "https://github.com/me/inner.git"
    assert result.certain is True


# ---------- _is_dirty: plain failure is not "dirty" ----------
#
# _is_dirty gates delete's pre-trash push, so "unknown" must read as dirty
# (covered by test_is_dirty_timeout_counts_as_dirty above). But a plain
# non-zero rc (corrupt husk / not a repository) must NOT — those are reported
# by find_corrupt_repos, and calling them dirty would make every run attempt a
# doomed add/commit.

def test_is_dirty_ignores_stdout_when_command_failed(tmp_path: Path, monkeypatch):
    """Non-zero rc is 'not dirty' even if git wrote something to stdout."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(
        git_ops.proc, "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 128, stdout=" M leftover\n", stderr="fatal: not a git repository"),
    )
    assert git_ops._is_dirty(repo) is False


def test_is_dirty_on_corrupt_husk_is_false(tmp_path: Path):
    """Real half-deleted husk (.git dir without HEAD): git fails, not 'dirty'."""
    husk = tmp_path / "husk"
    (husk / ".git" / "objects").mkdir(parents=True)
    (husk / "file.txt").write_text("orphaned", encoding="utf-8")
    assert git_ops.is_corrupt_repo(husk)
    assert git_ops._is_dirty(husk) is False
