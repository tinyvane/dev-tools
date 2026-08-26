"""Parallel git pull/push with per-repo progress.

Replaces `gita pull` / `gita push` so we control concurrency, error handling,
and progress display directly instead of parsing gita's output.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from codesync import output, proc
from codesync.remote_url import normalize, parse_github_remote


# Wall-clock backstop for the pull/push subprocess. These are unbounded
# transfers, so they belong on the same tier as clone — not T_NET, which is for
# a single bounded gh/git API call.
#
# It used to be T_NET (120s), which produced two bugs at once. A dead link was
# killed by this timeout long before the 300s HTTP low-speed / SSH ServerAlive
# policy could fire, making that whole stall-detection layer unreachable dead
# code on the pull/push path it was written for. And a LIVE but slow link had
# only 120s of transfer budget: at the 12-15 KB/s that policy documents, any
# repository needing more than ~1.8 MB timed out every single run.
_OP_TIMEOUT_SEC = proc.T_NET_LONG

# Pause before retrying failed ops. Gives GitHub's SSH side a beat to recover
# from connection throttling under parallel load. Patched to 0 in tests.
_RETRY_DELAY_SEC = 2.0


@dataclass
class OpResult:
    repo: Path
    ok: bool
    code: int
    detail: str   # short human-readable note (last stderr line for failures, "" for success)
    skipped: bool = False  # benign no-op (e.g. pull of a local branch not yet on remote) — ok=True, shown dim not red
    # Whether parallel_op's serial retry pass should run this op again.
    #
    # DEFAULT TRUE IS LOAD-BEARING. The retry exists for one thing: parallel SSH
    # to github.com is intermittently throttled, and the resulting "Repository
    # not found / access rights" failures clear on a serial retry. Those land in
    # the generic error branch, so leaving the default True keeps that behavior
    # exactly as it was. Only failures that are DETERMINISTIC — the same command
    # would fail the same way a second later — opt out.
    retryable: bool = True


@dataclass
class OpSummary:
    op: str
    total: int
    ok: int
    failed: list[OpResult]
    elapsed: float


RepoDamage = Literal["husk", "incomplete-clone"]


@dataclass(frozen=True)
class DamagedRepo:
    path: Path
    kind: RepoDamage


def _has_loose_head_ref(heads: Path) -> bool:
    try:
        return any(path.is_file() for path in heads.rglob("*"))
    except OSError:
        return False


def _worktree_is_empty(entry: Path) -> bool:
    """True when the directory holds nothing but .git (nothing to lose)."""
    try:
        return not any(child.name != ".git" for child in entry.iterdir())
    except OSError:
        return False  # unreadable → assume content exists, never call it damaged


def is_corrupt_repo(entry: Path) -> RepoDamage | None:
    """Classify half-deleted husks and interrupted-clone leftovers.

    git refuses to operate on it ("fatal: not a git repository"), yet any
    .git-existence scan counts it as a repo — the classic Windows leftover from
    a delete that skipped read-only pack files (only .git/objects survives).
    A .git FILE (worktree / submodule gitlink) is never judged corrupt here.
    """
    g = entry / ".git"
    if not g.is_dir():
        return None
    if not (g / "HEAD").is_file():
        return "husk"
    if (not (g / "packed-refs").exists()
            and not _has_loose_head_ref(g / "refs" / "heads")
            and _worktree_is_empty(entry)):
        # An interrupted clone ALWAYS has an empty working tree: git checks out
        # only after the fetch completes. Requiring that is what separates it
        # from a freshly `git init`-ed repository, which has the identical .git
        # fingerprint (HEAD, no refs) but may hold the user's uncommitted work —
        # and which publish deliberately supports ("git init'd but no commits
        # yet"). Without this guard we would exclude such a directory from the
        # scan AND tell the user to delete it. Never widen this.
        return "incomplete-clone"
    return None


def find_repos(code_roots: list[Path]) -> list[Path]:
    """Scan one level into each root; return absolute paths of dirs containing .git.

    Symlinks are followed for the .git check (so submodule shims/worktrees work),
    but the iterator only walks one level — same depth as gita's default behavior.
    Damaged husks (see is_corrupt_repo) are excluded; find_corrupt_repos surfaces
    both half-deleted directories and interrupted clones separately.
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
            if is_corrupt_repo(entry):
                continue
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            repos.append(entry)
    return sorted(repos, key=lambda p: p.name.lower())


