"""Windows reserved device names (nul/con/...) left behind by POSIX shells.

Real Windows behaviour (Git for Windows, verified on the user's PC):

    > git add -A
    error: short read while indexing nul
    error: nul: failed to insert into database
    error: unable to index file 'nul'
    fatal: adding files failed          (exit 128 — nothing at all is staged)

These tests run on every OS, so the Windows-only gate is forced on through
`reserved_names.applies` and the failing `git add` is simulated.
"""
from __future__ import annotations

import subprocess
from pathlib import Path, PureWindowsPath

import pytest

import codesync.github_auto as ga
from codesync import config, followups, git_ops, proc, publish, reserved_names
from codesync.publish import OrphanCandidate, find_orphan_candidates
from tests import test_github_auto as gh_tests
from tests.test_github_auto import _ac, _repo

harness = gh_tests.harness

WINDOWS_ADD_ERR = (
    "error: short read while indexing nul\n"
    "error: nul: failed to insert into database\n"
    "error: unable to index file 'nul'\n"
    "fatal: adding files failed\n"
)


@pytest.fixture(autouse=True)
def _clean_followups():
    followups.clear()
    yield
    followups.clear()


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(reserved_names, "applies", lambda: True)


def _reserved_followups() -> list[followups.Followup]:
    return [f for f in followups.drain() if f.kind == "reserved-name"]


# ---------- name classification ----------

@pytest.mark.parametrize("name", [
    "nul", "NUL", "Nul", "nul.txt", "con", "CON.tar.gz", "prn", "aux",
    "com1", "COM9", "lpt1", "lpt9.log",
])
def test_reserved_names_detected(name):
    assert reserved_names.is_reserved(name)


@pytest.mark.parametrize("name", [
    "nul2", "null", "nulx.txt", "console", "com0", "com10", "lpt0", ".nul",
    "my-nul", "README.md", "auxiliary",
])
def test_ordinary_names_not_reserved(name):
    assert not reserved_names.is_reserved(name)


def test_delete_command_uses_extended_length_prefix():
    cmd = reserved_names.delete_command(PureWindowsPath(r"D:\SyncRepos\proj\nul"))
    assert cmd == r'Remove-Item -LiteralPath "\\?\D:\SyncRepos\proj\nul"'


def test_delete_command_handles_unc_paths():
    cmd = reserved_names.delete_command(PureWindowsPath(r"\\server\share\proj\nul"))
    assert cmd == r'Remove-Item -LiteralPath "\\?\UNC\server\share\proj\nul"'


def test_delete_command_keeps_existing_prefix():
    cmd = reserved_names.delete_command(r"\\?\D:\x\nul")
    assert cmd == r'Remove-Item -LiteralPath "\\?\D:\x\nul"'


def test_find_reserved_is_recursive_and_skips_dot_git(tmp_path):
    (tmp_path / "nul").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "CON.txt").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "nul").write_text("x", encoding="utf-8")
    found = reserved_names.find_reserved(tmp_path)
    assert sorted(found) == sorted([tmp_path / "nul", tmp_path / "src" / "CON.txt"])


