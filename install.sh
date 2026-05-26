#!/usr/bin/env bash
# codesync installer for macOS / Linux / WSL.
#   curl -fsSL https://raw.githubusercontent.com/tinyvane/dev-tools/main/install.sh | bash
#
# This script:
#   1. Verifies Python >= 3.11 is on PATH.
#   2. Checks for git and gh CLI (warns if missing, doesn't auto-install).
#   3. pip install --user git+https://github.com/tinyvane/dev-tools.git
#   4. Ensures the Python user-base bin directory is on PATH (writes to ~/.zshrc, fallback ~/.bashrc).
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
# 3. pip install
# ----------------------------------------------------------------------
section "安装 codesync"
detail "$PY -m pip install --user --upgrade git+$REPO_URL"
"$PY" -m pip install --user --upgrade "git+$REPO_URL"

# ----------------------------------------------------------------------
# 4. ensure user-base/bin is on PATH
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# done
# ----------------------------------------------------------------------
section "完成"
if command -v codesync >/dev/null 2>&1; then
    ok "codesync $(codesync --version 2>/dev/null | awk '{print $2}') 已就绪"
else
    err "codesync 未在 PATH 上，请重开 shell 或 source $RC"
fi
echo
detail "下一步："
detail "  1. 重开 shell（或 \`source $RC\`）"
detail "  2. \`codesync migrate-config\`（如果你有 V1 config.local.ps1）"
detail "  3. \`codesync sync\` 第一次会生成 config.toml 模板并提示编辑"