def any_repo(code_roots: list[Path]) -> bool:
    """True as soon as ONE operable repo is found — a cheap existence check.

    find_repos builds and sorts the whole list, resolving every entry. Callers
    that only need "is there anything here at all" (the SSH prewarm gate) pay
    that for nothing, which is exactly the duplicated scan this avoids.
    """
    for root in code_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            entries = root.iterdir()
        except OSError:
            continue
        for entry in entries:
            if (entry.is_dir() and (entry / ".git").exists()
                    and is_corrupt_repo(entry) is None):
                return True
    return False


def find_corrupt_repos(code_roots: list[Path]) -> list[DamagedRepo]:
    """One-level scan for damaged repositories so sync can name them once."""
    damaged: list[DamagedRepo] = []
    seen: set[Path] = set()
    for root in code_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or not (entry / ".git").exists():
                continue
            kind = is_corrupt_repo(entry)
            if kind is None:
                continue
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            damaged.append(DamagedRepo(entry, kind))
    return sorted(damaged, key=lambda item: item.path.name.lower())


@dataclass(frozen=True)
class PackCleanup:
    before_count: int
    before_bytes: int
    after_count: int
    after_bytes: int

    @property
    def removed_count(self) -> int:
        return max(0, self.before_count - self.after_count)

    @property
    def freed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


_STALE_PACK_AGE_SEC = 24 * 60 * 60


def cleanup_stale_packs(
    repos: list[Path], *, now: float | None = None,
    older_than_seconds: int = _STALE_PACK_AGE_SEC,
) -> PackCleanup:
    """Best-effort removal of tmp_pack_* files older than the safety window."""
    cutoff = (time.time() if now is None else now) - older_than_seconds
    candidates: list[Path] = []
    for repo in repos:
        pack_dir = repo / ".git" / "objects" / "pack"
        try:
            pack_entries = list(pack_dir.iterdir())
        except OSError:
            continue
        for path in pack_entries:
            if not path.name.startswith("tmp_pack_"):
                continue
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    candidates.append(path)
            except OSError:
                continue

    def measure(paths: list[Path]) -> tuple[int, int]:
        count = 0
        total = 0
        for path in paths:
            try:
                stat_result = path.stat()
            except OSError:
                continue
            count += 1
            total += stat_result.st_size
        return count, total

    before_count, before_bytes = measure(candidates)
    for path in candidates:
        try:
            path.unlink()
        except OSError:
            pass
    after_count, after_bytes = measure(candidates)
    return PackCleanup(before_count, before_bytes, after_count, after_bytes)


# ---------- safe repo-tree deletion (shared by delete + github_auto) ----------

def _clear_readonly_retry(func, path, _exc) -> None:
    """rmtree error handler: git packs objects read-only, and Windows refuses to
    delete read-only files (WinError 5). Clear the bit and retry the op."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise  # the callback otherwise suppresses the failure and reports success


def rmtree_repo(path: Path) -> tuple[bool, str]:
    """Delete a repo directory tree, handling the two Windows traps:
    read-only git objects (error handler clears the bit and retries; onexc on
    3.12+, onerror before) and the process CWD being inside the tree (step out
    to the parent first — Windows can't remove the CWD)."""
    try:
        cwd = Path.cwd().resolve()
        p = path.resolve()
        if cwd == p or p in cwd.parents:
            os.chdir(p.parent)
    except OSError:
        pass
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly_retry)
        else:
            shutil.rmtree(path, onerror=_clear_readonly_retry)
    except OSError as e:
        return False, str(e)
    if path.exists():
        return False, f"删除结束后目录仍存在: {path}"
    return True, ""


# ---------- duplicate-origin detection (v2.14.0) ----------

def _normalize_origin(url: str) -> str:
    return normalize(url)


@dataclass(frozen=True)
class OriginUrlResult:
    url: str | None
    certain: bool


