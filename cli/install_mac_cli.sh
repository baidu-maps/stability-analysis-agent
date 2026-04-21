#!/usr/bin/env bash
# install_mac_cli.sh — 将 StabilityAnalyzer 安装到 PATH（默认 ~/.local/bin/sa-agent）
#
# 默认安装到 ~/.local/bin，并在 ~/.zshrc 中幂等写入 PATH（无需用户手动 export）。
#
# 典型用法（解压 Release 后，与本目录下的 StabilityAnalyzer 同级）:
#   chmod +x install.sh StabilityAnalyzer
#   ./install.sh
#
# 从仓库根目录指定二进制:
#   bash cli/install_mac_cli.sh --binary dist/StabilityAnalyzer
#
# 不修改 shell 配置（自行保证 PATH）:
#   ./install.sh --no-modify-shell
#
# 系统级安装（可选，需 sudo）:
#   sudo bash install.sh --system
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_BIN_NAME="StabilityAnalyzer"
INSTALLED_NAME_DEFAULT="sa-agent"

MARK_BEGIN='# >>> sa-agent-cli PATH >>>'
MARK_END='# <<< sa-agent-cli PATH <<<'

BINARY=""
PREFIX="${INSTALL_PREFIX:-$HOME/.local}"
COMMAND_NAME="${INSTALL_COMMAND_NAME:-$INSTALLED_NAME_DEFAULT}"
SYSTEM=0
DRY_RUN=0
UNINSTALL=0
NO_MODIFY_SHELL=0

usage() {
  cat <<'EOF'
Usage: install_mac_cli.sh [options]

Install the StabilityAnalyzer CLI to a directory on your PATH.

Options:
  --binary PATH       Path to the StabilityAnalyzer executable
                      (default: ./StabilityAnalyzer next to this script, else ./StabilityAnalyzer in cwd)
  --prefix DIR        Install root; binaries are placed in DIR/bin (default: ~/.local)
  --command NAME      Installed command filename (default: sa-agent)
  --system            Same as --prefix /usr/local (often requires sudo)
  --no-modify-shell   Do not modify ~/.zshrc (default: for ~/.local install, append PATH block idempotently)
  --dry-run           Print actions without copying or removing files
  --uninstall         Remove PREFIX/bin/COMMAND_NAME and managed ~/.zshrc block when prefix is ~/.local
  -h, --help          Show this help

Environment:
  INSTALL_PREFIX         Default for --prefix
  INSTALL_COMMAND_NAME   Default for --command

Examples:
  ./install.sh
  ./install.sh --prefix "$HOME/.local" --command sa-agent
  ./install.sh --uninstall
EOF
}

abs_path() {
  local d f
  d="$(cd "$(dirname "$1")" && pwd)"
  f="$(basename "$1")"
  echo "$d/$f"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --binary)
      BINARY="${2:-}"
      shift 2
      ;;
    --prefix)
      PREFIX="${2:-}"
      shift 2
      ;;
    --command|--command-name)
      COMMAND_NAME="${2:-}"
      shift 2
      ;;
    --system)
      SYSTEM=1
      shift
      ;;
    --no-modify-shell)
      NO_MODIFY_SHELL=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$SYSTEM" -eq 1 ]]; then
  PREFIX="/usr/local"
fi

if [[ -z "$PREFIX" ]]; then
  echo "ERROR: empty --prefix" >&2
  exit 1
fi
if [[ -z "$COMMAND_NAME" ]]; then
  echo "ERROR: empty --command" >&2
  exit 1
fi

BIN_DIR="$PREFIX/bin"
TARGET="$BIN_DIR/$COMMAND_NAME"
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"

resolve_source_binary() {
  if [[ -n "$BINARY" ]]; then
    if [[ ! -f "$BINARY" ]]; then
      echo "ERROR: --binary not a file: $BINARY" >&2
      exit 1
    fi
    abs_path "$BINARY"
    return
  fi
  if [[ -f "$SCRIPT_DIR/$DEFAULT_BIN_NAME" ]]; then
    abs_path "$SCRIPT_DIR/$DEFAULT_BIN_NAME"
    return
  fi
  local cwd_bin
  cwd_bin="$(pwd)/$DEFAULT_BIN_NAME"
  if [[ -f "$cwd_bin" ]]; then
    abs_path "$cwd_bin"
    return
  fi
  echo "ERROR: could not find $DEFAULT_BIN_NAME. Use --binary /path/to/$DEFAULT_BIN_NAME" >&2
  exit 1
}

