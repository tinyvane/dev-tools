from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import codesync.github_auto as ga
from codesync import auth, followups, proc, state as state_mod, trash as trash_mod
from codesync.config import AutoCloneConfig


_REAL_GH_REPO_LIST = ga._gh_repo_list
_REAL_LOCAL_SCAN = ga._local_repos_by_owner
_REAL_MOVE_LOCAL_TO_TRASH = trash_mod.move_local_to_trash


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
        "clone_attempts": [],
        "clone_timeout": set(),
        "clone_to_local": False,
        "clone_kwargs": [],
        "scan_degraded": False,
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

    def fake_local(roots, owner, *, max_workers=1):
        moved = set(data["moved"])
        found = {n: tmp_path / n for n in data["local"] if n not in moved}
        return found, data["scan_degraded"]
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

    real_run = ga.proc.run
    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "clone"]:
            name = Path(cmd[-1]).name
            data["clone_attempts"].append(name)
            data["clone_kwargs"].append(dict(kwargs))
            if name in data["clone_timeout"]:
                return subprocess.CompletedProcess(cmd, proc.TIMEOUT_RC, "", "timeout")
            data["cloned"].append(name)
            if data["clone_to_local"]:
                data["local"].append(name)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(ga.proc, "run", fake_run)
    return data


