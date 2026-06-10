"""run_sync orchestration tests — focus on the read-only guarantee of --status."""
from __future__ import annotations

import pytest

from codesync import config as cfg_mod
from codesync import sync


@pytest.fixture(autouse=True)
def _no_version_probe(monkeypatch):
    """run_sync now prints a version banner (v2.10.0) which calls
    updater.latest_version. Stub it so these orchestration tests never touch the
    network or the real version-check cache."""
    import codesync.updater as up
    monkeypatch.setattr(up, "latest_version", lambda **k: None)


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
