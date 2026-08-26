from __future__ import annotations

import importlib
import subprocess

import pytest

from codesync import proc


def test_run_returns_124_on_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = proc.run(["git", "status"], timeout=17)

    assert result.returncode == proc.TIMEOUT_RC
    assert "17s" in result.stderr
    assert "git status" in result.stderr


def test_run_returns_127_when_binary_missing(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("missing-tool")

    monkeypatch.setattr(subprocess, "run", missing)
    result = proc.run(["missing-tool"], timeout=proc.T_QUICK)

    assert result.returncode == proc.NOTFOUND_RC
    assert "missing-tool" in result.stderr


def test_run_always_utf8_replace_never_text(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
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

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc.run(["git", "clone"], timeout=proc.T_NET_LONG, capture=False)

    assert "capture_output" not in seen


def test_codesync_timeout_scale_applies_at_module_load(monkeypatch):
    monkeypatch.setenv("CODESYNC_TIMEOUT_SCALE", "1.5")
    importlib.reload(proc)
    try:
        assert proc.T_QUICK == 45
        assert proc.T_LOCAL == 450
        assert proc.T_NET == 180
        assert proc.T_NET_LONG == 5400
    finally:
        monkeypatch.delenv("CODESYNC_TIMEOUT_SCALE")
        importlib.reload(proc)
