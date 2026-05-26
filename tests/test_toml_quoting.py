"""Verify _toml_str produces TOML-parseable output, especially for Windows paths."""
from __future__ import annotations

import tomllib

import pytest

from codesync.config import _toml_str


def _roundtrip(value: str) -> str:
    """Embed `value` as TOML, parse it back, return the parsed string."""
    toml_src = f"x = {_toml_str(value)}\n"
    parsed = tomllib.loads(toml_src)
    return parsed["x"]


@pytest.mark.parametrize("value", [
    "simple",
    "with spaces",
    "C:\\Users\\yiwang\\SyncRepos",
    "C:\\Users\\Username\\Documents",
    "/home/user/code",
    "~/SyncRepos",
    "$env:USERPROFILE\\foo",
    "value with \"double\" quotes",
    "value with mixed \" and \\ backslash",
    "tab\there",
    "newline\nhere",
])
def test_roundtrip(value: str) -> None:
    assert _roundtrip(value) == value, f"roundtrip failed for: {value!r}"


def test_single_quote_falls_back_to_basic() -> None:
    out = _toml_str("don't break")
    assert out.startswith('"') and out.endswith('"')
    assert _roundtrip("don't break") == "don't break"


def test_windows_path_uses_literal_string() -> None:
    """Critical: paths with \\U must not be interpreted as Unicode escape."""
    out = _toml_str(r"C:\Users\yiwang\SyncRepos")
    # Literal string: surrounded by single quotes, content unchanged.
    assert out == r"'C:\Users\yiwang\SyncRepos'"
