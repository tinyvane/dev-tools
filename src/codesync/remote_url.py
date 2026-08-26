"""Remote URL parsing with no dependencies on the rest of codesync."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_GITHUB_HOSTS = frozenset({"github.com", "ssh.github.com"})
_URL_SCHEMES = frozenset({"http", "https", "ssh", "git"})
_SCP_REMOTE_RE = re.compile(
    r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>[^?#]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GitHubRemote:
    owner: str
    name: str


@dataclass(frozen=True)
class RemoteLocation:
    host: str
    owner: str
    name: str


def _owner_name(path: str) -> tuple[str, str] | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, name = parts
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not owner or not name or owner in {".", ".."} or name in {".", ".."}:
        return None
    return owner, name


def parse_remote_location(url: str) -> RemoteLocation | None:
    """Parse an ordinary two-component Git remote into host/owner/name."""
    value = url.strip().rstrip("/")
    if not value:
        return None

    parsed = urlsplit(value)
    if parsed.scheme.lower() in _URL_SCHEMES and parsed.hostname:
        pair = _owner_name(parsed.path)
        if pair is None:
            return None
        return RemoteLocation(parsed.hostname.lower(), *pair)

    match = _SCP_REMOTE_RE.fullmatch(value)
    if match is None:
        return None
    pair = _owner_name(match.group("path"))
    if pair is None:
        return None
    return RemoteLocation(match.group("host").lower(), *pair)


def parse_github_remote(url: str) -> GitHubRemote | None:
    """Parse supported direct and ghproxy-prefixed GitHub remote URLs."""
    location = parse_remote_location(url)
    if location is not None and location.host in _GITHUB_HOSTS:
        return GitHubRemote(location.owner, location.name)

    # ghproxy-style URLs carry the complete real URL at the start of an outer
    # HTTP URL's path. Do not search arbitrary substrings: the embedded URL must
    # itself parse to one of GitHub's two exact hosts.
    outer = urlsplit(url.strip())
    if outer.scheme.lower() not in {"http", "https"} or not outer.hostname:
        return None
    embedded_url = outer.path.lstrip("/")
    if not embedded_url.lower().startswith(("http://", "https://", "ssh://", "git://")):
        return None
    embedded = parse_remote_location(embedded_url.rstrip("/"))
    if embedded is None or embedded.host not in _GITHUB_HOSTS:
        return None
    return GitHubRemote(embedded.owner, embedded.name)


def normalize(url: str) -> str:
    """Return a stable identity key for duplicate-origin comparisons."""
    github = parse_github_remote(url)
    if github is not None:
        return f"github.com/{github.owner.lower()}/{github.name.lower()}"
    value = url.strip().rstrip("/").lower()
    return value[:-4] if value.endswith(".git") else value
