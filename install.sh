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

color() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
section() { printf '\n%s\n' "$(color '36;1' "▸ $1")"; }
ok()      { printf '  %s\n' "$(color '32' "✓ $1")"; }
warn()    { printf '  %s\n' "$(color '33' "⚠ $1")"; }
err()     { printf '  %s\n' "$(color '31' "✗ $1")" >&2; }
detail()  { printf '  %s\n' "$(color '90' "$1")"; }

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
    # ----- pipx flow (PEP 668 externally-managed Python) -----
    section "安装 codesync (pipx)"
    detail "$PY 是 externally-managed Python (PEP 668)，改用 pipx (每个工具单独 venv)。"

    if ! command -v pipx >/dev/null 2>&1; then
        warn "pipx 未装。在 externally-managed Python 上，pipx 是装 Python 应用的标准方式。"
        # Try to auto-install via the detected OS package manager.
        # Use uname instead of $OSTYPE/$EUID since some pipes set neither cleanly.
        os_name="$(uname -s 2>/dev/null || echo unknown)"
        installer_cmd=""
        installer_label=""
        case "$os_name" in
            Darwin)
                if command -v brew >/dev/null 2>&1; then
                    installer_cmd="brew install pipx"
                    installer_label="Homebrew"
                fi
                ;;
            Linux)
                if command -v apt-get >/dev/null 2>&1; then
                    installer_cmd="sudo apt-get update && sudo apt-get install -y pipx"
                    installer_label="apt (Debian/Ubuntu, 需要 sudo)"
                elif command -v dnf >/dev/null 2>&1; then
                    installer_cmd="sudo dnf install -y pipx"
                    installer_label="dnf (Fedora/RHEL, 需要 sudo)"
                elif command -v yum >/dev/null 2>&1; then
                    installer_cmd="sudo yum install -y pipx"
                    installer_label="yum (老 RHEL/CentOS, 需要 sudo)"
                elif command -v pacman >/dev/null 2>&1; then
                    installer_cmd="sudo pacman -S --noconfirm python-pipx"
                    installer_label="pacman (Arch, 需要 sudo)"
                fi
                ;;
        esac

        if [ -z "$installer_cmd" ]; then
            err "未识别的 OS / 包管理器，无法自动装 pipx。"
            detail "OS: $os_name"
            detail "请手动装 pipx，然后重跑本脚本："
            detail "  macOS（先装 Homebrew）: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            detail "  其他:                   见 https://pipx.pypa.io/stable/installation/"
            exit 1
        fi

        # NOTE: braces around ${installer_label} are required.
        # bash 3.2 (still macOS default) under `set -u` mishandles `$var<non-ASCII>`
        # — it tries to include the UTF-8 lead byte of `。` (0xE3) in the variable
        # name, then fails with `installer_label?: unbound variable`. Newer bash
        # parses it correctly. Use ${...} to defensively delimit.
        detail "检测到 ${installer_label}。"
        detail "将运行: $installer_cmd"
        detail "（5 秒后开始，Ctrl+C 可取消）"
        for i in 5 4 3 2 1; do
            printf '  %s\r' "$(color '90' "$i...")"
            sleep 1
        done
        printf '\n'

        # Run the install. If it needs sudo and user hasn't pre-authenticated,
        # the sudo prompt should still appear in a curl|bash flow because the
        # bash subshell is attached to the terminal (only stdin is the pipe).
        if ! eval "$installer_cmd"; then
            err "pipx 装失败 (上面是 $installer_label 的输出)"
            detail "手动尝试上面的命令排查，然后重跑本脚本。"
            exit 1
        fi

        # Verify pipx is now callable (the install dir should be on default PATH).
        if ! command -v pipx >/dev/null 2>&1; then
            err "pipx 装了但命令找不到 —— PATH 没刷新？"
            detail "开新终端后重跑本脚本即可。"
            exit 1
        fi
        ok "pipx 装好了"
    fi
    PIPX_VER=$(pipx --version 2>/dev/null | head -1 || echo "?")
    ok "pipx $PIPX_VER 已就绪"

    detail "pipx install --force git+$REPO_URL"
    # --force makes "install" idempotent: overwrites existing install with the
    # new version. Equivalent to `pipx upgrade` semantics but works whether or
    # not codesync was already installed.
    pipx install --force "git+$REPO_URL"

    section "PATH 配置 (pipx managed)"
    # pipx ensurepath is idempotent — adds ~/.local/bin to ~/.zshrc / ~/.bashrc if missing.
    pipx ensurepath >/dev/null 2>&1 || true
    ok "pipx 已确保 ~/.local/bin 在 PATH (写入了 ~/.zshrc 或 ~/.bashrc)"
    detail "（如果是首次装 pipx，重开 shell 才生效）"

    # Make codesync findable in *this* shell too.
    export PATH="$HOME/.local/bin:$PATH"

else
    # ----- pip --user flow (traditional, non-PEP-668 Python) -----
    section "安装 codesync"
    detail "$PY -m pip install --user --upgrade git+$REPO_URL"
    "$PY" -m pip install --user --upgrade "git+$REPO_URL"

    section "PATH 配置"
    USER_BASE=$("$PY" -m site --user-base)
    USER_BIN="$USER_BASE/bin"

    if [ ! -d "$USER_BIN" ]; then
        warn "$USER_BIN 不存在（pip 可能装到了别处）"
    fi

    # Pick rc file: zsh default, bash fallback.
    RC=""
    if [ -n "${ZSH_VERSION:-}" ] || [ -f "$HOME/.zshrc" ]; then
        RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        RC="$HOME/.bashrc"
    else
        RC="$HOME/.zshrc"   # create one
    fi

    MARKER_START='# === codesync begin ==='
    MARKER_END='# === codesync end ==='

    if [ -f "$RC" ] && grep -qF "$MARKER_START" "$RC"; then
        detail "$RC 中已有 codesync 段落，跳过"
    else
        cat >> "$RC" <<EOF

$MARKER_START
# Added by codesync installer ($(date '+%Y-%m-%d')).
if [ -d "$USER_BIN" ]; then
    case ":\$PATH:" in
        *":$USER_BIN:"*) ;;
        *) export PATH="$USER_BIN:\$PATH" ;;
    esac
fi
$MARKER_END
EOF
        ok "已写入 $RC"
    fi

    # Make codesync findable in *this* shell too.
    export PATH="$USER_BIN:$PATH"
fi

# ----------------------------------------------------------------------
# done
# ----------------------------------------------------------------------
section "完成"
if command -v codesync >/dev/null 2>&1; then
    ok "codesync $(codesync --version 2>/dev/null | awk '{print $2}') 已就绪"
else
    err "codesync 未在 PATH 上，请重开 shell"
fi
echo
detail "下一步："
detail "  1. 重开 shell（让 PATH 在新会话里生效）"
detail "  2. \`codesync migrate-config\`（如果你有 V1 config.local.ps1）"
detail "  3. \`codesync sync\` 第一次会生成 config.toml 模板并提示编辑"