def read_origin_url(repo: Path) -> OriginUrlResult:
    """Read the stored origin URL without applying any insteadOf rewriting."""
    # --get-all, then take the FIRST line: a remote may carry several URLs
    # (`git remote set-url --add`), and Git fetches from the first one, which is
    # therefore the repository's identity. Plain `--get` returns the LAST value
    # (later config wins), naming a repo codesync never actually syncs with.
    #
    # --local is what keeps the three answers apart. Without it, `git config`
    # happily falls back to global/system scope, so a directory that is NOT a
    # repository at all — and a half-deleted husk — return rc 1 with an empty
    # stderr, byte-for-byte identical to a healthy repo that simply has no
    # origin. Everything downstream then reads "certainly no origin", and the
    # _ORIGIN_UNAVAILABLE gate that `delete`/`rename` use to refuse acting on an
    # unreadable repo can never fire. --local makes git answer
    # "fatal: --local can only be used inside a git repository" (rc 128) for
    # those, which is the honest "could not ask".
    #
    # The cost is that a remote.origin.url set in ~/.gitconfig is not seen. That
    # is deliberate: such a setting applies to EVERY repo on the machine, which
    # would make _local_repos_by_owner key them all under one name, make
    # find_duplicate_origins call every repo a duplicate, make publish consider
    # nothing an orphan, and point delete/rename at the wrong remote. Identity
    # is a per-repository fact; reading it from a global default is not a
    # feature worth supporting.
    r = proc.run(
        ["git", "-C", str(repo), "config", "--local", "--get-all", "remote.origin.url"],
        timeout=proc.T_QUICK,
    )
    first = next((ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()), "")
    url = first
    if r.returncode == 0 and url:
        return OriginUrlResult(url, True)
    if r.returncode == 1 and not url and not (r.stderr or "").strip():
        return OriginUrlResult(None, True)
    return OriginUrlResult(None, False)


def origin_url(repo: Path) -> str | None:
    """Return the repository's stored origin URL, or None if absent/unreadable."""
    return read_origin_url(repo).url


def scan_origins(repos: list[Path], *, max_workers: int) -> dict[Path, str]:
    """Read origin URLs concurrently; omit repos with no readable origin."""
    if not repos:
        return {}

    def origin_of(repo: Path) -> tuple[Path, str]:
        result = read_origin_url(repo)
        return repo, result.url or ""

    origins: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for repo, url in ex.map(origin_of, repos):
            if url:
                origins[repo] = url
    return origins


def find_duplicate_origins(
    repos: list[Path], *, max_workers: int = 8,
    origins: dict[Path, str] | None = None,
) -> dict[str, list[Path]]:
    """Origins shared by 2+ of the given repos → {normalized_origin: [paths]}.

    The same repo checked out twice (e.g. an old date-prefixed folder AND a
    canonical-named clone) wastes disk and risks editing the wrong copy /
    diverging on the shared remote — and it accumulates silently. This is
    advisory only: detect and report, never auto-fix/delete (the user decides
    which copy lives). Repos without an origin are ignored."""
    if origins is None:
        origins = scan_origins(repos, max_workers=max_workers)

    groups: dict[str, list[Path]] = {}
    for repo in repos:
        url = origins.get(repo, "")
        if url:
            groups.setdefault(_normalize_origin(url), []).append(repo)
    return {k: sorted(v, key=lambda p: p.name.lower())
            for k, v in groups.items() if len(v) > 1}


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
    """The GitHub owner from the stored origin URL, if reliably parseable."""
    parsed = parse_github_remote(origin_url(repo) or "")
    return parsed.owner if parsed else None


def my_owners(cfg, toplevel: list[Path], *,
              origins: dict[Path, str] | None = None) -> set[str]:
    """Lowercased set of GitHub owners considered "mine" — used to decide whether
    a nested repo is pushable (mine) or pull-only (third-party). Prefer the
    configured auto_clone.owner; otherwise derive from the top-level repos'
    origins (everything you cloned under code_roots is yours by assumption)."""
    if cfg.auto_clone and cfg.auto_clone.owner:
        return {cfg.auto_clone.owner.lower()}
    owners: set[str] = set()
    for r in toplevel:
        if origins is None:
            o = _origin_owner(r)
        else:
            parsed = parse_github_remote(origins.get(r, ""))
            o = parsed.owner if parsed else None
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
        # Pinned to the transfer tier explicitly, NOT derived from
        # _OP_TIMEOUT_SEC: this was `_OP_TIMEOUT_SEC * 4` back when that meant
        # T_NET, and it would silently have become an hour when the base moved
        # to the transfer tier. A first submodule update clones, so it belongs
        # on the same tier as clone — but only that tier.
        r = proc.run(
            ["git", "-C", str(p), "submodule", "update", "--init", "--recursive"],
            timeout=proc.T_NET_LONG,
        )
        if proc.timed_out(r):
            output.warn(f"  ✗ {p.name}: submodule update 超时（>{proc.T_NET_LONG}s），跳过")
            continue
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


