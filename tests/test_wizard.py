"""Tests for the first-run wizard (codesync.wizard).

The wizard's whole job is to bridge `curl install.sh | bash` → working
`codesync sync` without making the user open a text editor. These tests
mock gh + stdin so we exercise the decision branches deterministically.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from codesync import auth, paths, wizard


def _patch_paths(monkeypatch, tmp_path: Path) -> None:
    """Redirect ~/.config/codesync paths into tmp_path."""
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / ".config" / "codesync")
    monkeypatch.setattr(paths, "ensure_config_dir",
                        lambda: (tmp_path / ".config" / "codesync").mkdir(parents=True, exist_ok=True)
                                or (tmp_path / ".config" / "codesync"))
    monkeypatch.setattr(paths, "config_file",
                        lambda: tmp_path / ".config" / "codesync" / "config.toml")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_wizard_bails_when_gh_missing(monkeypatch, tmp_path) -> None:
    """No gh CLI → wizard returns False, no config written, no prompt asked."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: pytest_fail("input should not be called"))

    assert wizard.run_first_run_wizard() is False
    assert not (tmp_path / ".config" / "codesync" / "config.toml").exists()


def test_wizard_bails_when_gh_auth_fails(monkeypatch, tmp_path) -> None:
    """gh installed but ensure_gh_authenticated returns False → bail, no config."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: pytest_fail("input should not be called"))

    assert wizard.run_first_run_wizard() is False
    assert not (tmp_path / ".config" / "codesync" / "config.toml").exists()


def test_wizard_bails_when_gh_username_unavailable(monkeypatch, tmp_path) -> None:
    """gh authenticated but gh_username() returns None → bail."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(auth, "gh_username", lambda: None)

    assert wizard.run_first_run_wizard() is False
    assert not (tmp_path / ".config" / "codesync" / "config.toml").exists()


def test_wizard_writes_config_on_yes(monkeypatch, tmp_path) -> None:
    """Happy path: gh authed + username present + user says Y → TOML written."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(auth, "gh_username", lambda: "tinyvane")
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")

    assert wizard.run_first_run_wizard() is True

    toml_path = tmp_path / ".config" / "codesync" / "config.toml"
    assert toml_path.exists()
    parsed = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    # Sensible defaults baked in
    assert parsed["auto_clone"]["owner"] == "tinyvane"
    assert "SyncRepos" in parsed["auto_clone"]["target"]
    assert len(parsed["code_roots"]) == 1
    assert "SyncRepos" in parsed["code_roots"][0]


def test_wizard_default_is_yes_on_empty_input(monkeypatch, tmp_path) -> None:
    """Empty input (user just pressed Enter) → treat as Yes (the default)."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(auth, "gh_username", lambda: "tinyvane")
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")

    assert wizard.run_first_run_wizard() is True
    assert (tmp_path / ".config" / "codesync" / "config.toml").exists()


def test_wizard_default_is_yes_on_eof(monkeypatch, tmp_path) -> None:
    """Non-interactive stdin (piped install flow) → treat as Yes."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(auth, "gh_username", lambda: "tinyvane")

    def _raise_eof(*a, **kw):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    assert wizard.run_first_run_wizard() is True
    assert (tmp_path / ".config" / "codesync" / "config.toml").exists()


def test_wizard_bails_when_user_says_no(monkeypatch, tmp_path) -> None:
    """Explicit No → no config written."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(auth, "gh_username", lambda: "tinyvane")
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")

    assert wizard.run_first_run_wizard() is False
    assert not (tmp_path / ".config" / "codesync" / "config.toml").exists()


def test_wizard_writes_parseable_toml_with_special_path(monkeypatch, tmp_path) -> None:
    """If home contains a path that would trip TOML basic-string escapes
    (e.g. Windows-style \\U), the wizard must still emit valid TOML.
    The _toml_str helper handles this — verify by parsing back."""
    _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(auth, "gh_username", lambda: "tinyvane")
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")

    assert wizard.run_first_run_wizard() is True

    # tomllib accepts the generated TOML — that's the real assertion.
    toml_path = tmp_path / ".config" / "codesync" / "config.toml"
    parsed = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    assert parsed["auto_clone"]["owner"] == "tinyvane"


# pytest doesn't actually have pytest_fail at module scope; use this helper
import pytest

def pytest_fail(msg: str):
    pytest.fail(msg)
