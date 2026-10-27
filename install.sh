#!/usr/bin/env bash
# Install NVIDIA P-State Switcher.
#
#   sudo ./install.sh                 # system-wide, /usr/local (recommended)
#   ./install.sh --prefix "$HOME/.local"   # per-user, no root
#
# The script is idempotent: re-running upgrades the binary in place and keeps
# the existing ~/.config/nvidia-pstate-switcher.conf untouched.
set -euo pipefail

PREFIX="/usr/local"
CREATE_PIN_DIR=1

usage() {
  cat <<'EOF'
Usage: ./install.sh [--prefix DIR] [--no-pin-dir] [--help]

  --prefix DIR    install under DIR (default: /usr/local)
  --no-pin-dir    do not create /etc/nvidia-pstate-switcher (governor pin)
  --help          show this help

Exit codes: 0 success, 1 usage/environment error.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --no-pin-dir) CREATE_PIN_DIR=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$PREFIX/bin"
SHARE_DIR="$PREFIX/share"
ICON_DIR="$SHARE_DIR/icons/hicolor/scalable/apps"
PIN_DIR="/etc/nvidia-pstate-switcher"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

need_write_access() {
  local dir="$1"
  if [ -w "$dir" ] || [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  if sudo -n true 2>/dev/null; then
    return 0
  fi
  echo "error: no write access to $dir and no passwordless sudo" >&2
  echo "       try: sudo ./install.sh   or   ./install.sh --prefix \"\$HOME/.local\"" >&2
  exit 1
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "error: Python 3.10+ is required (found $(python3 -V 2>&1))" >&2
  exit 1
}

need_write_access "$BIN_DIR"
as_root install -d "$BIN_DIR"
as_root install -m 0755 "$SRC_DIR/nvidia-pstate-switcher.py" "$BIN_DIR/nvidia-pstate-switcher"

if [ -f "$SRC_DIR/icons/nvidia-pstate-switcher.svg" ]; then
  as_root install -d "$ICON_DIR"
  as_root install -m 0644 "$SRC_DIR/icons/nvidia-pstate-switcher.svg" \
    "$ICON_DIR/nvidia-pstate-switcher.svg"
  command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -q -t -f "$SHARE_DIR/icons/hicolor" 2>/dev/null || true
fi

# Governor pin lives outside $HOME on purpose: system guards (for example a
# cpufreq watchdog timer) run as root and must be able to read the intent.
if [ "$CREATE_PIN_DIR" -eq 1 ]; then
  if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then
    as_root install -d -m 2775 "$PIN_DIR"
    if getent group power >/dev/null 2>&1; then
      as_root chgrp power "$PIN_DIR"
    fi
  else
    echo "note: skipped $PIN_DIR (no root); the tray will enforce the pin itself" >&2
  fi
fi

echo "installed: $BIN_DIR/nvidia-pstate-switcher"
echo

missing=()
command -v nvidia-smi >/dev/null 2>&1 || missing+=("nvidia-smi (NVIDIA driver)")
command -v nvidia-pstate >/dev/null 2>&1 || missing+=("nvidia-pstate (pip install nvidia-pstate)")
python3 -c 'import PyQt6' 2>/dev/null || missing+=("PyQt6 (dnf install python3-pyqt6)")
command -v gdbus >/dev/null 2>&1 || missing+=("gdbus (glib2, for KDE Power Profiles)")
command -v cpupower >/dev/null 2>&1 || missing+=("cpupower (kernel-tools)")

if [ ${#missing[@]} -gt 0 ]; then
  echo "optional dependencies not found:"
  for item in "${missing[@]}"; do
    echo "  - $item"
  done
  echo
fi

echo "verify:  $BIN_DIR/nvidia-pstate-switcher --dump-state"
echo "start:   $BIN_DIR/nvidia-pstate-switcher   (add to autostart from the menu)"
