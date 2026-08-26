"""Tests for codesync's process-scoped GitHub SSH-over-443 routing."""
from __future__ import annotations

import shutil
import subprocess
import shlex
import tempfile
from pathlib import Path

import pytest

from codesync import git_transport, proc
from codesync.git_transport import (
    SshMultiplexState,
    SshTransportState,
    close_github_master,
    configure_github_ssh_over_443,
    configure_http_stall_detection,
    configure_ssh_command,
    configure_ssh_multiplexing,
    prewarm_github_master,
)
from codesync.known_hosts import KnownHostsState


@pytest.fixture(autouse=True)
def _reset_own_ssh_command(monkeypatch):
    monkeypatch.setattr(git_transport, "_OWN_SSH_COMMAND", None)


def _known_state(path: str = "/tmp/codesync-known-hosts") -> KnownHostsState:
    return KnownHostsState(path, "derived", "", True)


@pytest.fixture
def short_tmp():
    """A short-lived directory with a SHORT path.

    pytest's tmp_path is ~110 chars, which alone blows the unix-socket
    sun_path budget — useless for exercising ControlPath selection.
    """
    d = Path(tempfile.mkdtemp(prefix="cst-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "git@github.com:tinyvane/demo.git",
            "ssh://git@ssh.github.com:443/tinyvane/demo.git",
        ),
        (
            "ssh://git@github.com/tinyvane/demo.git",
            "ssh://git@ssh.github.com:443/tinyvane/demo.git",
        ),
    ],
)
def test_configure_rewrites_common_github_ssh_urls(
    tmp_path: Path, source: str, expected: str,
):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", source],
        check=True,
    )
    env: dict[str, str] = {}
    configure_github_ssh_over_443(env)

    result = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
        env=env,
    )

    assert result.stdout.strip() == expected


def test_configure_preserves_existing_entries_and_is_idempotent():
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "X-Test: existing",
    }

    configure_github_ssh_over_443(env)
    configure_github_ssh_over_443(env)

    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "X-Test: existing"
    assert env["GIT_CONFIG_VALUE_1"] == "git@github.com:"
    assert env["GIT_CONFIG_VALUE_2"] == "ssh://git@github.com/"


def test_http_stall_config_coexists_with_443_rewrites_and_is_idempotent():
    env: dict[str, str] = {}
    configure_github_ssh_over_443(env)
    configure_http_stall_detection(env)
    configure_http_stall_detection(env)

    pairs = {
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs == {
        ("url.ssh://git@ssh.github.com:443/.insteadOf", "git@github.com:"),
        ("url.ssh://git@ssh.github.com:443/.insteadOf", "ssh://git@github.com/"),
        ("http.lowSpeedLimit", "1000"),
        ("http.lowSpeedTime", str(git_transport.DEFAULT_STALL_SECONDS)),
    }


@pytest.mark.parametrize("kwargs", [{"bytes_per_sec": 0}, {"seconds": 0}])
def test_http_stall_config_zero_disables_injection(kwargs):
    env: dict[str, str] = {}
    configure_http_stall_detection(env, **kwargs)
    assert "GIT_CONFIG_COUNT" not in env


def test_configure_repairs_bad_count_without_overwriting_existing_slot():
    env = {
        "GIT_CONFIG_COUNT": "not-a-number",
        "GIT_CONFIG_KEY_4": "user.name",
        "GIT_CONFIG_VALUE_4": "existing",
    }

    configure_github_ssh_over_443(env)

    assert env["GIT_CONFIG_COUNT"] == "7"
    assert env["GIT_CONFIG_KEY_4"] == "user.name"
    assert env["GIT_CONFIG_VALUE_4"] == "existing"


def test_multiplex_disabled_on_windows(monkeypatch):
    monkeypatch.setattr(git_transport.os, "name", "nt")
    state = configure_ssh_multiplexing({})
    assert state.enabled is False
    assert "Windows" in state.reason


def test_multiplex_respects_existing_git_ssh_command():
    env = {"GIT_SSH_COMMAND": "ssh -i /tmp/user-key"}
    state = configure_ssh_multiplexing(env)
    assert state.enabled is False
    assert "用户自定义" in state.reason
    assert env["GIT_SSH_COMMAND"] == "ssh -i /tmp/user-key"


def test_multiplex_config_disabled_does_not_write_env():
    env: dict[str, str] = {}
    state = configure_ssh_multiplexing(env, enabled=False)
    assert state.enabled is False
    assert state.reason == "配置已关闭"
    assert "GIT_SSH_COMMAND" not in env


def test_multiplex_disables_when_every_candidate_is_too_long(monkeypatch, tmp_path):
    long_dir = tmp_path / ("x" * 120)
    monkeypatch.setattr(
        git_transport, "_control_dir_candidates", lambda: (long_dir, long_dir / "b"),
    )
    env: dict[str, str] = {}
    state = configure_ssh_multiplexing(env)
    assert state.enabled is False
    assert "ControlPath" in state.reason
    assert "GIT_SSH_COMMAND" not in env


def test_multiplex_falls_back_when_preferred_candidate_is_too_long(monkeypatch, tmp_path, short_tmp):
    too_long = tmp_path / ("x" * 120)
    usable = short_tmp / "s"
    monkeypatch.setattr(
        git_transport, "_control_dir_candidates", lambda: (too_long, usable),
    )
    state = configure_ssh_multiplexing({})
    assert state.enabled is True
    assert Path(state.control_path).parent == usable
    assert Path(state.control_path).name.endswith("-%C")


def test_control_path_budget_accounts_for_the_full_40_char_hash():
    # ssh expands %C to a 40-char SHA1 hex digest. Estimating 32 would under-count
    # by 8 bytes and let a path through that ssh then refuses to bind.
    assert git_transport._CONTROL_HASH_LEN == 40
    budget = git_transport._CONTROL_PATH_MAX_BYTES
    just_over = Path("/" + "a" * (budget - 40) + "/%C")
    assert git_transport._control_path_fits(just_over) is False


def test_multiplex_quotes_control_path_with_spaces(monkeypatch, short_tmp):
    control_dir = short_tmp / "My Dir"
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (control_dir,))
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(),
    )
    env: dict[str, str] = {}
    transport = configure_ssh_command(env)
    state = transport.mux
    argv = shlex.split(env["GIT_SSH_COMMAND"])
    assert state.enabled is True
    # The space in the directory must survive git's shell-like re-parsing intact.
    assert f"ControlPath={state.control_path}" in argv
    assert Path(state.control_path).parent == control_dir
    assert control_dir.stat().st_mode & 0o777 == 0o700


