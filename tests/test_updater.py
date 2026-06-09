"""Tests for codesync.updater — focus on the Windows detached-pip wiring,
since that was the silent-failure surface."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from codesync import __repo_url__, updater
from codesync.config import UpdateConfig


@pytest.fixture(autouse=True)
def _isolate_mirror(monkeypatch):
    """Keep _pip_args() network-free by default: no mirror env, github.com
    reachable (so _gh_mirror() returns "" = direct), cache cleared each test."""
    monkeypatch.delenv("CODESYNC_GH_MIRROR", raising=False)
    monkeypatch.delenv("CODESYNC_PIP_INDEX", raising=False)
    monkeypatch.setattr(updater, "_url_ok", lambda *a, **k: True)
    updater._gh_mirror.cache_clear()
    yield
    updater._gh_mirror.cache_clear()


def test_pip_args_is_well_formed() -> None:
    args = updater._pip_args()
    assert args[0] == sys.executable
    assert args[1:4] == ["-m", "pip", "install"]
    assert "--upgrade" in args
    assert args[-1] == f"git+{__repo_url__}.git@main"
    # --user only outside a venv — see _pip_args for why
    assert ("--user" in args) == (not updater._in_venv())


def test_pip_args_no_index_when_direct() -> None:
    """Direct (no mirror) → no --index-url override (use pip's default index)."""
    args = updater._pip_args()
    assert "--index-url" not in args


def test_pip_args_honors_gh_mirror_env(monkeypatch) -> None:
    """CODESYNC_GH_MIRROR rewrites the git+ spec and auto-adds a CN PyPI index."""
    monkeypatch.setenv("CODESYNC_GH_MIRROR", "https://ghfast.top/")  # trailing slash trimmed
    updater._gh_mirror.cache_clear()
    args = updater._pip_args()
    assert args[-1] == f"git+https://ghfast.top/{__repo_url__}.git@main"
    assert "--index-url" in args
    assert "tuna.tsinghua" in args[args.index("--index-url") + 1]


def test_pip_args_auto_mirror_when_github_down(monkeypatch) -> None:
    """No env var, github.com unreachable → first reachable mirror is used."""
    def fake_ok(url, **k):
        return "ghfast.top" in url  # github.com probe fails, mirror probe ok
    monkeypatch.setattr(updater, "_url_ok", fake_ok)
    updater._gh_mirror.cache_clear()
    args = updater._pip_args()
    assert "ghfast.top" in args[-1]


def test_pip_index_env_overrides_default(monkeypatch) -> None:
    """CODESYNC_PIP_INDEX takes precedence over the auto CN mirror."""
    monkeypatch.setenv("CODESYNC_GH_MIRROR", "https://ghfast.top")
    monkeypatch.setenv("CODESYNC_PIP_INDEX", "https://example.test/simple")
    updater._gh_mirror.cache_clear()
    args = updater._pip_args()
    assert args[args.index("--index-url") + 1] == "https://example.test/simple"


def test_pip_args_outside_venv_keeps_user(monkeypatch) -> None:
    """When sys.prefix == base_prefix (no venv), --user is needed to avoid
    needing root for system Python installs."""
    monkeypatch.setattr(sys, "prefix", "/system/python")
    monkeypatch.setattr(sys, "base_prefix", "/system/python")
    args = updater._pip_args()
    assert "--user" in args


def test_pip_args_in_venv_drops_user(monkeypatch) -> None:
    """In a venv (pipx-managed or stdlib venv), pip rejects --user. Must drop it."""
    monkeypatch.setattr(sys, "prefix", "/some/venv")
    monkeypatch.setattr(sys, "base_prefix", "/system/python")
    args = updater._pip_args()
    assert "--user" not in args
    # --upgrade still there
    assert "--upgrade" in args


def test_pip_args_handles_missing_base_prefix(monkeypatch) -> None:
    """Pythons predating PEP 405 lack base_prefix. getattr fallback covers this."""
    monkeypatch.setattr(sys, "prefix", "/anywhere")
    monkeypatch.delattr(sys, "base_prefix", raising=False)
    # Should not raise; treats as no-venv → --user present
    args = updater._pip_args()
    assert "--user" in args


def test_foreground_runs_synchronously(monkeypatch, capsys) -> None:
    """--foreground must call subprocess.run (synchronous), not Popen."""
    called = {}

    def fake_run(cmd, *a, **kw):
        called["cmd"] = cmd
        called["kwargs"] = kw
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("Popen must not be called in foreground"))

    rc = updater.self_update(foreground=True)
    assert rc == 0
    assert called["cmd"] == updater._pip_args()


