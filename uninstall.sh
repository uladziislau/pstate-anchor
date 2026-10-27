#!/usr/bin/env bash
# Uninstall P-State Anchor.
#
#   sudo ./uninstall.sh                       # remove from /usr/local
#   ./uninstall.sh --prefix "$HOME/.local"    # remove a per-user install
#   ./uninstall.sh --purge                    # also drop config, pin and desktop files
#
# Running instances are terminated so that the tray does not linger.
set -euo pipefail

PREFIX="/usr/local"
PURGE=0
PIN_DIR="/etc/pstate-anchor"

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

BIN="$PREFIX/bin/pstate-anchor"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/pstate-anchor.svg"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

package_installed=0
[ -e "$BIN" ] && package_installed=1

if pgrep -f "pstate-anchor" >/dev/null 2>&1; then
  pkill -f "pstate-anchor" || true
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
  # The watchdog runs from a systemd timer; leaving it behind means it keeps
  # firing against a missing binary (and would keep forcing a governor).
  GUARD_UNIT="/etc/systemd/system/pstate-anchor-guard.timer"
  if [ -e "$GUARD_UNIT" ] || [ -e "/usr/local/sbin/pstate-anchor-guard" ]; then
    as_root systemctl disable --now pstate-anchor-guard.timer 2>/dev/null || true
    as_root rm -f "$GUARD_UNIT" \
      "/etc/systemd/system/pstate-anchor-guard.service" \
      "$PREFIX/sbin/pstate-anchor-guard" \
      "/etc/default/pstate-anchor-guard"
    as_root systemctl daemon-reload 2>/dev/null || true
    echo "removed the governor guard and its policy"
  fi
  CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
  rm -f "$CONFIG_BASE/pstate-anchor.conf"
  rm -f "$CONFIG_BASE/autostart/pstate-anchor.desktop"
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/pstate-anchor.desktop"
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