# git pull's complaint when the current branch's configured upstream branch
# isn't on the remote. Both --rebase and --ff-only use these fetch-side messages.
# In codesync's commit→pull→push flow this happens for a brand-new LOCAL branch
# that hasn't been pushed yet: pull can't find the ref, then the later push
# creates it. Benign — not a real failure.
_PULL_NO_REMOTE_REF_RE = re.compile(
    r"no such ref was fetched|couldn'?t find remote ref|couldn’t find remote ref",
    re.IGNORECASE,
)

# A pull can finish its rebase but fail while re-applying --autostash. Git leaves
# no rebase operation to abort and preserves the user's changes in the stash:
#     Applying autostash resulted in conflicts.
#     Your changes are safe in the stash.
# Match that specific phrasing. A loose "autostash AND conflict anywhere" test is
# WRONG: git prints "Created autostash: <sha>" up front for every dirty repo, so
# an ordinary rebase CONFLICT also contains both words — which would divert the
# real conflict away from the abort path and strand the repo mid-rebase.
_AUTOSTASH_CONFLICT_RE = re.compile(
    r"applying\s+autostash\s+resulted\s+in\s+conflicts",
    re.IGNORECASE,
)


def in_progress_operation(repo: Path) -> str | None:
    """Return an unfinished Git operation found from repository marker files.

    This is deliberately filesystem-only: the normal 141-repo pull scan must
    not gain another subprocess per repo. A .git file is resolved the same way
    as a worktree/submodule gitlink, with relative gitdir paths based at repo.
    Unreadable or malformed metadata returns None, which is fail-OPEN: the pull
    is attempted. That is the safe direction here — Git refuses on its own if an
    operation really is unfinished, and the post-failure abort path consults this
    same function, so an unreadable repo is never auto-aborted either. The cost
    is that such a repo can be left mid-rebase with Git's own error shown; the
    alternative (refusing to pull whenever metadata is odd) would silently strand
    healthy repos instead.
    """
    dot_git = repo / ".git"
    try:
        if dot_git.is_dir():
            git_dir = dot_git
        elif dot_git.is_file():
            first_line = dot_git.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()[0]
            prefix, separator, raw_path = first_line.partition(":")
            if separator != ":" or prefix.strip().lower() != "gitdir":
                return None
            raw_path = raw_path.strip()
            if not raw_path:
                return None
            git_dir = Path(raw_path)
            if not git_dir.is_absolute():
                git_dir = repo / git_dir
        else:
            return None

        if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
            return "rebase"
        if (git_dir / "MERGE_HEAD").exists():
            return "merge"
        if (git_dir / "CHERRY_PICK_HEAD").exists():
            return "cherry-pick"
        if (git_dir / "REVERT_HEAD").exists():
            return "revert"
    except (OSError, IndexError):
        return None
    return None


def _upstream_missing_on_remote(repo: Path) -> bool:
    """True if the current branch has upstream config but its upstream branch
    doesn't exist on the remote yet — i.e. a local branch not pushed.

    Confirms the benign "new local branch" case behind a failed pull, so we
    don't silence a genuinely broken upstream (deleted/renamed remote branch
    still shows a real error). Only called on the pull failure path — never on
    the happy path — so it adds no per-repo cost to a normal scan.
    """
    def _git(*a: str) -> subprocess.CompletedProcess:
        return proc.run(
            ["git", "-C", str(repo), *a],
            timeout=proc.T_QUICK,
        )

    head = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    branch = head.stdout.strip()
    if head.returncode != 0 or not branch:
        return False  # detached HEAD — not this case
    remote = _git("config", f"branch.{branch}.remote").stdout.strip()
    merge = _git("config", f"branch.{branch}.merge").stdout.strip()
    if not remote or not merge:
        return False  # no upstream configured — a different problem, keep the error
    merge_branch = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") else merge
    # If the branch were on the remote, fetch would maintain this tracking ref.
    tracking = f"refs/remotes/{remote}/{merge_branch}"
    exists = _git("rev-parse", "--verify", "--quiet", tracking)
    return exists.returncode != 0  # missing → not on remote → benign, push will create it