def test_foreground_propagates_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 7))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("Popen must not be called"))
    assert updater.self_update(foreground=True) == 7


def test_unix_default_is_foreground(monkeypatch) -> None:
    """On Unix, the default (no --foreground) still runs synchronous —
    pip can overwrite in place there, no need for detach."""
    monkeypatch.setattr("os.name", "posix")
    called = {"run": False}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (called.__setitem__("run", True),
                                                             subprocess.CompletedProcess(a[0], 0))[1])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("Popen must not be called on Unix"))
    rc = updater.self_update(foreground=False)
    assert rc == 0
    assert called["run"] is True


def test_windows_detached_uses_log_file_and_devnull_stdin(monkeypatch, tmp_path) -> None:
    """The whole point of v2.2.2: Windows detached pip must NOT inherit closed
    console handles. stdout/stderr go to a real file, stdin = DEVNULL."""
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr(updater.paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(updater.paths, "ensure_config_dir", lambda: tmp_path)
    monkeypatch.setattr(updater.paths, "update_log_file", lambda: tmp_path / "update.log")

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class _Stub:
            pid = 12345
        return _Stub()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("run must not be called in detached mode"))

    rc = updater.self_update(foreground=False)
    assert rc == 0

    kw = captured["kwargs"]
    # The core invariant — these caused the silent failure:
    assert kw["stdin"] == subprocess.DEVNULL, "stdin must be DEVNULL (no inherited console)"
    assert kw["stderr"] == subprocess.STDOUT, "stderr must merge into stdout (=log file)"
    # stdout is the open log file handle — verify by checking it's a writable file object
    assert hasattr(kw["stdout"], "write"), "stdout must be a file-like object, not None"
    assert kw["close_fds"] is True

    # Log file must exist and have a header (so users can find it before pip runs).
    log = tmp_path / "update.log"
    assert log.exists()
    content = log.read_text(encoding="utf-8")
    assert "codesync --update" in content
    assert sys.executable in content  # cmd is logged


def test_windows_detached_uses_creationflags(monkeypatch, tmp_path) -> None:
    """Detached-process creation flags must be set on Windows (otherwise the
    child stays bound to the parent's console and dies with it)."""
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr(updater.paths, "ensure_config_dir", lambda: tmp_path)
    monkeypatch.setattr(updater.paths, "update_log_file", lambda: tmp_path / "update.log")

    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: (captured.update(kw), type("S", (), {"pid": 1})())[1])

    updater.self_update(foreground=False)

    # On a real Windows interpreter, DETACHED_PROCESS would resolve to 0x8.
    # We just assert creationflags is non-zero — if it's 0, the child is bound
    # to our console and the whole fix is moot.
    if hasattr(subprocess, "DETACHED_PROCESS"):
        assert captured["creationflags"] != 0
    else:
        # Non-Windows host running the test: subprocess module has no
        # DETACHED_PROCESS attr, so getattr(..., 0) = 0 is acceptable.
        assert "creationflags" in captured


# ---------- version gate (v2.7.0) ----------

@pytest.fixture
def _isolate_version_cache(monkeypatch, tmp_path):
    """Point the version-check cache at a temp file so tests don't touch the
    real config dir, and make sure no test here hits the network for `latest`."""
    monkeypatch.setattr(updater.paths, "version_check_file",
                        lambda: tmp_path / "version-check.json")
    monkeypatch.setattr(updater.paths, "ensure_config_dir", lambda: tmp_path)
    yield tmp_path


