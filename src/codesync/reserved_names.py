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
    # PowerShell double-quoted literals still expand $ and interpret backticks,
    # and it also closes them on the typographic quotes “ ” „ — all legal in
    # Windows file names, so each must be backtick-escaped too.
    value = value.replace("`", "``").replace("$", "`$")
    for quote in ('"', "\u201c", "\u201d", "\u201e"):
        value = value.replace(quote, "`" + quote)
    return f'Remove-Item -LiteralPath "{value}"'


def _is_link_or_reparse(st: os.stat_result) -> bool:
    # A Windows junction (mount-point reparse point) still lstat()s as S_IFDIR;
    # only st_file_attributes reveals it. Following one escapes the project.
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def is_plain_directory(path: Path) -> bool:
    """A real directory: not a symlink, junction or other reparse point."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not _is_link_or_reparse(st)


def _contents(directory: Path, *, best_effort: bool) -> Iterator[tuple[Path, bool]]:
    """Yield reserved entries and ordinary leaves; never follow directory links.

    Links and reparse points are yielded as leaves (never descended into), so
    only_reserved treats them as real content rather than as junk."""
    if not is_plain_directory(directory):
        if not best_effort:
            raise NotADirectoryError(str(directory))
        return
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            for entry in current.iterdir():
                try:
                    st = entry.lstat()
                except OSError:
                    if not best_effort:
                        raise
                    continue
                link = _is_link_or_reparse(st)
                is_dir = stat.S_ISDIR(st.st_mode) and not link
                if is_dir and entry.name == ".git":
                    continue
                # A link named `nul` is still a link: never offer to delete it.
                reserved = is_reserved(entry.name) and not link
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
