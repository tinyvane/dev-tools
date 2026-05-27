"""Tests for codesync.fork_setup — the `codesync fork-setup` backfill command
that adds `upstream` remote to forks missing one.

Pure unit tests: monkeypatch every gh / git subprocess call so we never touch
the network or shell out for real."""
from __future__ import annotations

import subprocess
from pathlib import Path

from codesync import fork_setup


# ---------- _gh_get_parent_url ----------

def test_get_parent_url_happy(monkeypatch) -> None:
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="git@github.com:anthropics/claude-code.git\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert fork_setup._gh_get_parent_url("tinyvane", "claude-code") == "git@github.com:anthropics/claude-code.git"


def test_get_parent_url_returns_none_on_gh_failure(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found"))
    assert fork_setup._gh_get_parent_url("x", "y") is None


def test_get_parent_url_returns_none_on_null_parent(monkeypatch) -> None:
    """gh's --jq prints the literal string 'null' when the field is absent."""
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="null\n", stderr=""))
    assert fork_setup._gh_get_parent_url("x", "y") is None


def test_get_parent_url_returns_none_on_empty(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr=""))
    assert fork_setup._gh_get_parent_url("x", "y") is None


# ---------- _git_remotes ----------

def test_git_remotes_parses_v_output(monkeypatch, tmp_path) -> None:
    sample = (
        "origin\tgit@github.com:tinyvane/foo.git (fetch)\n"
        "origin\tgit@github.com:tinyvane/foo.git (push)\n"
        "upstream\thttps://github.com/orig/foo.git (fetch)\n"
        "upstream\thttps://github.com/orig/foo.git (push)\n"
    )
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=sample, stderr=""))
    remotes = fork_setup._git_remotes(tmp_path)
    assert remotes == {
        "origin":   "git@github.com:tinyvane/foo.git",
        "upstream": "https://github.com/orig/foo.git",
    }


def test_git_remotes_empty_on_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a git repo"))
    assert fork_setup._git_remotes(tmp_path) == {}


# ---------- _list_user_forks ----------

def test_list_user_forks_parses_json(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0,
                            stdout='[{"name":"a"},{"name":"b"},{"name":"c"}]',
                            stderr=""))
    assert fork_setup._list_user_forks("tinyvane") == {"a", "b", "c"}


def test_list_user_forks_empty_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""))
    assert fork_setup._list_user_forks("x") == set()


def test_list_user_forks_handles_bad_json(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr=""))
    assert fork_setup._list_user_forks("x") == set()


# ---------- add_upstream_for_fork ----------

def test_add_upstream_calls_git_remote_add(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # First call is _gh_get_parent_url → return parent url
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="git@github.com:up/foo.git\n", stderr="")
        # Second call is `git -C ... remote add upstream <url>` → success
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = fork_setup.add_upstream_for_fork(tmp_path, "me", "foo")
    assert ok is True
    assert msg == "git@github.com:up/foo.git"
    # second call should have been git remote add upstream <url>
    git_call = next(c for c in calls if c[0] == "git")
    assert "remote" in git_call and "add" in git_call and "upstream" in git_call
    assert "git@github.com:up/foo.git" in git_call


def test_add_upstream_bails_when_parent_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="null\n", stderr=""))
    ok, msg = fork_setup.add_upstream_for_fork(tmp_path, "me", "foo")
    assert ok is False
    assert "parent" in msg or "拿不到" in msg


def test_add_upstream_reports_git_failure(monkeypatch, tmp_path) -> None:
    def fake_run(cmd, **kw):
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="git@github.com:up/foo.git\n", stderr="")
        # git remote add fails (e.g. remote already exists)
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="error: remote upstream already exists.\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = fork_setup.add_upstream_for_fork(tmp_path, "me", "foo")
    assert ok is False
    assert "already exists" in msg


# ---------- _ORIGIN_OWNER_NAME regex ----------

def test_origin_regex_ssh() -> None:
    m = fork_setup._ORIGIN_OWNER_NAME.search("git@github.com:tinyvane/Claude-Code.git")
    assert m is not None
    assert m.group(1) == "tinyvane"
    assert m.group(2) == "Claude-Code"


def test_origin_regex_https() -> None:
    m = fork_setup._ORIGIN_OWNER_NAME.search("https://github.com/tinyvane/Claude-Code.git")
    assert m is not None
    assert m.group(1) == "tinyvane"
    assert m.group(2) == "Claude-Code"


def test_origin_regex_https_no_dotgit() -> None:
    m = fork_setup._ORIGIN_OWNER_NAME.search("https://github.com/tinyvane/Claude-Code")
    assert m is not None
    assert m.group(1) == "tinyvane"
    assert m.group(2) == "Claude-Code"


def test_origin_regex_https_with_trailing_slash() -> None:
    m = fork_setup._ORIGIN_OWNER_NAME.search("https://github.com/tinyvane/Claude-Code/")
    assert m is not None
    assert m.group(1) == "tinyvane"
    assert m.group(2) == "Claude-Code"
