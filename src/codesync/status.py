"""Repo status display — replaces `gita ll` so we can:
- handle CJK character width correctly (中文 = 2 cells, not 1)
- replace cryptic single-char flags ([*?↓]) with readable labels
- summarize at the top
- filter to only problem repos with --problems
"""
from __future__ import annotations

import subprocess
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from codesync import output, proc


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
        # An unreadable repo is NOT clean. print_status(problems_only=True)
        # filters on this, so without the error term a repo whose git status
        # failed — or timed out — was dropped from the report entirely, and a
        # run could even claim "全部 clean，无需关注。" while hiding the one
        # repo that actually needed attention.
        if self.error:
            return False
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
        label = self.label
        if label == "clean":
            return "gray"
        if label == "error":
            return "red"
        if label == "diverged":
            return "magenta"
        if label.startswith("behind"):
            return "red"
        if label.startswith("ahead"):
            return "cyan"
        if label in ("modified", "mixed", "stash"):
            return "yellow"
        if label == "untracked":
            return "blue"
        if label == "no upstream":
            return "gray"
        return "reset"


def _run(repo: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    result = proc.run(
        ["git", "-C", str(repo), *args],
        timeout=timeout,
    )
    if proc.timed_out(result):
        raise TimeoutError
    return result


def _timeout_status(name: str) -> RepoStatus:
    return RepoStatus(name=name, branch="?", dirty=False, untracked=False,
                      ahead=0, behind=0, no_upstream=True, stashed=False,
                      last_subject="", last_relative="", error="timeout")


def _error_status(name: str, error: Exception) -> RepoStatus:
    return RepoStatus(name=name, branch="?", dirty=False, untracked=False,
                      ahead=0, behind=0, no_upstream=True, stashed=False,
                      last_subject="", last_relative="",
                      error=str(error)[:80])


def _compute_status_legacy(repo: Path) -> RepoStatus:
    """Five-command fallback for Git versions without status --show-stash."""
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
    except TimeoutError:
        return _timeout_status(name)
    except Exception as e:
        return _error_status(name, e)


# None until the first repo probes support. The lock matters because status scans
# run in a thread pool: old Git must see one failed probe, not one per worker.
_PORCELAIN_V2_SHOW_STASH_SUPPORTED: bool | None = None
_PORCELAIN_V2_PROBE_LOCK = threading.Lock()


def _status_v2(repo: Path) -> subprocess.CompletedProcess | None:
    """Run porcelain v2, or return None when this process must use legacy Git."""
    global _PORCELAIN_V2_SHOW_STASH_SUPPORTED

    if _PORCELAIN_V2_SHOW_STASH_SUPPORTED is False:
        return None
    if _PORCELAIN_V2_SHOW_STASH_SUPPORTED is True:
        return _run(repo, "status", "--porcelain=v2", "--branch", "--show-stash")

    with _PORCELAIN_V2_PROBE_LOCK:
        if _PORCELAIN_V2_SHOW_STASH_SUPPORTED is None:
            result = _run(
                repo, "status", "--porcelain=v2", "--branch", "--show-stash",
            )
            stderr = (result.stderr or "").lower()
            # Cache only a CONCLUSIVE verdict. Git parses the repository before
            # it parses options, so probing a BROKEN repo on old Git yields
            # "fatal: not a git repository" — no unknown-option marker. Reading
            # that as "supported" would then run an unsupported command against
            # every remaining repo and paint the whole run red. A repo-specific
            # failure says nothing about this Git's capabilities, so leave the
            # flag undecided and let the next repo settle it.
            if result.returncode == 0:
                _PORCELAIN_V2_SHOW_STASH_SUPPORTED = True
                return result
            if any(marker in stderr
                   for marker in ("unknown option", "unrecognized")):
                _PORCELAIN_V2_SHOW_STASH_SUPPORTED = False
                return None
            # Inconclusive: store nothing. The caller turns this failed result
            # into an error status for THIS repo only. Cost when every repo is
            # broken is that discovery serializes — acceptable, and each repo
            # still runs exactly one status subprocess.
            return result

    # Threads that waited for the probe leave the lock before running their own
    # repo command, so only capability discovery is serialized.
    if _PORCELAIN_V2_SHOW_STASH_SUPPORTED is False:
        return None
    return _run(repo, "status", "--porcelain=v2", "--branch", "--show-stash")


def _stderr_excerpt(result: subprocess.CompletedProcess) -> str:
    """The most informative line of a failed git command, trimmed for a row.

    Prefers git's own `fatal:`/`error:` line over the first line, which is
    often preamble. Local to status on purpose: git_ops._short_err serves the
    same idea for op results, but coupling the two modules to share five lines
    would be worse than repeating them.
    """
    text = (result.stderr or "") or (result.stdout or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return f"git 退出码 {result.returncode}"
    for line in lines:
        low = line.lower()
        if low.startswith("fatal:") or low.startswith("error:"):
            return line[:80]
    return lines[0][:80]


def _parse_porcelain_v2(repo: Path, result: subprocess.CompletedProcess) -> RepoStatus:
    # A failed status command means we do NOT know this repo's state. Reporting
    # the all-False default would render it gray "no upstream" / is_clean —
    # indistinguishable from a healthy local-only repo, and dropped entirely by
    # --problems. Same rule the rest of codesync follows: unknown is never
    # "clean", never "absent".
    if result.returncode != 0:
        return RepoStatus(
            name=repo.name, branch="?", dirty=False, untracked=False,
            ahead=0, behind=0, no_upstream=True, stashed=False,
            last_subject="", last_relative="", error=_stderr_excerpt(result),
        )

    branch = "?"
    initial = False
    upstream = ""
    saw_ab = False
    ahead = behind = 0
    stashed = False
    dirty = untracked = False

    for line in result.stdout.splitlines():
        if line == "# branch.oid (initial)":
            initial = True
        elif line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            saw_ab = True
            for count in line.removeprefix("# branch.ab ").split():
                if count.startswith("+"):
                    ahead = int(count[1:])
                elif count.startswith("-"):
                    behind = int(count[1:])
        elif line.startswith("# stash "):
            stashed = int(line.removeprefix("# stash ")) > 0
        elif line.startswith(("1 ", "2 ", "u ")):
            dirty = True
        elif line.startswith("? "):
            untracked = True
        # `! ` is intentionally ignored, matching porcelain v1's `!!` behavior.

    if initial:
        # rev-parse --abbrev-ref HEAD fails before the first commit; preserve the
        # legacy display instead of exposing porcelain v2's prospective name.
        branch = "?"

    r = _run(repo, "log", "-1", "--format=%s%x09%cr")
    if r.returncode == 0 and r.stdout.strip():
        subject, _, relative = r.stdout.strip().partition("\t")
    else:
        subject = relative = ""

    return RepoStatus(
        name=repo.name, branch=branch,
        dirty=dirty, untracked=untracked,
        ahead=ahead, behind=behind,
        # Git prints branch.upstream but OMITS branch.ab when the upstream is
        # configured yet its remote-tracking ref does not exist — a local branch
        # created but never pushed (the codex/* shape v2.18.0 handles). Reporting
        # that as "has upstream, 0 ahead" renders the repo `clean`, hiding
        # unpushed commits behind a dim row. Legacy called it "no upstream";
        # keep parity so both paths agree on label/is_clean.
        no_upstream=not (upstream and saw_ab),
        stashed=stashed,
        last_subject=subject, last_relative=relative,
    )


def compute_status(repo: Path) -> RepoStatus:
    """Compute the same display state with two Git calls on modern Git."""
    try:
        result = _status_v2(repo)
        if result is None:
            return _compute_status_legacy(repo)
        return _parse_porcelain_v2(repo, result)
    except TimeoutError:
        return _timeout_status(repo.name)
    except Exception as e:
        return _error_status(repo.name, e)


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


def _status_dimensions(status: RepoStatus) -> list[str]:
    """Return every actionable dimension, including ones hidden by label priority."""
    if status.error:
        return ["error"]

    dimensions: list[str] = []
    if status.dirty and status.untracked:
        dimensions.append("mixed")
    elif status.dirty:
        dimensions.append("modified")
    elif status.untracked:
        dimensions.append("untracked")

    if status.ahead and status.behind:
        dimensions.append("diverged")
    elif status.ahead:
        dimensions.append(f"ahead {status.ahead}")
    elif status.behind:
        dimensions.append(f"behind {status.behind}")

    if status.stashed:
        dimensions.append("stash")
    if status.no_upstream:
        dimensions.append("no upstream")
    return dimensions


def _print_resolution_guidance(
    entries: list[tuple[Path, RepoStatus]],
) -> None:
    """Explain safe next steps for every non-clean status without mutating repos."""
    problems = [(repo, item) for repo, item in entries if not item.is_clean]
    if not problems:
        return

    has_worktree = any(item.dirty or item.untracked for _, item in problems)
    has_stash = any(item.stashed for _, item in problems)
    has_ahead = any(item.ahead and not item.behind for _, item in problems)
    has_behind = any(item.behind and not item.ahead for _, item in problems)
    has_diverged = any(item.ahead and item.behind for _, item in problems)
    has_no_upstream = any(item.no_upstream and not item.error for _, item in problems)
    has_error = any(item.error for _, item in problems)
    repo_arg = '"<仓库完整路径>"'

    output.section("非 clean 仓库处理指引")
    output.detail("这里只给出建议，不会自动修改任何仓库。需要处理的目录：")
    for repo, item in problems:
        dimensions = " + ".join(_status_dimensions(item))
        output.info(f"    {repo}  [{dimensions}]")

    output.info("")
    output.info("  通用第一步：把下面的 <仓库完整路径> 换成上方目录，先查看详情：")
    output.info(f"    git -C {repo_arg} status --short --branch")

    if has_worktree:
        output.info("")
        output.info("  modified / untracked / mixed：有尚未提交的本地文件")
        output.info(f"    查看具体改动：git -C {repo_arg} diff")
        output.info(f"    查看已暂存改动：git -C {repo_arg} diff --cached")
        output.info("    确认全部要保留并同步后：")
        output.info(f"      git -C {repo_arg} add -A")
        output.info(f"      git -C {repo_arg} commit -m \"说明本次修改\"")
        output.info("      再运行 codesync sync 完成拉取和上传")
        output.info("    暂时不提交（包含未跟踪文件）：")
        output.info(f"      git -C {repo_arg} stash push -u -m \"临时保存\"")

    if has_stash:
        output.info("")
        output.info("  stash：以前暂存的改动仍保留在 stash 中")
        output.info(f"    列出：git -C {repo_arg} stash list")
        output.info(f"    预览最新一份：git -C {repo_arg} stash show --stat 'stash@{{0}}'")
        output.info(f"    安全恢复且保留备份：git -C {repo_arg} stash apply 'stash@{{0}}'")

    if has_ahead:
        output.info("")
        output.info("  ahead N：本地提交尚未上传")
        output.info(f"    上传当前仓库：git -C {repo_arg} push")

    if has_behind:
        output.info("")
        output.info("  behind N：远端有本机尚未拉取的提交")
        output.info("    先保证工作区 clean，再执行：")
        output.info(f"      git -C {repo_arg} pull --rebase")

    if has_diverged:
        output.info("")
        output.info("  diverged：本地和远端各有提交，先检查分叉再合并")
        output.info(f"    git -C {repo_arg} fetch")
        output.info(f"    git -C {repo_arg} log --oneline --left-right 'HEAD...@{{u}}'")
        output.info(f"    确认后：git -C {repo_arg} pull --rebase")
        output.info(
            f"    如 rebase 冲突且不准备继续：git -C {repo_arg} rebase --abort"
        )

    if has_no_upstream:
        output.info("")
        output.info("  no upstream：当前分支尚未关联远端分支")
        output.info(f"    先检查远端：git -C {repo_arg} remote -v")
        output.info(
            f"    确认 remote/分支后：git -C {repo_arg} "
            "push --set-upstream origin \"<当前分支>\""
        )

    if has_error:
        output.info("")
        output.info("  error：状态未知，不能当成 clean")
        output.info(f"    单独重试：git -C {repo_arg} status")
        output.info("    根据上方错误处理；未确认没有 Git 进程前，不要手动删除 index.lock。")

    output.info("")
    output.info("  处理后复查：codesync sync --status --problems")


def print_status(repos: list[Path], *, problems_only: bool = False,
                 max_workers: int = 8, show_legend: bool = True) -> None:
    if not repos:
        output.detail("(无 repo)")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        statuses = list(ex.map(compute_status, repos))

    entries = list(zip(repos, statuses, strict=True))
    entries.sort(key=lambda entry: (entry[1].is_clean, entry[1].name.lower()))
    statuses = [item for _, item in entries]
    _print_summary(statuses)
    if show_legend:
        _print_legend()
    output.info("")

    if problems_only:
        entries = [entry for entry in entries if not entry[1].is_clean]
        if not entries:
            output.good("全部 clean，无需关注。")
            return

    for _, item in entries:
        output.info(_render_row(item))

    _print_resolution_guidance(entries)
