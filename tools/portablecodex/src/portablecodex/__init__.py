"""PortableCodex — guided Codex portable workspace management."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
import re

__repo_url__ = "https://github.com/tinyvane/dev-tools"

_source_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
if _source_pyproject.is_file():
    _match = re.search(
        r'(?m)^version\s*=\s*["\']([^"\']+)["\']',
        _source_pyproject.read_text(encoding="utf-8"),
    )
    __version__ = _match.group(1) if _match else "0.0.0+source"
else:
    try:
        __version__ = _pkg_version("portablecodex")
    except PackageNotFoundError:
        __version__ = "0.0.0+source"
