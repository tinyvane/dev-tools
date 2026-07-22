"""Tests for codesync's process-scoped GitHub SSH-over-443 routing."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codesync.git_transport import configure_github_ssh_over_443


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
