"""run_sync orchestration tests — focus on the read-only guarantee of --status."""
from __future__ import annotations

from pathlib import Path

import pytest

from codesync import config as cfg_mod
from codesync import sync


@pytest.fixture(autouse=True)
def _no_version_probe(monkeypatch):
    """run_sync now prints a version banner (v2.10.0) which calls
    updater.latest_version. Stub it so these orchestration tests never touch the
    network or the real version-check cache."""
    import codesync.updater as up
    monkeypatch.setattr(up, "latest_version", lambda **k: up.__version__)
    monkeypatch.setattr(sync.time, "sleep", lambda _seconds: None)
    disabled = sync.git_transport.SshMultiplexState(False, "测试关闭", "")
    known = sync.KnownHostsState("/tmp/known_hosts", "cached", "", True)
    monkeypatch.setattr(
        sync.git_transport,
        "configure_ssh_command",
        lambda **k: sync.git_transport.SshTransportState(disabled, known),
    )
    monkeypatch.setattr(
        sync.git_transport, "prewarm_github_master", lambda *a, **k: False,
    )
    monkeypatch.setattr(sync.git_transport, "close_github_master", lambda *a, **k: None)


def test_safety_countdown_explains_guards(monkeypatch, capsys):
    sleeps: list[int] = []
    monkeypatch.setattr(sync.time, "sleep", lambda seconds: sleeps.append(seconds))

    known = sync.KnownHostsState("/tmp/known_hosts", "derived", "", True)
    assert sync._safety_countdown(4, 16, True, known_hosts=known) is True

    out = capsys.readouterr().out
    assert "ssh.github.com:443" in out
    assert "github.com:22" in out
    assert "只处理真正 ahead" in out
    assert "网络操作 workers=4" in out
    assert "本地扫描 workers=16" in out
    assert "SSH 连接复用：已启用" in out
    assert "known_hosts：已启用（来源 derived）" in out
    assert "Ctrl+C" in out
    assert sleeps == [1] * 10


def test_safety_countdown_ctrl_c_cancels_before_sync(monkeypatch, capsys):
    monkeypatch.setattr(sync.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt))

    unavailable = sync.KnownHostsState("", "", "测试不可用", False)
    assert sync._safety_countdown(
        1, 8, False, "配置已关闭", unavailable,
    ) is False
    assert "尚未执行 clone / publish / commit / pull / push" in capsys.readouterr().out


def test_run_sync_cancelled_before_any_sync_action(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config(code_roots=[]))
    monkeypatch.setattr(sync, "_safety_countdown", lambda *args, **kwargs: False)

    import codesync.git_ops as go
    monkeypatch.setattr(go, "find_repos", lambda roots: pytest.fail("scan must not start after cancellation"))
    import codesync.publish as pub
    monkeypatch.setattr(pub, "publish_orphans", lambda *a, **k: pytest.fail("publish must not start"))

    assert sync.run_sync() == 130


def test_pull_config_loads_default_and_explicit_false(monkeypatch, tmp_path):
    from codesync import paths

    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(paths, "config_file", lambda: config_file)

    config_file.write_text("code_roots = []\n", encoding="utf-8")
    assert cfg_mod.load().pull == cfg_mod.PullConfig(rebase=True)

    config_file.write_text(
        "code_roots = []\n\n[pull]\nrebase = false\n",
        encoding="utf-8",
    )
    loaded = cfg_mod.load()
    assert loaded.pull == cfg_mod.PullConfig(rebase=False)
    assert "[pull]\nrebase = false" in cfg_mod._to_toml(loaded)


