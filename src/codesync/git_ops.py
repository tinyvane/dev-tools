"""Parallel git pull/push with per-repo progress.

Replaces `gita pull` / `gita push` so we control concurrency, error handling,
and progress display directly instead of parsing gita's output.
"""
from __future__ import annotations

import os
import re
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


# ---------- nested repo discovery (v2.8.0) ----------

# Dirs we never descend into when hunting for nested git repos: build artifacts
# and dependency trees that can contain hundreds of vendored .git dirs and would
# make the scan crawl. Hidden dirs (incl. .git itself) are pruned separately.
_NESTED_SKIP_DIRS = {
    "node_modules", "vendor", "bower_components", "__pycache__", ".tox",
    "venv", ".venv", "env", "site-packages", "dist", "build", "out",
    ".next", ".nuxt", "target", ".gradle", "Pods", ".terraform",
}

# How deep (in path components below the outer repo root) we look for nested
# repos. The common layout is outer/inner/.git (depth 1). A small bound keeps
# the walk cheap; nested-inside-nested is intentionally not followed.
_NESTED_MAX_DEPTH = 3

_OWNER_RE = re.compile(r"github\.com[:/]([^/]+)/")


@dataclass
class NestedRepo:
    path: Path        # absolute path to the nested repo's working dir
    outer: Path       # the top-level repo it lives inside
    rel: str          # path relative to outer (posix), e.g. "frontend"
    is_submodule: bool  # registered in outer/.gitmodules (vs accidental embed)
    pushable: bool    # origin owner is one of "mine" → push; else pull-only


def _walk_nested_git(outer: Path, max_depth: int) -> list[Path]:
    """Bounded walk under `outer` returning dirs that contain a .git (nested
    repos). Does not descend INTO a found nested repo, into hidden dirs, or into
    artifact dirs. The outer's own .git is skipped (we start below the root)."""
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(outer):
        p = Path(dirpath)
        if p != outer and (p / ".git").exists():
            found.append(p)
            dirnames[:] = []  # a nested repo's internals are its own; stop here
            continue
        depth = len(p.relative_to(outer).parts)
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if d not in _NESTED_SKIP_DIRS and not d.startswith(".")]
    return found


def _gitmodules_paths(repo: Path) -> set[str]:
    """Submodule paths declared in repo/.gitmodules (posix), empty if none."""
    f = repo / ".gitmodules"
    if not f.exists():
        return set()
    paths: set[str] = set()
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("path") and "=" in s:
                val = s.split("=", 1)[1].strip()
                if val:
                    paths.add(val)
    except OSError:
        pass
    return paths


def _origin_owner(repo: Path) -> str | None:
    """The GitHub owner from origin's URL (handles ghproxy mirror prefixes since
    the regex anchors on 'github.com/<owner>/'). None if no origin or non-GitHub."""
    r = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    m = _OWNER_RE.search(r.stdout.strip())
    return m.group(1) if m else None


def my_owners(cfg, toplevel: list[Path]) -> set[str]:
    """Lowercased set of GitHub owners considered "mine" — used to decide whether
    a nested repo is pushable (mine) or pull-only (third-party). Prefer the
    configured auto_clone.owner; otherwise derive from the top-level repos'
    origins (everything you cloned under code_roots is yours by assumption)."""
    if cfg.auto_clone and cfg.auto_clone.owner:
        return {cfg.auto_clone.owner.lower()}
    owners: set[str] = set()
    for r in toplevel:
        o = _origin_owner(r)
        if o:
            owners.add(o.lower())
    return owners


def find_nested_repos(toplevel: list[Path], owners: set[str], *,
                      skip: tuple[str, ...] = (), max_depth: int = _NESTED_MAX_DEPTH
                      ) -> list[NestedRepo]:
    """Discover git repos nested inside each top-level repo and classify them.

    A nested repo is a "submodule" if its path is registered in the outer's
    .gitmodules, else "embedded". Pushable iff its origin owner is in `owners`.
    `skip` matches either the nested dir's basename or its path relative to the
    outer (posix)."""
    skip_set = set(skip)
    nested: list[NestedRepo] = []
    for outer in toplevel:
        sub_paths = _gitmodules_paths(outer)
        for inner in _walk_nested_git(outer, max_depth):
            rel = inner.relative_to(outer).as_posix()
            if inner.name in skip_set or rel in skip_set:
                continue
            owner = _origin_owner(inner)
            pushable = owner is not None and owner.lower() in owners
            nested.append(NestedRepo(
                path=inner, outer=outer, rel=rel,
                is_submodule=rel in sub_paths, pushable=pushable,
            ))
    return nested


