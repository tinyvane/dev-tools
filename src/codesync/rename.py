"""Rename a repo — locally and on GitHub — and auto-migrate renames done elsewhere.

Two entry points:

  rename_repo(names)        `codesync rename` — YOU rename a repo on THIS machine.
                            Renames it on GitHub (`gh repo rename`), moves the local
                            directory, and rewrites origin. Three remote situations:
                              - origin is github.com  → full rename (GitHub + local)
                              - origin is non-GitHub  → refuse to touch the remote,
                                offer to rename just the local directory
                              - no origin (orphan)    → local directory rename only

  detect_and_migrate(...)   Called from github_auto during `sync` — picks up renames
                            done on ANOTHER machine and applies them locally (mv dir +
                            origin set-url). Cheap: only repos whose origin name no
                            longer exists on GitHub cost an extra `gh api` call.

Order for a full rename is deliberate: commit/push (while everything is still
consistent under the OLD name) → GitHub rename → local mv → origin set-url. The
GitHub rename is the only step that realistically fails (name clash / no access);
doing it first means a failure aborts before we touch anything local.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from codesync import auth, git_ops, output, paths


# git@github.com:owner/name.git  |  https://github.com/owner/name(.git)
_REMOTE_RE = re.compile(
    r"^(?:git@|ssh://git@|https?://)([^/:]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$"
)


# ---------- origin parsing ----------

def _origin_url(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    url = r.stdout.strip()
    return url if (r.returncode == 0 and url) else None


def _parse_remote(url: str) -> tuple[str, str, str] | None:
    """(host, owner, name) from a remote URL, or None if unrecognized."""
    m = _REMOTE_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _valid_name(name: str) -> bool:
    return bool(name) and "/" not in name and not re.search(r"\s", name)


# ---------- gh helpers ----------

def _gh_repo_exists(owner: str, name: str) -> bool:
    r = subprocess.run(
        ["gh", "repo", "view", f"{owner}/{name}", "--json", "name"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _gh_repo_rename(owner: str, oldname: str, new: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["gh", "repo", "rename", new, "--repo", f"{owner}/{oldname}", "--yes"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def _gh_new_ssh_url(owner: str, name: str) -> str | None:
    r = subprocess.run(
        ["gh", "repo", "view", f"{owner}/{name}", "--json", "sshUrl", "--jq", ".sshUrl"],
        capture_output=True, text=True,
    )
    url = r.stdout.strip()
    return url if (r.returncode == 0 and url) else None


def _gh_canonical_name(owner: str, name: str) -> str | None:
    """Resolve a possibly-renamed repo to its current name.

    `gh api repos/{owner}/{name}` follows GitHub's 301 redirect for a renamed
    repo and returns the new name in `.name`. Returns None on 404 (the repo was
    deleted/archived-away, not renamed) or any error.
    """
    r = subprocess.run(
        ["gh", "api", f"repos/{owner}/{name}", "--jq", ".name"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


# ---------- local migration primitive ----------

def _set_origin(repo: Path, url: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def _move_dir(src: Path, dst: Path) -> tuple[bool, str]:
    if dst.exists():
        return False, f"目标目录已存在: {dst}"
    # Windows can't rename a directory that is (or contains) the process CWD —
    # the handle is held open. Step out to the parent first, then rename.
    try:
        cwd = Path.cwd().resolve()
        src_res = src.resolve()
        if cwd == src_res or src_res in cwd.parents:
            os.chdir(src_res.parent)
    except OSError:
        pass
    try:
        src.rename(dst)
    except OSError as e:
        return False, f"目录改名失败: {e}"
    return True, ""


# ---------- Claude Code conversation directory ----------
#
# Claude Code stores a repo's conversation transcripts under
# ~/.claude/projects/<mangled-abs-path>/, where the directory name is the repo's
# absolute path with ':' '/' '\' all replaced by '-'. When the repo moves, that
# directory name no longer matches the new path, so Claude treats it as a fresh
# empty project and the history is orphaned. We rename it to follow the repo.
#
# For users who sync ~/.claude/projects across machines (Dropbox + junction),
# this is SHARED storage: a rename on one machine propagates to the rest. So the
# rename is idempotent — if the target already exists (another machine + Dropbox
# already did it), we skip. See CLAUDE.md "Repo 改名".

def _claude_project_dirname(abs_path: str) -> str:
    return re.sub(r"[:/\\]", "-", abs_path)


def _resolve_claude_projects(rcfg) -> Path | None:
    """Resolve the configured Claude projects dir, or None if disabled/missing."""
    if rcfg is None or not getattr(rcfg, "sync_claude_projects", False):
        return None
    raw = getattr(rcfg, "claude_projects_dir", "") or "~/.claude/projects"
    d = Path(paths.expand(raw))
    return d if d.is_dir() else None


def _find_ci(projects: Path, name: str) -> Path | None:
    """Find a child dir of `projects` whose name equals `name` (case-insensitive)."""
    direct = projects / name
    if direct.exists():
        return direct
    low = name.lower()
    try:
        for entry in projects.iterdir():
            if entry.name.lower() == low:
                return entry
    except OSError:
        return None
    return None


def _rename_claude_project(projects: Path, old_abs: str, new_abs: str) -> None:
    """Idempotently rename the conversation dir to follow a repo move. Best-effort:
    never raises — a failure here must not derail the repo rename itself."""
    old_name = _claude_project_dirname(old_abs)
    new_name = _claude_project_dirname(new_abs)
    if old_name == new_name:
        return
    src = _find_ci(projects, old_name)
    if src is None:
        return  # no history for this repo (or already migrated away)
    if _find_ci(projects, new_name) is not None:
        return  # target already exists (Dropbox already propagated it) — skip
    try:
        src.rename(projects / new_name)
        output.detail(f"Claude 对话目录: {src.name} → {new_name}")
    except OSError as e:
        output.warn(f"Claude 对话目录改名失败（不影响 repo 改名）: {e}")


def _load_rename_cfg():
    """Load [rename] config tolerantly — returns defaults if no config file yet."""
    from codesync import config as cfg_mod
    try:
        return cfg_mod.load().rename
    except SystemExit:
        return cfg_mod.RenameConfig()


# ---------- entry point 1: manual rename (this machine) ----------

def _find_in_roots(name: str, roots: list[Path]) -> list[Path]:
    matches: list[Path] = []
    for root in roots:
        cand = root / name
        if cand.is_dir():
            matches.append(cand)
    return matches


def _handle_dirty_before_rename(repo: Path) -> bool:
    """If the repo has uncommitted or unpushed work, warn + 5s countdown, then
    auto-commit (if dirty) and push. Ctrl+C during the countdown aborts the whole
    rename. Returns True to proceed, False if the user aborted.
    """
    dirty = git_ops._is_dirty(repo)
    ahead = _ahead_count(repo)
    if not dirty and ahead == 0:
        return True

    bits = []
    if dirty:
        bits.append("有未提交改动")
    if ahead > 0:
        bits.append(f"有 {ahead} 个未 push 的 commit")
    output.warn(f"{repo.name} {'，'.join(bits)} — 改名前先帮你提交并推送。")
    output.info("  5 秒后自动 commit + push（Ctrl+C 取消整个改名）...")
    try:
        for i in range(5, 0, -1):
            output.detail(f"    {i}...")
            time.sleep(1)
    except KeyboardInterrupt:
        output.info("已取消改名。")
        return False

    if dirty:
        git_ops.auto_commit_dirty([repo], skip_names=set())
    # Push the (possibly newly-committed) work to the OLD name, while consistent.
    summary = git_ops.parallel_op([repo], "push", max_workers=1)
    if summary.failed:
        output.warn(f"{repo.name} push 失败 — 仍继续改名（远端旧名重定向会兜底）。")
    return True


def _ahead_count(repo: Path) -> int:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "@{u}..HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return 0  # no upstream configured → can't tell; don't block on it
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def _local_only_rename(repo: Path, new: str, why: str, projects: Path | None) -> int:
    """Rename just the local directory (no remote coordination)."""
    output.detail(why)
    output.info(f"  5 秒后将本地目录改名为 {new}（Ctrl+C 取消）...")
    try:
        for i in range(5, 0, -1):
            output.detail(f"    {i}...")
            time.sleep(1)
    except KeyboardInterrupt:
        output.info("已取消。")
        return 1
    old_abs = str(repo)
    new_path = repo.parent / new
    ok, msg = _move_dir(repo, new_path)
    if not ok:
        output.err(msg)
        return 1
    output.good(f"本地目录已改名: {repo.name} → {new}")
    if projects:
        _rename_claude_project(projects, old_abs, str(new_path))
    return 0


def rename_repo(names: list[str]) -> int:
    """`codesync rename <new>` (in repo dir) or `codesync rename <old> <new>`."""
    if len(names) == 1:
        new = names[0]
        repo = Path.cwd()
        old = repo.name
        if not _is_git_repo(repo):
            output.err(f"当前目录不是 git repo: {repo}")
            output.detail("请进入要改名的 repo 目录，或用 `codesync rename <旧名> <新名>`。")
            return 1
    elif len(names) == 2:
        old, new = names
        from codesync.config import load
        cfg = load()
        matches = _find_in_roots(old, cfg.code_roots_expanded)
        if not matches:
            output.err(f"在 code_roots 下找不到名为 {old} 的目录。")
            return 1
        if len(matches) > 1:
            output.err(f"多个 code_root 下都有 {old}，请 cd 进目标目录用单参数形式：")
            for m in matches:
                output.detail(f"  - {m}")
            return 1
        repo = matches[0]
    else:
        output.err("用法：codesync rename <新名>  或  codesync rename <旧名> <新名>")
        return 2

    if not _valid_name(new):
        output.err(f"非法的新名字: {new!r}（不能为空、含空白或 '/'）")
        return 1
    if new == old:
        output.err("新名字和旧名字相同。")
        return 1
    if (repo.parent / new).exists():
        output.err(f"目标目录已存在: {repo.parent / new}")
        return 1

    projects = _resolve_claude_projects(_load_rename_cfg())

    # No .git → a not-yet-published orphan: local rename only.
    if not _is_git_repo(repo):
        return _local_only_rename(repo, new, "该目录没有 .git（尚未发布），只改本地目录名。", projects)

    origin = _origin_url(repo)
    # No origin → local rename only (nothing remote to coordinate).
    if not origin:
        return _local_only_rename(repo, new, "该 repo 没有 origin 远端，只改本地目录名。", projects)

    parsed = _parse_remote(origin)
    # Non-GitHub origin → refuse to touch the remote; offer local-only rename.
    if not parsed or parsed[0] != "github.com":
        host = parsed[0] if parsed else "未知"
        return _local_only_rename(
            repo, new,
            f"origin 指向非 GitHub 远端（{host}），不会改远端。仅改本地目录名。",
            projects,
        )

    _, owner, oldname = parsed

    # gh must be available + authed for a GitHub rename.
    if not auth.ensure_gh_authenticated():
        output.err("gh 未认证，无法在 GitHub 上改名。")
        return 1

    # Reject if the target name already exists on GitHub (rename would fail / clobber).
    if _gh_repo_exists(owner, new):
        output.err(f"GitHub 上已存在 {owner}/{new}，换个名字。")
        return 1

    output.section(f"改名 {owner}/{oldname} → {owner}/{new}")

    # Dirty/unpushed → commit + push first (or abort on Ctrl+C).
    if not _handle_dirty_before_rename(repo):
        return 1

    # 1. GitHub first (the only step that realistically fails). Abort if it does.
    ok, msg = _gh_repo_rename(owner, oldname, new)
    if not ok:
        output.err(f"gh repo rename 失败，未改动本地: {msg}")
        return 1
    output.good(f"GitHub: {owner}/{oldname} → {owner}/{new}")

    # 2. Local directory move.
    old_abs = str(repo)
    new_path = repo.parent / new
    ok, msg = _move_dir(repo, new_path)
    if not ok:
        output.warn(f"GitHub 已改名，但本地目录改名失败: {msg}")
        output.detail(f"请手动把 {repo} 改名为 {new_path}，并更新 origin。")
        return 1

    # 3. Rewrite origin to the new URL.
    new_url = _gh_new_ssh_url(owner, new) or _GH_SSH.format(owner=owner, name=new)
    ok, msg = _set_origin(new_path, new_url)
    if not ok:
        output.warn(f"origin 更新失败（旧 URL 仍可经重定向工作）: {msg}")
    output.good(f"本地: {repo} → {new_path}（origin → {new_url}）")

    # 4. Follow the move with the Claude conversation dir (best-effort).
    if projects:
        _rename_claude_project(projects, old_abs, str(new_path))
    return 0


_GH_SSH = "git@github.com:{owner}/{name}.git"


# ---------- entry point 2: auto-migrate renames done elsewhere ----------

def detect_and_migrate(
    local_owned: dict[str, Path], active: dict[str, str], owner: str,
    *, claude_projects: Path | None = None,
) -> list[tuple[str, str]]:
    """Migrate repos that were renamed on another machine.

    `local_owned` maps origin-parsed repo name → local path (from github_auto's
    scan). `active` maps current GitHub repo name → sshUrl. A local repo whose
    origin name is NOT in `active` is suspicious: it was renamed, archived, or
    deleted on GitHub. We resolve it with one `gh api` call (follows the rename
    redirect); only a genuine rename (resolves to a different, still-active name)
    triggers a local mv + origin set-url. Returns the (old, new) pairs migrated.

    Cost note: the extra `gh api` fires only for names missing from `active` —
    normally zero, so this stays cheap on every sync.
    """
    migrations: list[tuple[str, str]] = []
    active_names = set(active.keys())
    for oldname, path in list(local_owned.items()):
        if oldname in active_names:
            continue  # still present under this name → fine
        canonical = _gh_canonical_name(owner, oldname)
        if not canonical or canonical == oldname:
            continue  # 404 (deleted/archived) or unchanged → not a rename, leave it
        if canonical not in active_names:
            continue  # resolved to something not in our active set → don't guess
        # Genuine rename: oldname → canonical.
        new_url = active.get(canonical)
        if new_url:
            ok, msg = _set_origin(path, new_url)
            if not ok:
                output.warn(f"改名迁移 {oldname} → {canonical}: origin 更新失败 {msg}")
                continue
        # Rename the directory too, but only when it matches the old repo name
        # (codesync's clone/publish convention) and the target is free.
        new_path = path
        if path.name == oldname:
            target = path.parent / canonical
            old_abs = str(path)
            ok, msg = _move_dir(path, target)
            if not ok:
                output.warn(f"改名迁移 {oldname} → {canonical}: {msg}（origin 已更新）")
            else:
                new_path = target
                # The working dir actually moved → follow with the conversation dir.
                if claude_projects:
                    _rename_claude_project(claude_projects, old_abs, str(target))
        migrations.append((oldname, canonical))
        output.good(f"改名迁移: {oldname} → {canonical}  ({new_path})")
    return migrations