def test_run_sync_routes_local_and_network_workers_separately(monkeypatch):
    repo = Path("/tmp/fake-repo")
    fake_cfg = cfg_mod.Config(
        code_roots=[],
        auto_clone=cfg_mod.AutoCloneConfig(owner="me", target="/tmp/target"),
        commit=cfg_mod.CommitConfig(skip=[]),
        submodules=cfg_mod.SubmodulesConfig(recurse=True),
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: fake_cfg)
    monkeypatch.setattr(sync, "_safety_countdown", lambda *args, **kwargs: True)

    import codesync.git_ops as go
    local_calls: list[tuple[str, int]] = []
    net_calls: list[tuple[str, int, bool | None]] = []
    find_calls = 0

    def fake_find_repos(_roots):
        nonlocal find_calls
        find_calls += 1
        return [repo]

    monkeypatch.setattr(go, "find_repos", fake_find_repos)
    monkeypatch.setattr(go, "find_corrupt_repos", lambda roots: [])

    def fake_my_owners(cfg, repos):
        local_calls.append(("owners", 11))
        return {"me"}

    monkeypatch.setattr(go, "my_owners", fake_my_owners)
    monkeypatch.setattr(go, "find_nested_repos", lambda *args, **kwargs: [])

    def fake_duplicates(repos, *, max_workers):
        local_calls.append(("origins", max_workers))
        return {}

    monkeypatch.setattr(
        go, "find_duplicate_origins", fake_duplicates,
    )
    monkeypatch.setattr(
        go, "auto_commit_dirty",
        lambda repos, skip, *, max_workers, **k:
            local_calls.append(("dirty", max_workers)) or [],
    )

    def fake_parallel(repos, op, *, max_workers, rebase=True):
        net_calls.append((op, max_workers, rebase if op == "pull" else None))
        return go.OpSummary(op=op, total=len(repos), ok=len(repos), failed=[], elapsed=0.0)

    monkeypatch.setattr(go, "parallel_op", fake_parallel)
    monkeypatch.setattr(
        sync.status_mod, "print_status",
        lambda repos, *, max_workers, **k:
            local_calls.append(("status", max_workers)),
    )
    import codesync.github_auto as ga
    monkeypatch.setattr(
        ga, "run",
        lambda *args, local_workers, **kwargs:
            local_calls.append(("github-auto", local_workers)) or [],
    )

    rc = sync.run_sync(
        net_workers=3, local_workers=11, no_publish=True,
    )
    assert rc == 0
    assert find_calls == 1  # auto_clone makes prewarm certain; step 3 scans once
    assert net_calls == [("pull", 3, True), ("push", 3, None)]
    assert ("origins", 11) in local_calls
    assert ("owners", 11) in local_calls
    assert ("github-auto", 11) in local_calls
    assert ("dirty", 11) in local_calls
    assert ("status", 11) in local_calls


def _stub_sync_pipeline(monkeypatch, tmp_path, fake_cfg, events):
    """Keep orchestration tests offline while exposing commit/pull ordering."""
    repo = tmp_path / "repo"
    monkeypatch.setattr(cfg_mod, "load", lambda: fake_cfg)
    monkeypatch.setattr(sync, "_safety_countdown", lambda *args, **kwargs: True)

    import codesync.git_ops as go
    monkeypatch.setattr(go, "find_repos", lambda _roots: [repo])
    monkeypatch.setattr(go, "find_corrupt_repos", lambda _roots: [])
    monkeypatch.setattr(
        go, "find_duplicate_origins", lambda _repos, *, max_workers: {},
    )
    monkeypatch.setattr(
        sync.status_mod, "print_status", lambda *args, **kwargs: None,
    )

    def fake_parallel(repos, op, *, max_workers, rebase=True):
        events.append((op, rebase if op == "pull" else None))
        return go.OpSummary(
            op=op, total=len(repos), ok=len(repos), failed=[], elapsed=0.0,
        )

    monkeypatch.setattr(go, "parallel_op", fake_parallel)
    return go


def test_run_sync_auto_commit_happens_before_pull(monkeypatch, tmp_path):
    events: list[tuple[str, bool | None]] = []
    fake_cfg = cfg_mod.Config(
        code_roots=[],
        commit=cfg_mod.CommitConfig(skip=[]),
        pull=cfg_mod.PullConfig(),
        submodules=cfg_mod.SubmodulesConfig(recurse=False),
    )
    go = _stub_sync_pipeline(monkeypatch, tmp_path, fake_cfg, events)
    monkeypatch.setattr(
        go,
        "auto_commit_dirty",
        lambda *args, **kwargs: events.append(("commit", None)) or [],
    )

    rc = sync.run_sync(no_publish=True)

    assert rc == 0
    assert events == [("commit", None), ("pull", True), ("push", None)]


def test_run_sync_pull_config_false_uses_ff_only_strategy(monkeypatch, tmp_path):
    events: list[tuple[str, bool | None]] = []
    fake_cfg = cfg_mod.Config(
        code_roots=[],
        pull=cfg_mod.PullConfig(rebase=False),
        submodules=cfg_mod.SubmodulesConfig(recurse=False),
    )
    go = _stub_sync_pipeline(monkeypatch, tmp_path, fake_cfg, events)
    monkeypatch.setattr(go, "auto_commit_dirty", lambda *args, **kwargs: [])

    rc = sync.run_sync(no_publish=True, no_push=True, no_commit=True)

    assert rc == 0
    assert events == [("pull", False)]


def test_run_sync_no_commit_still_pulls(monkeypatch, tmp_path):
    events: list[tuple[str, bool | None]] = []
    fake_cfg = cfg_mod.Config(
        code_roots=[],
        commit=cfg_mod.CommitConfig(enabled=True, skip=[]),
        submodules=cfg_mod.SubmodulesConfig(recurse=False),
    )
    go = _stub_sync_pipeline(monkeypatch, tmp_path, fake_cfg, events)
    monkeypatch.setattr(
        go,
        "auto_commit_dirty",
        lambda *args, **kwargs: pytest.fail("--no-commit must skip auto-commit"),
    )

    rc = sync.run_sync(
        no_publish=True, no_push=True, no_commit=True,
    )

    assert rc == 0
    assert events == [("pull", True)]


