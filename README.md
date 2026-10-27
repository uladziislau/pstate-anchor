# NVIDIA P-State Switcher

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![PyQt6](https://img.shields.io/badge/PyQt-6-green.svg)](https://riverbankcomputing.com/software/pyqt/)
[![CI](https://github.com/uladziislau/nvidia-pstate-switcher/actions/workflows/ci.yml/badge.svg)](https://github.com/uladziislau/nvidia-pstate-switcher/actions/workflows/ci.yml)

> **Русский**: [README.ru.md](README.ru.md)

A Linux system-tray utility for NVIDIA GPUs: P-states, CPU governor and KDE
power profiles in one menu, with live GPU / CPU-package / DRAM power readouts.

Single file. No daemon, no database — everything is read from sysfs,
`nvidia-smi` and D-Bus.

---

## Why

NVIDIA's GSP firmware can keep a GPU in P0 on an idle desktop, drawing ~45 W
where ~21 W would do. The firmware counts `TEST_ONLY` atomic KMS commits as GPU
work, and KWin issues two commits per frame.

Until that is fixed upstream, the practical workaround is to cap the P-state
manually. This tray makes it one click, reversible, and shows what each mode
costs in watts.

## Features

| Menu section | What it does |
|---|---|
| **Power Profile** | Switches the KDE profile (`power-saver` / `balanced` / `performance`) over D-Bus PowerProfiles; each profile also applies its GPU P-state and governor |
| **CPU Governor** | List comes from `scaling_available_governors` (includes `schedutil`); the chosen governor is **pinned** so automation stops reverting it |
| **GPU P-State** | P0 / P2 / P3 / P5 / P8 plus Auto (driver control); the choice survives reboots |
| **Status** | Current governor, active pin, profile, P-state, GPU power, CPU package and DRAM — instant and 1-minute average |
| **Run at startup** | Autostart with the desktop (the `.desktop` file is generated) |

The tray icon shows the current P-state and is drawn at runtime (no image
assets); the tooltip carries the full power summary.

## Requirements

- Linux with an NVIDIA GPU and the proprietary driver (`nvidia-smi`)
- Python **3.10+**
- [`nvidia-pstate`](https://github.com/sasha0552/nvidia-pstate) — CLI/library for
  P-state control (MIT, install from PyPI)
- PyQt6 — tray only; `--dump-state` and CLI control work without it
- `gdbus` (glib2) for KDE power profiles; `cpupower` for governor switching

### Installing dependencies

| Distribution | PyQt6 |
|---|---|
| Fedora | `sudo dnf install python3-pyqt6 kernel-tools` |
| Arch / CachyOS | `sudo pacman -S python-pyqt6 cpupower` |
| openSUSE | `sudo zypper install python3-pyqt6 cpupower` |
| Debian / Ubuntu | `sudo apt install python3-pyqt6 linux-cpupower` |
| pip (any) | `pip install pyqt6` |

```bash
pip install --user nvidia-pstate     # or: pipx install nvidia-pstate
```

## Install

```bash
git clone https://github.com/uladziislau/nvidia-pstate-switcher
cd nvidia-pstate-switcher
sudo ./install.sh                     # into /usr/local
./install.sh --prefix "$HOME/.local"  # or per-user only
```

The installer drops one file, an icon and the governor-pin directory
(`/etc/nvidia-pstate-switcher` when run as root). It never overwrites your
configuration and is idempotent.

Manual one-liner, without cloning:

```bash
curl -LO https://raw.githubusercontent.com/uladziislau/nvidia-pstate-switcher/main/nvidia-pstate-switcher.py
chmod +x nvidia-pstate-switcher.py
sudo install -m 0755 nvidia-pstate-switcher.py /usr/local/bin/nvidia-pstate-switcher
```

> **GNOME users** need `gnome-shell-extension-appindicator` for tray icons.

## Usage

### Tray

```bash
nvidia-pstate-switcher
```

Right-click for the menu, double-click to refresh immediately. Starting it again
does not spawn a second process — it surfaces the running instance.

### CLI

```bash
nvidia-pstate-switcher --dump-state            # JSON machine state
nvidia-pstate-switcher --pstate 5              # P5 (idle), then exit
nvidia-pstate-switcher --pstate 16             # hand control back to the driver
nvidia-pstate-switcher --governor schedutil    # set and pin the governor
nvidia-pstate-switcher --unpin-governor        # release the pin
nvidia-pstate-switcher --profile power-saver   # KDE profile, then exit
nvidia-pstate-switcher --version
```

`--dump-state` reports version, governor, available governors, pin, profile, GPU
data, RAPL availability and discovered dependencies. Tests and monitoring
scripts use the same path:

```bash
nvidia-pstate-switcher --dump-state | jq .gpu.power_w
```

## Governor pin: why a manual choice "resets itself"

Many systems run a watchdog that restores `schedutil` (or `powersave`) whenever
the governor deviates — for instance to stop power-profiles-daemon from pinning
the minimum frequency. Such a watchdog also reverts your manual choice, which
looks like "the tray does not work".

How the pin fixes that:

1. You pick a governor in the menu → it is applied and written to
   `/etc/nvidia-pstate-switcher/governor-pin` (override with `PSTATE_PIN_FILE`).
2. Every 2 seconds the tray compares the live governor with the pin and
   re-applies the pinned value if something changed it.
3. Third-party watchdogs must honour the pin: when the file exists and holds a
   valid name, automation should not switch the governor. A ready-made example
   lives in `examples/cpufreq-guard.sh`.

Release the pin with **Follow system (unpin)** or `--unpin-governor`; the file is
removed and automation becomes authoritative again.

When `/etc/nvidia-pstate-switcher` is not writable (per-user install), the tray
still holds the chosen governor for the session and says so in a notification.

### Permissions for governor changes

Writing `scaling_governor` needs root. The tray runs
`sudo -n cpupower frequency-set -g <name>`; add a sudoers rule to make that
passwordless:

```
uladzislau ALL=(root) NOPASSWD: /usr/bin/cpupower
```

If neither sudo nor sysfs access is available, the tray shows a warning instead
of failing silently.

## Configuration

`~/.config/nvidia-pstate-switcher.conf`:

```json
{
  "pstate": "0",
  "autostart": true,
  "governor": "schedutil",
  "pstates": ["0", "2", "3", "5", "8", "12", "15"]
}
```

| Key | Meaning |
|---|---|
| `pstate` | P-state applied at start-up (`16` = Auto) |
| `autostart` | Mirrors the presence of the autostart file |
| `governor` | Pinned governor (mirrors the pin file) |
| `pstates` | Optional custom list of P-states for the menu |

## P-states

| P-State | Label | Meaning |
|---|---|---|
| P0 | Max perf | Maximum performance |
| P2 | Balanced | Balanced |
| P3 | Medium | Medium power saving |
| P5 | Idle | Idle / low power |
| P8 | Deep idle | Deep idle |
| Auto (16) | Driver control | The driver decides |

Actual wattage depends on the GPU model; the live numbers are always in the
tooltip and in `nvidia-smi`.

## Plasma widget (optional)

`plasmoid/org.nvidia.pstate.monitor` is a compact panel applet showing P-state
and GPU power, fed by `nvidia-smi`:

```bash
kpackagetool6 --type Plasma/Applet --install plasmoid/org.nvidia.pstate.monitor
```

The widget is experimental: it shares no code with the tray and is verified
manually only.

## Limitations

- This is a workaround for the GSP firmware bug (**#5474539**), not a fix.
- RAPL counters are Intel-only; on AMD the CPU-package row stays empty, the rest
  works.
- Governor changes need root (see above); KDE profile changes do not.
- Tray behaviour depends on the desktop: KDE and GNOME (with the extension) are
  verified, others as reported.

## Investigation

The tool grew out of debugging why a GTX 1660 SUPER stayed in P0 on Wayland:

- root cause: GSP firmware bug #5474539 — `TEST_ONLY` atomic KMS commits count as
  GPU work;
- KWin issues two commits per frame (1 `TEST_ONLY` + 1 `REAL`) ≈ 126 ioctl/s at
  63 FPS, while the firmware threshold is ~102–114 commits/s;
- an LD_PRELOAD prototype (return 0 on `TEST_ONLY`) halved the commit rate but
  produced rendering artefacts;
- a one-line KWin patch (`return true` in `drm_commit.cpp:test()`) fixes it for
  good but requires rebuilding `kwin`;
- Warp Terminal keeps a CUDA context alive and adds ~45 W regardless of P-state.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/ruff check .
.venv/bin/pytest -q
./nvidia-pstate-switcher.py --dump-state
```

CI runs byte-compile, ruff, the test suite on Python 3.10–3.13, a CLI contract
check and shellcheck for the installers. Tests need no GPU, no Qt and no D-Bus:
external subsystems are replaced by temporary directories and parsers.

## License

MIT — see [LICENSE](LICENSE). The `nvidia-pstate` dependency is MIT-licensed
software by [sasha0552](https://github.com/sasha0552/nvidia-pstate).
