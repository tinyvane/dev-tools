from __future__ import annotations

import subprocess

from codesync import auth


def test_gh_auth_status_timeout_does_not_launch_login(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "auth", "status"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert auth.ensure_gh_authenticated() is False
    assert not any(cmd[:3] == ["gh", "auth", "login"] for cmd in calls)
    captured = capsys.readouterr()
    assert "gh auth status 超时" in captured.out + captured.err


def test_gh_auth_login_is_never_given_a_timeout(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(auth, "gh_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert auth.ensure_gh_authenticated() is False
    login = next(kwargs for cmd, kwargs in calls if cmd[:3] == ["gh", "auth", "login"])
    assert "timeout" not in login
    assert "stdin" not in login
    assert "stdout" not in login
    assert "stderr" not in login
