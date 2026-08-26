"""Process-local trust material for GitHub's SSH-over-443 endpoint."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from codesync import paths


GITHUB_443_HOST = "[ssh.github.com]:443"
_GITHUB_HOST = "github.com"
_META_URL = "https://api.github.com/meta"
_MANUAL_HINT = "ssh-keyscan -p 443 ssh.github.com >> ~/.ssh/known_hosts"
# api.github.com either answers a small JSON promptly or is being reset; a long
# wait buys nothing and is paid synchronously before the command runs.
_META_TIMEOUT_SEC = 5
# How long a FAILED probe suppresses the next one. Short on purpose: the free
# local derivation from ~/.ssh/known_hosts is attempted on every call
# regardless, so a user who fixes trust locally self-heals immediately and this
# marker never stands in the way. It only suppresses the network probe, and an
# hour lets someone who connects a VPN mid-session recover in that session.
_META_RETRY_AFTER_SEC = 3600


@dataclass(frozen=True)
class KnownHostsState:
    path: str
    source: str
    reason: str
    enabled: bool


def _cache_path() -> Path:
    return paths.config_dir() / "known_hosts"


def _user_known_hosts_path() -> Path:
    return Path.home() / ".ssh" / "known_hosts"


def _hashed_host_matches(pattern: str, host: str) -> bool:
    parts = pattern.split("|")
    if len(parts) != 4 or parts[0] or parts[1] != "1":
        return False
    try:
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except (binascii.Error, ValueError, TypeError):
        return False
    actual = hmac.new(salt, host.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(actual, expected)


def _plain_host_matches(patterns: str, host: str) -> bool:
    """Exact literal match against one comma-separated host pattern list.

    Deliberately NOT glob matching. A wildcard entry such as `*` in the user's
    known_hosts would otherwise match github.com and get copied in as a trusted
    key for ssh.github.com:443 — and ssh accepts a connection whose key matches
    ANY listed entry, so that would let a MITM holding the wildcard key through.
    GitHub's own entries are always literal, so exactness costs nothing.
    """
    return any(
        pattern.strip().lower() == host.lower()
        for pattern in patterns.split(",")
        if pattern.strip()
    )


def _host_field_matches(host_field: str, host: str) -> bool:
    if host_field.startswith("|1|"):
        return _hashed_host_matches(host_field, host)
    return _plain_host_matches(host_field, host)


def _github_keys_from_lines(lines: list[str]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if not fields:
            continue
        # Marked entries have semantics that cannot safely be copied to a plain
        # host-key entry. In particular, revoked keys must never regain trust.
        if fields[0] in {"@revoked", "@cert-authority"}:
            continue
        if fields[0].startswith("@") or len(fields) < 3:
            continue
        if not _host_field_matches(fields[0], _GITHUB_HOST):
            continue
        key = (fields[1], fields[2])
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _derive_from_user_known_hosts() -> list[tuple[str, str]]:
    path = _user_known_hosts_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _github_keys_from_lines(text.splitlines())


def _parse_key_strings(values: object) -> list[tuple[str, str]]:
    if not isinstance(values, list):
        return []
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        fields = value.split()
        if len(fields) < 2:
            continue
        key = (fields[0], fields[1])
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _normalise_keys(values: object) -> list[tuple[str, str]]:
    """Accept parsed pairs or raw ``ssh_keys`` strings (handy for stubs)."""
    if isinstance(values, list) and all(
        isinstance(item, tuple) and len(item) == 2 for item in values
    ):
        return list(dict.fromkeys(values))
    return _parse_key_strings(values)


def _meta_fetch_allowed() -> bool:
    """False while a recent probe failure is still suppressing the network.

    An unreadable or malformed marker means "no suppression recorded", so the
    probe runs — a corrupt cache must never permanently disable a trust source.
    """
    path = paths.known_hosts_probe_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        failed_at = float(raw["failed_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return True
    return (time.time() - failed_at) >= _META_RETRY_AFTER_SEC


def _record_meta_failure() -> None:
    path = paths.known_hosts_probe_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"failed_at": time.time()}), encoding="utf-8",
        )
    except OSError:
        pass  # best-effort; worst case we simply probe again next run


def _clear_meta_failure() -> None:
    try:
        paths.known_hosts_probe_file().unlink()
    except OSError:
        pass


def _fetch_github_meta_ssh_keys() -> list[tuple[str, str]]:
    """Fetch GitHub host keys over normal TLS PKI; certificate errors fail.

    Deliberately hits api.github.com directly and never the CODESYNC_GH_MIRROR
    proxies the updater uses: a mirror could serve any key it likes, and this is
    trust material, not a download. Unreachable (e.g. behind the GFW) simply
    means this source fails and we degrade.
    """
    request = urllib.request.Request(
        _META_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "codesync"},
    )
    with urllib.request.urlopen(request, timeout=_META_TIMEOUT_SEC) as response:
        payload = response.read().decode("utf-8", errors="replace")
    data = json.loads(payload)
    if not isinstance(data, dict):
        return []
    return _parse_key_strings(data.get("ssh_keys"))


def _cached_keys(path: Path) -> list[tuple[str, str]] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        # A codesync cache is deliberately single-host. Treat any foreign or
        # malformed content as damage instead of mounting it into ssh.
        if len(fields) < 3 or fields[0] != GITHUB_443_HOST:
            return None
        key = (fields[1], fields[2])
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys or None


def _write_cache(path: Path, keys: list[tuple[str, str]]) -> bool:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".known_hosts.", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as handle:
            for keytype, key in keys:
                handle.write(f"{GITHUB_443_HOST} {keytype} {key}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        return True
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        return False


def ensure_github_443_known_hosts() -> KnownHostsState:
    """Ensure a trusted, single-host cache for ``ssh.github.com:443``.

    A valid existing cache wins for idempotency. Otherwise keys are derived
    from the user's already-trusted github.com entry, then fetched from GitHub's
    HTTPS metadata endpoint. Every failure degrades without weakening ssh.
    """
    path = _cache_path()
    cached = _cached_keys(path)
    if cached:
        try:
            os.chmod(path, 0o600)
        except OSError:
            cached = None
        else:
            return KnownHostsState(str(path), "cached", "", True)

    derived = _derive_from_user_known_hosts()
    if derived and _write_cache(path, derived):
        return KnownHostsState(str(path), "derived", "", True)

    meta: list[tuple[str, str]] = []
    if _meta_fetch_allowed():
        try:
            meta = _normalise_keys(_fetch_github_meta_ssh_keys())
        except Exception:
            meta = []
        if meta:
            _clear_meta_failure()
        else:
            _record_meta_failure()
    if meta and _write_cache(path, meta):
        return KnownHostsState(str(path), "meta", "", True)

    reason = f"无法取得 GitHub 443 主机密钥；请手动执行：{_MANUAL_HINT}"
    return KnownHostsState("", "", reason, False)
