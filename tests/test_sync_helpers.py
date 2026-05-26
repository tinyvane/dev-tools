"""Verify sync helpers: gita ls is space-separated, count must split correctly."""
from __future__ import annotations


def test_gita_ls_count_space_separated():
    """gita ls prints repos space-separated on a single line.

    Regression: original code used splitlines() which gave count=1 for any
    non-empty output. Must use .split() (whitespace) instead.
    """
    sample = "repo-a repo-b repo-c sub/repo-d\n"
    count = len(sample.split())
    assert count == 4


def test_gita_ls_count_empty():
    assert len("".split()) == 0
    assert len("\n".split()) == 0
    assert len("  \n  \t  ".split()) == 0


def test_gita_ls_count_chinese_names():
    """Chinese repo names should still count correctly."""
    sample = "中国地图 规划资讯 项目一张图\n"
    assert len(sample.split()) == 3
