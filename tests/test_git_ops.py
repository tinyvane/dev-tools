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


def test_find_repos_skips_corrupt_husk(tmp_path: Path):
    """A .git dir that lost HEAD (half-deleted leftover: only objects/ survives
    a delete that skipped read-only pack files) is not an operable repo —
    find_repos excludes it, find_corrupt_repos surfaces it."""
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "good")
    husk = root / "husk"
    (husk / ".git" / "objects" / "pack").mkdir(parents=True)
    (husk / ".git" / "objects" / "pack" / "x.pack").write_bytes(b"\x00")

    assert [r.name for r in git_ops.find_repos([root])] == ["good"]
    assert [r.name for r in git_ops.find_corrupt_repos([root])] == ["husk"]
    assert git_ops.is_corrupt_repo(husk) is True
    assert git_ops.is_corrupt_repo(root / "good") is False


def test_gitlink_file_is_not_corrupt(tmp_path: Path):
    """A .git FILE (worktree / embedded-checkout gitlink) must never be judged
    corrupt — the HEAD check only applies to .git directories."""
    d = tmp_path / "linked"
    d.mkdir()
    (d / ".git").write_text("gitdir: ../somewhere/.git/worktrees/linked\n", encoding="utf-8")
    assert git_ops.is_corrupt_repo(d) is False


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
    ("git@gitlab.com:me/bar.git", "git@gitlab.com:me/bar"),
])
def test_normalize_origin(url, expected):
    assert git_ops._normalize_origin(url) == expected


def test_find_duplicate_origins_flags_same_remote_different_forms(tmp_path: Path):
    """ssh-form and https-form of the SAME repo in two folders → one dup group."""
    a = tmp_path / "old-dated-clone"; _init_repo(a)
    subprocess.run(["git", "-C", str(a), "remote", "add", "origin",
                    "git@github.com:me/foo.git"], check=True, capture_output=True)
    b = tmp_path / "foo"; _init_repo(b)
    subprocess.run(["git", "-C", str(b), "remote", "add", "origin",
                    "https://github.com/me/foo"], check=True, capture_output=True)
    c = tmp_path / "unique"; _init_repo(c)
    subprocess.run(["git", "-C", str(c), "remote", "add", "origin",
                    "git@github.com:me/unique.git"], check=True, capture_output=True)

    dup = git_ops.find_duplicate_origins([a, b, c])
    assert list(dup.keys()) == ["github.com/me/foo"]
    assert [p.name for p in dup["github.com/me/foo"]] == ["foo", "old-dated-clone"]


def test_find_duplicate_origins_ignores_unique_and_originless(tmp_path: Path):
    a = tmp_path / "a"; _init_repo(a)
    subprocess.run(["git", "-C", str(a), "remote", "add", "origin",
                    "git@github.com:me/a.git"], check=True, capture_output=True)
    b = tmp_path / "no-origin"; _init_repo(b)
    assert git_ops.find_duplicate_origins([a, b]) == {}
    assert git_ops.find_duplicate_origins([]) == {}


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