@pytest.mark.parametrize("s,expected", [
    ("2.6.2", (2, 6, 2)),
    ("2.7.0", (2, 7, 0)),
    ("2.7", (2, 7, 0)),
    ("3", (3, 0, 0)),
    ("2.6.2+source", (2, 6, 2)),
    ("0.0.0+source", (0, 0, 0)),
    ("garbage", None),
    ("", None),
])
def test_parse_version(s, expected) -> None:
    assert updater._parse_version(s) == expected


def test_latest_version_uses_fresh_cache_without_network(_isolate_version_cache, monkeypatch) -> None:
    """A cache entry within TTL is returned without calling the network."""
    (_isolate_version_cache / "version-check.json").write_text(
        '{"latest": "9.9.9", "checked_at": "%s"}'
        % updater.datetime.now(updater.timezone.utc).isoformat(),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "_fetch_latest_version",
                        lambda **k: pytest.fail("must not hit network with fresh cache"))
    assert updater.latest_version(ttl_hours=12) == "9.9.9"


def test_latest_version_reprobes_when_expired(_isolate_version_cache, monkeypatch) -> None:
    """Expired cache → re-probe; a successful probe updates the cache."""
    old = (updater.datetime.now(updater.timezone.utc)).replace(year=2000).isoformat()
    (_isolate_version_cache / "version-check.json").write_text(
        '{"latest": "1.0.0", "checked_at": "%s"}' % old, encoding="utf-8",
    )
    monkeypatch.setattr(updater, "_fetch_latest_version", lambda **k: "2.7.0")
    assert updater.latest_version(ttl_hours=12) == "2.7.0"


def test_latest_version_network_failure_returns_none(_isolate_version_cache, monkeypatch) -> None:
    """No cache + probe fails → None (caller fails open)."""
    monkeypatch.setattr(updater, "_fetch_latest_version", lambda **k: None)
    assert updater.latest_version(ttl_hours=12) is None


def test_gate_passes_when_up_to_date(monkeypatch) -> None:
    monkeypatch.setattr(updater, "__version__", "2.7.0")
    monkeypatch.setattr(updater, "latest_version", lambda **k: "2.7.0")
    assert updater.enforce_up_to_date(UpdateConfig(), skip=False) is True


def test_gate_blocks_when_outdated(monkeypatch) -> None:
    monkeypatch.setattr(updater, "__version__", "2.6.2")
    monkeypatch.setattr(updater, "latest_version", lambda **k: "2.7.0")
    assert updater.enforce_up_to_date(UpdateConfig(), skip=False) is False


def test_gate_skip_flag_bypasses_block(monkeypatch) -> None:
    monkeypatch.setattr(updater, "__version__", "2.6.2")
    monkeypatch.setattr(updater, "latest_version", lambda **k: "2.7.0")
    assert updater.enforce_up_to_date(UpdateConfig(), skip=True) is True


def test_gate_warns_only_when_block_disabled(monkeypatch) -> None:
    monkeypatch.setattr(updater, "__version__", "2.6.2")
    monkeypatch.setattr(updater, "latest_version", lambda **k: "2.7.0")
    uc = UpdateConfig(block_if_outdated=False)
    assert updater.enforce_up_to_date(uc, skip=False) is True


def test_gate_disabled_by_config(monkeypatch) -> None:
    """check=false → no probe, always proceed."""
    monkeypatch.setattr(updater, "__version__", "2.6.2")
    monkeypatch.setattr(updater, "latest_version",
                        lambda **k: pytest.fail("must not probe when check=false"))
    assert updater.enforce_up_to_date(UpdateConfig(check=False), skip=False) is True


def test_gate_skips_source_checkout(monkeypatch) -> None:
    """0.0.0+source → never gated (don't block developers)."""
    monkeypatch.setattr(updater, "__version__", "0.0.0+source")
    monkeypatch.setattr(updater, "latest_version",
                        lambda **k: pytest.fail("must not probe for source checkout"))
    assert updater.enforce_up_to_date(UpdateConfig(), skip=False) is True


def test_gate_fails_open_on_network_failure(monkeypatch) -> None:
    """latest unknown (network down) → proceed."""
    monkeypatch.setattr(updater, "__version__", "2.6.2")
    monkeypatch.setattr(updater, "latest_version", lambda **k: None)
    assert updater.enforce_up_to_date(UpdateConfig(), skip=False) is True
