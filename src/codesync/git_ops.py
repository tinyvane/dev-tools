"""Parallel git pull/push with per-repo progress.

Replaces `gita pull` / `gita push` so we control concurrency, error handling,
and progress display directly instead of parsing gita's output.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from codesync import output


# Per-op timeout. git operations should be fast; a stuck one means network hang.
_OP_TIMEOUT_SEC = 120


@dataclass
class OpResult:
    repo: Path
    ok: bool
    code: int
    detail: str   # short human-readable note (last stderr line for failures, "" for success)


@dataclass
class OpSummary:
    op: str
    total: int
    ok: int
    failed: list[OpResult]
    elapsed: float


def find_repos(code_roots: list[Path]) -> list[Path]:
    """Scan one level into each root; return absolute paths of dirs containing .git.

    Symlinks are followed for the .git check (so submodule shims/worktrees work),
    but the iterator only walks one level — same depth as gita's default behavior.
    """
    repos: list[Path] = []
    seen: set[Path] = set()
    for root in code_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            if not (entry / ".git").exists():
                continue
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            repos.append(entry)
    return sorted(repos, key=lambda p: p.name.lower())


def _short_err(stderr: str, stdout: str) -> str:
    """Pick the most informative single-line summary from git's output."""
    for stream in (stderr, stdout):
        for line in reversed(stream.splitlines()):
            line = line.strip()
            if line and not line.startswith("From "):
                return line[:120]
    return ""


def _run_one(repo: Path, op: str) -> OpResult:
    """Run a single git op. Returns OpResult — never raises."""
    args = ["git", "-C", str(repo), op]
    # Quieter output, but keep errors.
    if op == "pull":
        args += ["--ff-only", "--quiet"]
    elif op == "push":
        args += ["--quiet"]

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=_OP_TIMEOUT_SEC)
        ok = r.returncode == 0
        detail = "" if ok else _short_err(r.stderr or "", r.stdout or "")
        return OpResult(repo=repo, ok=ok, code=r.returncode, detail=detail)
    except subprocess.TimeoutExpired:
        return OpResult(repo=repo, ok=False, code=124, detail=f"timeout >{_OP_TIMEOUT_SEC}s")
    except FileNotFoundError:
        return OpResult(repo=repo, ok=False, code=127, detail="git not found")
    except Exception as e:  # last-resort safety net
        return OpResult(repo=repo, ok=False, code=1, detail=str(e)[:120])


def parallel_op(repos: list[Path], op: str, *, max_workers: int = 8) -> OpSummary:
    """Run `git <op>` on every repo in parallel, printing progress as each finishes."""
    total = len(repos)
    t0 = time.monotonic()

    if total == 0:
        output.detail("(无 repo 可操作)")
        return OpSummary(op=op, total=0, ok=0, failed=[], elapsed=0.0)

    width = len(str(total))
    done = 0
    failed: list[OpResult] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_one, r, op): r for r in repos}
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                done += 1
                idx = done
                if not res.ok:
                    failed.append(res)
            name = res.repo.name
            tag = output.hilite("✓", "green") if res.ok else output.hilite("✗", "red")
            prefix = f"  [{idx:>{width}}/{total}] {tag} {name}"
            if res.ok:
                output.info(prefix)
            else:
                output.info(f"{prefix}  {output.hilite(res.detail, 'yellow')}")

    elapsed = time.monotonic() - t0
    return OpSummary(op=op, total=total, ok=total - len(failed), failed=failed, elapsed=elapsed)


def print_summary(s: OpSummary) -> None:
    if s.total == 0:
        return
    color = "green" if not s.failed else ("yellow" if s.ok else "red")
    msg = f"{s.op}: {s.ok}/{s.total} OK，耗时 {s.elapsed:.1f}s"
    output.info(output.hilite(f"  {msg}", color))


def default_workers() -> int:
    """Decent default for git ops: I/O-bound, so go a bit above CPU count."""
    return min(16, max(4, (os.cpu_count() or 4) * 2))