# 安装前缀在解析后是否等于 ~/.local（用于决定是否维护 ~/.zshrc 中的 PATH 块）
is_resolved_home_local_prefix() {
  mkdir -p "$HOME/.local" "$PREFIX" 2>/dev/null || true
  local rp rhl
  rp="$(cd "$PREFIX" 2>/dev/null && pwd -P)" || return 1
  rhl="$(cd "$HOME/.local" 2>/dev/null && pwd -P)" || return 1
  [[ "$rp" == "$rhl" ]]
}

path_hint_manual() {
  local d="$BIN_DIR"
  case ":${PATH:-}:" in
    *":$d:"*) ;;
    *)
      echo ""
      echo "Note: $d is not on your PATH. Add to your shell rc, for example:"
      echo "  export PATH=\"$d:\$PATH\""
      ;;
  esac
}

remove_zshrc_path_block() {
  [[ -f "$ZSHRC" ]] || return 0
  if ! grep -Fq "$MARK_BEGIN" "$ZSHRC" 2>/dev/null; then
    return 0
  fi
  local tmp
  tmp="$(mktemp)"
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b {skip=1; next}
    $0 == e {skip=0; next}
    !skip {print}
  ' "$ZSHRC" > "$tmp" && mv "$tmp" "$ZSHRC"
  echo "Removed managed PATH block from $ZSHRC"
}

append_zshrc_path_block() {
  if [[ "$NO_MODIFY_SHELL" -eq 1 ]]; then
    return 0
  fi
  if ! is_resolved_home_local_prefix; then
    return 0
  fi
  if [[ -f "$ZSHRC" ]] && grep -Fq "$MARK_BEGIN" "$ZSHRC" 2>/dev/null; then
    echo "PATH block already present in $ZSHRC (idempotent skip)."
    return 0
  fi
  {
    echo ""
    echo "$MARK_BEGIN"
    echo 'export PATH="$HOME/.local/bin:$PATH"'
    echo "$MARK_END"
  } >> "$ZSHRC"
  echo "Updated $ZSHRC: new zsh sessions will include \$HOME/.local/bin in PATH."
}

if [[ "$UNINSTALL" -eq 1 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] would remove (if exists): $TARGET"
    if is_resolved_home_local_prefix && [[ "$NO_MODIFY_SHELL" -eq 0 ]]; then
      echo "[dry-run] would remove managed PATH block from $ZSHRC (if present)"
    fi
    exit 0
  fi
  if is_resolved_home_local_prefix && [[ "$NO_MODIFY_SHELL" -eq 0 ]]; then
    remove_zshrc_path_block
  fi
  if [[ -f "$TARGET" ]]; then
    rm -f "$TARGET"
    echo "Removed: $TARGET"
  else
    echo "Nothing to remove (missing): $TARGET"
  fi
  exit 0
fi

SRC="$(resolve_source_binary)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] would mkdir -p $BIN_DIR"
  echo "[dry-run] would cp $SRC -> $TARGET && chmod +x $TARGET"
  echo "[dry-run] would xattr -cr $TARGET (best-effort)"
  if [[ "$NO_MODIFY_SHELL" -eq 0 ]] && is_resolved_home_local_prefix; then
    if [[ -f "$ZSHRC" ]] && grep -Fq "$MARK_BEGIN" "$ZSHRC" 2>/dev/null; then
      echo "[dry-run] ~/.zshrc already contains PATH block (would skip append)"
    else
      echo "[dry-run] would append PATH block to $ZSHRC"
    fi
  elif [[ "$NO_MODIFY_SHELL" -eq 1 ]]; then
    echo "[dry-run] would not modify shell rc (--no-modify-shell)"
  fi
  exit 0
fi

mkdir -p "$BIN_DIR"
if [[ ! -d "$BIN_DIR" ]]; then
  echo "ERROR: could not create $BIN_DIR" >&2
  exit 1
fi

if [[ ! -w "$BIN_DIR" ]]; then
  echo "ERROR: no write permission to $BIN_DIR" >&2
  echo "Try: sudo $0 --system   or choose a prefix under your home directory." >&2
  exit 1
fi

cp -f "$SRC" "$TARGET"
chmod +x "$TARGET"
xattr -cr "$TARGET" 2>/dev/null || true

echo "Installed: $TARGET"

if [[ "$NO_MODIFY_SHELL" -eq 1 ]]; then
  path_hint_manual
elif is_resolved_home_local_prefix; then
  append_zshrc_path_block
  if [[ -n "${SHELL:-}" ]] && [[ "${SHELL##*/}" != "zsh" ]]; then
    echo ""
    echo "Note: your login shell is not zsh ($SHELL). PATH was written to $ZSHRC for zsh; use zsh or add the same export to your shell's rc file."
  fi
  echo "Run: source $ZSHRC   or open a new terminal tab, then: $COMMAND_NAME --help"
else
  path_hint_manual
  echo "Try: $COMMAND_NAME --help (after adding $BIN_DIR to PATH)"
fi
