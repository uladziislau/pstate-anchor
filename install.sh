#!/usr/bin/env bash
# Install P-State Anchor.
#
#   sudo ./install.sh                     # system-wide, /usr/local (recommended)
#   ./install.sh --prefix "$HOME/.local"  # per-user, no root
#
# The script is idempotent: re-running upgrades the binary in place and keeps
# the existing ~/.config/pstate-anchor.conf untouched. It only asks for
# sudo where the target directory is not writable by the current user.
set -euo pipefail

PREFIX="/usr/local"
CREATE_PIN_DIR=1
WITH_GUARD=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--prefix DIR] [--no-pin-dir] [--help]

  --prefix DIR    install under DIR (default: /usr/local)
  --no-pin-dir    do not create /etc/pstate-anchor (governor pin)
  --with-guard    install and enable pstate-anchor-guard (needs root):
                  keeps the pinned governor when something flips it
  --help          show this help

Exit codes: 0 success, 1 usage/environment error.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --no-pin-dir) CREATE_PIN_DIR=0; shift ;;
    --with-guard) WITH_GUARD=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$PREFIX/bin"
SHARE_DIR="$PREFIX/share"
ICON_DIR="$SHARE_DIR/icons/hicolor/scalable/apps"
PIN_DIR="/etc/pstate-anchor"

# Is the final path (or its nearest existing parent) writable by this user?
writable_target() {
  local target="$1" parent
  if [ -e "$target" ]; then
    [ -w "$target" ]
    return
  fi
  parent="$(dirname "$target")"
  while [ ! -e "$parent" ] && [ "$parent" != "/" ]; do
    parent="$(dirname "$parent")"
  done
  [ -w "$parent" ]
}

# Run a command as root only when the destination needs it.
place() {
  local mode="$1" src="$2" dst="$3"
  if writable_target "$dst"; then
    mkdir -p "$(dirname "$dst")"
    install -m "$mode" "$src" "$dst"
  elif sudo -n true 2>/dev/null; then
    sudo install -D -m "$mode" "$src" "$dst"
  else
    echo "error: $dst needs root (no passwordless sudo available)" >&2
    echo "       run: sudo ./install.sh  —  or  ./install.sh --prefix \"\$HOME/.local\"" >&2
    exit 1
  fi
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "error: Python 3.10+ is required (found $(python3 -V 2>&1))" >&2
  exit 1
}

place 0755 "$SRC_DIR/pstate-anchor.py" "$BIN_DIR/pstate-anchor"

if [ -f "$SRC_DIR/icons/pstate-anchor.svg" ]; then
  place 0644 "$SRC_DIR/icons/pstate-anchor.svg" \
    "$ICON_DIR/pstate-anchor.svg"
  if command -v gtk-update-icon-cache >/dev/null 2>&1 &&
     [ -d "$SHARE_DIR/icons/hicolor" ]; then
    gtk-update-icon-cache -q -t -f "$SHARE_DIR/icons/hicolor" 2>/dev/null || true
  fi
fi

# Governor pin lives outside $HOME on purpose: root-owned watchdogs must be able
# to read the intent. Without root the tray falls back to a per-user pin file.
if [ "$CREATE_PIN_DIR" -eq 1 ]; then
  if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then
    if [ "$(id -u)" -eq 0 ]; then
      install -d -m 2775 "$PIN_DIR"
      if getent group power >/dev/null 2>&1; then
        chgrp power "$PIN_DIR"
      fi
    else
      sudo install -d -m 2775 "$PIN_DIR"
      if getent group power >/dev/null 2>&1; then
        sudo chgrp power "$PIN_DIR"
      fi
    fi
  else
    echo "note: skipped $PIN_DIR (no root); the tray will keep the pin per user" >&2
  fi
fi

if [ "$WITH_GUARD" -eq 1 ]; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "error: --with-guard installs systemd units, run it under sudo" >&2
    exit 1
  fi
  install -m 0755 "$SRC_DIR/contrib/pstate-anchor-guard" \
    "$PREFIX/sbin/pstate-anchor-guard"
  install -m 0644 "$SRC_DIR/contrib/pstate-anchor-guard.service" \
    "$SRC_DIR/contrib/pstate-anchor-guard.timer" /etc/systemd/system/
  if [ ! -f /etc/default/pstate-anchor-guard ]; then
    install -m 0644 "$SRC_DIR/contrib/pstate-anchor-guard.env" \
      /etc/default/pstate-anchor-guard
  fi
  # A pre-rename watchdog would fight the guard: stop it, keep the files.
  if systemctl list-unit-files cpufreq-schedutil-guard.timer >/dev/null 2>&1; then
    systemctl disable --now cpufreq-schedutil-guard.timer 2>/dev/null || true
    echo "note: disabled legacy cpufreq-schedutil-guard.timer"
  fi
  systemctl daemon-reload
  systemctl enable --now pstate-anchor-guard.timer
  echo "guard:   $(systemctl is-enabled pstate-anchor-guard.timer) / $(systemctl is-active pstate-anchor-guard.timer)"
  echo "policy:  /etc/default/pstate-anchor-guard (DEFAULT_GOVERNOR)"
  echo "check:   $PREFIX/sbin/pstate-anchor-guard --check"
fi

echo "installed: $BIN_DIR/pstate-anchor"
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

echo "verify:  $BIN_DIR/pstate-anchor --dump-state"
echo "start:   $BIN_DIR/pstate-anchor"
echo "autostart: $BIN_DIR/pstate-anchor --autostart on"
