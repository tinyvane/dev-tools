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
from datetime import datetime
from pathlib import Path

from codesync import output


# Per-op timeout. git operations should be fast; a stuck one means network hang.
_OP_TIMEOUT_SEC = 120

# Pause before retrying failed ops. Gives GitHub's SSH side a beat to recover
# from connection throttling under parallel load. Patched to 0 in tests.
_RETRY_DELAY_SEC = 2.0


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
    """Pick the most informative single-line summary from git's output.

    Prefer a `fatal:` / `error:` / `ERROR:` line over trailing continuation
    lines. Git's no-access message ends with 'and the repository exists.', which
    is meaningless on its own — the useful line is 'fatal: Could not read from
    remote repository.' or 'ERROR: Repository not found.' a few lines up.
    """
    lines = [l.strip() for l in (stderr.splitlines() + stdout.splitlines()) if l.strip()]
    for line in lines:
        if line.startswith("From "):
            continue
        if line.lower().startswith(("fatal:", "error:")):
            return _clip(line)
    # No priority prefix found — fall back to the last non-"From " line.
    for line in reversed(lines):
        if not line.startswith("From "):
            return _clip(line)
    return ""


def _clip(line: str, limit: int = 120) -> str:
    """Truncate to `limit`, keeping head AND tail.

    Git error lines often put the reason at the end (e.g.
    `error: open("<very long path>"): Filename too long`); a plain head-cut
    would drop the part that explains the failure. Middle-ellipsis keeps both.
    """
    if len(line) <= limit:
        return line
    keep = limit - 1  # room for the ellipsis
    head = (keep + 1) // 2
    tail = keep - head
    return f"{line[:head]}…{line[-tail:]}"


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


def _execute_pass(repos: list[Path], op: str, max_workers: int, label: str = "") -> list[OpResult]:
    """Run one parallel pass over repos, printing per-repo progress. Returns all results."""
    total = len(repos)
    width = len(str(total))
    done = 0
    results: list[OpResult] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_one, r, op): r for r in repos}
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                done += 1
                idx = done
                results.append(res)
            name = res.repo.name
            tag = output.hilite("✓", "green") if res.ok else output.hilite("✗", "red")
            prefix = f"  {label}[{idx:>{width}}/{total}] {tag} {name}"
            if res.ok:
                output.info(prefix)
            else:
                output.info(f"{prefix}  {output.hilite(res.detail, 'yellow')}")
    return results


def parallel_op(repos: list[Path], op: str, *, max_workers: int = 8) -> OpSummary:
    """Run `git <op>` on every repo in parallel, printing progress as each finishes.

    Failed ops are retried once, SERIALLY. Parallel SSH to GitHub occasionally
    throttles connections, which surfaces as 'Repository not found / access
    rights' on repos that are perfectly fine — a serial retry clears those.
    Genuine failures (no push access, real conflicts) fail again and are kept.
    """
    total = len(repos)
    t0 = time.monotonic()

    if total == 0:
        output.detail("(无 repo 可操作)")
        return OpSummary(op=op, total=0, ok=0, failed=[], elapsed=0.0)

    results = _execute_pass(repos, op, max_workers)
    failed = [r for r in results if not r.ok]

    if failed:
        retry_repos = [r.repo for r in failed]
        output.detail(f"重试 {len(retry_repos)} 个失败的 {op}（串行，规避并发 SSH 限流）...")
        time.sleep(_RETRY_DELAY_SEC)
        retry_results = _execute_pass(retry_repos, op, max_workers=1, label="retry ")
        failed = [r for r in retry_results if not r.ok]

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


def _is_dirty(repo: Path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def auto_commit_dirty(repos: list[Path], skip_names: set[str], *, max_workers: int = 8) -> list[str]:
    """`git add -A` + commit every dirty repo (clean repos and skip_names skipped).

    Run AFTER pull (so the commit lands on top of remote, avoiding needless
    divergence) and BEFORE push (so the new commit gets pushed). Returns the
    list of committed repo names. Never raises — per-repo failure is logged.
    """
    targets = [r for r in repos if r.name not in skip_names]
    if not targets:
        output.detail("(无 repo 需要 auto-commit)")
        return []

    # Parallel dirty-detection; the actual commits run serially (few, and avoids
    # interleaving git output).
    dirty: list[Path] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for repo, is_dirty in zip(targets, ex.map(_is_dirty, targets)):
            if is_dirty:
                dirty.append(repo)

    if not dirty:
        output.detail("(没有脏 repo，无需 commit)")
        return []

    msg = f"chore: auto-commit {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    committed: list[str] = []
    for repo in dirty:
        add = subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, text=True)
        if add.returncode != 0:
            output.warn(f"  ✗ {repo.name}: git add 失败 {_short_err(add.stderr or '', add.stdout or '')}")
            continue
        com = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", msg],
            capture_output=True, text=True,
        )
        if com.returncode == 0:
            committed.append(repo.name)
            output.info(f"  {output.hilite('✓', 'green')} {repo.name}")
        else:
            output.warn(f"  ✗ {repo.name}: {_short_err(com.stderr or '', com.stdout or '')}")
    return committed
