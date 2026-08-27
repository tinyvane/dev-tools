from __future__ import annotations

import pytest

from codesync import followups


@pytest.fixture(autouse=True)
def _empty_collector():
    followups.clear()
    yield
    followups.clear()


def test_add_deduplicates_by_kind_and_title():
    followups.add("one", "first", ["cmd-1"], "kind-a")
    followups.add("one", "second", ["cmd-2"], "kind-a")
    followups.add("one", "third", ["cmd-3"], "kind-b")

    assert followups.drain() == [
        followups.Followup("one", "first", ("cmd-1",), "kind-a"),
        followups.Followup("one", "third", ("cmd-3",), "kind-b"),
    ]


def test_drain_returns_and_clears():
    followups.add("todo", "why", ["do-it"], "manual")

    assert len(followups.drain()) == 1
    assert followups.drain() == []


def test_same_title_in_different_repo_paths_is_not_deduplicated():
    followups.add(
        "repo 与远端分叉", "root one", ["cmd-one"], "diverged",
        identity="/root-one/repo",
    )
    followups.add(
        "repo 与远端分叉", "root two", ["cmd-two"], "diverged",
        identity="/root-two/repo",
    )

    pending = followups.drain()
    assert [item.detail for item in pending] == ["root one", "root two"]


def test_clear_discards_pending_items():
    followups.add("todo", "why", [], "manual")
    followups.clear()
    assert followups.drain() == []


def test_print_followups_is_silent_when_empty(capsys):
    followups.print_followups()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_print_followups_prints_commands_and_drains(capsys):
    followups.add("todo", "line one\nline two", ["do-it"], "manual")

    followups.print_followups()

    out = capsys.readouterr().out
    assert "需要你处理的事项" in out
    assert "todo" in out
    assert "line one" in out and "line two" in out
    assert "$ do-it" in out
    assert "共 1 项待处理" in out
    assert followups.drain() == []
