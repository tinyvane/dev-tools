"""`codesync delete` — remove a local repo and archive it on GitHub (v2.9.0).

The archive IS the cross-machine delete signal: github_auto already treats an
archived repo (gone from the `active` list) as "remove it locally" on every
other machine (`to_rm_local = known ∩ local ∩ ¬active`, with its own 5s
countdown). So this command only needs to, on THIS machine:

  1. (safety) commit + push any local work first, so GitHub's archived copy is
     current — archive is recoverable, an un-pushed local change would not be.
  2. `gh repo archive owner/name` — flips it out of `active`.
  3. delete the local folder.

An explicit single-repo delete intentionally bypasses the bulk-delete guards in
github_auto (abort_if_local_missing_pct) — those exist to catch ACCIDENTAL mass
local disappearance, not a deliberate `codesync delete`.

Archive (not delete) on the GitHub side is deliberate: it's reversible
(unarchive) and is exactly the signal codesync already keys off. We do NOT
rename/prefix the repo — a rename would be picked up by detect_and_migrate as a
move, not a delete.

The Claude conversation dir under ~/.claude/projects is left untouched (history
is Dropbox-shared and small; deletion would be irreversible and propagate).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from codesync import git_ops, output
from codesync.git_ops import rmtree_repo as _rmtree_safe  # impl shared with github_auto
from codesync.rename import (
    _ahead_count, _find_in_roots, _gh_canonical_name, _is_git_repo,
    _origin_url, _parse_remote,
)


def _gh_archive(owner: str, name: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["gh", "repo", "archive", f"{owner}/{name}", "--yes"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def _countdown(action: str) -> bool:
    """5s countdown; Ctrl+C aborts. Returns True to proceed."""
    output.info(f"  5 秒后{action}（Ctrl+C 取消）...")
    try:
        for i in range(5, 0, -1):
            output.detail(f"    {i}...")
            time.sleep(1)
    except KeyboardInterrupt:
        output.info("已取消。")
        return False
    return True


def delete_repo(name: str | None, *, yes: bool = False) -> int:
    """`codesync delete` (in the repo dir) or `codesync delete <name>`."""
    if name is None:
        repo = Path.cwd()
        if not _is_git_repo(repo):
            output.err(f"当前目录不是 git repo: {repo}")
            output.detail("请进入要删除的 repo 目录，或用 `codesync delete <名字>`。")
            return 1
    else:
        from codesync.config import load
        cfg = load()
        matches = _find_in_roots(name, cfg.code_roots_expanded)
        if not matches:
            output.err(f"在 code_roots 下找不到名为 {name} 的目录。")
            return 1
        if len(matches) > 1:
            output.err(f"多个 code_root 下都有 {name}，请 cd 进目标目录用无参数形式：")
            for m in matches:
                output.detail(f"  - {m}")
            return 1
        repo = matches[0]

    repo_name = repo.name
    origin = _origin_url(repo) if _is_git_repo(repo) else None
    parsed = _parse_remote(origin) if origin else None  # (host, owner, name)
    is_github = bool(parsed) and parsed[0].endswith("github.com")

    # Redirect guard: if the origin NAME is stale (the repo was renamed on
    # GitHub), `gh repo archive <old-name>` follows the 301 and would archive
    # the CURRENT repo under its new name — i.e. deleting a leftover folder
    # whose origin says `UIdesigner` would archive the kept `20260313-UIdesigner`.
    # In that case touch nothing remote: local delete only.
    do_remote = is_github
    canonical = None
    if is_github:
        host, owner, gh_name = parsed
        canonical = _gh_canonical_name(owner, gh_name)
        if canonical and canonical.lower() != gh_name.lower():
            do_remote = False

    # Show the plan.
    output.section(f"删除 repo: {repo_name}")
    output.detail(f"本地目录: {repo}")
    if is_github and not do_remote:
        host, owner, gh_name = parsed
        output.warn(f"GitHub 上 {owner}/{gh_name} 已改名为 {canonical} —— 这个目录的 origin 已过期。")
        output.warn(f"为避免经重定向误归档现用的 {canonical}，只删本地目录，不动 GitHub、不推送。")
    elif is_github:
        host, owner, gh_name = parsed
        output.detail(f"GitHub:   {owner}/{gh_name}  → 将 archive（可恢复，其他机器 sync 时会跟着删本地）")
    elif origin:
        output.warn(f"origin 非 GitHub（{origin}）— 只删本地，不归档；其他机器不会自动删。")
    else:
        output.warn("无 origin / 无 .git — 只删本地目录。")
    output.warn("注意: 未追踪 / .gitignore 的本地数据（如 .env、数据文件）会一并丢失，且不在 GitHub 上。")

    # Safety: commit + push unsynced work first so the archived copy is current.
    # (Skipped on a stale-name redirect — pushing there would shove this old
    # copy's branches into the kept repo.)
    if _is_git_repo(repo) and do_remote:
        dirty = git_ops._is_dirty(repo)
        ahead = _ahead_count(repo)
        if dirty or ahead > 0:
            bits = []
            if dirty:
                bits.append("有未提交改动")
            if ahead > 0:
                bits.append(f"有 {ahead} 个未 push 的 commit")
            output.warn(f"{repo_name} {'，'.join(bits)} — 删除前先 commit + push，确保归档副本是最新的。")

    if not yes and not _countdown(f"归档并删除 {repo_name}" if do_remote else f"删除本地 {repo_name}"):
        return 1

    # 1. commit + push any local work (only meaningful for a github repo we'll archive).
    if _is_git_repo(repo) and do_remote:
        if git_ops._is_dirty(repo):
            git_ops.auto_commit_dirty([repo], skip_names=set())
        summary = git_ops.parallel_op([repo], "push", max_workers=1)
        if summary.failed:
            output.warn(f"{repo_name} push 失败 — 仍继续归档+删除（GitHub 已有的提交会被归档保留）。")

    # 2. archive on GitHub (the cross-machine delete signal).
    if do_remote:
        host, owner, gh_name = parsed
        ok, msg = _gh_archive(owner, gh_name)
        if ok:
            output.good(f"GitHub {owner}/{gh_name} 已 archive。")
            # Tombstone: if the repo is later unarchived on the web, this
            # machine must not auto-clone it back (the delete→re-clone flap).
            from codesync import github_auto
            github_auto.add_tombstone(gh_name)
        else:
            output.warn(f"archive 失败（{msg}）— 仍删本地；其他机器不会自动删。"
                        "（若是你自己的 repo，下次 sync 会再尝试归档。）")

    # 3. delete the local folder.
    ok, msg = _rmtree_safe(repo)
    if not ok:
        output.err(f"删除本地目录失败: {msg}")
        return 1
    output.good(f"已删除本地目录: {repo}")
    if do_remote:
        output.detail("其他机器下次 `codesync sync` 会检测到归档并删除各自的本地副本。")
    return 0