def test_status_only_is_read_only(monkeypatch):
    """`codesync sync --status` must NOT trigger auto_clone (which clones/archives —
    a write). It also must not pull/push/publish/commit. We assert by failing if any
    write-path function is invoked."""
    fake_cfg = cfg_mod.Config(
        code_roots=[],
        auto_clone=cfg_mod.AutoCloneConfig(owner="x", target="/tmp/nope"),
        commit=cfg_mod.CommitConfig(),
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: fake_cfg)

    import codesync.github_auto as ga
    monkeypatch.setattr(ga, "run", lambda *a, **k: pytest.fail("auto_clone must not run in --status"))

    import codesync.git_ops as go
    monkeypatch.setattr(go, "find_repos", lambda roots: [])
    monkeypatch.setattr(go, "parallel_op", lambda *a, **k: pytest.fail("pull/push must not run in --status"))
    monkeypatch.setattr(go, "auto_commit_dirty", lambda *a, **k: pytest.fail("auto-commit must not run in --status"))

    import codesync.publish as pub
    monkeypatch.setattr(pub, "publish_orphans", lambda *a, **k: pytest.fail("publish must not run in --status"))

    rc = sync.run_sync(status_only=True)
    assert rc == 0


def test_status_only_skips_auto_clone_even_with_config(monkeypatch):
    """Regression: pre-v2.4.1, auto_clone ran in --status mode (in push mode, no less,
    so it could archive locally-deleted repos). Lock it down."""
    calls = {"auto_clone": 0}
    fake_cfg = cfg_mod.Config(
        code_roots=[],
        auto_clone=cfg_mod.AutoCloneConfig(owner="me", target="/tmp/x"),
    )
    monkeypatch.setattr(cfg_mod, "load", lambda: fake_cfg)

    import codesync.github_auto as ga
    monkeypatch.setattr(ga, "run", lambda *a, **k: calls.__setitem__("auto_clone", calls["auto_clone"] + 1))
    import codesync.git_ops as go
    monkeypatch.setattr(go, "find_repos", lambda roots: [])

    sync.run_sync(status_only=True)
    assert calls["auto_clone"] == 0


def test_missing_autoclone_prints_hint(monkeypatch, capsys):
    """No [auto_clone] in config → sync must SAY so (one dim line) instead of
    silently never cloning repos created on other machines (the V1-migrated
    config trap: feature absent for months, every sync 'succeeded')."""
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config(code_roots=[]))
    import codesync.git_ops as go
    monkeypatch.setattr(go, "find_repos", lambda roots: [])
    import codesync.publish as pub
    monkeypatch.setattr(pub, "publish_orphans", lambda *a, **k: 0)

    rc = sync.run_sync(status_only=False, no_push=True, no_commit=True)
    assert rc == 0
    assert "未配置 [auto_clone]" in capsys.readouterr().out


def test_missing_autoclone_hint_absent_in_status_mode(monkeypatch, capsys):
    """--status keeps quiet about it (read-only report, no nagging)."""
    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config(code_roots=[]))
    import codesync.git_ops as go
    monkeypatch.setattr(go, "find_repos", lambda roots: [])

    sync.run_sync(status_only=True)
    assert "未配置 [auto_clone]" not in capsys.readouterr().out


def test_duplicate_origin_warning_shown(monkeypatch, capsys, tmp_path):
    """Two top-level folders sharing one origin → advisory warning with both names."""
    import subprocess as sp
    for name in ("foo", "foo-old"):
        d = tmp_path / name
        d.mkdir()
        sp.run(["git", "init", "--quiet"], cwd=d, check=True)
        sp.run(["git", "-C", str(d), "remote", "add", "origin",
                "git@github.com:me/foo.git"], check=True, capture_output=True)

    monkeypatch.setattr(cfg_mod, "load", lambda: cfg_mod.Config(code_roots=[str(tmp_path)]))
    import codesync.publish as pub
    monkeypatch.setattr(pub, "publish_orphans", lambda *a, **k: 0)
    import codesync.git_ops as go
    monkeypatch.setattr(go, "parallel_op",
                        lambda repos, op, **k: go.OpSummary(op=op, total=len(repos),
                                                            ok=len(repos), failed=[], elapsed=0.0))
    monkeypatch.setattr(go, "auto_commit_dirty", lambda *a, **k: [])

    rc = sync.run_sync(status_only=False, no_push=True, no_commit=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "个 origin 被多个本地目录共用" in out
    assert "foo, foo-old" in out
