"""Known rebaseline: recover from the bulk local-missing guard without deleting state."""
from __future__ import annotations

import pytest

import codesync.github_auto as ga
from codesync import cli
from tests import test_github_auto as gh_tests
from tests.test_github_auto import _ac, _baseline, _repo

# Reuse the github_auto offline harness (fake gh listing, local scan, clone, state).
harness = gh_tests.harness


def _stale_known_machine(harness, *, known, local):
    harness["gh"] = [_repo(n) for n in known]
    harness["local"] = list(local)
    harness["clone_to_local"] = True
    _baseline(harness, known)


def _run(harness, **kwargs):
    kwargs.setdefault("push", True)
    return ga.run(_ac(harness["tmp"]), [harness["tmp"]], auto_migrate=False, **kwargs)


def _no_input(prompt=""):
    raise AssertionError("non-interactive run must not prompt")


NAMES = [f"r{i}" for i in range(10)]


def test_guard_noninteractive_aborts_and_hints_rebaseline(harness, monkeypatch, capsys):
    _stale_known_machine(harness, known=NAMES, local=[])
    monkeypatch.setattr("builtins.input", _no_input)
    with pytest.raises(SystemExit):
        _run(harness, interactive=False)
    out = capsys.readouterr()
    text = out.out + out.err
    assert "codesync sync --rebaseline" in text
    assert harness["cloned"] == []
    assert harness["remote_trashed"] == []
    assert harness["memory"]["Known"] == NAMES


def test_guard_default_is_noninteractive(harness, monkeypatch):
    _stale_known_machine(harness, known=NAMES, local=[])
    monkeypatch.setattr("builtins.input", _no_input)
    with pytest.raises(SystemExit):
        _run(harness)
    assert harness["cloned"] == []


def test_rebaseline_flag_clones_missing_and_keeps_other_state(harness, monkeypatch):
    _stale_known_machine(harness, known=NAMES, local=["r0"])
    harness["gh"].append(_repo("tomb"))
    memory = harness["memory"]
    memory["Tombstones"] = {"RID-tomb": "2026-06-20T12:00:00+00:00"}
    trash_record = {
        "repo_id": "RID-gone", "owner": "me", "original_name": "gone",
        "remote_name": "gone", "trashed_at": "2026-06-20T12:00:00+00:00",
        "local_only": True,
    }
    memory["Trash"] = {"RID-gone": dict(trash_record)}
    memory["JournalSeen"] = {"deletions/abc.json": "2026-06-20T12:00:00+00:00"}
    monkeypatch.setattr("builtins.input", _no_input)

    _run(harness, rebaseline=True)

    assert sorted(harness["cloned"]) == NAMES[1:]
    assert "tomb" not in harness["cloned"]
    assert harness["remote_trashed"] == []
    assert memory["Tombstones"] == {"RID-tomb": "2026-06-20T12:00:00+00:00"}
    assert memory["Trash"]["RID-gone"] == trash_record
    assert memory["JournalSeen"] == {"deletions/abc.json": "2026-06-20T12:00:00+00:00"}
    assert memory["Known"] == NAMES
    assert memory["PendingArchives"] == {}


def test_rebaseline_never_archives_even_below_threshold(harness):
    _stale_known_machine(harness, known=["a", "b", "c"], local=["a", "b"])
    _run(harness, rebaseline=True)
    assert harness["remote_trashed"] == []
    assert harness["cloned"] == ["c"]


def test_rebaseline_works_without_push(harness):
    _stale_known_machine(harness, known=NAMES, local=[])
    _run(harness, push=False, rebaseline=True)
    assert sorted(harness["cloned"]) == NAMES
    assert harness["memory"]["PendingArchives"] == {}


def test_rebaseline_refused_when_scan_degraded(harness, capsys):
    _stale_known_machine(harness, known=NAMES, local=["r0"])
    harness["clone_to_local"] = False
    harness["scan_degraded"] = True
    _run(harness, rebaseline=True)
    assert harness["cloned"] == []
    assert set(harness["memory"]["Known"]) == set(NAMES)
    assert "不重建 baseline" in capsys.readouterr().out


def test_interactive_yes_rebaselines_and_continues(harness, monkeypatch, capsys):
    _stale_known_machine(harness, known=NAMES, local=[])
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "y")
    _run(harness, interactive=True)
    assert len(prompts) == 1
    assert "[y/N]" in prompts[0]
    assert sorted(harness["cloned"]) == NAMES
    assert harness["remote_trashed"] == []
    assert harness["memory"]["Known"] == NAMES
    assert "像是新机器" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "n", "no", "rebaseline"])
def test_interactive_anything_but_yes_aborts(harness, monkeypatch, answer):
    _stale_known_machine(harness, known=NAMES, local=[])
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    with pytest.raises(SystemExit):
        _run(harness, interactive=True)
    assert harness["cloned"] == []
    assert harness["remote_trashed"] == []
    assert harness["memory"]["Known"] == NAMES


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_interactive_eof_or_ctrl_c_aborts(harness, monkeypatch, exc):
    _stale_known_machine(harness, known=NAMES, local=[])

    def boom(prompt=""):
        raise exc
    monkeypatch.setattr("builtins.input", boom)
    with pytest.raises(SystemExit):
        _run(harness, interactive=True)
    assert harness["cloned"] == []
    assert harness["memory"]["Known"] == NAMES


