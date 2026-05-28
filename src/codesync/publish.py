"""Auto-publish "orphan" local directories to GitHub.

Closes the gap left by auto_clone: that one syncs GitHub → local. This one
syncs local → GitHub for stuff that exists only on your machine.

Scope of "orphan":
    a) A subdirectory of any code_root that has files but no `.git/` —
       e.g. `mkdir new-project && drop some files in`. We treat this as
       "you've started a new project, codesync will git-init + publish".
    b) A subdirectory with `.git/` but no `origin` remote — e.g. you ran
       `git init` by hand but never pushed anywhere.

Not orphan:
    - Has `.git/` and an `origin` remote → already tracked.
    - Empty directory → nothing to commit, skip.
    - Hidden directory (name starts with .) → not a project.
    - Name matches obvious build artifacts (node_modules, __pycache__,
      .venv, venv, dist, build) → skip even if non-empty.

Safety:
    - Lists all candidates before doing anything.
    - 5-second Ctrl+C-able countdown unless `[publish] skip_confirmation = true`.
    - Per-candidate failure (gh repo name conflict, push reject, etc.) is
      logged and we continue; one bad candidate doesn't kill the rest.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from codesync import auth, output


# Direct children of code_roots that look like build / venv / cache artifacts.
# We skip these even when non-empty — they're never user projects.
NEVER_PUBLISH_NAMES: frozenset[str] = frozenset({
    "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode",
})


@dataclass
class OrphanCandidate:
    path: Path
    name: str
    has_git: bool        # True if .git already exists (need only publish, no init)
    reason: str           # human-readable summary for the listing


def find_orphan_candidates(code_roots: list[Path], skip: set[str]) -> list[OrphanCandidate]:
    """Scan code_roots one level deep for publishable orphans. See module docstring."""
    candidates: list[OrphanCandidate] = []
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
            if entry.name.startswith("."):
                continue
            if entry.name in NEVER_PUBLISH_NAMES or entry.name in skip:
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            # Empty (or only-dotfile-and-empty) dir → nothing to commit later.
            try:
                if not any(entry.iterdir()):
                    continue
            except OSError:
                continue

            has_git = (entry / ".git").exists()
            if has_git:
                # Has .git but maybe no origin → still an orphan.
                r = subprocess.run(
                    ["git", "-C", str(entry), "remote", "get-url", "origin"],
                    capture_output=True, text=True,
                )
                if r.returncode == 0 and r.stdout.strip():
                    continue  # already has origin; not an orphan
                candidates.append(OrphanCandidate(
                    path=entry, name=entry.name, has_git=True,
                    reason="git repo without origin remote",
                ))
            else:
                candidates.append(OrphanCandidate(
                    path=entry, name=entry.name, has_git=False,
                    reason="directory without .git/",
                ))
    return candidates


def _gh_repo_exists(owner: str, name: str) -> bool:
    """True if owner/name already exists on GitHub (so we don't try to create over it)."""
    r = subprocess.run(
        ["gh", "repo", "view", f"{owner}/{name}", "--json", "name"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def publish_one(candidate: OrphanCandidate, owner: str) -> tuple[bool, str]:
    """Run the full publish flow for one candidate. Returns (success, message)."""
    repo_dir = candidate.path
    name = candidate.name

    if _gh_repo_exists(owner, name):
        return False, f"GitHub 上已有 {owner}/{name}（改名或手动处理）"

    if not candidate.has_git:
        # git init + add + commit before gh repo create --source=. expects HEAD
        r = subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"git init 失败: {r.stderr.strip() or r.stdout.strip()}"
        r = subprocess.run(["git", "-C", str(repo_dir), "add", "."],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"git add 失败: {r.stderr.strip() or r.stdout.strip()}"
        # If `git add .` staged nothing (e.g., only .gitignore matched), commit would fail.
        # Use --quiet 不 produce diff output; rely on exit code 0 = nothing staged.
        r = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"])
        if r.returncode == 0:
            return False, "无可提交内容（git add . 没暂存任何文件）"
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m", "Initial commit"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, f"git commit 失败: {r.stderr.strip() or r.stdout.strip()}"

    # `gh repo create --source=. --remote=origin --push` does add remote + push in one shot.
    r = subprocess.run(
        ["gh", "repo", "create", f"{owner}/{name}",
         "--private", "--source=.", "--remote=origin", "--push"],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"gh repo create 失败: {(r.stderr or r.stdout).strip()}"
    return True, f"{owner}/{name} (private)"


def publish_orphans(cfg) -> int:
    """Entry point called from sync.run_sync().

    Detects orphans, prints them, runs the safety countdown (unless config says
    skip), then publishes each. Returns count of successful publishes.

    Bails to 0 (cleanly, no error) if:
        - No candidates
        - gh CLI missing or not authed
        - Owner can't be derived from config or gh
    """
    publish_cfg = getattr(cfg, "publish", None)
    skip_names: set[str] = set(publish_cfg.skip) if publish_cfg else set()
    skip_confirmation: bool = bool(publish_cfg.skip_confirmation) if publish_cfg else False

    candidates = find_orphan_candidates(cfg.code_roots_expanded, skip_names)
    if not candidates:
        return 0

    output.section(f"发布本地孤儿目录 ({len(candidates)})")
    for c in candidates:
        marker = "[init+push]" if not c.has_git else "[push only]"
        output.detail(f"  {marker} {c.name}  ({c.reason})")

    # Need gh for repo create; bail cleanly if not available.
    if not auth.ensure_gh_authenticated():
        output.warn("gh 未认证 — 跳过 publish。其他 sync 步骤继续。")
        return 0

    owner: str | None = None
    if cfg.auto_clone:
        owner = cfg.auto_clone.owner
    if not owner:
        owner = auth.gh_username()
    if not owner:
        output.warn("拿不到 GitHub owner — 跳过 publish。")
        return 0

    if not skip_confirmation:
        output.info("  即将创建 private repo，5 秒后开始（Ctrl+C 取消）...")
        try:
            for i in range(5, 0, -1):
                output.detail(f"    {i}...")
                time.sleep(1)
        except KeyboardInterrupt:
            output.info("已取消 publish。")
            return 0

    success = 0
    for c in candidates:
        output.detail(f"[{c.name}] publishing...")
        ok, msg = publish_one(c, owner)
        if ok:
            output.good(f"[{c.name}] ✓ {msg}")
            success += 1
        else:
            output.warn(f"[{c.name}] {msg}")
    return success
