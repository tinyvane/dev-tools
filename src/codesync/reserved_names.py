"""Windows device-name detection and manual cleanup commands (no file mutation)."""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path, PurePath


_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def applies() -> bool:
    return os.name == "nt"


def is_reserved(name: str) -> bool:
    return name.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED


def delete_command(path: PurePath | str) -> str:
    value = str(path)
    if not value.startswith("\\\\?\\"):
        value = ("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\")
                 else "\\\\?\\" + value)
    # PowerShell double-quoted literals still expand $ and interpret backticks.
    value = value.replace("`", "``").replace("$", "`$").replace('"', '`"')
    return f'Remove-Item -LiteralPath "{value}"'


def _contents(directory: Path, *, best_effort: bool) -> Iterator[tuple[Path, bool]]:
    """Yield reserved entries and ordinary leaves; never follow directory links."""
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            for entry in current.iterdir():
                try:
                    is_dir = stat.S_ISDIR(entry.lstat().st_mode)
                except OSError:
                    if not best_effort:
                        raise
                    continue
                if is_dir and entry.name == ".git":
                    continue
                reserved = is_reserved(entry.name)
                if reserved or not is_dir:
                    yield entry, reserved
                else:
                    pending.append(entry)
        except OSError:
            if not best_effort:
                raise


def find_reserved(directory: Path) -> list[Path]:
    return [path for path, reserved in _contents(directory, best_effort=True) if reserved]


def only_reserved(directory: Path) -> bool:
    found = False
    try:
        for _, reserved in _contents(directory, best_effort=False):
            if not reserved:
                return False
            found = True
    except OSError:
        return False
    return found