def update_submodules(parents: list[Path], *, max_workers: int = 8) -> None:
    """`git submodule update --init --recursive` on each parent (repos that have
    a .gitmodules). Checks out the recorded commits; first run clones missing
    submodules. Idempotent and cheap on subsequent runs. Never raises."""
    if not parents:
        return
    output.section("更新 submodule（git submodule update --init）")
    for p in parents:
        r = subprocess.run(
            ["git", "-C", str(p), "submodule", "update", "--init", "--recursive"],
            capture_output=True, text=True, timeout=_OP_TIMEOUT_SEC * 4,
        )
        if r.returncode == 0:
            output.info(f"  {output.hilite('✓', 'green')} {p.name}")
        else:
            output.warn(f"  ✗ {p.name}: {_short_err(r.stderr or '', r.stdout or '')}")


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


def auto_commit_dirty(repos: list[Path], skip_names: set[str], *, max_workers: int = 8,
                      exclude_map: dict[Path, set[str]] | None = None) -> list[str]:
    """`git add -A` + commit every dirty repo (clean repos and skip_names skipped).

    Run AFTER pull (so the commit lands on top of remote, avoiding needless
    divergence) and BEFORE push (so the new commit gets pushed). Returns the
    list of committed repo names. Never raises — per-repo failure is logged.

    exclude_map (v2.8.0): outer-repo path → set of nested paths (relative,
    posix) to unstage after `git add -A`. This keeps a nested repo's moving
    gitlink pointer OUT of the superproject's commit — the nested repo is synced
    independently, and baking its SHA into the outer would leave the outer
    perpetually dirty/conflicting across machines (there's no .gitmodules to
    resolve an embedded repo's pointer).
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
        # Unstage any nested-repo gitlink so the outer doesn't commit a moving
        # pointer (the nested repo syncs on its own). See exclude_map docstring.
        excl = exclude_map.get(repo) if exclude_map else None
        if excl:
            subprocess.run(
                ["git", "-C", str(repo), "reset", "-q", "--", *excl],
                capture_output=True, text=True,
            )
        # `git add -A` may stage nothing even though the repo is "dirty" — the
        # classic case is a dirty submodule / embedded git repo: the superproject
        # sees ` M <gitlink>` but there's no new commit pointer to record, so
        # there is genuinely nothing to commit. Committing anyway just fails with
        # "no changes added to commit", which used to read as a hard error every
        # run. Detect the empty stage and report it honestly instead.
        staged = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            capture_output=True, text=True,
        )
        if staged.returncode == 0:  # exit 0 = nothing staged
            subs = _dirty_submodules(repo)
            if subs:
                output.warn(
                    f"  ⚠ {repo.name}: 无可提交 — 内含脏的嵌套仓库/submodule "
                    f"({', '.join(subs)})，其改动不会被同步"
                )
            else:
                output.detail(f"  ({repo.name}: 无可暂存，跳过)")
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


def _dirty_submodules(repo: Path) -> list[str]:
    """Names of gitlink paths whose working tree is dirty (modified content).

    These are nested git repos (proper submodules or accidental embedded
    clones) — `git status --porcelain` shows them as a modified entry, but
    `git add -A` in the superproject can't stage their uncommitted content.
    Returns the gitlink paths so the caller can warn that they go un-synced.
    """
    porcelain = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if porcelain.returncode != 0:
        return []
    # Paths git reports as changed (strip the 2-char XY status + space).
    changed = [ln[3:].strip() for ln in porcelain.stdout.splitlines() if ln.strip()]
    if not changed:
        return []
    # Which of those are gitlinks (mode 160000)?
    ls = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "--", *changed],
        capture_output=True, text=True,
    )
    if ls.returncode != 0:
        return []
    subs: list[str] = []
    for ln in ls.stdout.splitlines():
        # format: "<mode> <sha> <stage>\t<path>"
        if ln.startswith("160000 "):
            path = ln.split("\t", 1)[-1].strip()
            if path:
                subs.append(path)
    return subs