def _needs_push(repo: Path) -> bool:
    """True only when the current branch has something meaningful to push.

    A tracked branch needs a push when HEAD is ahead of its upstream. A branch
    without an upstream still gets one push attempt when it has a commit, which
    preserves first-push / push.autoSetupRemote behavior. Detection failures
    fail open so a real Git problem remains visible instead of being hidden.
    """
    # T_QUICK, not the transfer tier: both of these read local refs only and
    # never touch the network, so they must not inherit a transfer budget.
    # Their fail-open behavior on timeout/error is unchanged — a push is still
    # attempted so a real Git problem stays visible.
    ahead = proc.run(
        ["git", "-C", str(repo), "rev-list", "--count", "@{upstream}..HEAD"],
        timeout=proc.T_QUICK,
    )
    if ahead.returncode == 0:
        try:
            return int(ahead.stdout.strip()) > 0
        except ValueError:
            return True

    # No upstream is expected for a new branch/repository. Push it only if
    # HEAD exists; an unborn empty repository has nothing to send.
    head = proc.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD"],
        timeout=proc.T_QUICK,
    )
    if head.returncode == 0:
        return True
    if head.returncode == 1 and not head.stdout.strip() and not head.stderr.strip():
        return False  # --quiet's normal result for an unborn branch
    return True


def _run_one(repo: Path, op: str, *, rebase: bool = True) -> OpResult:
    """Run a single git op. Returns OpResult — never raises."""
    if op == "pull":
        op_name = in_progress_operation(repo)
        if op_name is not None:
            return OpResult(
                repo=repo,
                ok=False,
                code=1,
                detail=f"存在未完成的 {op_name}，已跳过（请先手动收尾）",
                # Deterministic: the marker files are still there a second later.
                retryable=False,
            )

    if op == "push" and not _needs_push(repo):
        return OpResult(repo=repo, ok=True, code=0, detail="无待推送提交", skipped=True)

    args = ["git", "-C", str(repo), op]
    # Quieter output, but keep errors.
    if op == "pull":
        if rebase:
            args += ["--rebase", "--autostash", "--quiet"]
        else:
            args += ["--ff-only", "--quiet"]
    elif op == "push":
        args += ["--quiet"]

    try:
        r = proc.run(args, timeout=_OP_TIMEOUT_SEC)
        ok = r.returncode == 0
        combined = (r.stderr or "") + "\n" + (r.stdout or "")
        if not ok and op == "pull" and _PULL_NO_REMOTE_REF_RE.search(combined) \
                and _upstream_missing_on_remote(repo):
            # Local branch not yet on the remote — the push pass will create it.
            return OpResult(repo=repo, ok=True, code=0, detail="新分支·待推送", skipped=True)
        # Repository STATE decides between the two rebase failure shapes, not the
        # message text: a stranded rebase must be rolled back, while an autostash
        # that failed to re-apply has nothing to abort and holds the user's work
        # in a stash entry. The pre-guard above proved no rebase was running
        # before this pull, so anything in progress now is ours to abort.
        if op == "pull" and rebase and in_progress_operation(repo) == "rebase":
            abort = proc.run(
                ["git", "-C", str(repo), "rebase", "--abort"],
                timeout=proc.T_LOCAL,
            )
            if abort.returncode == 0:
                return OpResult(
                    repo=repo,
                    ok=False,
                    code=r.returncode,
                    detail="rebase 冲突，已回滚到同步前状态（需人工处理）",
                    # A content conflict is deterministic: retrying just pays a
                    # second full network pull to conflict and abort again.
                    retryable=False,
                )
            return OpResult(
                repo=repo,
                ok=False,
                code=r.returncode,
                # This is the single most actionable message codesync emits, and
                # it MUST survive into the summary — hence retryable=False. The
                # retry used to re-run this repo, hit the pre-guard (a rebase is
                # now in progress) and overwrite it with the far vaguer
                # "存在未完成的 rebase，已跳过", losing the exact command.
                retryable=False,
                detail=(
                    "rebase 冲突且自动回滚失败，仓库停留在 rebase 中间态；"
                    f"请手动运行：git -C \"{repo}\" rebase --abort"
                ),
            )
        if op == "pull" and rebase and _AUTOSTASH_CONFLICT_RE.search(combined):
            return OpResult(
                repo=repo,
                ok=False,
                code=r.returncode or 1,
                detail="autostash 应用冲突，你的改动在 stash 里（`git stash list`）",
                # The stash entry and conflict will still need manual handling;
                # a second pull cannot make that deterministic state disappear.
                retryable=False,
            )
        detail = "" if ok else _short_err(r.stderr or "", r.stdout or "")
        return OpResult(repo=repo, ok=ok, code=r.returncode, detail=detail)
    except Exception as e:  # last-resort safety net
        return OpResult(repo=repo, ok=False, code=1, detail=str(e)[:120])