def test_multiplex_configuration_is_idempotent(monkeypatch, short_tmp):
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp / "s",))
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(),
    )
    env: dict[str, str] = {}
    first = configure_ssh_command(env)
    command = env["GIT_SSH_COMMAND"]
    second = configure_ssh_command(env)
    assert second == first
    assert env["GIT_SSH_COMMAND"] == command
    assert env["GIT_SSH_COMMAND"].count("ControlMaster=auto") == 1
    argv = shlex.split(env["GIT_SSH_COMMAND"])
    assert sum(a.startswith("ServerAliveInterval=") for a in argv) == 1
    assert sum(a.startswith("ServerAliveCountMax=") for a in argv) == 1


def test_ssh_command_preserves_default_known_hosts_then_appends_codesync(monkeypatch):
    managed = "/tmp/path with spaces/known_hosts"
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(managed),
    )
    env: dict[str, str] = {}

    state = configure_ssh_command(env, multiplex_enabled=False)

    argv = shlex.split(env["GIT_SSH_COMMAND"])
    option = next(arg for arg in argv if arg.startswith("UserKnownHostsFile="))
    files = shlex.split(option.removeprefix("UserKnownHostsFile="))
    assert files[:2] == ["~/.ssh/known_hosts", "~/.ssh/known_hosts2"]
    # ssh splits this list on whitespace itself, so a path containing a space
    # must stay one entry after ssh's own (double-quote aware) parse.
    assert files[-1] == managed
    assert state.known_hosts.enabled is True
    assert "StrictHostKeyChecking" not in env["GIT_SSH_COMMAND"]


