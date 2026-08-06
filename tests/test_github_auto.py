from __future__ import annotations

from pathlib import Path

import pytest

import codesync.github_auto as ga
from codesync import auth, state as state_mod, trash as trash_mod
from codesync.config import AutoCloneConfig


def _repo(name: str, *, repo_id: str | None = None, archived: bool = False, owner: str = "me") -> dict:
    return {
        "id": repo_id or f"RID-{name}",
        "name": name,
        "isFork": False,
        "isArchived": archived,
        "sshUrl": f"git@github.com:{owner}/{name}.git",
        "owner": {"login": owner},
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    marker = tmp_path / "state.json"
    marker.write_text("{}", encoding="utf-8")
    memory = state_mod.default_state()
    data = {
        "tmp": tmp_path,
        "gh": [],
        "local": [],
        "moved": [],
        "remote_trashed": [],
        "remote_fail": set(),
        "cloned": [],
        "memory": memory,
    }
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(ga.paths, "known_repos_file", lambda: marker)
    monkeypatch.setattr(ga, "_gh_repo_list", lambda owner: data["gh"])
    monkeypatch.setattr(state_mod, "load_state", lambda: memory)

    def update(mutator):
        mutator(memory)
        return memory
    monkeypatch.setattr(state_mod, "update_state", update)

    def fake_local(roots, owner):
        moved = set(data["moved"])
        return {n: tmp_path / n for n in data["local"] if n not in moved}
    monkeypatch.setattr(ga, "_local_repos_by_owner", fake_local)

    def move_local(path, record):
        data["moved"].append(Path(path).name)
        if Path(path).is_dir():
            Path(path).rmdir()
        return True, tmp_path / trash_mod.LOCAL_TRASH_DIR / str(record["remote_name"]), ""
    monkeypatch.setattr(trash_mod, "move_local_to_trash", move_local)

    def trash_remote(ident):
        if ident.name in data["remote_fail"]:
            return False, None, "archive failed"
        data["remote_trashed"].append(ident.name)
        return True, {
            "repo_id": ident.repo_id,
            "owner": ident.owner,
            "original_name": ident.name,
            "remote_name": f"zz-trash--v1--20260620-120000--hash--{ident.name}",
            "trashed_at": "2026-06-20T12:00:00+00:00",
        }, ""
    monkeypatch.setattr(trash_mod, "trash_remote", trash_remote)

    real_run = ga.subprocess.run
    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "clone"]:
            data["cloned"].append(Path(cmd[-1]).name)
            class Result:
                returncode = 0
            return Result()
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(ga.subprocess, "run", fake_run)
    return data


def _ac(tmp_path, **kwargs) -> AutoCloneConfig:
    return AutoCloneConfig(owner="me", target=str(tmp_path), skip_confirmation=True, **kwargs)


def _baseline(harness, names):
    harness["memory"]["Known"] = list(names)
    harness["memory"]["Repositories"] = {
        f"RID-{name}": {"name": name, "path": str(harness["tmp"] / name), "owner": "me"}
        for name in names
    }


def test_local_delete_moves_remote_to_named_trash(harness):
    harness["gh"] = [_repo("r1"), _repo("r2")]
    harness["local"] = ["r1"]
    _baseline(harness, ["r1", "r2"])
    ga.run(_ac(harness["tmp"], abort_if_local_missing_pct=100), [harness["tmp"]], push=True, auto_migrate=False)
    assert harness["remote_trashed"] == ["r2"]
    assert harness["memory"]["Known"] == ["r1"]
    assert "RID-r2" in harness["memory"]["Trash"]


def test_no_push_preserves_pending_delete_intent(harness):
    harness["gh"] = [_repo("r1"), _repo("r2")]
    harness["local"] = ["r1"]
    _baseline(harness, ["r1", "r2"])
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)
    assert harness["remote_trashed"] == []
    assert "r2" in harness["memory"]["Known"]
    assert "RID-r2" in harness["memory"]["PendingArchives"]


def test_remote_trash_failure_preserves_pending_delete_intent(harness):
    harness["gh"] = [_repo("r1"), _repo("r2")]
    harness["local"] = ["r1"]
    harness["remote_fail"] = {"r2"}
    _baseline(harness, ["r1", "r2"])
    ga.run(_ac(harness["tmp"], abort_if_local_missing_pct=100), [harness["tmp"]], push=True, auto_migrate=False)
    assert "r2" in harness["memory"]["Known"]
    assert "RID-r2" in harness["memory"]["PendingArchives"]


def test_archived_repo_id_moves_local_directory_to_trash(harness):
    remote_name = "zz-trash--v1--20260620-120000--hash--foo"
    harness["gh"] = [_repo(remote_name, repo_id="RID-old", archived=True)]
    harness["local"] = ["foo"]
    harness["memory"]["Known"] = ["foo"]
    harness["memory"]["Repositories"] = {
        "RID-old": {"name": "foo", "path": str(harness["tmp"] / "foo"), "owner": "me"}
    }
    (harness["tmp"] / "foo").mkdir()
    ga.run(_ac(harness["tmp"], abort_if_shrink_pct=100), [harness["tmp"]], push=True, auto_migrate=False)
    assert harness["moved"] == ["foo"]
    assert "RID-old" in harness["memory"]["Trash"]
    assert "foo" not in harness["memory"]["Known"]


