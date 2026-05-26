"""Tests for status display: CJK width, status detection, label semantics."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codesync import status


# ---------- visual width ----------

@pytest.mark.parametrize("s,expected", [
    ("", 0),
    ("abc", 3),
    ("hello world", 11),
    ("中文", 4),
    ("中国地图飞线", 12),
    ("混合mixed", 9),         # 4 (CJK 2x) + 5 ascii
    ("规划资讯组项目一张图", 20),
    ("emoji 😀", 8),           # emoji is wide
])
def test_visual_width(s, expected):
    assert status.visual_width(s) == expected


def test_pad_visual_pads_short():
    out = status.pad_visual("ab", 10)
    assert out == "ab" + " " * 8


def test_pad_visual_no_op_when_already_wide():
    out = status.pad_visual("中国地图飞线", 8)  # already 12 wide
    assert out == "中国地图飞线"


def test_pad_visual_cjk_correct():
    """The bug we're fixing: padding a CJK string should reserve 2 cells per char."""
    out = status.pad_visual("中文", 10)
    assert status.visual_width(out) == 10


def test_truncate_visual_short_passthrough():
    assert status.truncate_visual("hi", 100) == "hi"


def test_truncate_visual_ascii():
    out = status.truncate_visual("abcdefghij", 5)
    assert status.visual_width(out) <= 5
    assert out.endswith("…")


def test_truncate_visual_cjk():
    """Don't cut mid-character; respect 2-cell width."""
    out = status.truncate_visual("中国地图飞线", 6)
    assert status.visual_width(out) <= 6
    assert out.endswith("…")


# ---------- RepoStatus label/color ----------

def _make(**kwargs):
    defaults = dict(
        name="r", branch="main", dirty=False, untracked=False,
        ahead=0, behind=0, no_upstream=False, stashed=False,
        last_subject="", last_relative="",
    )
    defaults.update(kwargs)
    return status.RepoStatus(**defaults)


def test_label_clean():
    s = _make()
    assert s.is_clean
    assert s.label == "clean"


def test_label_modified():
    s = _make(dirty=True)
    assert not s.is_clean
    assert s.label == "modified"


def test_label_untracked():
    s = _make(untracked=True)
    assert s.label == "untracked"


def test_label_mixed():
    s = _make(dirty=True, untracked=True)
    assert s.label == "mixed"


def test_label_ahead():
    assert _make(ahead=3).label == "ahead 3"


def test_label_behind():
    assert _make(behind=5).label == "behind 5"


def test_label_diverged_beats_ahead_and_behind():
    s = _make(ahead=2, behind=3)
    assert s.label == "diverged"


def test_label_behind_beats_modified():
    s = _make(dirty=True, behind=1)
    assert s.label == "behind 1"


def test_label_no_upstream():
    assert _make(no_upstream=True).label == "no upstream"


def test_label_error_wins():
    s = _make(dirty=True)
    s.error = "boom"
    assert s.label == "error"


# ---------- compute_status against real tiny repos ----------

def _git(repo: Path, *args: str):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init_repo_with_commit(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "tester")
    (repo / "file.txt").write_text("hello")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def test_compute_status_clean(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo_with_commit(repo)

    s = status.compute_status(repo)
    assert s.dirty is False
    assert s.untracked is False
    # No upstream means we can't compute ahead/behind; that's expected for a bare init.
    assert s.no_upstream is True
    assert s.last_subject == "init"


def test_compute_status_dirty(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo_with_commit(repo)
    (repo / "file.txt").write_text("changed")

    s = status.compute_status(repo)
    assert s.dirty is True
    assert s.untracked is False


def test_compute_status_untracked(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo_with_commit(repo)
    (repo / "new.txt").write_text("new")

    s = status.compute_status(repo)
    assert s.untracked is True
    assert s.dirty is False


def test_compute_status_both_dirty_and_untracked(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo_with_commit(repo)
    (repo / "file.txt").write_text("changed")
    (repo / "new.txt").write_text("new")

    s = status.compute_status(repo)
    assert s.dirty is True
    assert s.untracked is True
    assert s.label == "mixed"
