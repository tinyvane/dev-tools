from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    """~/.config/codesync — same layout on all platforms."""
    return Path.home() / ".config" / "codesync"


def ensure_config_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return config_dir() / "config.toml"


def known_repos_file() -> Path:
    return config_dir() / "known-repos.json"


def db_sync_state_file() -> Path:
    return config_dir() / "db-sync-state.json"


def db_sync_backup_dir() -> Path:
    d = config_dir() / "db-sync-backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def update_log_file() -> Path:
    return config_dir() / "update.log"


def version_check_file() -> Path:
    """Cache for the once-per-TTL "latest version" lookup (v2.7.0)."""
    return config_dir() / "version-check.json"


def expand(p: str) -> str:
    """Expand ~, $VAR, %VAR% in a path string. Idempotent on already-absolute paths."""
    s = os.path.expandvars(p)
    s = os.path.expanduser(s)
    return s
