"""Process-local collection of actionable work left for the user."""
from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from codesync import output


@dataclass(frozen=True)
class Followup:
    title: str
    detail: str
    commands: tuple[str, ...]
    kind: str


_items: list[Followup] = []
_keys: set[tuple[str, str]] = set()
_lock = threading.Lock()


def add(
    title: str,
    detail: str,
    commands: Sequence[str],
    kind: str,
    *,
    identity: str | None = None,
) -> None:
    """Add the first item for a logical identity, falling back to its title."""
    key = (kind, identity or title)
    item = Followup(title, detail, tuple(commands), kind)
    with _lock:
        if key in _keys:
            return
        _keys.add(key)
        _items.append(item)


def drain() -> list[Followup]:
    """Return every pending item and atomically clear the collector."""
    with _lock:
        pending = list(_items)
        _items.clear()
        _keys.clear()
    return pending


def clear() -> None:
    """Discard every pending item."""
    with _lock:
        _items.clear()
        _keys.clear()


def print_followups() -> None:
    """Print pending work grouped by kind; stay completely silent when empty."""
    pending = drain()
    if not pending:
        return

    grouped: dict[str, list[Followup]] = {}
    for item in pending:
        grouped.setdefault(item.kind, []).append(item)

    output.section("需要你处理的事项")
    for items in grouped.values():
        for item in items:
            output.warn(item.title)
            for line in item.detail.splitlines():
                output.detail(f"  {line}")
            for command in item.commands:
                output.info(f"    {output.hilite(f'$ {command}', 'cyan')}")
    output.info(f"  共 {len(pending)} 项待处理。")