def test_windows_keeps_known_hosts_while_disabling_mux(monkeypatch):
    monkeypatch.setattr(git_transport.os, "name", "nt")
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(),
    )
    env: dict[str, str] = {}

    configure_github_ssh_over_443(env)
    configure_http_stall_detection(env)
    state = configure_ssh_command(env)

    assert state.mux.enabled is False
    assert "Windows" in state.mux.reason
    assert state.known_hosts.enabled is True
    assert "UserKnownHostsFile=" in env["GIT_SSH_COMMAND"]
    assert "ControlMaster" not in env["GIT_SSH_COMMAND"]
    argv = shlex.split(env["GIT_SSH_COMMAND"])
    assert any(a.startswith("ServerAliveInterval=") for a in argv)
    assert any(a.startswith("ServerAliveCountMax=") for a in argv)
    pairs = {
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert ("http.lowSpeedLimit", "1000") in pairs
    assert ("http.lowSpeedTime", str(git_transport.DEFAULT_STALL_SECONDS)) in pairs


def test_user_git_ssh_command_disables_both_features_without_overwrite(monkeypatch):
    monkeypatch.setattr(
        git_transport,
        "ensure_github_443_known_hosts",
        lambda: pytest.fail("user override must avoid touching known_hosts"),
    )
    env = {"GIT_SSH_COMMAND": "ssh -i /tmp/user-key"}

    state = configure_ssh_command(env)

    assert isinstance(state, SshTransportState)
    assert state.mux.enabled is False
    assert state.known_hosts.enabled is False
    assert "用户自定义" in state.mux.reason
    assert "用户自定义" in state.known_hosts.reason
    assert env["GIT_SSH_COMMAND"] == "ssh -i /tmp/user-key"


def test_own_previous_command_can_be_rewritten_without_stacking(monkeypatch, short_tmp):
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp / "s",))
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(),
    )
    env: dict[str, str] = {}

    first = configure_ssh_command(env, multiplex_enabled=False)
    first_command = env["GIT_SSH_COMMAND"]
    second = configure_ssh_command(env, multiplex_enabled=True)
    argv = shlex.split(env["GIT_SSH_COMMAND"])

    assert first.known_hosts.enabled is True
    assert second.mux.enabled is True
    assert env["GIT_SSH_COMMAND"] != first_command
    assert argv.count("ControlMaster=auto") == 1
    assert sum(arg.startswith("UserKnownHostsFile=") for arg in argv) == 1


def test_known_hosts_config_opt_out_does_not_touch_cache(monkeypatch):
    monkeypatch.setattr(
        git_transport,
        "ensure_github_443_known_hosts",
        lambda: pytest.fail("disabled means do not touch known_hosts"),
    )
    env: dict[str, str] = {}

    state = configure_ssh_command(
        env, multiplex_enabled=False, known_hosts_enabled=False, stall_seconds=0,
    )

    assert state.known_hosts.enabled is False
    assert state.known_hosts.reason == "配置已关闭"
    assert "GIT_SSH_COMMAND" not in env


def test_ssh_stall_detection_can_be_disabled(monkeypatch):
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(),
    )
    env: dict[str, str] = {}
    configure_ssh_command(env, multiplex_enabled=False, stall_seconds=0)
    argv = shlex.split(env["GIT_SSH_COMMAND"])
    assert not any(arg.startswith("ServerAlive") for arg in argv)
    assert any(arg.startswith("UserKnownHostsFile=") for arg in argv)


def test_ssh_command_contains_mux_known_hosts_and_server_alive(monkeypatch, short_tmp):
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp / "s",))
    monkeypatch.setattr(
        git_transport, "ensure_github_443_known_hosts", lambda: _known_state(),
    )
    env: dict[str, str] = {}
    configure_ssh_command(env)
    argv = shlex.split(env["GIT_SSH_COMMAND"])
    assert "ControlMaster=auto" in argv
    assert any(arg.startswith("UserKnownHostsFile=") for arg in argv)
    assert any(a.startswith("ServerAliveInterval=") for a in argv)
    assert any(a.startswith("ServerAliveCountMax=") for a in argv)


def test_prewarm_treats_github_exit_one_auth_message_as_success(monkeypatch):
    calls: list[tuple[list[str], int]] = []

    def fake_run(argv, *, timeout, **kwargs):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Hi me! You've successfully authenticated",
        )

    monkeypatch.setattr(proc, "run", fake_run)
    state = SshMultiplexState(True, "", "/tmp/codesync/%C", 60)
    assert prewarm_github_master(state, timeout=proc.T_NET) is True
    assert calls[0][1] == proc.T_NET
    assert calls[0][0][-4:] == ["-p", "443", "-T", "git@ssh.github.com"]


@pytest.mark.parametrize("exc", [TimeoutError(), FileNotFoundError()])
def test_prewarm_and_close_swallow_launch_failures(monkeypatch, exc):
    monkeypatch.setattr(proc, "run", lambda *a, **k: (_ for _ in ()).throw(exc))
    state = SshMultiplexState(True, "", "/tmp/codesync/%C")
    assert prewarm_github_master(state, timeout=proc.T_NET) is False
    close_github_master(state)  # must not raise