def test_only_reserved(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    assert reserved_names.only_reserved(d) is False  # empty is not "junk-only"
    (d / "nul").write_text("83 bytes of error text", encoding="utf-8")
    (d / "empty-sub").mkdir()
    assert reserved_names.only_reserved(d) is True
    (d / "empty-sub" / "real.txt").write_text("x", encoding="utf-8")
    assert reserved_names.only_reserved(d) is False


# ---------- auto-commit ----------

def _init_repo(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=p, check=True)
    (p / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)


def _tracked(repo: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files"],
                         check=True, capture_output=True, text=True).stdout
    return set(out.split())


@pytest.fixture
def windows_git_add(monkeypatch, windows):
    """Make a plain `git add` fail like Git for Windows while any untracked
    reserved-name file would be swept in; pathspec-excluded adds run for real."""
    adds: list[list[str]] = []
    real_run = proc.run

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["git"] and "add" in cmd:
            adds.append(list(cmd))
            repo = Path(cmd[cmd.index("-C") + 1]) if "-C" in cmd else Path(kwargs.get("cwd", "."))
            excluded = any(str(a).startswith(":(exclude") for a in cmd)
            if not excluded and reserved_names.find_reserved(repo):
                return subprocess.CompletedProcess(cmd, 128, "", WINDOWS_ADD_ERR)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(proc, "run", fake_run)
    return adds


def test_auto_commit_excludes_reserved_file_and_commits_the_rest(tmp_path, windows_git_add):
    repo = tmp_path / "repo-a"
    _init_repo(repo)
    (repo / "new.txt").write_text("real work", encoding="utf-8")
    (repo / "nul").write_text("junk", encoding="utf-8")

    committed = git_ops.auto_commit_dirty([repo], skip_names=set())

    assert committed == ["repo-a"]
    tracked = _tracked(repo)
    assert "new.txt" in tracked
    assert "nul" not in tracked
    assert (repo / "nul").exists()  # never auto-deleted
    items = _reserved_followups()
    assert len(items) == 1
    assert reserved_names.delete_command(repo / "nul") in items[0].commands


def test_auto_commit_reserved_file_in_subdirectory(tmp_path, windows_git_add):
    repo = tmp_path / "repo-a"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x", encoding="utf-8")
    (repo / "src" / "NUL.txt").write_text("junk", encoding="utf-8")

    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == ["repo-a"]
    tracked = _tracked(repo)
    assert "src/main.py" in tracked
    assert "src/NUL.txt" not in tracked
    items = _reserved_followups()
    assert reserved_names.delete_command(repo / "src" / "NUL.txt") in items[0].commands


def test_auto_commit_only_reserved_file_commits_nothing_but_reports(
    tmp_path, windows_git_add, capsys,
):
    repo = tmp_path / "repo-a"
    _init_repo(repo)
    (repo / "nul").write_text("junk", encoding="utf-8")

    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == []
    assert _tracked(repo) == {"README.md"}
    assert _reserved_followups()
    assert "nul" in capsys.readouterr().out


def test_auto_commit_happy_path_runs_a_single_plain_add(tmp_path, windows_git_add):
    repo = tmp_path / "repo-a"
    _init_repo(repo)
    (repo / "new.txt").write_text("x", encoding="utf-8")

    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == ["repo-a"]
    assert len(windows_git_add) == 1
    assert not any(str(a).startswith(":(exclude") for a in windows_git_add[0])


def test_auto_commit_other_add_failure_is_not_retried(tmp_path, monkeypatch, windows):
    repo = tmp_path / "repo-a"
    _init_repo(repo)
    (repo / "new.txt").write_text("x", encoding="utf-8")
    adds: list[list[str]] = []
    real_run = proc.run

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["git"] and "add" in cmd:
            adds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: index.lock exists\n")
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(proc, "run", fake_run)

    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == []
    assert len(adds) == 1
    assert _reserved_followups() == []


def test_auto_commit_unchanged_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(reserved_names, "applies", lambda: False)
    repo = tmp_path / "repo-a"
    _init_repo(repo)
    (repo / "nul").write_text("legit on POSIX", encoding="utf-8")
    assert git_ops.auto_commit_dirty([repo], skip_names=set()) == ["repo-a"]
    assert "nul" in _tracked(repo)
    assert _reserved_followups() == []


# ---------- publish ----------

def _dir(parent: Path, name: str, files: list[str]) -> Path:
    d = parent / name
    d.mkdir()
    for f in files:
        target = d / f
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return d


def test_junk_only_dir_is_not_an_orphan_candidate(tmp_path, windows):
    _dir(tmp_path, "junk", ["nul"])
    _dir(tmp_path, "real", ["main.py", "nul"])
    names = [c.name for c in find_orphan_candidates([tmp_path], skip=set())]
    assert names == ["real"]


def test_junk_only_dir_is_still_a_candidate_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(reserved_names, "applies", lambda: False)
    _dir(tmp_path, "junk", ["nul"])
    assert [c.name for c in find_orphan_candidates([tmp_path], skip=set())] == ["junk"]


def test_publish_orphans_reports_junk_only_dir(tmp_path, monkeypatch, windows):
    junk = _dir(tmp_path, "junk", ["nul"])
    monkeypatch.setattr(publish.auth, "ensure_gh_authenticated",
                        lambda: pytest.fail("nothing publishable; gh must not be touched"))
    cfg = config.Config(code_roots=[str(tmp_path)])
    assert publish.publish_orphans(cfg) == 0
    items = _reserved_followups()
    assert len(items) == 1
    assert reserved_names.delete_command(junk / "nul") in items[0].commands


def test_publish_orphans_does_not_report_excluded_paths(tmp_path, monkeypatch, windows):
    junk = _dir(tmp_path, "junk", ["nul"])
    cfg = config.Config(code_roots=[str(tmp_path)])
    assert publish.publish_orphans(cfg, exclude_paths={junk}) == 0
    assert _reserved_followups() == []


def test_publish_one_excludes_reserved_files_from_initial_commit(monkeypatch, tmp_path, windows):
    proj = _dir(tmp_path, "proj", ["main.py", "nul", "sub/aux.txt"])
    c = OrphanCandidate(path=proj, name="proj", has_git=False, reason="")
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: False)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "diff" in cmd and "--cached" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, _msg = publish.publish_one(c, "me")
    assert ok is True
    add = next(cmd for cmd in calls if "add" in cmd and cmd[:1] == ["git"])
    assert ":(exclude,literal)nul" in add
    assert ":(exclude,literal)sub/aux.txt" in add
    items = _reserved_followups()
    assert reserved_names.delete_command(proj / "nul") in items[0].commands


