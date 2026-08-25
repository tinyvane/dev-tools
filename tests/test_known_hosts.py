"""Offline tests for the codesync-managed GitHub SSH-443 known_hosts file."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

import pytest

from codesync import known_hosts


@pytest.fixture(autouse=True)
def _isolated_known_hosts(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    user_file = tmp_path / ".ssh" / "known_hosts"
    monkeypatch.setattr(known_hosts.paths, "config_dir", lambda: config_dir)
    monkeypatch.setattr(known_hosts, "_user_known_hosts_path", lambda: user_file)
    monkeypatch.setattr(known_hosts, "_fetch_github_meta_ssh_keys", lambda: [])
    return user_file, config_dir / "known_hosts"


def _write_user_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", errors="replace")


def test_derives_plain_github_entry(_isolated_known_hosts):
    user_file, cache = _isolated_known_hosts
    _write_user_file(
        user_file,
        "github.com,1.2.3.4 ssh-ed25519 AAA\n"
        "gitlab.com ssh-ed25519 NOT-GITHUB\n",
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.enabled is True
    assert state.source == "derived"
    assert cache.read_text(encoding="utf-8", errors="replace") == (
        "[ssh.github.com]:443 ssh-ed25519 AAA\n"
    )


def test_derives_hashed_github_entry(_isolated_known_hosts):
    user_file, cache = _isolated_known_hosts
    salt = b"real-test-salt"
    digest = hmac.new(salt, b"github.com", hashlib.sha1).digest()
    hashed = "|1|{}|{}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )
    _write_user_file(user_file, f"{hashed} ssh-ed25519 HASHED-KEY\n")

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.source == "derived"
    assert "ssh-ed25519 HASHED-KEY" in cache.read_text(
        encoding="utf-8", errors="replace",
    )


def test_revoked_entry_is_never_copied(_isolated_known_hosts):
    user_file, cache = _isolated_known_hosts
    _write_user_file(
        user_file,
        "@revoked github.com ssh-ed25519 REVOKED\n"
        "github.com ssh-ed25519 TRUSTED\n",
    )

    known_hosts.ensure_github_443_known_hosts()

    contents = cache.read_text(encoding="utf-8", errors="replace")
    assert "TRUSTED" in contents
    assert "REVOKED" not in contents


def test_cert_authority_entry_is_not_copied(_isolated_known_hosts):
    user_file, cache = _isolated_known_hosts
    _write_user_file(
        user_file,
        "@cert-authority github.com ssh-ed25519 CA-KEY\n"
        "github.com ecdsa-sha2-nistp256 HOST-KEY\n",
    )

    known_hosts.ensure_github_443_known_hosts()

    contents = cache.read_text(encoding="utf-8", errors="replace")
    assert "HOST-KEY" in contents
    assert "CA-KEY" not in contents


def test_non_github_entries_never_appear_in_output(_isolated_known_hosts):
    user_file, cache = _isolated_known_hosts
    _write_user_file(
        user_file,
        "example.com ssh-ed25519 EXAMPLE\n"
        "github.com ssh-ed25519 GITHUB\n",
    )

    known_hosts.ensure_github_443_known_hosts()

    contents = cache.read_text(encoding="utf-8", errors="replace")
    assert "GITHUB" in contents
    assert "EXAMPLE" not in contents
    assert all(
        line.startswith("[ssh.github.com]:443 ")
        for line in contents.splitlines()
    )


def test_meta_is_used_when_user_has_no_trusted_entry(
    monkeypatch, _isolated_known_hosts,
):
    _user_file, cache = _isolated_known_hosts
    monkeypatch.setattr(
        known_hosts,
        "_fetch_github_meta_ssh_keys",
        lambda: ["ssh-ed25519 META-ED", "ecdsa-sha2-nistp256 META-ECDSA"],
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.source == "meta"
    assert cache.read_text(encoding="utf-8", errors="replace").splitlines() == [
        "[ssh.github.com]:443 ssh-ed25519 META-ED",
        "[ssh.github.com]:443 ecdsa-sha2-nistp256 META-ECDSA",
    ]


def test_valid_cache_wins_without_derivation_or_network(
    monkeypatch, _isolated_known_hosts,
):
    _user_file, cache = _isolated_known_hosts
    cache.parent.mkdir(parents=True)
    cache.write_text(
        "[ssh.github.com]:443 ssh-ed25519 CACHED\n",
        encoding="utf-8",
        errors="replace",
    )
    monkeypatch.setattr(
        known_hosts,
        "_derive_from_user_known_hosts",
        lambda: pytest.fail("cache must avoid derivation"),
    )
    monkeypatch.setattr(
        known_hosts,
        "_fetch_github_meta_ssh_keys",
        lambda: pytest.fail("cache must avoid network"),
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.enabled is True
    assert state.source == "cached"


def test_total_failure_is_explicit_and_keeps_strict_checking(
    _isolated_known_hosts,
):
    state = known_hosts.ensure_github_443_known_hosts()

    assert state.enabled is False
    assert "ssh-keyscan -p 443 ssh.github.com" in state.reason
    assert state.path == ""


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod has no POSIX mode bits")
def test_generated_file_is_mode_0600(_isolated_known_hosts):
    user_file, cache = _isolated_known_hosts
    _write_user_file(user_file, "github.com ssh-ed25519 MODE\n")

    known_hosts.ensure_github_443_known_hosts()

    assert cache.stat().st_mode & 0o777 == 0o600


def test_wildcard_entry_is_never_treated_as_a_github_key():
    """A `*` entry must not be copied in as a trusted ssh.github.com:443 key.

    ssh accepts a connection whose key matches ANY listed entry, so importing a
    wildcard key would let whoever holds it MITM the 443 endpoint.
    """
    lines = ["* ssh-ed25519 ATTACKER", "github.com ssh-rsa REAL"]
    assert known_hosts._github_keys_from_lines(lines) == [("ssh-rsa", "REAL")]


def test_lookalike_hostnames_are_not_matched():
    lines = [
        "notgithub.com ssh-rsa EVIL",
        "github.com.evil.tld ssh-rsa EVIL2",
        "gith?b.com ssh-rsa EVIL3",
    ]
    assert known_hosts._github_keys_from_lines(lines) == []


def test_comma_list_and_case_insensitivity_still_match():
    assert known_hosts._github_keys_from_lines(
        ["GitHub.COM,140.82.121.4 ssh-rsa REAL"]
    ) == [("ssh-rsa", "REAL")]
