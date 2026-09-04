from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    return Path.home() / ".config" / "portablecodex"


def config_file() -> Path:
    return config_dir() / "config.toml"


def legacy_codesync_config_file() -> Path:
    return Path.home() / ".config" / "codesync" / "config.toml"


def expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))
