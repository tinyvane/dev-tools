"""Tests for codesync.rename — manual rename + cross-machine auto-migration.

Subprocess/network helpers (gh, git) are monkeypatched; the actual directory
moves run for real against tmp_path so the filesystem side is exercised.
"""
from __future__ import annotations

import pytest

from codesync import config, git_ops, rename


# ---------- pure helpers ----------

@pytest.mark.parametrize("name,ok", [
    ("good", True),
    ("good-name_1.2", True),
    ("", False),
    ("has/slash", False),
    ("has space", False),
    ("tab\tname", False),
])
def test_valid_name(name, ok):
    assert rename._valid_name(name) is ok


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:me/foo.git", ("github.com", "me", "foo")),
    ("https://github.com/me/foo.git", ("github.com", "me", "foo")),
    ("https://github.com/me/foo", ("github.com", "me", "foo")),
    ("git@gitlab.com:me/foo.git", ("gitlab.com", "me", "foo")),
    ("ssh://git@github.com/me/foo.git", ("github.com", "me", "foo")),
])
def test_parse_remote(url, expected):
    assert rename._parse_remote(url) == expected


def test_parse_remote_unrecognized():
    assert rename._parse_remote("not a url") is None


# ---------- detect_and_migrate (the multi-machine core) ----------

def _make_repo(parent, name):
    d = parent / name
    d.mkdir()
    (d / ".git").mkdir()
    return d


