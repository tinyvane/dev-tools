"""Verify __version__ is sourced from installed package metadata."""
from __future__ import annotations

import re

import codesync


def test_version_is_a_string():
    assert isinstance(codesync.__version__, str)
    assert len(codesync.__version__) > 0


def test_version_looks_like_semver_or_fallback():
    v = codesync.__version__
    # Either real SemVer (e.g. "2.0.0", "2.1.0a1") or the source-checkout fallback.
    assert re.match(r"^\d+\.\d+\.\d+", v) or v == "0.0.0+source", (
        f"unexpected version: {v!r}"
    )


def test_version_matches_pyproject_when_installed():
    """If pyproject.toml is reachable, __version__ should match its declared version.

    Skips silently when run from a wheel that has no source pyproject.toml nearby.
    """
    import tomllib
    from pathlib import Path

    # Walk up looking for pyproject.toml — works for `pip install -e .` and source checkouts.
    here = Path(__file__).resolve()
    pyproject = None
    for p in [here] + list(here.parents):
        candidate = p / "pyproject.toml"
        if candidate.exists():
            pyproject = candidate
            break

    if pyproject is None:
        import pytest
        pytest.skip("pyproject.toml not findable")

    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    if codesync.__version__ != "0.0.0+source":
        assert codesync.__version__ == declared, (
            f"installed metadata says {codesync.__version__!r} but "
            f"pyproject says {declared!r}"
        )
