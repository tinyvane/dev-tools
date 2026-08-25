"""Process-scoped Git transport hardening.

Keep codesync's GitHub SSH traffic off port 22 without rewriting repository
remotes or the user's ~/.ssh/config.  Git's environment-backed config is
inherited by every git/gh child process launched by this codesync process.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path

from codesync import paths, proc
from codesync.known_hosts import KnownHostsState, ensure_github_443_known_hosts


_GITHUB_SSH_443_BASE = "ssh://git@ssh.github.com:443/"
_GITHUB_SSH_REWRITES = (
    (f"url.{_GITHUB_SSH_443_BASE}.insteadOf", "git@github.com:"),
    (f"url.{_GITHUB_SSH_443_BASE}.insteadOf", "ssh://git@github.com/"),
)
_CONFIG_INDEX_RE = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_(\d+)$")
_CONTROL_PATH_MAX_BYTES = 90
# ssh expands %C to a 40-char SHA1 hex digest of %l%h%p%r — note it is 40, not
# 32, and it is PORT-SENSITIVE. Budgeting for the wrong length silently pushes
# the socket path over the sun_path limit, where ssh fails every connection.
_CONTROL_HASH_LEN = 40

# codesync rewrites GitHub SSH URLs to ssh://git@ssh.github.com:443/, so git
# always dials this host on THIS port. The master connection we pre-warm and
# tear down must use the identical host/port/user triple, otherwise %C hashes
# to a different socket and the shared connection is never actually reused.
_GITHUB_SSH_HOST = "ssh.github.com"
_GITHUB_SSH_PORT = "443"
_GITHUB_SSH_TARGET = f"git@{_GITHUB_SSH_HOST}"

# Socket names are "<pid>-%C". The PID prefix makes every control socket owned
# by exactly one codesync process: two concurrent runs must not share a master,
# because each one tears its own down on exit and would otherwise kill a
# connection the other is actively pulling/pushing over.
_SOCKET_NAME_RE = re.compile(r"^(\d+)-[0-9a-f]{%d}$" % _CONTROL_HASH_LEN)
_DEFAULT_KNOWN_HOSTS = ("~/.ssh/known_hosts", "~/.ssh/known_hosts2")

# The CLI configures known_hosts before loading the full config, then sync may
# replace that command with a mux-enabled version. Remember our exact output so
# the second call is not mistaken for a user override. Any other non-empty value
# remains user-owned and is never overwritten.
_OWN_SSH_COMMAND: str | None = None


@dataclass(frozen=True)
class SshMultiplexState:
    enabled: bool
    reason: str
    control_path: str
    persist_seconds: int = 60
    known_hosts_path: str = ""


@dataclass(frozen=True)
class SshTransportState:
    mux: SshMultiplexState
    known_hosts: KnownHostsState


def configure_github_ssh_over_443(
    env: MutableMapping[str, str] | None = None,
) -> None:
    """Route GitHub SSH URLs through GitHub's official port-443 endpoint.

    The settings live only in *env* (``os.environ`` by default), so repositories
    retain their existing origin URLs and manual Git/SSH commands outside
    codesync remain untouched. Existing ``GIT_CONFIG_*`` entries are preserved.
    Calling this function more than once is idempotent.
    """
    target = os.environ if env is None else env

    try:
        declared_count = max(0, int(target.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        declared_count = 0

    # Preserve even partially populated inherited entries. Normalising a bad
    # GIT_CONFIG_COUNT also makes the resulting child Git configuration valid.
    inherited_indexes = [
        int(match.group(1))
        for name in target
        if (match := _CONFIG_INDEX_RE.fullmatch(name))
    ]
    count = max(declared_count, max(inherited_indexes, default=-1) + 1)

    present = {
        (target.get(f"GIT_CONFIG_KEY_{index}"), target.get(f"GIT_CONFIG_VALUE_{index}"))
        for index in range(count)
    }
    for key, value in _GITHUB_SSH_REWRITES:
        if (key, value) in present:
            continue
        target[f"GIT_CONFIG_KEY_{count}"] = key
        target[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1

    target["GIT_CONFIG_COUNT"] = str(count)


def _control_path_fits(template: Path) -> bool:
    estimated = str(template).replace("%C", "x" * _CONTROL_HASH_LEN)
    return len(os.fsencode(estimated)) <= _CONTROL_PATH_MAX_BYTES


def _control_dir_candidates() -> tuple[Path, ...]:
    """Where the control sockets may live, most to least preferred.

    The config dir is private and persistent but can be long; a shared /tmp
    path is the shortest option that still fits the sun_path limit when a deep
    home or a long TMPDIR blows the budget.
    """
    candidates = [paths.config_dir() / "ssh", Path(tempfile.gettempdir()) / "codesync-ssh"]
    uid = getattr(os, "getuid", None)
    if uid is not None:
        candidates.append(Path(f"/tmp/codesync-ssh-{uid()}"))
    return tuple(candidates)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. EPERM — the pid exists, it just isn't ours
    return True


def _sweep_stale_sockets(directory: Path) -> None:
    """Drop control sockets left behind by codesync processes that are gone.

    A master normally removes its own socket, but a killed process cannot.
    Best-effort only: never raise, and never touch a socket whose owning PID is
    still running.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        match = _SOCKET_NAME_RE.match(entry.name)
        if match is None or _pid_alive(int(match.group(1))):
            continue
        try:
            entry.unlink()
        except OSError:
            pass


