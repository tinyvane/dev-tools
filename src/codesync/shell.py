from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(args: list[str], cwd: Path | None = None, env: dict | None = None,
        capture: bool = False, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess. Cross-platform safe — no shell, no cmd /c, no bash -c."""
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=capture,
        text=True,
        check=check,
    )


def ensure_gita() -> bool:
    """Install gita via pip --user if missing. Returns True if available afterwards."""
    if have("gita"):
        return True

    # Try install
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "gita"],
        text=True,
    )
    if r.returncode != 0:
        return False

    return have("gita")
