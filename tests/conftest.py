from __future__ import annotations

import copy

import pytest

try:
    from codesync import journal
except ImportError:  # pragma: no cover - only before the journal module exists
    journal = None


class MemoryJournalBackend:
    """In-memory stand-in for the GitHub-backed deletion journal.

    Every test gets a fresh, empty journal so no test can reach the real
    `gh api` through sync/delete/trash code paths.
    """

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], dict] = {}
        self.unavailable = False
        self.calls: list[tuple[str, str]] = []

    def _check(self, op: str, arg: str) -> None:
        self.calls.append((op, arg))
        if self.unavailable:
            raise journal.JournalUnavailable("simulated journal outage")

    def put(self, owner: str, path: str, payload: dict) -> None:
        self.files[(owner.casefold(), path)] = copy.deepcopy(payload)

    def paths(self, owner: str = "me") -> list[str]:
        return sorted(p for o, p in self.files if o == owner.casefold())

    def reads(self) -> list[str]:
        return [arg for op, arg in self.calls if op == "read"]

    def list_paths(self, owner: str) -> list[str]:
        self._check("list", owner)
        return self.paths(owner)

    def read_json(self, owner: str, path: str) -> dict:
        self._check("read", path)
        try:
            return copy.deepcopy(self.files[(owner.casefold(), path)])
        except KeyError:
            raise journal.JournalUnavailable(f"missing {path}") from None

    def create_json(self, owner: str, path: str, payload: dict, message: str) -> bool:
        self._check("create", path)
        key = (owner.casefold(), path)
        if key in self.files:
            return False
        self.files[key] = copy.deepcopy(payload)
        return True

    def delete_path(self, owner: str, path: str, message: str) -> None:
        self._check("delete", path)
        self.files.pop((owner.casefold(), path), None)


@pytest.fixture(autouse=True)
def journal_backend(monkeypatch):
    if journal is None:
        yield None
        return
    backend = MemoryJournalBackend()
    monkeypatch.setattr(journal, "backend", backend)
    yield backend