def _execute_pass(repos: list[Path], op: str, max_workers: int, label: str = "",
                  *, rebase: bool = True) -> list[OpResult]:
    """Run one parallel pass over repos, printing per-repo progress. Returns all results."""
    total = len(repos)
    width = len(str(total))
    done = 0
    results: list[OpResult] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_run_one, r, op, rebase=rebase): r
            for r in repos
        }
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                done += 1
                idx = done
                results.append(res)
            name = res.repo.name
            if res.skipped:
                tag = output.hilite("·", "gray")
            elif res.ok:
                tag = output.hilite("✓", "green")
            else:
                tag = output.hilite("✗", "red")
            prefix = f"  {label}[{idx:>{width}}/{total}] {tag} {name}"
            if res.skipped:
                output.info(f"{prefix}  {output.hilite(res.detail, 'gray')}")
            elif res.ok:
                output.info(prefix)
            else:
                output.info(f"{prefix}  {output.hilite(res.detail, 'yellow')}")
    return results


def parallel_op(repos: list[Path], op: str, *, max_workers: int = 8,
                rebase: bool = True) -> OpSummary:
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

    results = _execute_pass(repos, op, max_workers, rebase=rebase)
    failed = [r for r in results if not r.ok]
    non_retryable = [r for r in failed if not r.retryable]
    retryable = [r for r in failed if r.retryable]

    if retryable:
        retry_repos = [r.repo for r in retryable]
        output.detail(f"重试 {len(retry_repos)} 个失败的 {op}（串行，规避并发 SSH 限流）...")
        time.sleep(_RETRY_DELAY_SEC)
        retry_results = _execute_pass(
            retry_repos, op, max_workers=1, label="retry ", rebase=rebase,
        )
        failed = non_retryable + [r for r in retry_results if not r.ok]
    else:
        failed = non_retryable

    elapsed = time.monotonic() - t0
    return OpSummary(op=op, total=total, ok=total - len(failed), failed=failed, elapsed=elapsed)


def print_summary(s: OpSummary) -> None:
    if s.total == 0:
        return
    color = "green" if not s.failed else ("yellow" if s.ok else "red")
    msg = f"{s.op}: {s.ok}/{s.total} OK，耗时 {s.elapsed:.1f}s"
    output.info(output.hilite(f"  {msg}", color))


def default_local_workers() -> int:
    """Default concurrency for local-only Git metadata operations."""
    return min(32, (os.cpu_count() or 4) * 4)