def test_publish_one_without_reserved_files_keeps_plain_add(monkeypatch, tmp_path, windows):
    proj = _dir(tmp_path, "proj", ["main.py"])
    c = OrphanCandidate(path=proj, name="proj", has_git=False, reason="")
    monkeypatch.setattr(publish, "_gh_repo_exists", lambda owner, name: False)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "diff" in cmd and "--cached" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert publish.publish_one(c, "me")[0] is True
    add = next(cmd for cmd in calls if "add" in cmd and cmd[:1] == ["git"])
    assert not any(str(a).startswith(":(exclude") for a in add)


def test_preflight_junk_only_dir_is_not_a_project_candidate(tmp_path, monkeypatch):
    _dir(tmp_path, "junk", ["nul"])
    cfg = config.Config(code_roots=[str(tmp_path)])
    monkeypatch.setattr(reserved_names, "applies", lambda: True)
    assert config.project_root_problems(cfg, include_unpublished=True)
    monkeypatch.setattr(reserved_names, "applies", lambda: False)
    assert config.project_root_problems(cfg, include_unpublished=True) == []


# ---------- clone target conflicts ----------

def test_empty_clone_target_dir_is_cloned_into(harness, monkeypatch):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "")
    blocked: set[Path] = set()
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False,
           auto_migrate=False, blocked_publish_paths=blocked)
    assert harness["cloned"] == ["foo"]
    assert blocked == set()
    assert [f for f in followups.drain() if f.kind == "clone-conflict"] == []


def test_junk_only_clone_target_gets_precise_diagnosis(harness, monkeypatch, windows, capsys):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    (dest / "nul").write_text("83 bytes", encoding="utf-8")
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "")
    blocked: set[Path] = set()
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False,
           auto_migrate=False, blocked_publish_paths=blocked)
    assert harness["cloned"] == []
    assert blocked == {dest}
    assert (dest / "nul").exists()
    items = followups.drain()
    reserved = [f for f in items if f.kind == "reserved-name"]
    assert len(reserved) == 1
    assert reserved_names.delete_command(dest / "nul") in reserved[0].commands
    assert "codesync sync" in reserved[0].commands
    assert [f for f in items if f.kind == "clone-conflict"] == []
    assert "保留名" in capsys.readouterr().out


def test_clone_conflict_backup_name_is_hidden(harness, monkeypatch):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    (dest / "README.md").write_text("someone else's project", encoding="utf-8")
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "")
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)
    assert harness["cloned"] == []
    conflict = [f for f in followups.drain() if f.kind == "clone-conflict"]
    assert len(conflict) == 1
    backup = dest.parent / ".foo-local"
    assert f'mv "{dest}" "{backup}"' in conflict[0].commands
    assert not any(f'{dest}.local"' in c for c in conflict[0].commands)


def test_clone_conflict_with_foreign_origin_uses_hidden_backup(harness, monkeypatch):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "git@github.com:other/foo.git")
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)
    conflict = [f for f in followups.drain() if f.kind == "clone-conflict"]
    assert f'mv "{dest}" "{dest.parent / ".foo-local"}"' in conflict[0].commands