def test_migrate_renames_dir_and_origin(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    local_owned = {"foo": foo}
    active = {"bar": "git@github.com:me/bar.git"}  # foo gone, bar is the new name

    monkeypatch.setattr(rename, "_gh_canonical_name",
                        lambda o, n: "bar" if n == "foo" else None)
    set_calls = []
    monkeypatch.setattr(rename, "_set_origin",
                        lambda p, u: (set_calls.append((p, u)), (True, ""))[1])

    migs = rename.detect_and_migrate(local_owned, active, "me")

    assert migs == [("foo", "bar")]
    assert (tmp_path / "bar").is_dir()
    assert not foo.exists()
    assert set_calls == [(foo, "git@github.com:me/bar.git")]


def test_migrate_skips_name_still_active(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    # foo is still present on GitHub → no api call, no migration.
    monkeypatch.setattr(rename, "_gh_canonical_name",
                        lambda o, n: pytest.fail("should not call gh for active repo"))
    migs = rename.detect_and_migrate({"foo": foo}, {"foo": "u"}, "me")
    assert migs == []
    assert foo.is_dir()


def test_migrate_skips_deleted_repo(tmp_path, monkeypatch):
    gone = _make_repo(tmp_path, "gone")
    # 404 from gh api → repo deleted/archived-away, not renamed. Leave it alone.
    monkeypatch.setattr(rename, "_gh_canonical_name", lambda o, n: None)
    migs = rename.detect_and_migrate({"gone": gone}, {"other": "u"}, "me")
    assert migs == []
    assert gone.is_dir()


def test_migrate_skips_when_canonical_not_active(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    # Resolves to a name that isn't in our active set → don't guess.
    monkeypatch.setattr(rename, "_gh_canonical_name", lambda o, n: "weird")
    migs = rename.detect_and_migrate({"foo": foo}, {"bar": "u"}, "me")
    assert migs == []
    assert foo.is_dir()


def test_migrate_updates_origin_only_when_dirname_differs(tmp_path, monkeypatch):
    # Local dir name doesn't match the old repo name → update origin, leave dir.
    custom = _make_repo(tmp_path, "custom")
    monkeypatch.setattr(rename, "_gh_canonical_name", lambda o, n: "bar")
    set_calls = []
    monkeypatch.setattr(rename, "_set_origin",
                        lambda p, u: (set_calls.append((p, u)), (True, ""))[1])
    migs = rename.detect_and_migrate({"foo": custom}, {"bar": "newurl"}, "me")
    assert migs == [("foo", "bar")]
    assert custom.is_dir()           # dir untouched
    assert not (tmp_path / "bar").exists()
    assert set_calls == [(custom, "newurl")]


# ---------- rename_repo (manual, this machine) ----------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # No real countdowns, and never touch the real ~/.claude/projects: default the
    # Claude-projects sync OFF for the manual-rename tests (the ones that exercise
    # it pass an explicit projects dir or call the helpers directly).
    monkeypatch.setattr(rename.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rename, "_load_rename_cfg",
                        lambda: config.RenameConfig(sync_claude_projects=False))


def test_rename_full_github_flow(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    monkeypatch.chdir(foo)

    monkeypatch.setattr(rename, "_origin_url", lambda r: "git@github.com:me/foo.git")
    monkeypatch.setattr(rename.auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(rename, "_gh_repo_exists", lambda o, n: False)
    rename_calls = []
    monkeypatch.setattr(rename, "_gh_repo_rename",
                        lambda o, old, new: (rename_calls.append((o, old, new)), (True, ""))[1])
    monkeypatch.setattr(rename, "_gh_new_ssh_url", lambda o, n: "git@github.com:me/bar.git")
    set_calls = []
    monkeypatch.setattr(rename, "_set_origin",
                        lambda p, u: (set_calls.append((p, u)), (True, ""))[1])
    monkeypatch.setattr(git_ops, "_is_dirty", lambda r: False)
    monkeypatch.setattr(rename, "_ahead_count", lambda r: 0)

    rc = rename.rename_repo(["bar"])

    assert rc == 0
    assert rename_calls == [("me", "foo", "bar")]
    assert (tmp_path / "bar").is_dir()
    assert not foo.exists()
    assert set_calls and set_calls[0][1] == "git@github.com:me/bar.git"


def test_rename_aborts_when_github_rename_fails(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    monkeypatch.chdir(foo)
    monkeypatch.setattr(rename, "_origin_url", lambda r: "git@github.com:me/foo.git")
    monkeypatch.setattr(rename.auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(rename, "_gh_repo_exists", lambda o, n: False)
    monkeypatch.setattr(rename, "_gh_repo_rename", lambda o, old, new: (False, "boom"))
    monkeypatch.setattr(git_ops, "_is_dirty", lambda r: False)
    monkeypatch.setattr(rename, "_ahead_count", lambda r: 0)

    rc = rename.rename_repo(["bar"])
    assert rc == 1
    assert foo.is_dir()                       # local untouched on remote failure
    assert not (tmp_path / "bar").exists()


def test_rename_rejects_existing_github_name(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    monkeypatch.chdir(foo)
    monkeypatch.setattr(rename, "_origin_url", lambda r: "git@github.com:me/foo.git")
    monkeypatch.setattr(rename.auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(rename, "_gh_repo_exists", lambda o, n: True)   # name taken
    rc = rename.rename_repo(["bar"])
    assert rc == 1
    assert foo.is_dir()


def test_rename_non_github_origin_is_local_only(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    monkeypatch.chdir(foo)
    monkeypatch.setattr(rename, "_origin_url", lambda r: "git@gitlab.com:me/foo.git")
    # gh must never be touched for a non-GitHub origin.
    monkeypatch.setattr(rename.auth, "ensure_gh_authenticated",
                        lambda: pytest.fail("must not auth gh for non-github origin"))
    rc = rename.rename_repo(["bar"])
    assert rc == 0
    assert (tmp_path / "bar").is_dir()
    assert not foo.exists()


def test_rename_no_origin_is_local_only(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    monkeypatch.chdir(foo)
    monkeypatch.setattr(rename, "_origin_url", lambda r: None)
    rc = rename.rename_repo(["bar"])
    assert rc == 0
    assert (tmp_path / "bar").is_dir()


def test_rename_rejects_existing_target_dir(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    (tmp_path / "bar").mkdir()                 # target already taken
    monkeypatch.chdir(foo)
    rc = rename.rename_repo(["bar"])
    assert rc == 1
    assert foo.is_dir()


def test_rename_one_arg_requires_git_repo(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()                              # no .git
    monkeypatch.chdir(plain)
    rc = rename.rename_repo(["bar"])
    assert rc == 1
    assert plain.is_dir()


def test_rename_rejects_same_name(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    monkeypatch.chdir(foo)
    rc = rename.rename_repo(["foo"])
    assert rc == 1


# ---------- Claude conversation directory ----------

@pytest.mark.parametrize("path,mangled", [
    (r"C:\Users\me\SyncRepos\foo", "C--Users-me-SyncRepos-foo"),
    ("C:/Users/me/SyncRepos/foo", "C--Users-me-SyncRepos-foo"),
    ("/home/me/SyncRepos/foo", "-home-me-SyncRepos-foo"),
])
def test_claude_project_dirname(path, mangled):
    assert rename._claude_project_dirname(path) == mangled


def test_rename_claude_project_renames(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "C--x-foo").mkdir()
    rename._rename_claude_project(projects, "C:/x/foo", "C:/x/bar")
    assert (projects / "C--x-bar").is_dir()
    assert not (projects / "C--x-foo").exists()


def test_rename_claude_project_idempotent_when_target_exists(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "C--x-foo").mkdir()
    (projects / "C--x-bar").mkdir()        # another machine + Dropbox already did it
    rename._rename_claude_project(projects, "C:/x/foo", "C:/x/bar")
    assert (projects / "C--x-foo").is_dir()  # left untouched, no crash
    assert (projects / "C--x-bar").is_dir()


def test_rename_claude_project_missing_src_is_noop(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    rename._rename_claude_project(projects, "C:/x/foo", "C:/x/bar")
    assert not (projects / "C--x-bar").exists()


def test_rename_claude_project_case_insensitive(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "C--x-Foo").mkdir()        # on-disk name has different casing
    rename._rename_claude_project(projects, "C:/x/foo", "C:/x/bar")
    assert (projects / "C--x-bar").is_dir()


def test_migrate_also_renames_claude_project(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / rename._claude_project_dirname(str(foo))).mkdir()

    monkeypatch.setattr(rename, "_gh_canonical_name", lambda o, n: "bar")
    monkeypatch.setattr(rename, "_set_origin", lambda p, u: (True, ""))

    migs = rename.detect_and_migrate(
        {"foo": foo}, {"bar": "url"}, "me", claude_projects=projects,
    )

    assert migs == [("foo", "bar")]
    new_repo = tmp_path / "bar"
    assert new_repo.is_dir()
    assert (projects / rename._claude_project_dirname(str(new_repo))).is_dir()
    assert not (projects / rename._claude_project_dirname(str(foo))).exists()


def test_rename_full_flow_renames_claude_project(tmp_path, monkeypatch):
    foo = _make_repo(tmp_path, "foo")
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / rename._claude_project_dirname(str(foo))).mkdir()

    monkeypatch.setattr(rename, "_resolve_claude_projects", lambda rcfg: projects)
    monkeypatch.chdir(foo)
    monkeypatch.setattr(rename, "_origin_url", lambda r: "git@github.com:me/foo.git")
    monkeypatch.setattr(rename.auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(rename, "_gh_repo_exists", lambda o, n: False)
    monkeypatch.setattr(rename, "_gh_repo_rename", lambda o, old, new: (True, ""))
    monkeypatch.setattr(rename, "_gh_new_ssh_url", lambda o, n: "git@github.com:me/bar.git")
    monkeypatch.setattr(rename, "_set_origin", lambda p, u: (True, ""))
    monkeypatch.setattr(git_ops, "_is_dirty", lambda r: False)
    monkeypatch.setattr(rename, "_ahead_count", lambda r: 0)

    rc = rename.rename_repo(["bar"])

    assert rc == 0
    new_repo = tmp_path / "bar"
    assert (projects / rename._claude_project_dirname(str(new_repo))).is_dir()
    assert not (projects / rename._claude_project_dirname(str(foo))).exists()
