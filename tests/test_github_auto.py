from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import codesync.github_auto as ga
from codesync import auth, state as state_mod, trash as trash_mod
from codesync.config import AutoCloneConfig


_REAL_GH_REPO_LIST = ga._gh_repo_list
_REAL_LOCAL_SCAN = ga._local_repos_by_owner


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

    real_run = ga.subprocess.run
    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "clone"]:
            name = Path(cmd[-1]).name
            data["clone_attempts"].append(name)
            data["clone_kwargs"].append(dict(kwargs))
            if name in data["clone_timeout"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
            data["cloned"].append(name)
            if data["clone_to_local"]:
                data["local"].append(name)
            class Result:
                returncode = 0
            return Result()
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(ga.subprocess, "run", fake_run)
    return data


def _fake_local_repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo
