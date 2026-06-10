#!/usr/bin/env bash
# codesync installer for macOS / Linux / WSL.
#   curl -fsSL https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh | bash
#
# This script:
#   1. Verifies Python >= 3.11 is on PATH.
#   2. Checks for git and gh CLI (warns if missing, doesn't auto-install).
#   3. Detects PEP 668 (externally-managed Python, e.g. Homebrew / recent Debian):
#        - PEP 668 marked:    pipx install --force git+... (per-tool venv)
#                             auto-installs pipx via brew/apt/dnf/yum/pacman if missing
#        - Not marked:        pip install --user --upgrade git+...
#   4. Ensures the right bin directory is on PATH:
#        - pipx path: pipx ensurepath
#        - pip --user path: append snippet to ~/.zshrc or ~/.bashrc
#
# Idempotent: re-running upgrades codesync in place.

set -euo pipefail

REPO_URL="https://github.com/tinyvane/dev-tools.git"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

# GitHub mirrors tried (in order) when github.com is unreachable and the user
# didn't set CODESYNC_GH_MIRROR. These are public ghproxy-style prefixes; they
# come and go, hence the env-var escape hatch. Form: <mirror>/https://github.com/...
DEFAULT_MIRRORS="https://ghfast.top https://gh-proxy.com https://mirror.ghproxy.com"
GH_MIRROR=""

color() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
section() { printf '\n%s\n' "$(color '36;1' "▸ $1")"; }
ok()      { printf '  %s\n' "$(color '32' "✓ $1")"; }
warn()    { printf '  %s\n' "$(color '33' "⚠ $1")"; }
err()     { printf '  %s\n' "$(color '31' "✗ $1")" >&2; }
detail()  { printf '  %s\n' "$(color '90' "$1")"; }

# Reachability probe. curl (we were likely piped through it) → wget → give up.
# Any HTTP response counts as reachable; we only care that TLS completes.
probe_url() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --connect-timeout 5 --max-time 12 -o /dev/null "$1" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=12 -O /dev/null "$1" >/dev/null 2>&1
    else
        return 1
    fi
}

# Decide whether to route GitHub through a mirror (for users behind the GFW).
#   CODESYNC_GH_MIRROR set  → trust it, no probing.
#   unset + github.com OK   → direct.
#   unset + github.com dead → first reachable DEFAULT_MIRRORS entry.
resolve_gh_mirror() {
    if [ -n "${CODESYNC_GH_MIRROR:-}" ]; then
        GH_MIRROR="${CODESYNC_GH_MIRROR%/}"
        ok "使用指定 GitHub 镜像: $GH_MIRROR"
        return
    fi
    section "网络探测"
    if probe_url "https://github.com/tinyvane/dev-tools"; then
        ok "github.com 直连可用"
        return
    fi
    warn "github.com 直连失败，自动探测国内镜像…"
    for m in $DEFAULT_MIRRORS; do
        if probe_url "${m%/}/https://github.com/tinyvane/dev-tools"; then
            GH_MIRROR="${m%/}"
            ok "使用 GitHub 镜像: $GH_MIRROR"
            return
        fi
    done
    warn "所有镜像都探测失败，仍走直连（可能失败）。"
    detail "可手动指定: CODESYNC_GH_MIRROR=https://你的镜像 重跑本脚本"
}

# The git+ spec pip/pipx installs, mirror-rewritten when GH_MIRROR is set.
gh_git_spec() {
    if [ -n "$GH_MIRROR" ]; then
        printf 'git+%s/%s' "$GH_MIRROR" "$REPO_URL"
    else
        printf 'git+%s' "$REPO_URL"
    fi
}

# When a GitHub mirror is in play the user is likely behind the GFW, where
# pypi.org (needed for the setuptools/wheel build deps) is slow/flaky. Route
# pip through a CN PyPI mirror via PIP_INDEX_URL (honored by both pip and the
# pip that pipx drives). CODESYNC_PIP_INDEX overrides; an already-set
# PIP_INDEX_URL is respected.
resolve_pip_index() {
    if [ -n "${CODESYNC_PIP_INDEX:-}" ]; then
        export PIP_INDEX_URL="$CODESYNC_PIP_INDEX"
        detail "pip index: $PIP_INDEX_URL (CODESYNC_PIP_INDEX)"
    elif [ -n "$GH_MIRROR" ] && [ -z "${PIP_INDEX_URL:-}" ]; then
        export PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
        detail "镜像环境：pip 构建依赖走清华 PyPI 镜像（设 CODESYNC_PIP_INDEX 可改）"
    fi
}

# Append a PATH snippet for $1 to the user's shell rc (idempotent via markers).
add_dir_to_rc() {
    _dir="$1"
    _rc=""
    if [ -n "${ZSH_VERSION:-}" ] || [ -f "$HOME/.zshrc" ]; then
        _rc="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        _rc="$HOME/.bashrc"
    else
        _rc="$HOME/.zshrc"   # create one
    fi
    if [ -f "$_rc" ] && grep -qF '# === codesync begin ===' "$_rc"; then
        detail "$_rc 中已有 codesync 段落，跳过"
        return
    fi
    cat >> "$_rc" <<EOF

# === codesync begin ===
# Added by codesync installer ($(date '+%Y-%m-%d')).
if [ -d "$_dir" ]; then
    case ":\$PATH:" in
        *":$_dir:"*) ;;
        *) export PATH="$_dir:\$PATH" ;;
    esac
fi
# === codesync end ===
EOF
    ok "已写入 $_rc"
}

# Self-managed venv install — the robust PEP 668 path when there's no MODERN
# pipx. A venv is its own environment (not externally-managed), so pip works
# normally inside it: we control the pip version, and `codesync --update`
# upgrades in place because _in_venv() is true for the venv's python.
# Needs $PY (set later by the python-finding section) at call time.
install_via_venv() {
    section "安装 codesync (自管理 venv)"
    _venv="$HOME/.local/share/codesync/venv"
    detail "venv: $_venv"

    if ! "$PY" -m venv --help >/dev/null 2>&1; then
        err "$PY 缺少 venv 模块（Debian/Ubuntu/麒麟 把它拆成单独的包）"
        detail "装上再重跑： sudo apt install python3-venv   # 或对应版本 python3.11-venv"
        exit 1
    fi

    if [ ! -x "$_venv/bin/python" ]; then
        rm -rf "$_venv"
        if ! "$PY" -m venv "$_venv"; then
            err "创建 venv 失败（多半缺 ensurepip / python3-venv）"
            detail "装上再重跑： sudo apt install python3-venv   # 或 python3.11-venv"
            exit 1
        fi
    fi

    _vpy="$_venv/bin/python"
    # Refresh build tooling inside the venv so the PEP 517 build of codesync
    # works even if the base python shipped an ancient pip. Honors PIP_INDEX_URL.
    detail "升级 venv 内 pip / setuptools / wheel"
    "$_vpy" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 \
        || warn "venv 内升级构建工具失败，继续尝试安装"

    _spec="$(gh_git_spec)"
    detail "$_vpy -m pip install --upgrade $_spec"
    "$_vpy" -m pip install --upgrade "$_spec"

    mkdir -p "$HOME/.local/bin"
    ln -sf "$_venv/bin/codesync" "$HOME/.local/bin/codesync"
    ok "已链接 ~/.local/bin/codesync -> $_venv/bin/codesync"

    add_dir_to_rc "$HOME/.local/bin"
    export PATH="$HOME/.local/bin:$PATH"
}

# ----------------------------------------------------------------------
# 1. find a usable python
# ----------------------------------------------------------------------
section "查找 Python (需要 >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR})"
PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        v=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
        major=${v%.*}
        minor=${v#*.}
        if [ "$major" -gt "$MIN_PY_MAJOR" ] || { [ "$major" -eq "$MIN_PY_MAJOR" ] && [ "$minor" -ge "$MIN_PY_MINOR" ]; }; then
            PY="$candidate"
            ok "找到 $candidate (Python $v)"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    err "未找到 Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
    detail "  macOS:  brew install python@3.13"
    detail "  Ubuntu: sudo apt install python3.12 python3.12-venv"
    exit 1
fi

# ----------------------------------------------------------------------
# 2. dependency hints (don't auto-install — too OS-specific)
# ----------------------------------------------------------------------
section "依赖检查"

if command -v git >/dev/null 2>&1; then ok "git $(git --version | awk '{print $3}')"; else err "git 未安装，请先装 git"; exit 1; fi

if command -v gh >/dev/null 2>&1; then
    ok "gh $(gh --version | head -1 | awk '{print $3}')"
else
    warn "gh (GitHub CLI) 未安装 — auto_clone 功能需要它"
    detail "  macOS:  brew install gh"
    detail "  Ubuntu: sudo apt install gh"
fi

# ----------------------------------------------------------------------
# 2.5 GitHub / PyPI mirror resolution (GFW-friendly)
# ----------------------------------------------------------------------
resolve_gh_mirror
resolve_pip_index

# ----------------------------------------------------------------------
# 3. install path: pip --user OR pipx, depending on PEP 668
# ----------------------------------------------------------------------
# PEP 668: Homebrew Python (macOS) and recent Debian/Ubuntu mark their stdlib
# directory with an EXTERNALLY-MANAGED file, which makes `pip install --user`
# refuse. The PEP 668-recommended alternative is pipx (per-tool isolated venv).
EXTERNALLY_MANAGED=0
STDLIB=$("$PY" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))' 2>/dev/null || echo "")
if [ -n "$STDLIB" ] && [ -f "$STDLIB/EXTERNALLY-MANAGED" ]; then
    EXTERNALLY_MANAGED=1
fi

if [ "$EXTERNALLY_MANAGED" = "1" ]; then
    # PEP 668 externally-managed Python. Two install paths:
    #   - a MODERN pipx (>= 1.0) if already present → keep the well-tested flow
    #   - otherwise a self-managed venv (no sudo, no pipx-version roulette)
    #
    # We deliberately do NOT apt-install pipx anymore. Some distros (notably
    # Kylin / older Debian) ship pipx 0.12.x, which can't install from a git URL
    # ("Package cannot be a url") and bundles a pip too old to build a modern
    # pyproject. The venv path sidesteps all of that — a venv is its own
    # environment, so PEP 668 doesn't apply and we control the pip version.
    section "安装 codesync (PEP 668 externally-managed)"
    detail "$PY 是 externally-managed Python (PEP 668)。"

    USE_PIPX=0
    if command -v pipx >/dev/null 2>&1; then
        PIPX_VER=$(pipx --version 2>/dev/null | head -1 | tr -d '[:space:]')
        PIPX_MAJOR=${PIPX_VER%%.*}
        case "$PIPX_MAJOR" in ''|*[!0-9]*) PIPX_MAJOR=0 ;; esac
        if [ "$PIPX_MAJOR" -ge 1 ]; then
            USE_PIPX=1
            ok "检测到 pipx $PIPX_VER（modern）"
        else
            warn "pipx $PIPX_VER 太旧（不支持从 git URL 安装），改用自管理 venv"
        fi
    else
        detail "未检测到 pipx，使用自管理 venv（无需 sudo / 无需装 pipx）"
    fi

    if [ "$USE_PIPX" = "1" ]; then
        PIPX_SPEC="$(gh_git_spec)"
        detail "pipx install --force $PIPX_SPEC"
        # --force makes "install" idempotent: overwrites existing install with
        # the new version, whether or not codesync was already installed.
        pipx install --force "$PIPX_SPEC"

        section "PATH 配置 (pipx managed)"
        # pipx ensurepath is idempotent — adds ~/.local/bin to ~/.zshrc / ~/.bashrc.
        pipx ensurepath >/dev/null 2>&1 || true
        ok "pipx 已确保 ~/.local/bin 在 PATH (写入了 ~/.zshrc 或 ~/.bashrc)"
        detail "（如果是首次装 pipx，重开 shell 才生效）"
        export PATH="$HOME/.local/bin:$PATH"
    else
        install_via_venv
    fi

else
    # ----- pip --user flow (traditional, non-PEP-668 Python) -----
    section "安装 codesync"
    PIP_SPEC="$(gh_git_spec)"
    detail "$PY -m pip install --user --upgrade $PIP_SPEC"
    "$PY" -m pip install --user --upgrade "$PIP_SPEC"

    section "PATH 配置"
    USER_BASE=$("$PY" -m site --user-base)
    USER_BIN="$USER_BASE/bin"

    if [ ! -d "$USER_BIN" ]; then
        warn "$USER_BIN 不存在（pip 可能装到了别处）"
    fi

    add_dir_to_rc "$USER_BIN"
    # Make codesync findable in *this* shell too.
    export PATH="$USER_BIN:$PATH"
fi

# ----------------------------------------------------------------------
# done
# ----------------------------------------------------------------------
section "完成"
if command -v codesync >/dev/null 2>&1; then
    # NR==1: --version may print extra lines (latest-version note); only the
    # first line is the parseable "codesync X.Y.Z".
    ok "codesync $(codesync --version 2>/dev/null | awk 'NR==1{print $2}') 已就绪"
else
    err "codesync 未在 PATH 上，请重开 shell"
fi
echo
detail "下一步："
detail "  1. 重开 shell（让 PATH 在新会话里生效）"
detail "  2. \`codesync migrate-config\`（如果你有 V1 config.local.ps1）"
detail "  3. \`codesync sync\` 第一次会生成 config.toml 模板并提示编辑"
