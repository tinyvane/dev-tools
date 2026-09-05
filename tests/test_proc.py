from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time

import pytest

from codesync import proc


class _FakePopen:
    def __init__(self, argv, **kwargs):
        self.args = argv
        self.kwargs = kwargs
        self.returncode = 0
        self.pid = 43210
        self.stdout = None
        self.stderr = None

    def communicate(self, timeout=None):
        return "", ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_run_returns_124_on_timeout(monkeypatch):
    class TimedOut(_FakePopen):
        def communicate(self, timeout=None):
            if self.returncode == 0:
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", TimedOut)
    monkeypatch.setattr(proc, "_terminate_process_tree", lambda child: child.kill())
    result = proc.run(["git", "pull"], timeout=17)

    assert result.returncode == proc.TIMEOUT_RC
    assert "17s" in result.stderr
    assert "git pull" in result.stderr


def test_run_returns_127_when_binary_missing(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("missing-tool")

    monkeypatch.setattr(subprocess, "run", missing)
    result = proc.run(["missing-tool"], timeout=proc.T_QUICK)

    assert result.returncode == proc.NOTFOUND_RC
    assert "missing-tool" in result.stderr


def test_run_always_utf8_replace_never_text(monkeypatch):
    seen = {}

    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        seen.update(kwargs), subprocess.CompletedProcess(argv, 0, "", "")
    )[1])
    proc.run(["git", "status"], timeout=proc.T_QUICK)

    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
    assert "text" not in seen
    assert "universal_newlines" not in seen


def test_run_rejects_string_command():
    with pytest.raises(TypeError):
        proc.run("git status", timeout=proc.T_QUICK)  # type: ignore[arg-type]


def test_capture_false_does_not_set_capture_output(monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakePopen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    proc.run(["git", "clone"], timeout=proc.T_NET_LONG, capture=False)

    assert "stdout" not in seen
    assert "stderr" not in seen


def test_run_passes_an_isolated_environment_and_can_inherit_stdin(monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakePopen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        seen.update(kwargs), subprocess.CompletedProcess(argv, 0, "", "")
    )[1])
    supplied = {"CODEX_HOME": "V:/CodexPortable/home"}
    proc.run(
        ["installer"], timeout=proc.T_NET_LONG, capture=False,
        stdin_devnull=False, env=supplied,
    )

    assert seen["env"] == supplied
    assert seen["env"] is not supplied
    assert "stdin" not in seen


def test_git_children_force_noninteractive_credentials(monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakePopen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GCM_INTERACTIVE", "Always")

    proc.run(["git", "pull"], timeout=proc.T_NET_LONG)

    child_env = seen["env"]
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    assert child_env["GCM_INTERACTIVE"] == "Never"
    assert os.environ["GIT_TERMINAL_PROMPT"] == "1"
    assert os.environ["GCM_INTERACTIVE"] == "Always"


def test_timeout_terminates_descendants_holding_capture_pipes(monkeypatch):
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    started = time.monotonic()
    monkeypatch.setattr(proc, "_needs_tree_timeout", lambda _argv: True)

    result = proc.run([sys.executable, "-c", code], timeout=1)

    assert result.returncode == proc.TIMEOUT_RC
    assert time.monotonic() - started < 15


def test_codesync_timeout_scale_applies_at_module_load(monkeypatch):
    monkeypatch.setenv("CODESYNC_TIMEOUT_SCALE", "1.5")
    importlib.reload(proc)
    try:
        assert proc.T_QUICK == 45
        assert proc.T_LOCAL == 450
        assert proc.T_NET == 180
        assert proc.T_NET_LONG == 1350
    finally:
        monkeypatch.delenv("CODESYNC_TIMEOUT_SCALE")
        importlib.reload(proc)
