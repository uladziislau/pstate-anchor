#!/usr/bin/env bash
# Uninstall NVIDIA P-State Switcher.
#
#   sudo ./uninstall.sh                       # remove from /usr/local
#   ./uninstall.sh --prefix "$HOME/.local"    # remove a per-user install
#   ./uninstall.sh --purge                    # also drop config, pin and desktop files
#
# Running instances are terminated so that the tray does not linger.
set -euo pipefail

PREFIX="/usr/local"
PURGE=0
PIN_DIR="/etc/nvidia-pstate-switcher"

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [--prefix DIR] [--purge] [--help]

  --prefix DIR   prefix used at install time (default: /usr/local)
  --purge        also remove config, governor pin, autostart and desktop entries
  --help         show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --purge) PURGE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

BIN="$PREFIX/bin/nvidia-pstate-switcher"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/nvidia-pstate-switcher.svg"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

package_installed=0
[ -e "$BIN" ] && package_installed=1

if pgrep -f "nvidia-pstate-switcher" >/dev/null 2>&1; then
  pkill -f "nvidia-pstate-switcher" || true
  echo "stopped running instance"
fi

if [ -e "$BIN" ]; then
  as_root rm -f "$BIN"
  echo "removed $BIN"
fi

if [ -e "$ICON" ]; then
  as_root rm -f "$ICON"
  echo "removed $ICON"
fi

if [ "$PURGE" -eq 1 ]; then
  CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
  rm -f "$CONFIG_BASE/nvidia-pstate-switcher.conf"
  rm -f "$CONFIG_BASE/autostart/nvidia-pstate-switcher.desktop"
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/nvidia-pstate-switcher.desktop"
  echo "removed user config, autostart and desktop entries"
  if [ -d "$PIN_DIR" ] && { [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; }; then
    as_root rm -rf "$PIN_DIR"
    echo "removed $PIN_DIR"
  fi
fi

if [ "$package_installed" -eq 0 ]; then
  echo "note: nothing was installed at $BIN" >&2
fi

echo "uninstalled"