# ---------- adversarial review fixes ----------

@pytest.mark.parametrize("quote", ['"', "\u201c", "\u201d", "\u201e"])
def test_delete_command_escapes_every_powershell_double_quote(quote):
    path = f"C:\\work\\x{quote}; Write-Output INJECTED; #\\nul"
    cmd = reserved_names.delete_command(PureWindowsPath(path))
    body = cmd[len('Remove-Item -LiteralPath "'):-1]
    # Every quote char inside the literal is backtick-escaped, so the string
    # cannot be closed early and nothing after it runs as a second command.
    assert f"`{quote}" in body
    assert body.replace(f"`{quote}", "").count(quote) == 0


def test_delete_command_escapes_variables_and_backticks():
    cmd = reserved_names.delete_command(PureWindowsPath(r"C:\a$env:X`b\nul"))
    assert cmd == r'Remove-Item -LiteralPath "\\?\C:\a`$env:X``b\nul"'


def test_scan_never_follows_directory_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "nul").write_text("x", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "nul").write_text("x", encoding="utf-8")
    (proj / "link").symlink_to(outside, target_is_directory=True)
    assert reserved_names.find_reserved(proj) == [proj / "nul"]
    assert reserved_names.only_reserved(proj) is False  # the link is real content


def test_link_named_nul_is_never_reported(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "nul").symlink_to(outside, target_is_directory=True)
    assert reserved_names.find_reserved(proj) == []
    assert reserved_names.only_reserved(proj) is False


def test_scan_root_that_is_a_link_is_not_scanned(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "nul").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert reserved_names.find_reserved(link) == []
    assert reserved_names.only_reserved(link) is False
    assert reserved_names.is_plain_directory(link) is False


def test_junction_reparse_attribute_is_not_descended(tmp_path, monkeypatch):
    import stat as stat_mod
    proj = tmp_path / "proj"
    junction = proj / "junction"
    junction.mkdir(parents=True)
    (junction / "nul").write_text("outside the project", encoding="utf-8")
    (proj / "nul").write_text("x", encoding="utf-8")
    junction_ino = junction.lstat().st_ino
    monkeypatch.setattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)
    real_check = reserved_names._is_link_or_reparse

    def fake_check(st):
        # Emulate Windows: a junction lstat()s as S_IFDIR plus the reparse attribute.
        return st.st_ino == junction_ino or real_check(st)
    monkeypatch.setattr(reserved_names, "_is_link_or_reparse", fake_check)
    assert reserved_names.find_reserved(proj) == [proj / "nul"]
    assert reserved_names.only_reserved(proj) is False


def test_empty_clone_target_is_removed_then_cloned_fresh(harness, monkeypatch):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "")
    seen_existing: list[bool] = []
    real_run = ga.proc.run

    def clone_spy(cmd, **kwargs):
        if cmd[:2] == ["git", "clone"]:
            seen_existing.append(Path(cmd[-1]).exists())
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(ga.proc, "run", clone_spy)
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)
    assert harness["cloned"] == ["foo"]
    assert seen_existing == [False]  # git creates (and on failure removes) its own dir


def test_empty_clone_target_that_fills_up_is_left_alone(harness, monkeypatch):
    harness["gh"] = [_repo("foo")]
    dest = harness["tmp"] / "foo"
    dest.mkdir()
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "")

    def refuse(self):
        raise OSError(66, "Directory not empty")
    monkeypatch.setattr(Path, "rmdir", refuse)
    blocked: set[Path] = set()
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False,
           auto_migrate=False, blocked_publish_paths=blocked)
    assert harness["cloned"] == []
    assert blocked == {dest}
    assert [f for f in followups.drain() if f.kind == "clone-conflict"]


def test_empty_symlinked_clone_target_is_not_replaced(harness, monkeypatch):
    harness["gh"] = [_repo("foo")]
    real = harness["tmp"] / "elsewhere"
    real.mkdir()
    dest = harness["tmp"] / "foo"
    dest.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(ga.git_ops, "origin_url", lambda path: "")
    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)
    assert harness["cloned"] == []
    assert dest.is_symlink() and real.is_dir()
