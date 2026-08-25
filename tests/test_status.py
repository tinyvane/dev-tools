"""Tests for status display: CJK width, status detection, label semantics."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codesync import status


@pytest.fixture(autouse=True)
def _reset_porcelain_v2_probe(monkeypatch):
    monkeypatch.setattr(status, "_PORCELAIN_V2_SHOW_STASH_SUPPORTED", None)


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


def test_compute_status_timeout_is_reported_as_error(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = status.compute_status(tmp_path)
    assert result.error == "timeout"


_PORCELAIN_V2_SAMPLE = """# branch.oid 0123456789abcdef
# branch.head main
# branch.upstream origin/main
# branch.ab +2 -3
# stash 1
1 .M N... 100644 100644 100644 abcdef abcdef tracked.txt
? new.txt
! ignored.txt
"""


def _completed(args, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, rc, stdout, stderr)


def test_compute_status_parses_real_porcelain_v2_shape(monkeypatch, tmp_path):
    def fake_run(repo, *args, timeout=10):
        if args[0] == "status":
            return _completed(args, stdout=_PORCELAIN_V2_SAMPLE)
        assert args == ("log", "-1", "--format=%s%x09%cr")
        return _completed(args, stdout="subject\t2 hours ago\n")

    monkeypatch.setattr(status, "_run", fake_run)
    result = status.compute_status(tmp_path / "repo")

    assert result.branch == "main"
    assert result.ahead == 2
    assert result.behind == 3
    assert result.no_upstream is False
    assert result.stashed is True
    assert result.dirty is True
    assert result.untracked is True
    assert result.last_subject == "subject"
    assert result.last_relative == "2 hours ago"


def test_porcelain_v2_ignored_entry_is_not_dirty(monkeypatch, tmp_path):
    sample = "# branch.head main\n# branch.upstream origin/main\n# branch.ab +0 -0\n! ignored.txt\n"
    monkeypatch.setattr(
        status, "_run",
        lambda repo, *args, timeout=10: _completed(
            args, stdout=sample if args[0] == "status" else "subject\tnow\n",
        ),
    )

    result = status.compute_status(tmp_path / "repo")
    assert result.dirty is False
    assert result.untracked is False
    assert result.is_clean is True


def test_porcelain_v2_missing_upstream_sets_no_upstream(monkeypatch, tmp_path):
    sample = "# branch.oid abc\n# branch.head main\n"
    monkeypatch.setattr(
        status, "_run",
        lambda repo, *args, timeout=10: _completed(
            args, stdout=sample if args[0] == "status" else "",
        ),
    )

    result = status.compute_status(tmp_path / "repo")
    assert result.no_upstream is True
    assert result.ahead == 0
    assert result.behind == 0


def test_porcelain_v2_detached_head_display(monkeypatch, tmp_path):
    sample = "# branch.oid abc\n# branch.head (detached)\n"
    monkeypatch.setattr(
        status, "_run",
        lambda repo, *args, timeout=10: _completed(
            args, stdout=sample if args[0] == "status" else "",
        ),
    )

    assert status.compute_status(tmp_path / "repo").branch == "(detached)"


def test_old_git_falls_back_once_then_stays_on_legacy(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    def fake_run(repo, *args, timeout=10):
        calls.append(args)
        if args[:2] == ("status", "--porcelain=v2"):
            return _completed(args, 129, stderr="error: unknown option --show-stash")
        outputs = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "main\n",
            ("status", "--porcelain=v1"): " M tracked.txt\n",
            ("rev-list", "--left-right", "--count", "@{u}...HEAD"): "0 1\n",
            ("stash", "list"): "",
            ("log", "-1", "--format=%s%x09%cr"): "subject\tnow\n",
        }
        return _completed(args, stdout=outputs[args])

    monkeypatch.setattr(status, "_run", fake_run)
    first = status.compute_status(tmp_path / "one")
    second = status.compute_status(tmp_path / "two")

    probes = [args for args in calls if args[:2] == ("status", "--porcelain=v2")]
    assert len(probes) == 1
    assert len(calls) == 11  # one failed capability probe + two legacy 5-call scans
    assert first.label == second.label == "ahead 1"


def test_porcelain_v2_and_legacy_preserve_label_and_clean_semantics(
    monkeypatch, tmp_path,
):
    repo = tmp_path / "repo"
    outputs = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "main\n",
        ("status", "--porcelain=v1"): " M tracked.txt\n?? new.txt\n!! ignored.txt\n",
        ("rev-list", "--left-right", "--count", "@{u}...HEAD"): "3 2\n",
        ("stash", "list"): "stash@{0}: WIP\n",
        ("log", "-1", "--format=%s%x09%cr"): "subject\t2 hours ago\n",
    }
    monkeypatch.setattr(
        status, "_run",
        lambda repo, *args, timeout=10: _completed(args, stdout=outputs[args]),
    )
    modern = status._parse_porcelain_v2(
        repo, _completed(("status",), stdout=_PORCELAIN_V2_SAMPLE),
    )
    legacy = status._compute_status_legacy(repo)

    assert modern.label == legacy.label
    assert modern.color == legacy.color
    assert modern.is_clean == legacy.is_clean


def test_upstream_without_branch_ab_is_not_reported_clean(monkeypatch, tmp_path):
    """A configured-but-unpushed upstream must not render as `clean`.

    Git emits `# branch.upstream` but NO `# branch.ab` when the remote-tracking
    ref does not exist yet (a local branch never pushed). Treating that as
    "upstream present, 0 ahead" hides unpushed commits behind a dim clean row.
    """
    v2 = (
        "# branch.oid d801941\n"
        "# branch.head feature\n"
        "# branch.upstream origin/feature\n"
    )

    def fake_run(repo, *args, timeout=10):
        if args[0] == "status":
            return subprocess.CompletedProcess(list(args), 0, v2, "")
        return subprocess.CompletedProcess(list(args), 0, "subject\tan hour ago", "")

    monkeypatch.setattr(status, "_run", fake_run)
    monkeypatch.setattr(status, "_PORCELAIN_V2_SHOW_STASH_SUPPORTED", True)

    st = status.compute_status(tmp_path / "repo")

    assert st.no_upstream is True
    assert st.label == "no upstream"
    assert st.is_clean is True  # same as the legacy path for this shape


def test_upstream_with_branch_ab_reports_ahead(monkeypatch, tmp_path):
    v2 = (
        "# branch.oid d801941\n"
        "# branch.head main\n"
        "# branch.upstream origin/main\n"
        "# branch.ab +2 -3\n"
    )

    def fake_run(repo, *args, timeout=10):
        if args[0] == "status":
            return subprocess.CompletedProcess(list(args), 0, v2, "")
        return subprocess.CompletedProcess(list(args), 0, "subject\tan hour ago", "")

    monkeypatch.setattr(status, "_run", fake_run)
    monkeypatch.setattr(status, "_PORCELAIN_V2_SHOW_STASH_SUPPORTED", True)

    st = status.compute_status(tmp_path / "repo")

    assert (st.no_upstream, st.ahead, st.behind) == (False, 2, 3)
    assert st.label == "diverged"
