from __future__ import annotations

import os
import subprocess
import sys

from codesync import __repo_url__, output


# Install command. `--user` so unprivileged install. `--upgrade` so we go forward,
# never backward; pin to main via the git+ URL.
def _pip_args() -> list[str]:
    return [
        sys.executable, "-m", "pip", "install",
        "--user", "--upgrade",
        f"git+{__repo_url__}.git@main",
    ]


def self_update() -> int:
    output.section("codesync 自更新")
    cmd = _pip_args()
    output.detail(" ".join(cmd))

    if os.name == "nt":
        # Windows: pip can't replace a .exe held by the current process.
        # Detach a child pip and exit. User re-runs to use new version.
        creationflags = 0
        try:
            creationflags = (
                subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )
        except AttributeError:
            pass
        subprocess.Popen(cmd, close_fds=True, creationflags=creationflags)
        output.good("升级已在后台开始。**当前进程退出**，请稍等几秒后重跑 `codesync` 使用新版。")
        return 0

    # Unix: pip can overwrite in place; run sync and let user see output.
    r = subprocess.run(cmd)
    if r.returncode == 0:
        output.good("升级完成。下次跑 codesync 即为新版。")
        return 0
    output.err("升级失败。详见上方 pip 输出。")
    return r.returncode