def default_net_workers(*, multiplexed: bool = False) -> int:
    """Default concurrency for network Git operations.

    With a shared ControlMaster, N concurrent git processes ride ONE TCP
    connection, so concurrency is close to free — hence the higher value.

    Without it each op dials its own connection, but 4 is still the right
    default rather than 1. v2.19.1 chose 1 to "避免 VPS 短时间并发建立大量
    Git/SSH 外连", and that reasoning quietly became a serious cost: Windows
    OpenSSH has no ControlMaster at all, so `multiplexed` is ALWAYS False
    there and every repository was pulled strictly one at a time. At the
    6.6-10.2s per-handshake figure measured for ssh.github.com:443 without
    reuse, a 141-repo sync spent 15-24 minutes on handshakes alone, serially —
    which is exactly the "太长就失去意义" failure mode.

    Four concurrent connections is not "大量外连"; it is what an ordinary
    `git fetch --all` in a few terminals already does. And the case v2.19.1
    was protecting against — intermittent GitHub throttling surfacing as
    bogus "Repository not found" — is already handled downstream by
    parallel_op's serial retry pass, which exists for precisely that.

    Override with `--workers N` or [sync].net_workers if a constrained host
    needs the old behavior; delete/rename still pass 1 explicitly.
    """
    return 8 if multiplexed else 4


def _is_dirty(repo: Path) -> bool:
    r = proc.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        timeout=proc.T_QUICK,
    )
    # A timeout means "unknown", and unknown must read as DIRTY: this gates
    # delete's pre-trash push, where a wrong "clean" would trash uncommitted work.
    if proc.timed_out(r):
        return True
    # A plain non-zero rc (half-deleted husk, not a repository) is NOT dirty —
    # those are surfaced by find_corrupt_repos, and treating them as dirty would
    # make every run attempt a doomed add/commit on them.
    return r.returncode == 0 and bool(r.stdout.strip())


def auto_commit_dirty(repos: list[Path], skip_names: set[str], *, max_workers: int = 8,
                      exclude_map: dict[Path, set[str]] | None = None) -> list[str]:
    """`git add -A` + commit every dirty repo (clean repos and skip_names skipped).

    Run BEFORE pull so user work is recorded before history changes; the pull's
    rebase then replays that local commit on the remote tip. Also runs before
    push so the new commit gets uploaded. Returns the list of committed repo
    names. Never raises — per-repo failure is logged.

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
        add = proc.run(
            ["git", "-C", str(repo), "add", "-A"],
            timeout=proc.T_LOCAL,
        )
        if add.returncode != 0:
            output.warn(f"  ✗ {repo.name}: git add 失败 {_short_err(add.stderr or '', add.stdout or '')}")
            continue
        # Unstage any nested-repo gitlink so the outer doesn't commit a moving
        # pointer (the nested repo syncs on its own). See exclude_map docstring.
        excl = exclude_map.get(repo) if exclude_map else None
        if excl:
            reset = proc.run(
                ["git", "-C", str(repo), "reset", "-q", "--", *excl],
                timeout=proc.T_QUICK,
            )
            if reset.returncode != 0:
                output.warn(
                    f"  ✗ {repo.name}: 嵌套 gitlink 撤销暂存失败 "
                    f"{_short_err(reset.stderr or '', reset.stdout or '')}"
                )
                continue
        # `git add -A` may stage nothing even though the repo is "dirty" — the
        # classic case is a dirty submodule / embedded git repo: the superproject
        # sees ` M <gitlink>` but there's no new commit pointer to record, so
        # there is genuinely nothing to commit. Committing anyway just fails with
        # "no changes added to commit", which used to read as a hard error every
        # run. Detect the empty stage and report it honestly instead.
        staged = proc.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            timeout=proc.T_QUICK,
        )
        if proc.timed_out(staged) or staged.returncode in {proc.NOTFOUND_RC, proc.OSERR_RC}:
            output.warn(f"  ✗ {repo.name}: 无法确认暂存状态，跳过 commit")
            continue
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
        com = proc.run(
            ["git", "-C", str(repo), "commit", "-m", msg],
            timeout=proc.T_LOCAL,
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
    porcelain = proc.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        timeout=proc.T_QUICK,
    )
    if porcelain.returncode != 0:
        return []
    # Paths git reports as changed (strip the 2-char XY status + space).
    changed = [ln[3:].strip() for ln in porcelain.stdout.splitlines() if ln.strip()]
    if not changed:
        return []
    # Which of those are gitlinks (mode 160000)?
    ls = proc.run(
        ["git", "-C", str(repo), "ls-files", "-s", "--", *changed],
        timeout=proc.T_QUICK,
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
