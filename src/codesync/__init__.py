"""codesync — personal multi-machine git/db sync tool."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

__repo_url__ = "https://github.com/tinyvane/dev-tools"

try:
    __version__ = _pkg_version("codesync")
except PackageNotFoundError:
    # Not installed (e.g. running from a source checkout without `pip install -e .`).
    __version__ = "0.0.0+source"
