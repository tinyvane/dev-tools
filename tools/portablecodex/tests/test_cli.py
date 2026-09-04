from __future__ import annotations

import pytest

from portablecodex import cli


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("portablecodex ")


def test_onboard_parser_has_explicit_noninteractive_controls():
    args = cli._build_parser().parse_args([
        "onboard", "--root", r"V:\CodexPortable",
        "--mode", "connect", "--execute",
    ])
    assert args.command == "onboard"
    assert args.mode == "connect"
    assert args.execute is True


@pytest.mark.parametrize("action", ["status", "doctor"])
def test_context_parser(action):
    args = cli._build_parser().parse_args([
        "context", action, "--sessions-dir", "C:/state/sessions", "--json",
    ])
    assert args.command == "context"
    assert args.context_command == action
    assert args.json is True


def test_configured_root_is_used_when_cli_root_is_omitted(monkeypatch):
    received = {}
    monkeypatch.setattr(
        cli.config, "load",
        lambda **_kwargs: cli.config.Config(portable_root=r"X:\Portable"),
    )

    def fake_run(action, **kwargs):
        received.update(action=action, **kwargs)
        return 0

    monkeypatch.setattr("portablecodex.portable.run_portable", fake_run)
    assert cli.main(["status"]) == 0
    assert received["root"] == r"X:\Portable"


def test_context_does_not_load_portable_configuration(monkeypatch):
    monkeypatch.setattr(
        cli.config, "load", lambda: pytest.fail("context must not load root config"),
    )
    monkeypatch.setattr("portablecodex.context_sync.run_context", lambda *a, **k: 0)
    assert cli.main(["context", "status"]) == 0