def test_prewarm_and_close_target_the_same_endpoint_git_dials(monkeypatch):
    """%C hashes host+PORT+user, so a port-22 master is a socket git never uses.

    codesync rewrites GitHub SSH to ssh://git@ssh.github.com:443/, so warming or
    closing on the default port silently manages the wrong connection: the
    prewarm buys nothing and the teardown leaves the real master running.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        proc, "run",
        lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    state = SshMultiplexState(
        True, "", "/tmp/codesync-ssh/%C", 60, "/tmp/codesync-known-hosts",
    )

    prewarm_github_master(state, timeout=proc.T_NET)
    close_github_master(state)

    assert len(calls) == 2
    for argv in calls:
        assert "-p" in argv and argv[argv.index("-p") + 1] == "443"
        assert argv[-1].endswith("git@ssh.github.com")
        known_option = next(
            arg for arg in argv if arg.startswith("UserKnownHostsFile=")
        )
        assert known_option.endswith("/tmp/codesync-known-hosts")


def test_prepare_control_dir_rejects_a_symlinked_directory(short_tmp):
    tmp_path = short_tmp
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert git_transport._prepare_control_dir(link) is False


def test_control_path_is_pid_scoped_so_runs_cannot_close_each_other(monkeypatch, short_tmp):
    """Two concurrent codesync runs must not share one master connection.

    Both tear their master down on exit, so a shared socket means a --status or
    Ctrl+C run can kill the connection another sync is mid-pull on.
    """
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp,))
    monkeypatch.setattr(git_transport.os, "getpid", lambda: 11111)
    first = configure_ssh_multiplexing({})
    monkeypatch.setattr(git_transport.os, "getpid", lambda: 22222)
    second = configure_ssh_multiplexing({})

    assert first.control_path != second.control_path
    assert first.control_path.endswith("11111-%C")
    assert second.control_path.endswith("22222-%C")


def test_sweep_removes_only_sockets_of_dead_processes(monkeypatch, short_tmp):
    digest = "a" * git_transport._CONTROL_HASH_LEN
    dead = short_tmp / f"4242-{digest}"
    alive = short_tmp / f"4343-{digest}"
    unrelated = short_tmp / "keep-me.txt"
    for f in (dead, alive, unrelated):
        f.write_text("")
    monkeypatch.setattr(git_transport, "_pid_alive", lambda pid: pid == 4343)

    git_transport._sweep_stale_sockets(short_tmp)

    assert not dead.exists()
    assert alive.exists()
    assert unrelated.exists()


def test_sweep_never_raises_on_an_unreadable_directory(short_tmp):
    git_transport._sweep_stale_sockets(short_tmp / "does-not-exist")  # must not raise


def test_http_stall_detection_is_idempotent_and_updates_in_place():
    """cli.main() installs defaults and run_sync() re-applies the user's values,
    so a plain append would grow GIT_CONFIG_* on every call."""
    env: dict[str, str] = {}
    git_transport.configure_github_ssh_over_443(env)
    git_transport.configure_http_stall_detection(env, bytes_per_sec=1000, seconds=120)
    first = env["GIT_CONFIG_COUNT"]

    git_transport.configure_http_stall_detection(env, bytes_per_sec=2000, seconds=60)

    assert env["GIT_CONFIG_COUNT"] == first
    # A dict would collapse the two 443 rewrites: they share one config key.
    entries = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(first))
    ]
    assert ("http.lowSpeedLimit", "2000") in entries
    assert ("http.lowSpeedTime", "60") in entries
    # Both 443 rewrites must survive untouched.
    assert sum(1 for _, v in entries if v == "git@github.com:") == 1
    assert sum(1 for _, v in entries if v == "ssh://git@github.com/") == 1


def test_http_stall_detection_disabled_injects_nothing():
    env: dict[str, str] = {}
    git_transport.configure_http_stall_detection(env, bytes_per_sec=0, seconds=120)
    assert "GIT_CONFIG_COUNT" not in env


def test_disabling_neutralises_defaults_installed_by_cli_main():
    """cli.main() injects the defaults before any config is read, so `[sync]
    stall_bytes_per_sec = 0` can only take effect by overwriting them."""
    env: dict[str, str] = {}
    git_transport.configure_github_ssh_over_443(env)
    git_transport.configure_http_stall_detection(env)

    git_transport.configure_http_stall_detection(env, bytes_per_sec=0, seconds=0)

    entries = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert entries["http.lowSpeedLimit"] == "0"   # 0 = no low-speed abort in Git
    assert entries["http.lowSpeedTime"] == "0"


def test_stall_window_tolerates_silent_server_side_compression():
    """The window must outlast GitHub's zero-byte "Compressing objects" phase.

    codesync passes --quiet and captures stderr through a pipe, and
    `git fetch --quiet` asks the server for no sideband progress, so a healthy
    fetch of a large repository delivers ZERO bytes while the server prepares
    the pack. Any positive lowSpeedLimit aborts once that outlasts the window,
    so a short window kills healthy fetches rather than dead connections.
    """
    assert git_transport.DEFAULT_STALL_SECONDS >= 300
    # Still far quicker than the duration backstop it is meant to front-run.
    assert git_transport.DEFAULT_STALL_SECONDS * 4 <= proc.T_NET_LONG


def test_ssh_server_alive_window_matches_the_stall_budget(short_tmp, monkeypatch):
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp,))
    env: dict[str, str] = {}
    git_transport.configure_ssh_command(
        env, multiplex_enabled=True,
        stall_seconds=git_transport.DEFAULT_STALL_SECONDS,
    )
    argv = shlex.split(env["GIT_SSH_COMMAND"])
    interval = next(int(a.split("=")[1]) for a in argv if a.startswith("ServerAliveInterval="))
    count = next(int(a.split("=")[1]) for a in argv if a.startswith("ServerAliveCountMax="))
    assert interval * count >= git_transport.DEFAULT_STALL_SECONDS


def test_stall_disabled_end_to_end_across_both_callers():
    """cli.main() installs the defaults and _run_sync() applies the user's
    [sync] values. Only composing the two real functions on one env proves that
    `stall_bytes_per_sec = 0` actually neutralises the policy; the sync-level
    tests stub this helper out.
    """
    from codesync import cli
    from codesync.config import SyncConfig

    env: dict[str, str] = {}
    git_transport.configure_github_ssh_over_443(env)
    git_transport.configure_http_stall_detection(env)                 # cli.main
    disabled = SyncConfig(stall_bytes_per_sec=0, stall_seconds=0)
    git_transport.configure_http_stall_detection(                     # _run_sync
        env,
        bytes_per_sec=disabled.stall_bytes_per_sec,
        seconds=disabled.stall_seconds,
    )

    entries = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert entries["http.lowSpeedLimit"] == "0"
    assert entries["http.lowSpeedTime"] == "0"
    # cli.main must actually reach for the helper this test composes.
    assert hasattr(cli, "configure_http_stall_detection")


def test_prewarmed_master_carries_the_keepalive(monkeypatch, short_tmp):
    """ServerAlive must land on the MASTER connection.

    The master owns the TCP socket; multiplexed slaves attach over the control
    socket and never run their own transport keepalive. Without these flags on
    the pre-warmed master, the SSH half of stall detection is inert in the
    default (ControlMaster on) configuration.
    """
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp,))
    env: dict[str, str] = {}
    state = git_transport.configure_ssh_command(env, multiplex_enabled=True)
    assert state.mux.enabled

    captured: list[list[str]] = []
    monkeypatch.setattr(
        proc, "run",
        lambda argv, **k: captured.append(argv) or subprocess.CompletedProcess(
            argv, 1, "", "Hi me! You've successfully authenticated"),
    )
    assert git_transport.prewarm_github_master(state.mux, timeout=proc.T_NET) is True

    argv = captured[0]
    interval = argv[argv.index("-o", argv.index("ssh")) :]
    assert any(a.startswith("ServerAliveInterval=") for a in argv), argv
    assert any(a.startswith("ServerAliveCountMax=") for a in argv), argv
    # And still on the port git actually dials.
    assert argv[argv.index("-p") + 1] == "443"


def test_keepalive_budget_matches_the_stall_window(monkeypatch, short_tmp):
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp,))
    state = git_transport.configure_ssh_command({}, multiplex_enabled=True)
    budget = state.mux.alive_interval * state.mux.alive_count_max
    assert budget >= git_transport.DEFAULT_STALL_SECONDS


def test_no_keepalive_on_state_when_stall_disabled(monkeypatch, short_tmp):
    monkeypatch.setattr(git_transport, "_control_dir_candidates", lambda: (short_tmp,))
    state = git_transport.configure_ssh_command({}, multiplex_enabled=True, stall_seconds=0)
    assert git_transport._server_alive_argv(state.mux) == []
