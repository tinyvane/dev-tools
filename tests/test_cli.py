"""CLI parser surface tests — verify arg routing without executing side effects."""
from __future__ import annotations

import pytest

from codesync import cli
from codesync.cli import _build_parser


@pytest.fixture
def parser():
    return _build_parser()


def test_version_flag(parser):
    # --version is now a store_true handled in main() (it shows latest-version
    # status), not argparse's exit-on-print action.
    ns = parser.parse_args(["--version"])
    assert ns.version is True


def test_update_force_flag(parser):
    ns = parser.parse_args(["--update", "--force"])
    assert ns.update is True and ns.force is True


def test_no_args(parser):
    ns = parser.parse_args([])
    assert ns.command is None
    assert ns.update is False


def test_update_long(parser):
    ns = parser.parse_args(["--update"])
    assert ns.update is True


def test_update_short(parser):
    ns = parser.parse_args(["-U"])
    assert ns.update is True


def test_sync_no_flags(parser):
    ns = parser.parse_args(["sync"])
    assert ns.command == "sync"
    assert ns.push is False
    assert ns.status is False


def test_sync_push(parser):
    ns = parser.parse_args(["sync", "--push"])
    assert ns.command == "sync"
    assert ns.push is True


def test_sync_status(parser):
    ns = parser.parse_args(["sync", "--status"])
    assert ns.command == "sync"
    assert ns.status is True


def test_sync_worker_overrides(parser):
    ns = parser.parse_args(["sync", "--workers", "3", "--local-workers", "12"])
    assert ns.workers == 3
    assert ns.local_workers == 12


@pytest.mark.parametrize("flag", ["--workers", "--local-workers"])
@pytest.mark.parametrize("value", ["0", "-1", "nope"])
def test_sync_worker_overrides_must_be_positive(parser, flag, value):
    with pytest.raises(SystemExit):
        parser.parse_args(["sync", flag, value])


def test_sync_version_gate_has_no_bypass(parser):
    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "--skip-version-check"])


def test_trash_subcommands(parser):
    listed = parser.parse_args(["trash", "list"])
    restored = parser.parse_args(["trash", "restore", "foo"])
    purged = parser.parse_args(["trash", "purge", "foo", "-y"])
    assert listed.trash_command == "list"
    assert restored.name == "foo"
    assert purged.name == "foo" and purged.yes is True


def test_migrate_config(parser):
    ns = parser.parse_args(["migrate-config"])
    assert ns.command == "migrate-config"


def test_config_path(parser):
    ns = parser.parse_args(["config-path"])
    assert ns.command == "config-path"


def test_rename_one_name(parser):
    ns = parser.parse_args(["rename", "new-name"])
    assert ns.command == "rename"
    assert ns.names == ["new-name"]


def test_rename_two_names(parser):
    ns = parser.parse_args(["rename", "old", "new"])
    assert ns.command == "rename"
    assert ns.names == ["old", "new"]


def test_rename_requires_a_name(parser):
    with pytest.raises(SystemExit):
        parser.parse_args(["rename"])


def test_unknown_command_errors(parser):
    with pytest.raises(SystemExit):
        parser.parse_args(["bogus"])


def test_main_installs_http_stall_defaults_for_every_subcommand(monkeypatch):
    """delete/rename push over the network too. With T_NET_LONG at an hour, a
    dead HTTPS connection on those paths would otherwise hang for the full hour."""
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "configure_http_stall_detection",
        lambda *a, **k: calls.append("stall"),
    )
    monkeypatch.setattr(cli, "configure_github_ssh_over_443", lambda *a, **k: None)
    monkeypatch.setattr(cli, "configure_ssh_command", lambda *a, **k: None)

    cli.main(["config-path"])

    assert calls == ["stall"]


# ---------- SSH setup must be gated on the subcommand ----------
#
# configure_ssh_command can reach the network (the GitHub host-key metadata
# probe). It used to run before argparse for EVERY invocation, so `--version`,
# `config-path` and even `--help` blocked on it. These pin the gate.

def _ssh_calls(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        cli, "configure_ssh_command", lambda **kw: calls.append(kw),
    )
    return calls


@pytest.mark.parametrize("argv", [["--version"], ["config-path"], []])
def test_local_only_commands_never_configure_ssh(monkeypatch, argv):
    calls = _ssh_calls(monkeypatch)
    monkeypatch.setattr(cli, "configure_github_ssh_over_443", lambda: None)
    monkeypatch.setattr(cli, "configure_http_stall_detection", lambda: None)
    monkeypatch.setattr(
        "codesync.updater.report_pending_update", lambda: None,
    )
    monkeypatch.setattr(
        "codesync.updater.print_version_cli", lambda: None,
    )
    cli.main(argv)
    assert calls == []


def test_trash_list_is_local_but_restore_is_not(parser):
    assert cli._needs_ssh(parser.parse_args(["trash", "list"])) is False
    assert cli._needs_ssh(parser.parse_args(["trash", "restore", "x"])) is True
    assert cli._needs_ssh(parser.parse_args(["trash", "purge", "x"])) is True


@pytest.mark.parametrize(
    "argv",
    [["sync"], ["init"], ["fork-setup"], ["rename", "a", "b"], ["delete", "x"]],
)
def test_network_commands_do_configure_ssh(parser, argv):
    assert cli._needs_ssh(parser.parse_args(argv)) is True


def test_update_does_not_configure_ssh(parser):
    """--update goes over HTTPS via pip; it never uses git SSH."""
    assert cli._needs_ssh(parser.parse_args(["--update"])) is False
