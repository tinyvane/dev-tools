from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from codesync import config, delete, git_ops, github_auto, state, trash


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


@pytest.fixture
def harness_real_push(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    memory = state.default_state()
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(state, "load_state", lambda: memory)
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
    one.mkdir()
    two.mkdir()
    _repo(one / "foo")
    _repo(two / "foo")
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(one), str(two)]))
    assert delete.delete_repo("foo", yes=True) == 1
    assert (one / "foo").exists() and (two / "foo").exists()


def test_push_before_trash_returns_reason_not_attribute_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "foo")
    missing = tmp_path / "nonexistent.git"
    _git(repo, "remote", "set-url", "origin", missing.as_uri())
    _git(repo, "config", "push.default", "current")
    monkeypatch.setattr(git_ops, "_RETRY_DELAY_SEC", 0)

    ok, msg = delete._push_before_trash(repo)

    assert ok is False
    assert msg


def test_push_before_trash_ok_when_nothing_to_push(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True, capture_output=True)
    repo = _repo(tmp_path / "foo")
    _git(repo, "remote", "set-url", "origin", remote.as_uri())
    _git(repo, "push", "--set-upstream", "origin", "HEAD")

    assert delete._push_before_trash(repo) == (True, "")


def test_push_before_trash_detects_uncommitted_leftovers(tmp_path):
    repo = _repo(tmp_path / "outer")
    nested = _repo(repo / "nested", remote=None)
    _git(repo, "add", "nested")
    _git(repo, "commit", "-m", "track nested gitlink", "--quiet")
    (nested / "file.txt").write_text("dirty", encoding="utf-8")

    ok, msg = delete._push_before_trash(repo)

    assert ok is False
    assert msg.startswith("自动 commit 未完成")


def test_delete_push_failure_preserves_both_sides_end_to_end(
    harness_real_push, monkeypatch, capsys,
):
    root, _ = harness_real_push
    repo = _repo(root / "foo")
    missing = root / "nonexistent.git"
    _git(repo, "remote", "set-url", "origin", missing.as_uri())
    _git(repo, "config", "push.default", "current")
    monkeypatch.setattr(delete, "_origin_url", lambda p: "git@github.com:me/foo.git")
    called = {"remote": False}
    monkeypatch.setattr(
        trash, "trash_remote",
        lambda ident: (called.__setitem__("remote", True), None, "")[1:],
    )

    assert delete.delete_repo("foo", yes=True) == 1
    assert repo.exists()
    assert called["remote"] is False
    captured = capsys.readouterr()
    assert "删除前同步失败，远端和本地均保留原状" in captured.out + captured.err


def test_op_result_has_detail_not_message():
    names = {field.name for field in dataclasses.fields(git_ops.OpResult)}
    assert "detail" in names
    assert "message" not in names


def test_origin_url_timeout_aborts_delete(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda *args: pytest.fail("delete must abort before touching GitHub"),
    )

    assert delete.delete_repo("foo", yes=True) == 1
    assert repo.exists()


# ---------- --local-only ----------
#
# Removing a repo locally while leaving GitHub alone is NOT just "skip the
# remote steps". sync computes:
#     to_clone   = active ∧ ¬known ∧ ¬local ∧ ¬tombstoned
#     to_archive = known ∧ active ∧ ¬local
# so leaving it in Known ARCHIVES it on the next sync, and dropping it from
# Known RE-CLONES it. Only a tombstone plus Known-removal expresses
# "deliberately absent, leave the remote alone".

def test_local_only_never_touches_the_remote(harness, monkeypatch):
    root, memory = harness
    _repo(root / "foo")
    monkeypatch.setattr(
        trash, "trash_remote",
        lambda ident: pytest.fail("--local-only must not rename/archive on GitHub"),
    )

    assert delete.delete_repo("foo", yes=True, local_only=True) == 0
    assert not (root / "foo").exists()


def test_local_only_without_github_origin_removes_known_before_move(harness):
    root, memory = harness
    repo = _repo(root / "local", remote=None)
    memory["Known"] = ["local"]

    assert delete.delete_repo("local", yes=True, local_only=True) == 0

    assert not repo.exists()
    assert memory["Known"] == []
    assert memory["Tombstones"] == {}


