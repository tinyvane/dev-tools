"""Process-scoped Git transport hardening.

Keep codesync's GitHub SSH traffic off port 22 without rewriting repository
remotes or the user's ~/.ssh/config.  Git's environment-backed config is
inherited by every git/gh child process launched by this codesync process.
"""
from __future__ import annotations

import os
import re
from collections.abc import MutableMapping


_GITHUB_SSH_443_BASE = "ssh://git@ssh.github.com:443/"
_GITHUB_SSH_REWRITES = (
    (f"url.{_GITHUB_SSH_443_BASE}.insteadOf", "git@github.com:"),
    (f"url.{_GITHUB_SSH_443_BASE}.insteadOf", "ssh://git@github.com/"),
)
_CONFIG_INDEX_RE = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_(\d+)$")


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
