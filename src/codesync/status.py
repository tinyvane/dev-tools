"""Repo status display — replaces `gita ll` so we can:
- handle CJK character width correctly (中文 = 2 cells, not 1)
- replace cryptic single-char flags ([*?↓]) with readable labels
- summarize at the top
- filter to only problem repos with --problems
"""
from __future__ import annotations

import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from codesync import output


# ---------- visual width (CJK-aware) ----------

def visual_width(s: str) -> int:
    """Cells this string occupies in a terminal (East-Asian wide chars = 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in s)


def pad_visual(s: str, width: int) -> str:
    return s + " " * max(0, width - visual_width(s))


def truncate_visual(s: str, max_width: int) -> str:
    if visual_width(s) <= max_width:
        return s
    out: list[str] = []
    w = 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("F", "W") else 1
        if w + cw + 1 > max_width:  # leave a cell for the ellipsis
            return "".join(out) + "…"
        out.append(c)
        w += cw
    return "".join(out)


# ---------- per-repo status ----------

@dataclass
class RepoStatus:
    name: str
    branch: str
    dirty: bool         # working tree or index has modifications
    untracked: bool
    ahead: int
    behind: int
    no_upstream: bool
    stashed: bool
    last_subject: str
    last_relative: str
    error: str = ""

    @property
    def is_clean(self) -> bool:
        return not (self.dirty or self.untracked or self.ahead or self.behind or self.stashed)

    @property
    def label(self) -> str:
        """One-word primary status label."""
        if self.error:
            return "error"
        if self.ahead and self.behind:
            return "diverged"
        if self.behind:
            return f"behind {self.behind}"
        if self.ahead:
            return f"ahead {self.ahead}"
        if self.dirty and self.untracked:
            return "mixed"
        if self.dirty:
            return "modified"
        if self.untracked:
            return "untracked"
        if self.stashed:
            return "stash"
        if self.no_upstream:
            return "no upstream"
        return "clean"

    @property
    def color(self) -> str:
        l = self.label
        if l == "clean":
            return "gray"
        if l == "error":
            return "red"
        if l == "diverged":
            return "magenta"
        if l.startswith("behind"):
            return "red"
        if l.startswith("ahead"):
            return "cyan"
        if l in ("modified", "mixed", "stash"):
            return "yellow"
        if l == "untracked":
            return "blue"
        if l == "no upstream":
            return "gray"
        return "reset"


def _run(repo: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def compute_status(repo: Path) -> RepoStatus:
    name = repo.name
    try:
        # branch (or detached HEAD)
        r = _run(repo, "rev-parse", "--abbrev-ref", "HEAD")
        branch = r.stdout.strip() if r.returncode == 0 else "?"
        if branch == "HEAD":
            branch = "(detached)"

        # porcelain working-tree status
        r = _run(repo, "status", "--porcelain=v1")
        lines = r.stdout.splitlines() if r.returncode == 0 else []
        dirty = any(
            (ln[:2] not in ("??", "!!")) and (ln[0] != " " or ln[1] != " ")
            for ln in lines if len(ln) >= 2 and not ln.startswith("?")
        )
        untracked = any(ln.startswith("??") for ln in lines)

        # ahead/behind vs upstream
        r = _run(repo, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.split()
            behind = int(parts[0]) if len(parts) > 0 else 0
            ahead = int(parts[1]) if len(parts) > 1 else 0
            no_upstream = False
        else:
            behind = ahead = 0
            no_upstream = True

        # stash
        r = _run(repo, "stash", "list")
        stashed = bool(r.stdout.strip())

        # last commit subject + relative time
        r = _run(repo, "log", "-1", "--format=%s%x09%cr")
        if r.returncode == 0 and r.stdout.strip():
            subject, _, relative = r.stdout.strip().partition("\t")
        else:
            subject = relative = ""

        return RepoStatus(
            name=name, branch=branch,
            dirty=dirty, untracked=untracked,
            ahead=ahead, behind=behind, no_upstream=no_upstream,
            stashed=stashed,
            last_subject=subject, last_relative=relative,
        )
    except subprocess.TimeoutExpired:
        return RepoStatus(name=name, branch="?", dirty=False, untracked=False,
                          ahead=0, behind=0, no_upstream=True, stashed=False,
                          last_subject="", last_relative="", error="timeout")
    except Exception as e:
        return RepoStatus(name=name, branch="?", dirty=False, untracked=False,
                          ahead=0, behind=0, no_upstream=True, stashed=False,
                          last_subject="", last_relative="",
                          error=str(e)[:80])


# ---------- display ----------

LABEL_WIDTH = 12      # "no upstream" is 11; pad to 12
BRANCH_WIDTH = 14
NAME_WIDTH = 36       # truncate longer names; pads shorter
SUBJECT_WIDTH = 50


def _render_row(s: RepoStatus) -> str:
    label = pad_visual(s.label, LABEL_WIDTH)
    name = pad_visual(truncate_visual(s.name, NAME_WIDTH), NAME_WIDTH)
    branch = pad_visual(truncate_visual(s.branch, BRANCH_WIDTH), BRANCH_WIDTH)
    subject = pad_visual(truncate_visual(s.last_subject, SUBJECT_WIDTH), SUBJECT_WIDTH)
    when = s.last_relative

    suffix_bits = []
    if s.stashed and not s.label.startswith("stash"):
        suffix_bits.append("+stash")
    if s.no_upstream and s.label != "no upstream":
        suffix_bits.append("+no-upstream")
    suffix = ("  " + " ".join(suffix_bits)) if suffix_bits else ""

    if s.is_clean:
        # everything dim for clean rows so problems pop
        return (f"  {output.hilite(label, s.color)} {output.hilite(name, 'gray')} "
                f"{output.hilite(branch, 'gray')} {output.hilite(subject, 'gray')}  "
                f"{output.hilite(when, 'gray')}")
    return (f"  {output.hilite(label, s.color)} {name} "
            f"{output.hilite(branch, 'gray')} {subject}  "
            f"{output.hilite(when, 'gray')}{suffix}")


def _print_summary(statuses: list[RepoStatus]) -> None:
    total = len(statuses)
    by_label: dict[str, int] = {}
    for s in statuses:
        # group by first word of label so "ahead 3" and "ahead 7" merge
        key = s.label.split()[0]
        by_label[key] = by_label.get(key, 0) + 1

    order = ["clean", "modified", "mixed", "untracked", "stash",
             "ahead", "behind", "diverged", "no", "error"]
    parts = []
    for k in order:
        if k in by_label:
            display_key = "no upstream" if k == "no" else k
            color = {
                "clean": "green", "modified": "yellow", "mixed": "yellow",
                "untracked": "blue", "stash": "magenta",
                "ahead": "cyan", "behind": "red", "diverged": "magenta",
                "no": "gray", "error": "red",
            }.get(k, "reset")
            parts.append(output.hilite(f"{by_label[k]} {display_key}", color))
    line = "  " + f"{total} repos · " + " · ".join(parts)
    output.info(line)


def _print_legend() -> None:
    output.info("  " + output.hilite(
        "labels: clean / modified / untracked / mixed / stash / "
        "ahead N / behind N / diverged / no upstream",
        "gray",
    ))


def print_status(repos: list[Path], *, problems_only: bool = False,
                 max_workers: int = 8, show_legend: bool = True) -> None:
    if not repos:
        output.detail("(无 repo)")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        statuses = list(ex.map(compute_status, repos))

    statuses.sort(key=lambda s: (s.is_clean, s.name.lower()))
    _print_summary(statuses)
    if show_legend:
        _print_legend()
    output.info("")

    if problems_only:
        statuses = [s for s in statuses if not s.is_clean]
        if not statuses:
            output.good("全部 clean，无需关注。")
            return

    for s in statuses:
        output.info(_render_row(s))
