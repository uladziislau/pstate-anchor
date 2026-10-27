#!/usr/bin/env bash
# Example cpufreq guard that respects the nvidia-pstate-switcher governor pin.
#
# Purpose: some systems force a governor back (for example to keep schedutil
# for sched_ext, or to undo power-profiles-daemon setting `powersave`). Such a
# guard must not fight the user, so it honours the pin written by the tray:
#
#   /etc/nvidia-pstate-switcher/governor-pin   (override: PSTATE_PIN_FILE)
#
# Behaviour
#   - pin present and valid  → enforce the pinned governor
#   - no pin                 → enforce $DEFAULT_GOVERNOR (schedutil)
#   - value unavailable      → do nothing, log once
#
# Install (systemd timer, every 15 s):
#   sudo install -m 0755 examples/cpufreq-guard.sh /usr/local/sbin/cpufreq-guard
#   sudo tee /etc/systemd/system/cpufreq-guard.service >/dev/null <<'EOF'
#   [Unit]
#   Description=Keep the pinned cpufreq governor
#   [Service]
#   Type=oneshot
#   ExecStart=/usr/local/sbin/cpufreq-guard
#   EOF
#   sudo tee /etc/systemd/system/cpufreq-guard.timer >/dev/null <<'EOF'
#   [Unit]
#   Description=Check the cpufreq governor every 15 s
#   [Timer]
#   OnBootSec=60
#   OnUnitActiveSec=15
#   AccuracySec=5
#   [Install]
#   WantedBy=timers.target
#   EOF
#   sudo systemctl enable --now cpufreq-guard.timer
set -euo pipefail

GOV_PATH="${PSTATE_GOVERNOR_PATH:-/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor}"
AVAILABLE_PATH="${PSTATE_AVAILABLE_GOVERNORS_PATH:-/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors}"
PIN_FILE="${PSTATE_PIN_FILE:-/etc/nvidia-pstate-switcher/governor-pin}"
DEFAULT_GOVERNOR="${DEFAULT_GOVERNOR:-schedutil}"

[ -r "$GOV_PATH" ] || exit 0

target="$DEFAULT_GOVERNOR"
if [ -r "$PIN_FILE" ]; then
  pinned="$(tr -d '[:space:]' < "$PIN_FILE")"
  if [ -n "$pinned" ]; then
    if grep -qw "$pinned" "$AVAILABLE_PATH"; then
      target="$pinned"
    else
      logger -t cpufreq-guard "ignoring unavailable pin '$pinned'"
    fi
  fi
fi

current="$(cat "$GOV_PATH")"
[ "$current" = "$target" ] && exit 0

if grep -qw "$target" "$AVAILABLE_PATH" 2>/dev/null; then
  cpupower frequency-set -g "$target" >/dev/null 2>&1 || true
  new="$(cat "$GOV_PATH")"
  if [ "$new" = "$target" ]; then
    logger -t cpufreq-guard "governor restored: $current -> $target"
  else
    logger -t cpufreq-guard "failed to set $target (was $current, now $new)"
  fi
fi