def test_interactive_partial_local_is_diagnosed_as_not_new_machine(harness, monkeypatch, capsys):
    # 4/10 still present locally (missing 60% > 50%): not a fresh machine.
    _stale_known_machine(harness, known=NAMES, local=NAMES[:4])
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    with pytest.raises(SystemExit):
        _run(harness, interactive=True)
    out = capsys.readouterr().out
    assert "不像新机器" in out
    assert "像是新机器" not in out.replace("不像新机器", "")


def test_interactive_prompt_lists_code_roots(harness, monkeypatch, capsys):
    _stale_known_machine(harness, known=NAMES, local=[])
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    with pytest.raises(SystemExit):
        _run(harness, interactive=True)
    assert str(harness["tmp"]) in capsys.readouterr().out


def test_no_prompt_when_guard_not_triggered(harness, monkeypatch):
    _stale_known_machine(harness, known=NAMES, local=NAMES)
    monkeypatch.setattr("builtins.input", _no_input)
    _run(harness, interactive=True)
    assert harness["cloned"] == []
    assert harness["memory"]["Known"] == NAMES


# ---------- CLI plumbing ----------

def _capture_run_sync(monkeypatch):
    calls: list[dict] = []
    import codesync.sync as sync_mod
    monkeypatch.setattr(sync_mod, "run_sync", lambda **kw: calls.append(kw) or 0)
    monkeypatch.setattr(cli, "_ensure_runtime_config", lambda args: True)
    monkeypatch.setattr(cli, "_configure_ssh_if_needed", lambda args: None)
    monkeypatch.setattr("codesync.updater.report_pending_update", lambda: None)
    return calls


@pytest.mark.parametrize("tty", [True, False])
def test_cli_sync_passes_rebaseline_and_interactive(monkeypatch, tty):
    calls = _capture_run_sync(monkeypatch)
    monkeypatch.setattr(cli, "_can_prompt", lambda: tty)
    assert cli.main(["sync", "--rebaseline"]) == 0
    assert calls[-1]["rebaseline"] is True
    assert calls[-1]["interactive"] is tty
    assert cli.main(["sync"]) == 0
    assert calls[-1]["rebaseline"] is False


@pytest.mark.parametrize("command", ["pull", "push"])
def test_rebaseline_is_sync_only(command):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([command, "--rebaseline"])


def test_rebaseline_waives_candidate_check_like_allow_empty(monkeypatch, tmp_path):
    from codesync import config
    cfg = config.Config(code_roots=[str(tmp_path)])
    args = cli._build_parser().parse_args(["sync", "--rebaseline"])
    assert cli._runtime_root_problems(cfg, args) == []
    missing = config.Config(code_roots=[str(tmp_path / "missing")])
    assert cli._runtime_root_problems(missing, args)


def test_run_sync_forwards_rebaseline_to_github_auto(monkeypatch, tmp_path):
    import codesync.sync as sync_mod
    import codesync.updater as up

    class Stop(Exception):
        pass

    seen: list[dict] = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs)
        raise Stop

    monkeypatch.setattr(ga, "run", fake_run)
    monkeypatch.setattr(up, "latest_version", lambda **k: up.__version__)
    disabled = sync_mod.git_transport.SshMultiplexState(False, "测试关闭", "")
    known = sync_mod.KnownHostsState("/tmp/known_hosts", "cached", "", True)
    monkeypatch.setattr(
        sync_mod.git_transport, "configure_ssh_command",
        lambda **k: sync_mod.git_transport.SshTransportState(disabled, known),
    )
    monkeypatch.setattr(sync_mod.git_transport, "prewarm_github_master", lambda *a, **k: False)
    monkeypatch.setattr(sync_mod.git_transport, "configure_http_stall_detection", lambda **k: None)
    monkeypatch.setattr(sync_mod.git_transport, "close_github_master", lambda *a, **k: None)
    monkeypatch.setattr(
        sync_mod.git_ops, "cleanup_stale_packs",
        lambda roots: sync_mod.git_ops.PackCleanup(0, 0, 0, 0),
    )
    monkeypatch.setattr(sync_mod, "_safety_countdown", lambda *a, **k: True)
    cfg = sync_mod.cfg_mod.Config(
        code_roots=[str(tmp_path)],
        auto_clone=sync_mod.cfg_mod.AutoCloneConfig(owner="me", target=str(tmp_path)),
    )
    monkeypatch.setattr(sync_mod.cfg_mod, "load", lambda: cfg)

    with pytest.raises(Stop):
        sync_mod.run_sync(rebaseline=True, interactive=True)
    assert seen[-1]["rebaseline"] is True
    assert seen[-1]["interactive"] is True

    with pytest.raises(Stop):
        sync_mod.run_sync()
    assert seen[-1]["rebaseline"] is False
    assert seen[-1]["interactive"] is False