def _fake_local_repo(root: Path, name: str) -> Path:
    repo = root / name
    heads = repo / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (heads / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    return repo


def test_local_repos_parallel_scan_matches_serial_merge_order(monkeypatch, tmp_path):
    first = _fake_local_repo(tmp_path, "first-dir")
    second = _fake_local_repo(tmp_path, "second-dir")
    third = _fake_local_repo(tmp_path, "third-party")
    scan_order = [p for p in tmp_path.iterdir() if (p / ".git").exists()]

    urls = {
        first: "git@github.com:Me/same.git",
        second: "https://github.com/me/same.git",
        third: "git@github.com:other/skip.git",
    }

    def fake_run(cmd, *, timeout):
        repo = Path(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, urls[repo] + "\n", "")

    monkeypatch.setattr(ga.proc, "run", fake_run)
    found, degraded = ga._local_repos_by_owner(
        [tmp_path], "ME", max_workers=3,
    )

    expected_last = [p for p in scan_order if p in (first, second)][-1]
    assert found == {"same": expected_last}
    assert degraded is False


@pytest.mark.parametrize("returncode", [proc.TIMEOUT_RC, proc.NOTFOUND_RC, proc.OSERR_RC])
def test_local_repos_parallel_scan_aggregates_any_uncertainty(
    monkeypatch, tmp_path, returncode,
):
    good = _fake_local_repo(tmp_path, "good")
    slow = _fake_local_repo(tmp_path, "slow")

    def fake_run(cmd, *, timeout):
        repo = Path(cmd[2])
        if repo == slow:
            return subprocess.CompletedProcess(cmd, returncode, "", "failed")
        return subprocess.CompletedProcess(
            cmd, 0, "git@github.com:me/good.git\n", "",
        )

    monkeypatch.setattr(ga.proc, "run", fake_run)
    found, degraded = ga._local_repos_by_owner(
        [tmp_path], "me", max_workers=2,
    )

    assert found == {"good": good}
    assert degraded is True


def test_local_repos_recognizes_ssh_443_rewrite_shape(monkeypatch, tmp_path):
    repo = _fake_local_repo(tmp_path, "custom-dir")

    def fake_run(cmd, *, timeout):
        assert cmd[-4:] == [
            "config", "--local", "--get-all", "remote.origin.url",
        ]
        return subprocess.CompletedProcess(
            cmd, 0, "ssh://git@ssh.github.com:443/Me/foo.git\n", "",
        )

    monkeypatch.setattr(ga.proc, "run", fake_run)
    found, degraded = ga._local_repos_by_owner([tmp_path], "me", max_workers=2)
    assert found == {"foo": repo}
    assert degraded is False


def test_local_repos_excludes_incomplete_clone_without_degrading(monkeypatch, tmp_path):
    repo = tmp_path / "interrupted"
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(
        ga.proc, "run",
        lambda *args, **kwargs: pytest.fail("damaged repo must not query origin"),
    )
    assert ga._local_repos_by_owner([tmp_path], "me") == ({}, False)


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


def test_missing_remote_404_is_diagnosed_from_local_origin(
    harness, monkeypatch, capsys,
):
    harness["gh"] = [_repo("r1")]
    harness["local"] = ["r1", "r2"]
    _baseline(harness, ["r1", "r2"])
    monkeypatch.setattr(
        ga.git_ops, "origin_url",
        lambda path: "git@github.com:transferred-owner/r2.git",
    )
    probes: list[tuple[str, str]] = []

    def fake_identity(owner, name):
        probes.append((owner, name))
        return "not_found", None, "HTTP 404"

    monkeypatch.setattr(trash_mod, "get_remote_identity", fake_identity)

    ga.run(
        _ac(harness["tmp"], abort_if_shrink_pct=100),
        [harness["tmp"]], push=True, auto_migrate=False,
    )

    assert probes == [("transferred-owner", "r2")]
    assert harness["moved"] == []
    out = capsys.readouterr().out
    assert "GitHub 上已确认不存在（404）" in out
    assert "codesync delete r2 --local-only" in out
    assert "gh auth status" in out


def test_missing_remote_redirect_reports_set_url(harness, monkeypatch, capsys):
    harness["gh"] = [_repo("r1")]
    harness["local"] = ["r1", "r2"]
    _baseline(harness, ["r1", "r2"])
    monkeypatch.setattr(
        ga.git_ops, "origin_url", lambda path: "https://github.com/old/r2.git",
    )
    monkeypatch.setattr(
        trash_mod,
        "get_remote_identity",
        lambda owner, name: (
            "ok", trash_mod.RepoIdentity("RID-r2", "new", "renamed"), "",
        ),
    )

    ga.run(
        _ac(harness["tmp"], abort_if_shrink_pct=100),
        [harness["tmp"]], push=False, auto_migrate=False,
    )

    out = capsys.readouterr().out
    assert "已重定向到 new/renamed" in out
    assert "remote set-url origin git@github.com:new/renamed.git" in out


def test_many_missing_remotes_skip_per_repo_probe(harness, monkeypatch, capsys):
    names = [f"gone-{i}" for i in range(21)]
    harness["gh"] = [_repo("still-active")]
    harness["local"] = names
    _baseline(harness, names)
    monkeypatch.setattr(
        trash_mod,
        "get_remote_identity",
        lambda *args: pytest.fail("bulk guard must skip per-repo GitHub probes"),
    )

    ga.run(
        _ac(harness["tmp"], abort_if_shrink_pct=100),
        [harness["tmp"]], push=False, auto_migrate=False,
    )

    assert "数量过多，跳过逐个确诊" in capsys.readouterr().out


def test_held_remote_probe_stops_at_total_time_budget(
    monkeypatch, tmp_path, capsys,
):
    followups.clear()
    held = [(f"gone-{i}", tmp_path / f"gone-{i}") for i in range(3)]
    monkeypatch.setattr(
        ga.git_ops,
        "origin_url",
        lambda path: f"git@github.com:me/{path.name}.git",
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(ga.time, "monotonic", lambda: clock["now"])
    probes: list[str] = []

    def fake_identity(owner, name):
        probes.append(name)
        clock["now"] = ga._HELD_PROBE_BUDGET_SEC + 1
        return "unavailable", None, "timeout"

    monkeypatch.setattr(trash_mod, "get_remote_identity", fake_identity)

    ga._report_held_remotes(held)

    assert probes == ["gone-0"]
    out = capsys.readouterr().out
    assert out.count("已达本轮确诊时间预算，其余项下轮再查") == 2
    assert "codesync delete" not in out


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


def test_gh_repo_list_timeout_skips_all_destructive_ops(harness, monkeypatch, capsys):
    harness["gh"] = [_repo("foo")]
    _baseline(harness, ["foo"])
    monkeypatch.setattr(ga, "_gh_repo_list", _REAL_GH_REPO_LIST)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "list"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ga.run(
        _ac(harness["tmp"], abort_if_local_missing_pct=100),
        [harness["tmp"]], push=True, auto_migrate=False,
    ) == []
    assert harness["remote_trashed"] == []
    assert harness["moved"] == []
    assert harness["cloned"] == []
    assert harness["memory"]["Known"] == ["foo"]
    captured = capsys.readouterr()
    assert "本轮跳过所有 GitHub 操作" in captured.out + captured.err


def test_local_origin_scan_timeout_never_archives(harness, monkeypatch, capsys):
    harness["gh"] = [_repo("foo")]
    _baseline(harness, ["foo"])
    custom = harness["tmp"] / "custom-dir"
    (custom / ".git").mkdir(parents=True)
    (custom / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    heads = custom / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    (heads / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr(ga, "_local_repos_by_owner", _REAL_LOCAL_SCAN)
    previous_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if (cmd[:2] == ["git", "-C"] and "config" in cmd
                and "remote.origin.url" in cmd):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        return previous_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ga.run(
        _ac(harness["tmp"], abort_if_local_missing_pct=100),
        [harness["tmp"]], push=True, auto_migrate=True,
    )

    assert harness["remote_trashed"] == []
    assert harness["moved"] == []
    assert harness["memory"]["Known"] == ["foo"]
    captured = capsys.readouterr()
    assert "本地 origin 扫描退化" in captured.out + captured.err


def test_known_repo_changed_from_https_to_ssh_is_not_archived(harness, monkeypatch):
    """A protocol migration must not make a known local repo disappear."""
    harness["gh"] = [_repo("foo")]
    _baseline(harness, ["foo"])
    repo = harness["tmp"] / "custom-dir"
    heads = repo / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (heads / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr(ga, "_local_repos_by_owner", _REAL_LOCAL_SCAN)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "-C"] and "remote.origin.url" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, "git@github.com:me/foo.git\n", "",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(ga.proc, "run", fake_run)
    ga.run(
        _ac(harness["tmp"], abort_if_local_missing_pct=100),
        [harness["tmp"]], push=True, auto_migrate=False,
    )

    assert harness["remote_trashed"] == []
    assert harness["memory"]["Known"] == ["foo"]


def test_clone_timeout_warns_and_continues(harness, capsys):
    harness["gh"] = [_repo("one"), _repo("two")]
    harness["clone_timeout"] = {"one"}
    harness["clone_to_local"] = True

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["clone_attempts"] == ["one", "two"]
    assert harness["cloned"] == ["two"]
    assert harness["memory"]["Known"] == ["two"]
    assert all("capture_output" not in kwargs for kwargs in harness["clone_kwargs"])
    captured = capsys.readouterr()
    assert "git clone 超时" in captured.out + captured.err
    assert str(harness["tmp"] / "one") in captured.out + captured.err


@pytest.mark.parametrize(
    "local_origin",
    [
        "git@github.com:ME/foo.git",
        "https://github.com/me/foo.git",
    ],
)
def test_same_origin_incomplete_clone_is_trashed_then_recloned(
    harness, monkeypatch, local_origin,
):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    (dest / ".git" / "refs" / "heads").mkdir(parents=True)
    (dest / ".git" / "HEAD").write_text(
        "ref: refs/heads/.invalid\n", encoding="utf-8",
    )
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: local_origin)
    monkeypatch.setattr(trash_mod, "move_local_to_trash", _REAL_MOVE_LOCAL_TO_TRASH)
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    entries = trash_mod.iter_local_trash([harness["tmp"]])
    assert len(entries) == 1
    assert entries[0][1]["original_name"] == "foo"
    assert entries[0][1]["incomplete_clone"] is True
    assert entries[0][1]["remote_name"] == ""
    assert harness["cloned"] == ["foo"]


def test_clone_target_husk_is_never_deleted_and_names_delete_command(
    harness, monkeypatch, capsys,
):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        ga.git_ops, "origin_url", lambda path: "git@github.com:me/foo.git",
    )
    monkeypatch.setattr(
        ga, "_move_incomplete_clone_to_trash",
        lambda path: pytest.fail("husk must never be auto-moved"),
    )

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert dest.exists()
    assert harness["cloned"] == []
    assert "codesync delete foo" in capsys.readouterr().out


def test_clone_target_owned_by_another_owner_is_not_deleted(
    harness, monkeypatch, capsys,
):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    monkeypatch.setattr(
        ga.git_ops, "origin_url", lambda path: "git@github.com:other/foo.git",
    )
    monkeypatch.setattr(
        ga, "_move_incomplete_clone_to_trash",
        lambda path: pytest.fail("conflicting directory must not be auto-moved"),
    )

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert dest.exists()
    assert harness["cloned"] == []
    assert "remote set-url" in capsys.readouterr().out


def test_incomplete_clone_toctou_recheck_fails_closed(
    harness, monkeypatch,
):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    damage = iter(["incomplete-clone", None])
    monkeypatch.setattr(ga.git_ops, "is_corrupt_repo", lambda path: next(damage))
    monkeypatch.setattr(
        ga.git_ops, "origin_url", lambda path: "git@github.com:me/foo.git",
    )
    monkeypatch.setattr(
        ga, "_move_incomplete_clone_to_trash",
        lambda path: pytest.fail("changed directory must not be auto-moved"),
    )

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert dest.exists()
    assert harness["cloned"] == []


def test_incomplete_clone_trash_move_failure_is_fail_closed(
    harness, monkeypatch, capsys,
):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    monkeypatch.setattr(
        ga.git_ops, "is_corrupt_repo", lambda path: "incomplete-clone",
    )
    monkeypatch.setattr(
        ga.git_ops, "origin_url", lambda path: "git@github.com:me/foo.git",
    )
    trash_target = harness["tmp"] / trash_mod.LOCAL_TRASH_DIR / "saved-foo"
    monkeypatch.setattr(
        ga,
        "_move_incomplete_clone_to_trash",
        lambda path: (False, trash_target, "permission denied"),
    )

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert dest.exists()
    assert harness["cloned"] == []
    out = capsys.readouterr().out
    assert "移动失败，不 clone" in out
    assert str(dest) in out
    assert str(trash_target) in out
