"""Offline tests for the codesync-managed GitHub SSH-443 known_hosts file."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
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


def test_cache_is_used_without_network_when_there_is_nothing_to_derive_from(
    monkeypatch, _isolated_known_hosts,
):
    """The cache is the FALLBACK, not the winner.

    Previously this asserted the cache must avoid derivation entirely — which
    is the bug: cache-first pins a stale key forever after a GitHub host-key
    rotation. Derivation now always runs (one local file read); the contract
    that survives is that this path must never touch the network.
    """
    _user_file, cache = _isolated_known_hosts
    cache.parent.mkdir(parents=True)
    cache.write_text(
        "[ssh.github.com]:443 ssh-ed25519 CACHED\n",
        encoding="utf-8",
        errors="replace",
    )
    monkeypatch.setattr(
        known_hosts,
        "_fetch_github_meta_ssh_keys",
        lambda: pytest.fail("cache fallback must avoid network"),
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.enabled is True
    assert state.source == "cached"


def test_derivation_beats_a_stale_cache_so_rotation_self_heals(
    monkeypatch, _isolated_known_hosts,
):
    """GitHub rotated its host keys in March 2023.

    With the cache consulted first, the old key was pinned forever: every pull
    failed "Host key verification failed" and the only escape was deleting the
    cache by hand — which nothing told the user to do. CLAUDE.md has always
    required derive-first; the code did the opposite from the commit that
    introduced both.
    """
    user_file, cache = _isolated_known_hosts
    cache.parent.mkdir(parents=True)
    cache.write_text(
        "[ssh.github.com]:443 ssh-ed25519 OLDKEY\n", encoding="utf-8",
    )
    _write_user_file(user_file, "github.com ssh-ed25519 ROTATEDKEY\n")
    monkeypatch.setattr(
        known_hosts,
        "_fetch_github_meta_ssh_keys",
        lambda: pytest.fail("derivation must not need the network"),
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.source == "derived"
    assert "ROTATEDKEY" in cache.read_text(encoding="utf-8")
    assert "OLDKEY" not in cache.read_text(encoding="utf-8")


def test_unrefreshable_stale_cache_keeps_working_rather_than_disabling_trust(
    monkeypatch, _isolated_known_hosts,
):
    """A failed REFRESH must never downgrade a working setup.

    Disabling here would turn a possibly-stale-but-working key into blanket
    "Host key verification failed" on every repo — strictly worse.
    """
    import os as _os

    _user_file, cache = _isolated_known_hosts
    cache.parent.mkdir(parents=True)
    cache.write_text("[ssh.github.com]:443 ssh-ed25519 CACHED\n", encoding="utf-8")
    stale = time.time() - known_hosts._CACHE_TTL_SEC - 1
    _os.utime(cache, (stale, stale))
    monkeypatch.setattr(
        known_hosts, "_fetch_github_meta_ssh_keys",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.enabled is True
    assert state.source == "cached-stale"
    assert str(cache) in state.reason  # names the escape hatch


def test_identical_derivation_refreshes_mtime_without_rewriting(
    _isolated_known_hosts,
):
    """Content unchanged → don't rewrite, but DO re-stamp mtime.

    Without the utime the TTL would never reset and every run would re-probe.
    """
    import os as _os

    user_file, cache = _isolated_known_hosts
    _write_user_file(user_file, "github.com ssh-ed25519 SAMEKEY\n")
    known_hosts.ensure_github_443_known_hosts()
    first_mtime = cache.stat().st_mtime
    old = first_mtime - 10_000
    _os.utime(cache, (old, old))

    known_hosts.ensure_github_443_known_hosts()

    assert cache.stat().st_mtime > old


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


# ---------- negative cache for the HTTPS metadata probe ----------
#
# Nothing was written on the failure path, so a blocked network re-paid the
# full HTTPS timeout on every invocation, forever — worst on exactly the
# GFW-hampered networks the 443 routing exists to serve.

def test_failed_probe_is_not_retried_until_the_ttl_expires(monkeypatch):
    attempts: list[int] = []

    def failing():
        attempts.append(1)
        raise OSError("network blocked")

    monkeypatch.setattr(known_hosts, "_fetch_github_meta_ssh_keys", failing)

    first = known_hosts.ensure_github_443_known_hosts()
    second = known_hosts.ensure_github_443_known_hosts()
    third = known_hosts.ensure_github_443_known_hosts()

    assert len(attempts) == 1
    # Suppression must degrade EXACTLY like a fresh failure — never grant trust.
    assert first.enabled is False
    assert (second.enabled, second.path) == (first.enabled, first.path)
    assert third == second


def test_expired_marker_allows_one_more_probe(monkeypatch):
    attempts: list[int] = []
    monkeypatch.setattr(
        known_hosts, "_fetch_github_meta_ssh_keys",
        lambda: attempts.append(1) or (_ for _ in ()).throw(OSError("blocked")),
    )
    known_hosts.ensure_github_443_known_hosts()
    marker = known_hosts.paths.known_hosts_probe_file()
    marker.write_text(
        '{"failed_at": %f}' % (time.time() - known_hosts._META_RETRY_AFTER_SEC - 1),
        encoding="utf-8",
    )

    known_hosts.ensure_github_443_known_hosts()

    assert len(attempts) == 2


def test_corrupt_marker_never_permanently_blocks_the_probe(monkeypatch):
    """A damaged cache must not silently disable a trust source forever."""
    marker = known_hosts.paths.known_hosts_probe_file()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("not json at all", encoding="utf-8")

    assert known_hosts._meta_fetch_allowed() is True


def test_successful_probe_clears_the_marker(monkeypatch):
    marker = known_hosts.paths.known_hosts_probe_file()
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Keep clear of Windows' clock-resolution/float-formatting boundary: with
    # an exact current timestamp and a zero TTL, rounding could make failed_at
    # a fraction of a microsecond newer than the subsequent time.time().
    marker.write_text('{"failed_at": %f}' % (time.time() - 1), encoding="utf-8")
    # Pretend the TTL already lapsed so the probe is allowed to run.
    monkeypatch.setattr(known_hosts, "_META_RETRY_AFTER_SEC", 0)
    monkeypatch.setattr(
        known_hosts, "_fetch_github_meta_ssh_keys",
        lambda: [("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")],
    )

    state = known_hosts.ensure_github_443_known_hosts()

    assert state.enabled is True
    assert state.source == "meta"
    assert not marker.exists()
