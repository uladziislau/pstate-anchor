# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are calendar-based: `0.YYYY.MM.DD`. The leading zero marks the project
as pre-1.0 — the CLI and the config file are stable, internal APIs are not.

## [0.2026.10.27] - 2026-10-27

First release prepared as a standalone, community-oriented project.

### Added

- **CPU governor control** in the tray, with the list of governors taken from
  `scaling_available_governors` instead of a hardcoded pair; `schedutil` is now
  selectable.
- **Governor pin**: an explicit choice is written to
  `/etc/pstate-anchor/governor-pin` (override with `PSTATE_PIN_FILE`)
  and re-applied by the tray, so automation such as a cpufreq watchdog no
  longer silently reverts it. `--unpin-governor` hands control back.
- **CLI**: `--version`, `--dump-state` (JSON, works without Qt or a GPU),
  `--pstate`, `--governor`, `--unpin-governor`, `--profile`. `--oneshot` is
  kept as an alias of `--pstate`.
- **KDE Power Profile** read/write through `gdbus`, so the value is available
  on headless systems too.
- Power monitor reads RAPL counters straight from sysfs; the helper script
  `power-rapl-read.sh` and its sudo rule are no longer required.
- `install.sh` / `uninstall.sh`, an application icon, and an optional Plasma
  widget (`plasmoid/org.nvidia.pstate.monitor`).
- Test suite (pytest) and CI: ruff, byte-compile, tests, CLI contract and
  shellcheck on every push.

### Changed

- Project renamed to **P-State Anchor** (`pstate-anchor`). The tray reads the
  old `~/.config/nvidia-pstate-switcher.conf` once and removes autostart and
  desktop files that still point at the pre-rename binary.
- PyQt6 is imported lazily: every read-only path works on machines without GUI
  libraries, and the error message explains what to install.
- The tray's IPC socket lives in `$XDG_RUNTIME_DIR` instead of a world-accessible
  `/tmp` path, so multi-user machines get their own instance.
- Generated `.desktop` files point at the real binary path rather than a
  hardcoded `/usr/local/bin` location.
- Multi-GPU systems are supported: power is summed and the P-state of the first
  GPU is reported.

### Removed

- Windscribe VPN traffic monitoring. It is unrelated to GPU power state, parsed
  localized CLI output and belonged to a separate tool.
- Dead code: `_windscribe_traffic()`, `PowerMonitor.summary()`, duplicate
  profile/icon handling.

### Fixed

- Crash when `nvidia-smi` returned more than one line of GPU data.
- The active governor was never check-marked when it was `schedutil`.

### Migration from 1.0.0

The configuration file format is unchanged; `pstate` and `autostart` keep
working. The new optional `governor` key stores the pinned governor. Install
the pin directory once with `sudo ./install.sh`, or let the tray enforce the pin
from the session when the file is not writable.

## [0.2025.06.26] - 2025-06-26

- Initial public release: tray with manual P-state switching, autostart,
  `--oneshot` mode and an investigation of the GSP P0 bug.