def test_local_only_tombstones_by_repository_id_so_sync_wont_reclone(
    harness, monkeypatch,
):
    """The ID, never the name.

    Name-keyed tombstones cannot distinguish a new repo from a deleted one that
    shared its name — the v2.9-v2.16 accident root cause.
    """
    root, memory = harness
    _repo(root / "foo")
    memory["Known"] = ["foo"]
    monkeypatch.setattr(trash, "trash_remote", lambda ident: pytest.fail("no remote writes"))

    assert delete.delete_repo("foo", yes=True, local_only=True) == 0

    assert "RID-1" in memory["Tombstones"]
    assert "foo" not in memory["Known"]
    assert memory["Trash"]["RID-1"]["local_only"] is True
    # remote_name stays the LIVE name — nothing was renamed on GitHub.
    assert memory["Trash"]["RID-1"]["remote_name"] == "foo"


def test_local_only_still_fails_closed_without_a_repository_id(harness, monkeypatch):
    """Identity is unreadable → no ID → we must not invent a name tombstone."""
    root, memory = harness
    _repo(root / "foo")
    monkeypatch.setattr(
        trash, "get_remote_identity", lambda owner, name: ("error", None, "boom"),
    )

    assert delete.delete_repo("foo", yes=True, local_only=True) == 1
    assert (root / "foo").exists()
    assert memory["Tombstones"] == {}


def test_local_only_still_pushes_work_before_trashing(harness_real_push, monkeypatch):
    """Leaving GitHub alone is not a reason to lose uncommitted work."""
    root, memory = harness_real_push
    repo = _repo(root / "foo")
    (repo / "new.txt").write_text("unsaved", encoding="utf-8")
    pushed: list[str] = []
    monkeypatch.setattr(
        delete, "_push_before_trash",
        lambda r: pushed.append(r.name) or (True, ""),
    )
    monkeypatch.setattr(trash, "trash_remote", lambda ident: pytest.fail("no remote writes"))

    assert delete.delete_repo("foo", yes=True, local_only=True) == 0
    assert pushed == ["foo"]


def test_local_only_remote_404_moves_without_tombstone_or_remote_write(
    harness, monkeypatch,
):
    root, memory = harness
    repo = _repo(root / "foo")
    memory["Known"] = ["foo"]
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("not_found", None, "HTTP 404"),
    )
    monkeypatch.setattr(
        delete, "_origin_url", lambda repo: "git@github.com:me/foo.git",
    )
    monkeypatch.setattr(
        delete, "_push_before_trash",
        lambda path: pytest.fail("a confirmed-missing remote must not be pushed"),
    )
    monkeypatch.setattr(
        trash, "trash_remote",
        lambda ident: pytest.fail("--local-only must not mutate GitHub"),
    )

    assert delete.delete_repo("foo", yes=True, local_only=True) == 0

    assert not repo.exists()
    assert len(trash.iter_local_trash([root])) == 1
    assert memory["Tombstones"] == {}
    assert memory["Trash"] == {}
    assert "foo" not in memory["Known"]