def test_old_id_moves_before_new_same_name_is_cloned(harness):
    remote_name = "zz-trash--v1--20260620-120000--hash--foo"
    harness["gh"] = [
        _repo(remote_name, repo_id="RID-old", archived=True),
        _repo("foo", repo_id="RID-new"),
    ]
    harness["local"] = ["foo"]
    harness["memory"]["Known"] = ["foo"]
    harness["memory"]["Repositories"] = {
        "RID-old": {"name": "foo", "path": str(harness["tmp"] / "foo"), "owner": "me"}
    }
    (harness["tmp"] / "foo").mkdir()
    ga.run(_ac(harness["tmp"], abort_if_shrink_pct=100), [harness["tmp"]], push=True, auto_migrate=False)
    assert harness["moved"] == ["foo"]
    assert harness["cloned"] == ["foo"]


def test_missing_remote_without_archive_signal_never_moves_local(harness):
    harness["gh"] = [_repo("r1")]
    harness["local"] = ["r1", "r2"]
    _baseline(harness, ["r1", "r2"])
    ga.run(_ac(harness["tmp"], abort_if_shrink_pct=100), [harness["tmp"]], push=True, auto_migrate=False)
    assert harness["moved"] == []
    assert "r2" in harness["memory"]["Known"]


def test_missing_code_root_aborts_before_remote_actions(harness):
    missing = harness["tmp"] / "missing"
    with pytest.raises(SystemExit):
        ga.run(_ac(missing), [missing], push=True, auto_migrate=False)
    assert harness["remote_trashed"] == []


def test_skip_repo_ignores_remote_trash_signal(harness):
    remote_name = "zz-trash--v1--20260620-120000--hash--foo"
    harness["gh"] = [_repo(remote_name, repo_id="RID-old", archived=True)]
    harness["local"] = ["foo"]
    harness["memory"]["Known"] = ["foo"]
    harness["memory"]["Repositories"] = {
        "RID-old": {"name": "foo", "path": str(harness["tmp"] / "foo"), "owner": "me"}
    }
    (harness["tmp"] / "foo").mkdir()
    ga.run(_ac(harness["tmp"], skip=["foo"], abort_if_shrink_pct=100),
           [harness["tmp"]], push=True, auto_migrate=False)
    assert harness["moved"] == []


def test_excluded_fork_ignores_remote_trash_signal(harness):
    remote_name = "zz-trash--v1--20260620-120000--hash--forked"
    remote = _repo(remote_name, repo_id="RID-fork", archived=True)
    remote["isFork"] = True
    harness["gh"] = [remote]
    harness["local"] = ["forked"]
    harness["memory"]["Repositories"] = {
        "RID-fork": {"name": "forked", "path": str(harness["tmp"] / "forked"), "owner": "me"}
    }
    (harness["tmp"] / "forked").mkdir()
    ga.run(_ac(harness["tmp"], include_forks=False, abort_if_shrink_pct=100),
           [harness["tmp"]], push=True, auto_migrate=False)
    assert harness["moved"] == []


def test_tombstoned_id_blocks_clone_of_unarchived_repo(harness, capsys):
    harness["gh"] = [_repo("foo", repo_id="RID-old")]
    harness["memory"]["Tombstones"] = {"RID-old": "2026-06-20T12:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["cloned"] == []
    captured = capsys.readouterr()
    assert "曾被删除" in captured.out + captured.err
    assert "不自动 clone" in captured.out + captured.err


def test_same_name_new_id_is_still_cloned(harness):
    harness["gh"] = [_repo("foo", repo_id="RID-new")]
    harness["memory"]["Tombstones"] = {"RID-old": "2026-06-20T12:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["cloned"] == ["foo"]


def test_legacy_name_keyed_tombstone_is_inert(harness):
    harness["gh"] = [_repo("foo", repo_id="RID-x")]
    harness["memory"]["Tombstones"] = {"foo": "2026-06-20T12:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["cloned"] == ["foo"]


def test_missing_remote_id_does_not_block_clone(harness):
    remote = _repo("foo")
    remote.pop("id")
    harness["gh"] = [remote]
    harness["memory"]["Tombstones"] = {"RID-old": "2026-06-20T12:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["cloned"] == ["foo"]


def test_zz_trash_named_repo_is_never_cloned(harness):
    name = "zz-trash--v1--20260620-120000--hash--foo"
    harness["gh"] = [_repo(name, repo_id="RID-old")]

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["cloned"] == []


def test_tombstone_cleared_when_repo_is_local_again(harness):
    harness["gh"] = [_repo("foo", repo_id="RID-x")]
    harness["local"] = ["foo"]
    harness["memory"]["Tombstones"] = {"RID-x": "2026-06-20T12:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["memory"]["Tombstones"] == {}
