"""CLI parser surface tests — verify arg routing without executing side effects."""
from __future__ import annotations

import pytest

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