def test_remote_reappearing_after_404_local_delete_is_cloned_not_archived(
    harness, monkeypatch,
):
    root, memory = harness
    _repo(root / "foo")
    memory["Known"] = ["foo"]
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("not_found", None, "HTTP 404"),
    )
    monkeypatch.setattr(
        delete, "_origin_url", lambda repo: "git@github.com:me/foo.git",
    )

    assert delete.delete_repo("foo", yes=True, local_only=True) == 0
    assert memory["Known"] == []

    state_marker = root / "known-repos.json"
    state_marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(github_auto.auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(github_auto.paths, "known_repos_file", lambda: state_marker)
    monkeypatch.setattr(
        github_auto,
        "_gh_repo_list",
        lambda owner: [{
            "id": "RID-newly-visible",
            "name": "foo",
            "isFork": False,
            "isArchived": False,
            "sshUrl": "git@github.com:me/foo.git",
            "owner": {"login": "me"},
        }],
    )
    monkeypatch.setattr(
        github_auto, "_local_repos_by_owner", lambda *args, **kwargs: ({}, False),
    )
    cloned: list[str] = []

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["git", "clone"]
        cloned.append(Path(cmd[-1]).name)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(github_auto.proc, "run", fake_run)
    monkeypatch.setattr(
        trash,
        "trash_remote",
        lambda ident: pytest.fail("newly visible remote must never be archived"),
    )

    github_auto.run(
        config.AutoCloneConfig(
            owner="me", target=str(root), skip_confirmation=True,
            abort_if_shrink_pct=100, abort_if_local_missing_pct=100,
        ),
        [root],
        push=True,
        auto_migrate=False,
    )

    assert cloned == ["foo"]


def test_non_local_only_remote_404_still_fails_closed(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("not_found", None, "HTTP 404"),
    )

    assert delete.delete_repo("foo", yes=True, local_only=False) == 1
    assert repo.exists()


def test_local_only_remote_unavailable_still_fails_closed(harness, monkeypatch):
    root, _ = harness
    repo = _repo(root / "foo")
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("unavailable", None, "timeout"),
    )

    assert delete.delete_repo("foo", yes=True, local_only=True) == 1
    assert repo.exists()


def test_remote_404_warns_about_unpushed_commits(harness, monkeypatch, capsys):
    root, _ = harness
    _repo(root / "foo")
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("not_found", None, "HTTP 404"),
    )
    monkeypatch.setattr(
        delete, "_origin_url", lambda repo: "git@github.com:me/foo.git",
    )
    seen: list[tuple[list[str], float]] = []

    def fake_run(cmd, *, timeout, **kwargs):
        seen.append((cmd, timeout))
        return subprocess.CompletedProcess(cmd, 0, "3\n", "")

    monkeypatch.setattr(delete.proc, "run", fake_run)

    assert delete.delete_repo("foo", yes=True, local_only=True) == 0
    assert seen == [([
        "git", "-C", str(root / "foo"), "rev-list", "--count",
        "@{upstream}..HEAD",
    ], delete.proc.T_QUICK)]
    assert "本地有 3 个提交从未推送" in capsys.readouterr().out


def test_local_only_persists_safe_intent_before_moving(harness, monkeypatch):
    root, memory = harness
    repo = _repo(root / "foo")
    memory["Known"] = ["foo"]
    real_move = trash.move_local_to_trash

    def move_then_interrupt(path, record):
        ok, dest, msg = real_move(path, record)
        assert ok, msg
        assert dest is not None
        raise KeyboardInterrupt

    monkeypatch.setattr(trash, "move_local_to_trash", move_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        delete.delete_repo("foo", yes=True, local_only=True)

    assert not repo.exists()
    assert "foo" not in memory["Known"]
    assert memory["Tombstones"]["RID-1"]


def test_local_only_final_state_failure_keeps_remote_protected(
    harness, monkeypatch, capsys,
):
    root, memory = harness
    repo = _repo(root / "foo")
    memory["Known"] = ["foo"]
    writes = 0

    def update(mutator):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        mutator(memory)
        return memory

    monkeypatch.setattr(state, "update_state", update)

    assert delete.delete_repo("foo", yes=True, local_only=True) == 1

    assert not repo.exists()
    assert "foo" not in memory["Known"]
    assert memory["Tombstones"]["RID-1"]
    assert memory["Trash"] == {}
    captured = capsys.readouterr()
    assert "完整 Trash 状态落账失败" in captured.err
    assert "已按 Repository ID 记录 tombstone" not in captured.out


def test_remote_404_state_failure_happens_before_local_move(
    harness, monkeypatch,
):
    root, memory = harness
    repo = _repo(root / "foo")
    memory["Known"] = ["foo"]
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("not_found", None, "HTTP 404"),
    )
    monkeypatch.setattr(
        delete, "_origin_url", lambda path: "git@github.com:me/foo.git",
    )
    monkeypatch.setattr(
        state, "update_state",
        lambda mutator: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert delete.delete_repo("foo", yes=True, local_only=True) == 1
    assert repo.exists()
    assert trash.iter_local_trash([root]) == []