def _prepare_control_dir(directory: Path) -> bool:
    """Create a private 0700 socket dir we actually own, else reject it.

    The /tmp candidate lives in a world-writable place, so "it exists" is not
    good enough: a symlink or another user's directory there must never be
    adopted as our socket home.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        if directory.is_symlink() or not directory.is_dir():
            return False
        uid = getattr(os, "getuid", None)
        if uid is not None and directory.stat().st_uid != uid():
            return False
    except OSError:
        return False
    return True


def configure_ssh_multiplexing(
    env: MutableMapping[str, str] | None = None,
    *,
    enabled: bool = True,
    persist_seconds: int = 60,
) -> SshMultiplexState:
    """Prepare process-scoped OpenSSH ControlMaster connection reuse.

    This function no longer writes ``GIT_SSH_COMMAND`` itself. The single
    assembly point is :func:`configure_ssh_command`, which combines these
    options with codesync's known_hosts file without duplication.
    """
    target = os.environ if env is None else env
    if not enabled:
        return SshMultiplexState(False, "配置已关闭", "", persist_seconds)
    if os.name == "nt":
        return SshMultiplexState(
            False, "Windows OpenSSH 不支持 ControlMaster", "", persist_seconds,
        )

    existing = target.get("GIT_SSH_COMMAND", "").strip()
    if existing and existing != _OWN_SSH_COMMAND:
        return SshMultiplexState(
            False, "已存在用户自定义 GIT_SSH_COMMAND", "", persist_seconds,
        )

    chosen: Path | None = None
    socket_name = f"{os.getpid()}-%C"
    for directory in _control_dir_candidates():
        template = directory / socket_name
        # A candidate that is too long or that we cannot own privately is not a
        # failure — try the next one and only give up once all are exhausted.
        if not _control_path_fits(template) or not _prepare_control_dir(directory):
            continue
        _sweep_stale_sockets(directory)
        chosen = template
        break
    if chosen is None:
        return SshMultiplexState(
            False, "无可用的 ControlPath 目录（路径过长或不可私有创建）", "", persist_seconds,
        )

    return SshMultiplexState(True, "", str(chosen), persist_seconds)


def _ssh_list_entry(path: str) -> str:
    """Quote one entry of an ssh whitespace-separated file list.

    UserKnownHostsFile is split on whitespace by ssh ITSELF, so a path
    containing a space (``/Users/My Name/...``) silently becomes two bogus
    filenames and the file is never read. ssh's own config parser accepts
    double-quoted entries, which is a separate layer from the shlex quoting
    that protects the whole option from git's shell-like re-parsing.
    Verified: unquoted space path -> "no host key is known"; quoted -> works.
    """
    if not path:
        return path
    if any(ch.isspace() for ch in path):
        return '"' + path.replace('"', r'\"') + '"'
    return path


def _known_hosts_value(path: str) -> str:
    return " ".join(_ssh_list_entry(p) for p in (*_DEFAULT_KNOWN_HOSTS, path))


def _disabled_known_hosts(reason: str) -> KnownHostsState:
    return KnownHostsState("", "", reason, False)


def configure_ssh_command(
    env: MutableMapping[str, str] | None = None,
    *,
    multiplex_enabled: bool = True,
    persist_seconds: int = 60,
    known_hosts_enabled: bool = True,
) -> SshTransportState:
    """Assemble codesync's one process-scoped ``GIT_SSH_COMMAND``.

    User known-host files remain first so OpenSSH's normal TOFU writes keep
    going to ``~/.ssh/known_hosts``. The codesync-managed GitHub-443 file is
    appended as an additional read-only trust source. A user-supplied command
    disables both features rather than being parsed or modified.
    """
    global _OWN_SSH_COMMAND

    target = os.environ if env is None else env
    existing = target.get("GIT_SSH_COMMAND", "").strip()
    if existing and existing != _OWN_SSH_COMMAND:
        reason = "已存在用户自定义 GIT_SSH_COMMAND"
        return SshTransportState(
            mux=SshMultiplexState(False, reason, "", persist_seconds),
            known_hosts=_disabled_known_hosts(reason),
        )

    if known_hosts_enabled:
        try:
            known_state = ensure_github_443_known_hosts()
        except Exception:
            known_state = _disabled_known_hosts(
                "GitHub 443 known_hosts 初始化失败；请手动执行："
                "ssh-keyscan -p 443 ssh.github.com >> ~/.ssh/known_hosts"
            )
    else:
        known_state = _disabled_known_hosts("配置已关闭")

    mux_state = configure_ssh_multiplexing(
        target,
        enabled=multiplex_enabled,
        persist_seconds=persist_seconds,
    )
    if mux_state.enabled and known_state.enabled:
        mux_state = replace(mux_state, known_hosts_path=known_state.path)

    argv = ["ssh"]
    if known_state.enabled:
        argv.extend(["-o", f"UserKnownHostsFile={_known_hosts_value(known_state.path)}"])
    if mux_state.enabled:
        argv.extend(
            [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={mux_state.control_path}",
                "-o", f"ControlPersist={mux_state.persist_seconds}s",
            ]
        )

    if len(argv) == 1:
        if existing and existing == _OWN_SSH_COMMAND:
            target.pop("GIT_SSH_COMMAND", None)
            _OWN_SSH_COMMAND = None
        return SshTransportState(mux=mux_state, known_hosts=known_state)

    command = " ".join(shlex.quote(arg) for arg in argv)
    target["GIT_SSH_COMMAND"] = command
    _OWN_SSH_COMMAND = command
    return SshTransportState(mux=mux_state, known_hosts=known_state)


def _known_hosts_argv(path: str) -> list[str]:
    if not path:
        return []
    return ["-o", f"UserKnownHostsFile={_known_hosts_value(path)}"]


def prewarm_github_master(state: SshMultiplexState, *, timeout: int) -> bool:
    """Best-effort creation of the shared GitHub SSH master connection."""
    if not state.enabled:
        return False
    try:
        result = proc.run(
            [
                "ssh", *_known_hosts_argv(state.known_hosts_path),
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={state.control_path}",
                "-o", f"ControlPersist={state.persist_seconds}s",
                "-o", "BatchMode=yes",
                "-p", _GITHUB_SSH_PORT, "-T", _GITHUB_SSH_TARGET,
            ],
            timeout=timeout,
        )
    except Exception:
        return False
    combined = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "successfully authenticated" in combined


def close_github_master(state: SshMultiplexState) -> None:
    """Best-effort shutdown of THIS process's GitHub SSH master connection.

    Safe to call unconditionally — including from --status or a cancelled run
    that never pre-warmed — because the PID-scoped ControlPath guarantees we can
    only ever close a master this process owns.
    """
    if not state.enabled:
        return
    try:
        proc.run(
            [
                "ssh", *_known_hosts_argv(state.known_hosts_path), "-O", "exit",
                "-o", f"ControlPath={state.control_path}",
                "-p", _GITHUB_SSH_PORT, _GITHUB_SSH_TARGET,
            ],
            timeout=proc.T_QUICK,
        )
    except Exception:
        pass
