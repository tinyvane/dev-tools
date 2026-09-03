"""codesync — personal multi-machine Git and Codex context tool."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
import re

__repo_url__ = "https://github.com/tinyvane/dev-tools"

_source_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
if _source_pyproject.is_file():
    # PYTHONPATH/source checkouts must not inherit stale metadata from a different
    # installed copy; the strict sync gate needs the version of the code running.
    _match = re.search(
        r'(?m)^version\s*=\s*["\']([^"\']+)["\']',
        _source_pyproject.read_text(encoding="utf-8"),
    )
    __version__ = _match.group(1) if _match else "0.0.0+source"
else:
    try:
        __version__ = _pkg_version("codesync")
    except PackageNotFoundError:
        __version__ = "0.0.0+source"
