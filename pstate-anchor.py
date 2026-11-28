#!/usr/bin/env python3
"""P-State Anchor — system tray for NVIDIA GPU P-states, CPU governor
and KDE Power Profiles.

The script is intentionally a single file: installation is one `curl` + `chmod`.
PyQt6 is imported lazily so that every read-only code path (state dump, CLI
control, tests, CI) works on a machine without GUI libraries.

Licensed under the MIT license; see LICENSE.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from statistics import mean

__version__ = "0.2026.11.28"

# ── configuration surface ─────────────────────────────────
# Every path below can be overridden through the environment. That keeps the
# code testable (temporary directories, fake sysfs trees) and portable across
# distributions that place RAPL counters differently.

PSTATE_AUTO = "16"  # nvidia-pstate: 16 == let the driver decide

#: KDE Power Profile → (GPU P-state, CPU governor)
PROFILE_MAP = {
    "power-saver": {"pstate": "5", "governor": "schedutil"},
    "balanced": {"pstate": PSTATE_AUTO, "governor": "schedutil"},
    "performance": {"pstate": "0", "governor": "schedutil"},
}

PROFILE_LABELS = {
    "power-saver": "🔋 Power Saver",
    "balanced": "⚖️  Balanced",
    "performance": "🚀 Performance",
}

#: Fallback when the kernel does not expose scaling_available_governors.
FALLBACK_GOVERNORS = ("schedutil", "powersave", "performance")

#: Governors the kernel offers but that must not be pinned: `userspace` freezes
#: the frequency until a userspace daemon changes it, which looks like a hung
#: desktop. It is hidden from the menu and rejected by the CLI.
UNSUPPORTED_GOVERNORS = ("userspace",)

#: P-states offered in the tray menu when the CLI cannot enumerate them.
DEFAULT_PSTATES = {
    "0": "Max perf",
    "2": "Balanced",
    "3": "Medium",
    "5": "Idle",
    "8": "Deep idle",
    PSTATE_AUTO: "Auto (driver control)",
}

#: RAPL package power limits (PL1 long term, PL2 short term), in watts.
#: `stock` is the firmware default of a Xeon E5 v3 desktop board; `eco` is the
#: measured sweet spot (≈19 % less energy per operation, ≈7 % less throughput).
#: Pairs below 80 W trade all-core clocks for lower package power: they do not
#: touch single-thread boost (≈20–30 W of package power), so interactive work
#: stays at full clocks. Ordered by descending PL1 — that is the menu order.
#: The kernel accepts any value; the firmware maxima are a heuristic, not a
#: kernel-enforced limit.
POWER_LIMIT_PRESETS = {
    "200_240": (200, 240),
    "175_210": (175, 210),
    "stock": (145, 174),
    "eco": (105, 125),
    "80_100": (80, 100),
    "70_90": (70, 90),
    "60_80": (60, 80),
    "50_70": (50, 70),
    "40_70": (40, 70),
    "40_60": (40, 60),
    "30_70": (30, 70),
    "30_50": (30, 50),
}
POWER_LIMIT_LABELS = {
    "200_240": "200/240 W (BIOS max)",
    "175_210": "175/210 W",
    "stock": "Stock 145/174 W",
    "eco": "Eco 105/125 W",
    "80_100": "80/100 W",
    "70_90": "70/90 W",
    "60_80": "60/80 W",
    "50_70": "50/70 W",
    "40_70": "40/70 W",
    "40_60": "40/60 W",
    "30_70": "30/70 W",
    "30_50": "30/50 W",
}

CONFIG_FILE = os.environ.get(
    "PSTATE_CONFIG_FILE",
    os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "pstate-anchor.conf",
    ),
)
AUTOSTART_FILE = os.environ.get(
    "PSTATE_AUTOSTART_FILE",
    os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "autostart",
        "pstate-anchor.desktop",
    ),
)
DESKTOP_FILE = os.environ.get(
    "PSTATE_DESKTOP_FILE",
    os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "applications",
        "pstate-anchor.desktop",
    ),
)
#: Pre-rename artefact names (nvidia-pstate-switcher). Read once so that an
#: existing installation keeps its configuration and stops autostarting twice.
LEGACY_NAME = "nvidia-pstate-switcher"
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
LEGACY_CONFIG_FILE = os.path.join(CONFIG_HOME, f"{LEGACY_NAME}.conf")
LEGACY_AUTOSTART_FILE = os.path.join(
    CONFIG_HOME, "autostart", f"{LEGACY_NAME}.desktop"
)
LEGACY_DESKTOP_FILE = os.path.join(
    DATA_HOME, "applications", f"{LEGACY_NAME}.desktop"
)

#: Governor pin. Written by the tray/CLI, read by external guards so that a
#: manual choice survives automation such as cpufreq-schedutil-guard.timer.
PIN_FILE = os.environ.get(
    "PSTATE_PIN_FILE", "/etc/pstate-anchor/governor-pin"
)
#: Fallback pin used when the system directory is not writable (per-user
#: install). Same contract, different location.
USER_PIN_FILE = os.path.join(
    os.path.dirname(CONFIG_FILE), "pstate-anchor", "governor-pin"
)
GOVERNOR_PATH = os.environ.get(
    "PSTATE_GOVERNOR_PATH",
    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
)
AVAILABLE_GOVERNORS_PATH = os.environ.get(
    "PSTATE_AVAILABLE_GOVERNORS_PATH",
    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors",
)
RAPL_PKG_PATH = os.environ.get(
    "PSTATE_RAPL_PKG", "/sys/class/powercap/intel-rapl:0/energy_uj"
)
RAPL_DRAM_PATH = os.environ.get(
    "PSTATE_RAPL_DRAM", "/sys/class/powercap/intel-rapl:0:0/energy_uj"
)
RAPL_PL1_PATH = os.environ.get(
    "PSTATE_RAPL_PL1",
    "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw",
)
RAPL_PL2_PATH = os.environ.get(
    "PSTATE_RAPL_PL2",
    "/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw",
)
RAPL_PL1_MAX_PATH = os.environ.get(
    "PSTATE_RAPL_PL1_MAX",
    "/sys/class/powercap/intel-rapl:0/constraint_0_max_power_uw",
)
RAPL_PL2_MAX_PATH = os.environ.get(
    "PSTATE_RAPL_PL2_MAX",
    "/sys/class/powercap/intel-rapl:0/constraint_1_max_power_uw",
)
RAPL_PL1_WINDOW_PATH = os.environ.get(
    "PSTATE_RAPL_PL1_WINDOW",
    "/sys/class/powercap/intel-rapl:0/constraint_0_time_window_us",
)
RAPL_PL2_WINDOW_PATH = os.environ.get(
    "PSTATE_RAPL_PL2_WINDOW",
    "/sys/class/powercap/intel-rapl:0/constraint_1_time_window_us",
)
#: Setuid-free root bridge for the power-limit writes: the tray calls
#: `sudo -n <helper> PL1 PL2`, exactly like `cpupower` for the governor.
POWER_LIMIT_HELPER = os.environ.get(
    "PSTATE_POWER_LIMIT_HELPER",
    "/usr/local/libexec/pstate-anchor/power-limit",
)
IPC_SOCKET = os.environ.get(
    "PSTATE_IPC_SOCKET",
    os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
        "pstate-anchor.sock",
    ),
)

POWER_HISTORY_SECONDS = 60
REFRESH_INTERVAL_SECONDS = 2.0
PIN_RETRY_SECONDS = 10.0
LIMIT_RETRY_SECONDS = 10.0

PP_SERVICE = "net.hadess.PowerProfiles"
PP_PATH = "/net/hadess/PowerProfiles"
PP_IFACE = "net.hadess.PowerProfiles"


# ── filesystem helpers ────────────────────────────────────

def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def read_governor(path: str = GOVERNOR_PATH) -> str | None:
    """Current cpufreq governor, or None when cpufreq is unavailable."""
    value = _read_text(path)
    return value or None


def available_governors(path: str = AVAILABLE_GOVERNORS_PATH) -> list[str]:
    """Governors offered by the running kernel, fallback to a sane default."""
    raw = _read_text(path)
    if not raw:
        return list(FALLBACK_GOVERNORS)
    names = [name for name in raw.split() if name]
    return names or list(FALLBACK_GOVERNORS)


def active_pin_file() -> str:
    """Pin location that can actually be written on this machine.

    The system path is preferred so root-owned watchdogs can read the intent;
    a per-user path is used when the system directory is absent or read-only
    (install without root).
    """
    directory = os.path.dirname(PIN_FILE)
    if os.path.isfile(PIN_FILE):
        return PIN_FILE
    if directory and os.path.isdir(directory) and os.access(directory, os.W_OK):
        return PIN_FILE
    return USER_PIN_FILE


def pin_files() -> list[str]:
    """Every location consulted for the pin, most authoritative first."""
    files = [PIN_FILE]
    if USER_PIN_FILE != PIN_FILE:
        files.append(USER_PIN_FILE)
    return files


def selectable_governors(path: str = AVAILABLE_GOVERNORS_PATH) -> list[str]:
    """Governors offered to the user: everything except UNSUPPORTED_GOVERNORS."""
    return [
        name for name in available_governors(path)
        if name not in UNSUPPORTED_GOVERNORS
    ]


def read_pin(path: str | None = None) -> str | None:
    """Governor the user pinned against automatic tuning, if any."""
    if path is not None:
        value = _read_text(path)
        return value or None
    for candidate in pin_files():
        value = _read_text(candidate)
        if value:
            return value
    return None


def write_pin(
    governor: str | None,
    path: str | None = None,
    available_path: str = AVAILABLE_GOVERNORS_PATH,
) -> tuple[bool, str]:
    """Persist (or clear) the governor pin. Returns (ok, message)."""
    target = path or active_pin_file()
    if governor is None:
        candidates = [target] if path is not None else pin_files()
        removed = False
        for candidate in candidates:
            try:
                os.remove(candidate)
                removed = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                return False, f"cannot remove {candidate}: {exc}"
        return (True, "pin cleared") if removed else (True, "no pin to clear")

    if governor in UNSUPPORTED_GOVERNORS:
        return False, (
            f"governor {governor!r} cannot be pinned: it freezes the frequency"
        )
    if governor not in available_governors(available_path):
        return False, f"governor {governor!r} is not available"

    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(governor + "\n")
    except OSError as exc:
        return False, f"cannot write {target}: {exc}"
    return True, f"pin set to {governor} ({target})"


def apply_governor(
    governor: str,
    governor_path: str = GOVERNOR_PATH,
    available_path: str = AVAILABLE_GOVERNORS_PATH,
) -> tuple[bool, str]:
    """Set the cpufreq governor, preferring the passwordless cpupower call."""
    if governor in UNSUPPORTED_GOVERNORS:
        return False, (
            f"governor {governor!r} is not supported: it freezes the frequency"
        )
    if governor not in available_governors(available_path):
        return False, f"governor {governor!r} is not available"
    if read_governor(governor_path) == governor:
        return True, "already active"

    attempts = (
        ["sudo", "-n", "cpupower", "frequency-set", "-g", governor],
    )
    last_error = ""
    for cmd in attempts:
        if shutil.which(cmd[0]) is None:
            last_error = f"{cmd[0]} not found"
            continue
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0 and read_governor(governor_path) == governor:
            return True, f"governor set to {governor}"
        last_error = (result.stderr or result.stdout).strip() or (
            f"exit code {result.returncode}"
        )

    # Direct sysfs write works when the user owns the cpufreq files.
    try:
        with open(governor_path, "w", encoding="utf-8") as handle:
            handle.write(governor)
    except OSError as exc:
        return False, last_error or f"cannot write {governor_path}: {exc}"
    if read_governor(governor_path) == governor:
        return True, f"governor set to {governor} (sysfs)"
    return False, last_error or "governor did not change"


def parse_gdbus_string(raw: str) -> str | None:
    """Extract the value from gdbus output such as `(<'performance'>,)`."""
    text = raw.strip()
    if not text.startswith("("):
        return None
    inner = text[1:text.rindex(")")] if text.endswith(")") else text[1:]
    value = inner.strip().rstrip(",").strip().strip("<>'\"")
    return value or None


def power_profile_command() -> list[str]:
    """argv that prints the active KDE Power Profile (single source of truth)."""
    return [
        "gdbus", "call", "--system",
        "--dest", PP_SERVICE,
        "--object-path", PP_PATH,
        "--method", "org.freedesktop.DBus.Properties.Get",
        PP_IFACE, "ActiveProfile",
    ]


def read_power_profile() -> str | None:
    """Active KDE Power Profile via gdbus (no Qt, hence CLI friendly)."""
    if shutil.which("gdbus") is None:
        return None
    try:
        result = subprocess.run(
            power_profile_command(),
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_gdbus_string(result.stdout)


def set_power_profile(profile: str) -> tuple[bool, str]:
    """Switch the KDE Power Profile via gdbus."""
    if profile not in PROFILE_MAP:
        return False, f"unknown profile {profile!r}"
    if shutil.which("gdbus") is None:
        return False, "gdbus not found"
    try:
        result = subprocess.run(
            [
                "gdbus", "call", "--system",
                "--dest", PP_SERVICE,
                "--object-path", PP_PATH,
                "--method", "org.freedesktop.DBus.Properties.Set",
                PP_IFACE, "ActiveProfile",
                f"<'{profile}'>",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, f"profile set to {profile}"


# ── configuration file ────────────────────────────────────

DEFAULT_CONFIG = {
    "pstate": PSTATE_AUTO,
    "autostart": True,
    "governor": None,
    "power_limit": None,
}


def load_config(path: str = CONFIG_FILE) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    source = path
    if path == CONFIG_FILE and not os.path.isfile(path):
        source = LEGACY_CONFIG_FILE
    try:
        with open(source, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return cfg
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(cfg: dict, path: str = CONFIG_FILE) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle)


# ── autostart / desktop entry ─────────────────────────────

def _desktop_entry(exec_path: str, autostart: bool) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=P-State Anchor",
        "Comment=GPU P-state, CPU governor, Power Profiles, Power monitor",
        f"Exec={exec_path}",
        "Icon=pstate-anchor",
        "Categories=System;Hardware;",
        "Terminal=false",
        "StartupNotify=false",
    ]
    if autostart:
        lines.append("X-KDE-autostart-condition=true")
    return "\n".join(lines) + "\n"


def _exec_path() -> str:
    """Absolute path used in generated .desktop entries."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and os.sep in argv0:
        return os.path.abspath(argv0)
    if argv0:
        found = shutil.which(argv0)
        if found:
            return os.path.abspath(found)
    found = shutil.which("pstate-anchor")
    return os.path.abspath(found) if found else "pstate-anchor"


def autostart_enabled(path: str = AUTOSTART_FILE) -> bool:
    return os.path.isfile(path)


def set_autostart(enabled: bool, path: str = AUTOSTART_FILE) -> None:
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_desktop_entry(_exec_path(), autostart=True))
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def cleanup_legacy_entries() -> list[str]:
    """Remove autostart/desktop files left by the pre-rename installation.

    Only files that still point at the old binary are touched, so a file the
    user edited by hand is never destroyed by mistake.
    """
    removed = []
    for path in (LEGACY_AUTOSTART_FILE, LEGACY_DESKTOP_FILE):
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            continue
        if LEGACY_NAME not in content:
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            continue
    return removed


def ensure_desktop_entry(path: str = DESKTOP_FILE) -> None:
    if os.path.isfile(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_desktop_entry(_exec_path(), autostart=False))


# ── nvidia-smi / RAPL parsing ─────────────────────────────

def parse_smi(raw: str) -> tuple[float, str, int]:
    """Parse `nvidia-smi --query-gpu=index,power.draw,pstate`.

    Returns (total power in W, P-state of the first GPU, GPU count).
    Power is summed over all GPUs; consumers that only want one GPU keep the
    first line. Malformed or `[N/A]` fields degrade to 0.0 / "—".
    """
    total = 0.0
    pstate = "—"
    count = 0
    for line in raw.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        count += 1
        if count == 1:
            pstate = parts[2] or "—"
        try:
            total += float(parts[1])
        except ValueError:
            continue
    return total, pstate, count


def read_rapl_counters(
    pkg_path: str = RAPL_PKG_PATH, dram_path: str = RAPL_DRAM_PATH
) -> tuple[int, int] | None:
    pkg = _read_text(pkg_path)
    dram = _read_text(dram_path)
    if pkg is None or dram is None:
        return None
    try:
        return int(pkg), int(dram)
    except ValueError:
        return None


def rapl_watts(previous: int, current: int, interval: float) -> float:
    """Convert a RAPL microjoule delta into watts."""
    if interval <= 0:
        return 0.0
    return (current - previous) / 1_000_000 / interval


# ── RAPL package power limits (PL1 / PL2) ─────────────────

def _uw_to_watts(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw) // 1_000_000
    except ValueError:
        return None


def read_power_limits(
    pl1_path: str = RAPL_PL1_PATH, pl2_path: str = RAPL_PL2_PATH
) -> tuple[int, int] | None:
    """Current (PL1, PL2) in watts, or None when RAPL limits are absent."""
    pl1 = _uw_to_watts(_read_text(pl1_path))
    pl2 = _uw_to_watts(_read_text(pl2_path))
    if pl1 is None or pl2 is None:
        return None
    return pl1, pl2


def power_limit_bounds(
    pl1_path: str = RAPL_PL1_MAX_PATH, pl2_path: str = RAPL_PL2_MAX_PATH
) -> tuple[int, int] | None:
    """(max PL1, max PL2) in watts. PL1 has its own ceiling: it cannot go up."""
    pl1 = _uw_to_watts(_read_text(pl1_path))
    pl2 = _uw_to_watts(_read_text(pl2_path))
    if pl1 is None or pl2 is None:
        return None
    return pl1, pl2


def _us_to_seconds(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return int(raw) / 1_000_000
    except ValueError:
        return None


def read_power_limit_window(
    pl1_path: str = RAPL_PL1_WINDOW_PATH,
    pl2_path: str = RAPL_PL2_WINDOW_PATH,
) -> tuple[float, float] | None:
    """(PL1 window, PL2 window) in seconds, or None when RAPL omits them.

    The window is what makes PL1 meaningful: a limit is an energy budget over
    this period, so 200 W for 20 s is a different machine from 200 W for 1 s.
    """
    pl1 = _us_to_seconds(_read_text(pl1_path))
    pl2 = _us_to_seconds(_read_text(pl2_path))
    if pl1 is None or pl2 is None:
        return None
    return pl1, pl2


def power_limit_above_firmware_max(
    limits: tuple[int, int] | None, bounds: tuple[int, int] | None
) -> bool:
    """True when the live limits exceed the firmware-reported maxima.

    `constraint_*_max_power_uw` goes stale when the BIOS raises the package
    limit: it keeps reporting the old ceiling while the kernel accepts and
    honours the higher write. Callers use this to word the status honestly.
    """
    if limits is None or bounds is None:
        return False
    return limits[0] > bounds[0] or limits[1] > bounds[1]


def _short_seconds(value: float) -> str:
    if value >= 1:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_power_limit_status(
    limits: tuple[int, int] | None,
    bounds: tuple[int, int] | None,
    window: tuple[float, float] | None = None,
) -> str:
    """One-line status: what runs now, what firmware claims, over what window."""
    if limits is None:
        return "PL1/PL2: — (RAPL limits absent)"
    text = f"PL1/PL2: {limits[0]}/{limits[1]} W"
    if bounds is not None:
        label = (
            "above firmware max"
            if power_limit_above_firmware_max(limits, bounds)
            else "firmware max"
        )
        text += f" ({label} {bounds[0]}/{bounds[1]} W)"
    if window is not None:
        text += f" · window {_short_seconds(window[0])} s"
    return text


def validate_power_limits(
    pl1_w: int, pl2_w: int, bounds: tuple[int, int] | None
) -> str | None:
    """Return a human-readable reason when the pair is unacceptable."""
    for name, value in (("PL1", pl1_w), ("PL2", pl2_w)):
        if not isinstance(value, int) or value < 1:
            return f"{name} must be a positive integer (got {value!r})"
    # The firmware-reported maxima (constraint_*_max_power_uw) are a
    # heuristic (TDP / 2×TDP), not a kernel-enforced limit: the kernel
    # accepts higher values. Leave enforcement to the kernel, which either
    # accepts the write or returns an error.
    return None


def parse_power_limit_spec(raw: str) -> tuple[int, int]:
    """`eco`/`stock` → preset, `105/125` → explicit watts."""
    key = raw.strip().lower()
    if key in POWER_LIMIT_PRESETS:
        return POWER_LIMIT_PRESETS[key]
    for separator in ("/", ","):
        if separator in key:
            left, _, right = key.partition(separator)
            try:
                return int(left), int(right)
            except ValueError as exc:
                raise ValueError(
                    f"invalid power limits {raw!r}: expected PL1/PL2 in watts"
                ) from exc
    try:
        value = int(key)
    except ValueError as exc:
        raise ValueError(
            f"invalid power limits {raw!r}: expected eco, stock or PL1/PL2"
        ) from exc
    return value, POWER_LIMIT_PRESETS["stock"][1]


def _write_power_limits(
    pl1_w: int, pl2_w: int, pl1_path: str, pl2_path: str
) -> str | None:
    for path, watts in ((pl1_path, pl1_w), (pl2_path, pl2_w)):
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(watts * 1_000_000))
        except OSError as exc:
            return f"cannot write {path}: {exc}"
    return None


def apply_power_limits(
    pl1_w: int,
    pl2_w: int,
    *,
    pl1_path: str = RAPL_PL1_PATH,
    pl2_path: str = RAPL_PL2_PATH,
    bounds: tuple[int, int] | None = None,
    helper: str | None = None,
    runner=None,
) -> tuple[bool, str]:
    """Set RAPL PL1/PL2, preferring the passwordless root helper."""
    if helper is None:
        helper = POWER_LIMIT_HELPER
    if bounds is None:
        bounds = power_limit_bounds()
    problem = validate_power_limits(pl1_w, pl2_w, bounds)
    if problem:
        return False, problem

    errors: list[str] = []
    if os.path.isfile(helper):
        cmd = ["sudo", "-n", helper, str(pl1_w), str(pl2_w)]
        run = runner or (
            lambda argv: subprocess.run(
                argv, capture_output=True, text=True, timeout=15, check=False
            )
        )
        try:
            result = run(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
        else:
            if result.returncode == 0:
                current = read_power_limits(pl1_path, pl2_path)
                if current == (pl1_w, pl2_w):
                    return True, f"PL1/PL2 set to {pl1_w}/{pl2_w} W"
                return False, (
                    f"helper exited 0 but limits read back as {current}"
                )
            errors.append(
                (result.stderr or result.stdout or "").strip()
                or f"helper exit code {result.returncode}"
            )

    direct = _write_power_limits(pl1_w, pl2_w, pl1_path, pl2_path)
    if direct is None:
        current = read_power_limits(pl1_path, pl2_path)
        if current == (pl1_w, pl2_w):
            return True, f"PL1/PL2 set to {pl1_w}/{pl2_w} W (sysfs)"
        return False, f"limits read back as {current}"
    errors.append(direct)
    return False, "; ".join(e for e in errors if e)


# ── sched_ext (scx_loader / scxctl) ───────────────────────
# The kernel's sched_ext lets user-space BPF schedulers replace the default
# EEVDF. scx_loader (D-Bus activated) owns the lifecycle; scxctl is the CLI
# client. We only reflect and switch — persistence across reboots belongs to
# scx_loader's own configuration, not to this tray.

SCHED_EXT_STATE_PATH = os.environ.get(
    "PSTATE_SCHED_EXT_STATE", "/sys/kernel/sched_ext/state"
)


def sched_ext_available(
    state_path: str = SCHED_EXT_STATE_PATH, which=None
) -> bool:
    """True when the kernel exposes sched_ext or scxctl is installed."""
    if os.path.exists(os.path.dirname(state_path)):
        return True
    finder = which or _binary
    return finder("scxctl") is not None


def _scxctl(
    args: list[str], runner=None, timeout: int = 15
) -> tuple[int, str]:
    """Run `scxctl <args>`; returns (exit_code, combined_output)."""
    binary = _binary("scxctl")
    if binary is None:
        return 127, "scxctl not found"
    run = runner or (
        lambda argv: subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    )
    try:
        result = run([binary, *args])
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode, output


def parse_scx_get(raw: str) -> dict:
    """`scxctl get` → {"running": bool, "scheduler": str|None, "mode": str|None}.

    Live output is one line: `running MLFQ in Auto mode` or
    `no scx scheduler running, use '...'`.
    """
    match = re.search(
        r"running\s+(\S+)\s+in\s+(.+?)\s+mode\b", raw, re.IGNORECASE
    )
    if match is None:
        return {"running": False, "scheduler": None, "mode": None}
    return {
        "running": True,
        "scheduler": match.group(1),
        "mode": match.group(2),
    }


def parse_scx_list(raw: str) -> list[str]:
    """`scxctl list` → ['beerland', 'bpfland', ...]."""
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        values = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [str(item) for item in values] if isinstance(values, list) else []


def parse_scx_modes(raw: str) -> list[str]:
    """`scxctl modes --sched X` → ['Auto', 'Gaming', ...] on the first line.

    Unlike `scxctl list` (a JSON array), the modes line prints bare names:
    `modes configured for MLFQ: [Auto, Gaming]`.
    """
    first = raw.splitlines()[0] if raw.splitlines() else ""
    start = first.find("[")
    end = first.rfind("]")
    if start == -1 or end <= start:
        return []
    inner = first[start + 1 : end]
    return [
        part.strip().strip('"').strip("'")
        for part in inner.split(",")
        if part.strip()
    ]


def sched_ext_status(runner=None) -> dict:
    """Live sched_ext snapshot for --dump-state and the tray.

    Keys: available, running, scheduler, mode, schedulers, modes, error.
    Never raises: a missing scx_loader or a D-Bus failure degrades to
    ``available``/``running`` False with the message in ``error``.
    """
    if not sched_ext_available():
        return {"available": False, "running": False, "error": "sched_ext absent"}

    code, out = _scxctl(["get"], runner=runner)
    if code != 0:
        return {
            "available": True,
            "running": False,
            "error": out or f"scxctl get exit {code}",
        }
    parsed = parse_scx_get(out)

    code, out = _scxctl(["list"], runner=runner)
    schedulers = parse_scx_list(out) if code == 0 else []

    modes: list[str] = []
    if parsed["running"] and parsed["scheduler"]:
        # `scxctl get` prints the display name (MLFQ); `modes --sched` wants
        # the binary name (scx_mlfq or plain mlfq). Try both spellings.
        name = parsed["scheduler"]
        for candidate in (name.lower(), f"scx_{name.lower()}"):
            code, out = _scxctl(["modes", "--sched", candidate], runner=runner)
            if code == 0:
                modes = parse_scx_modes(out)
                break

    return {
        "available": True,
        "running": parsed["running"],
        "scheduler": parsed["scheduler"],
        "mode": parsed["mode"],
        "schedulers": schedulers,
        "modes": modes,
        "error": None,
    }


def apply_sched_ext(
    sched: str | None = None,
    mode: str | None = None,
    stop: bool = False,
    runner=None,
) -> tuple[bool, str]:
    """Switch scheduler/mode via scxctl. Exactly one of sched/mode/stop."""
    if stop:
        args = ["stop"]
    elif sched:
        args = ["switch", "--sched", sched]
    elif mode:
        args = ["switch", "--mode", mode]
    else:
        return False, "nothing to do: pass sched, mode or stop"
    code, out = _scxctl(args, runner=runner)
    if code != 0:
        return False, out or f"scxctl exit {code}"
    return True, out or "ok"


# ── power monitor (rolling 1-minute average) ──────────────

class PowerMonitor:
    """Tracks GPU / CPU package / DRAM power with a rolling average."""

    def __init__(
        self,
        window: int = POWER_HISTORY_SECONDS,
        interval: float = REFRESH_INTERVAL_SECONDS,
    ):
        size = max(1, int(window / interval))
        self._interval = interval
        self.gpu_now = 0.0
        self.gpu_count = 0
        self.gpu: deque[float] = deque(maxlen=size)
        self.pkg: deque[float] = deque(maxlen=size)
        self.dram: deque[float] = deque(maxlen=size)
        self._last: tuple[int, int] | None = None
        # Session energy in watt-hours, integrated from the same samples.
        self.gpu_wh = 0.0
        self.pkg_wh = 0.0
        self.dram_wh = 0.0

    def add_gpu_sample(self, watts: float, count: int = 1) -> None:
        self.gpu_now = watts
        self.gpu_count = count
        self.gpu.append(watts)
        self.gpu_wh += watts * self._interval / 3600.0

    def add_rapl_samples(self, pkg_uj: int, dram_uj: int) -> None:
        if self._last is None:
            pkg_w = dram_w = 0.0
            pkg_j = dram_j = 0
        else:
            # Counter wrap-around yields a negative delta: treat as zero.
            pkg_j = max(0, pkg_uj - self._last[0])
            dram_j = max(0, dram_uj - self._last[1])
            pkg_w = rapl_watts(self._last[0], pkg_uj, self._interval)
            dram_w = rapl_watts(self._last[1], dram_uj, self._interval)
            pkg_w = max(0.0, min(pkg_w, 300.0))
            dram_w = max(0.0, min(dram_w, 50.0))
        self.pkg_wh += pkg_j / 3_600_000_000
        self.dram_wh += dram_j / 3_600_000_000
        self._last = (pkg_uj, dram_uj)
        self.pkg.append(pkg_w)
        self.dram.append(dram_w)

    @property
    def gpu_avg(self) -> float:
        return mean(self.gpu) if self.gpu else 0.0

    @property
    def pkg_avg(self) -> float:
        return mean(self.pkg) if self.pkg else 0.0

    @property
    def dram_avg(self) -> float:
        return mean(self.dram) if self.dram else 0.0

    @property
    def total_now(self) -> float:
        return (
            self.gpu_now
            + (self.pkg[-1] if self.pkg else 0.0)
            + (self.dram[-1] if self.dram else 0.0)
        )

    @property
    def total_avg(self) -> float:
        return self.gpu_avg + self.pkg_avg + self.dram_avg

    @property
    def total_wh(self) -> float:
        return self.gpu_wh + self.pkg_wh + self.dram_wh

    def as_dict(self) -> dict:
        return {
            "gpu_now": round(self.gpu_now, 2),
            "gpu_avg": round(self.gpu_avg, 2),
            "pkg_now": round(self.pkg[-1], 2) if self.pkg else None,
            "pkg_avg": round(self.pkg_avg, 2),
            "dram_now": round(self.dram[-1], 2) if self.dram else None,
            "dram_avg": round(self.dram_avg, 2),
            "total_now": round(self.total_now, 2),
            "total_avg": round(self.total_avg, 2),
            "gpu_wh": round(self.gpu_wh, 4),
            "pkg_wh": round(self.pkg_wh, 4),
            "dram_wh": round(self.dram_wh, 4),
            "total_wh": round(self.total_wh, 4),
        }


# ── state dump (no Qt required) ───────────────────────────

def _binary(name: str) -> str | None:
    return shutil.which(name) or (
        os.path.expanduser(f"~/.local/bin/{name}")
        if os.access(os.path.expanduser(f"~/.local/bin/{name}"), os.X_OK)
        else None
    )


def collect_state() -> dict:
    """Full machine-facing snapshot: used by `--dump-state` and by tests."""
    cfg = load_config()
    gpu_power, gpu_pstate, gpu_count = 0.0, "—", 0
    smi = _binary("nvidia-smi")
    if smi:
        try:
            result = subprocess.run(
                [
                    smi, "--query-gpu=index,power.draw,pstate",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                gpu_power, gpu_pstate, gpu_count = parse_smi(result.stdout)
        except (OSError, subprocess.SubprocessError):
            pass

    rapl = read_rapl_counters()
    limits = read_power_limits()
    bounds = power_limit_bounds()
    window = read_power_limit_window()
    return {
        "version": __version__,
        "governor": read_governor(),
        "available_governors": available_governors(),
        "selectable_governors": selectable_governors(),
        "governor_pin": read_pin(),
        "pin_file": active_pin_file(),
        "pin_files": pin_files(),
        "pin_writable": os.access(
            os.path.dirname(active_pin_file()) or "/", os.W_OK
        ),
        "power_profile": read_power_profile(),
        "config": cfg,
        "power_limit": {
            "pl1_w": limits[0] if limits else None,
            "pl2_w": limits[1] if limits else None,
            "pl1_max_w": bounds[0] if bounds else None,
            "pl2_max_w": bounds[1] if bounds else None,
            "pl1_window_s": round(window[0], 6) if window else None,
            "pl2_window_s": round(window[1], 6) if window else None,
            "above_firmware_max": power_limit_above_firmware_max(limits, bounds),
            "helper": POWER_LIMIT_HELPER
            if os.path.isfile(POWER_LIMIT_HELPER)
            else None,
        },
        "gpu": {
            "count": gpu_count,
            "pstate": gpu_pstate,
            "power_w": round(gpu_power, 2),
        },
        "rapl_readable": rapl is not None,
        "sched_ext": sched_ext_status(),
        "dependencies": {
            "nvidia-smi": smi,
            "nvidia-pstate": _binary("nvidia-pstate"),
            "cpupower": _binary("cpupower"),
            "gdbus": _binary("gdbus"),
            "PyQt6": _pyqt_available(),
            "scxctl": _binary("scxctl"),
        },
    }


def _pyqt_available() -> bool:
    try:
        import PyQt6  # noqa: F401
    except ImportError:
        return False
    return True


# ── Qt layer ──────────────────────────────────────────────
# Imported lazily: CLI paths and tests must work without GUI libraries.

try:
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

    HAVE_QT = True
except ImportError:  # pragma: no cover - exercised only on Qt-less boxes
    QtCore = QtGui = QtNetwork = QtWidgets = None  # type: ignore[assignment]
    HAVE_QT = False


if HAVE_QT:

    _icon_cache: dict[tuple[str, str], QtGui.QIcon] = {}


    def _icon_color() -> QtGui.QColor:
        """Text colour of the current theme: white icons vanish on light panels."""
        app = QtWidgets.QApplication.instance()
        if app is not None:
            color = app.palette().color(QtGui.QPalette.ColorRole.WindowText)
            if color.isValid():
                return color
        return QtGui.QColor("white")


    def _render_icon(label: str, size: int = 48):
        """Draw the P-state label onto a transparent pixmap."""
        color = _icon_color()
        key = (label, color.name())
        if key in _icon_cache:
            return _icon_cache[key]

        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)

        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)

        font = QtGui.QFont("sans-serif", size // 2, QtGui.QFont.Weight.Bold)
        path = QtGui.QPainterPath()
        path.addText(0, 0, font, label)

        bounds = path.boundingRect()
        path.translate(
            (size - bounds.width()) / 2 - bounds.x(),
            (size - bounds.height()) / 2 - bounds.y(),
        )

        painter.setPen(QtGui.QPen(color, 2))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)
        painter.end()

        icon = QtGui.QIcon(pixmap)
        _icon_cache[key] = icon
        return icon


    class CommandRunner(QtCore.QObject):
        """Async QProcess wrapper: never blocks the tray's UI thread."""

        finished = QtCore.pyqtSignal(str)
        failed = QtCore.pyqtSignal(str)

        def run(self, cmd: list[str]) -> None:
            self._proc = QtCore.QProcess(self)
            self._proc.setProgram(cmd[0])
            self._proc.setArguments(cmd[1:])
            self._proc.setProcessChannelMode(
                QtCore.QProcess.ProcessChannelMode.MergedChannels
            )
            self._proc.finished.connect(self._on_done)
            self._proc.errorOccurred.connect(self._on_error)
            self._proc.start()

        def _on_done(self, exit_code: int) -> None:
            output = self._proc.readAllStandardOutput().data().decode().strip()
            if exit_code == 0:
                self.finished.emit(output)
            else:
                self.failed.emit(output or f"exit code {exit_code}")

        def _on_error(self, err) -> None:
            self.failed.emit(f"cannot launch {self._proc.program()} ({err.name})")


    class TrayApp(QtWidgets.QSystemTrayIcon):
        """Tray icon with power-profile, governor, P-state and power sections."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._cfg = load_config()
            self._pin = read_pin()
            self._last_pin_apply = 0.0
            self._last_limit_apply = 0.0
            self._limit_failed = False
            self._current_label = "—"
            self._current_profile: str | None = None
            self._current_governor: str | None = None
            self._power = PowerMonitor()
            self._pstates = dict(DEFAULT_PSTATES)

            ensure_desktop_entry()
            cleanup_legacy_entries()

            self._smi_bin = _binary("nvidia-smi") or "nvidia-smi"
            self._pstate_bin = _binary("nvidia-pstate") or "nvidia-pstate"

            self._smi_runner = CommandRunner()
            self._smi_runner.finished.connect(self._on_smi_ok)

            self._pstate_runner = CommandRunner()
            self._pstate_runner.finished.connect(lambda _: self._refresh())
            self._pstate_runner.failed.connect(self._on_pstate_failure)

            self._sched_runner = CommandRunner()
            self._sched_runner.finished.connect(self._on_sched_ext_done)
            self._sched_runner.failed.connect(self._on_sched_ext_fail)

            # Read the KDE profile asynchronously: a blocking gdbus call in the
            # refresh loop could stall the tray for up to its 5 s timeout.
            self._profile_runner = CommandRunner()
            self._profile_runner.finished.connect(self._on_profile_ok)
            self._gdbus = shutil.which("gdbus")

            # sched_ext state is read synchronously once: three short scxctl
            # calls (~100 ms) at startup, then refreshed on menu open and
            # after every switch — never inside the 2 s power loop.
            self._menu = None
            self._sched_ext = sched_ext_status()
            self._sched_sig = self._sched_signature(self._sched_ext)

            self._build_menu()

            saved = self._cfg.get("pstate", PSTATE_AUTO)
            self._mark_active_pstate(str(saved))
            if str(saved) != PSTATE_AUTO:
                self._set_pstate(str(saved))

            # RAPL limits reset to the firmware default on every boot, so a
            # saved profile is re-applied here rather than at menu-open time.
            saved_limit = self._cfg.get("power_limit")
            if saved_limit in POWER_LIMIT_PRESETS:
                ok, message = apply_power_limits(*POWER_LIMIT_PRESETS[saved_limit])
                if not ok:
                    self._notify("CPU Power Limit", message, False)

            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self._refresh)
            self._timer.start(int(REFRESH_INTERVAL_SECONDS * 1000))

            self._ipc = QtNetwork.QLocalServer(self)
            QtNetwork.QLocalServer.removeServer(IPC_SOCKET)
            if not self._ipc.listen(IPC_SOCKET):
                print(f"IPC listen failed on {IPC_SOCKET}", file=sys.stderr)
            self._ipc.newConnection.connect(self._on_ipc_connection)

            self.activated.connect(self._on_activate)
            QtWidgets.QApplication.instance().paletteChanged.connect(
                lambda _: self._repaint_icon()
            )
            self.setIcon(_render_icon("—"))
            self.setToolTip("P-State Anchor")
            self.show()
            self._refresh()

        # ── menu ──────────────────────────────────────────────

        def _build_menu(self) -> None:
            if self._menu is None:
                self._menu = QtWidgets.QMenu()
                self._menu.triggered.connect(self._on_menu_trigger)
                self._menu.aboutToShow.connect(self._on_menu_about_to_show)
                self.setContextMenu(self._menu)
                self._submenus = []
            else:
                # Submenus are child widgets, not actions: clear() leaves
                # them orphaned, so retire them explicitly before rebuilding.
                for submenu in self._submenus:
                    submenu.deleteLater()
                self._submenus = []
                self._menu.clear()
            self._profile_actions = []
            self._governor_actions = []
            self._pstate_actions = []
            self._sched_actions = []
            self._sched_mode_actions = []

            self._profile_menu = self._submenu("Profile")
            for profile_id, label in PROFILE_LABELS.items():
                action = self._profile_menu.addAction(label)
                action.setData(("profile", profile_id))
                action.setCheckable(True)
                self._profile_actions.append(action)

            self._governor_menu = self._submenu("CPU Governor")
            for governor in selectable_governors():
                action = self._governor_menu.addAction(governor)
                action.setData(("governor", governor))
                action.setCheckable(True)
                self._governor_actions.append(action)
            unpin = self._governor_menu.addAction("Follow system (unpin)")
            unpin.setData(("unpin", None))

            self._sched_menu = self._submenu("sched_ext")
            self._build_sched_ext_section(self._sched_menu)

            self._gpu_menu = self._submenu("GPU P-State")
            for pstate, label in self._pstates.items():
                text = f"P{pstate} — {label}" if label else f"P{pstate}"
                action = self._gpu_menu.addAction(text)
                action.setData(("pstate", pstate))
                action.setCheckable(True)
                self._pstate_actions.append(action)

            self._limit_menu = self._submenu("Power Limit")
            self._power_actions = []
            for key, label in POWER_LIMIT_LABELS.items():
                # Separate above-stock presets from stock/eco and from the
                # low-power tail: three groups read better than twelve rows.
                if key in ("stock", "80_100"):
                    self._limit_menu.addSeparator()
                action = self._limit_menu.addAction(label)
                action.setData(("power_limit", key))
                action.setCheckable(True)
                self._power_actions.append(action)

            self._status_menu = self._submenu("Status")
            self._section(self._status_menu, "CPU")
            self._info_governor = self._info_item(self._status_menu, "CPU: —")
            self._info_pin = self._info_item(self._status_menu, "Pin: —")
            self._info_profile = self._info_item(self._status_menu, "Profile: —")
            self._info_power = self._info_item(self._status_menu, "PL1/PL2: —")
            self._info_schedext = self._info_item(self._status_menu, "sched_ext: —")
            self._status_menu.addSeparator()
            self._section(self._status_menu, "Power")
            self._info_gpu = self._info_item(self._status_menu, "GPU: — / —")
            self._info_pkg = self._info_item(self._status_menu, "PKG: —")
            self._info_dram = self._info_item(self._status_menu, "DRAM: —")
            self._info_total = self._info_item(self._status_menu, "TOTAL: —")
            self._info_energy = self._info_item(self._status_menu, "Energy: —")

            self._menu.addSeparator()
            autostart = self._menu.addAction("Run at startup")
            autostart.setCheckable(True)
            autostart.setChecked(autostart_enabled())
            autostart.triggered.connect(self._toggle_autostart)

            self._menu.addSeparator()
            self._menu.addAction("Refresh", self._refresh)
            self._menu.addAction("Quit", QtWidgets.QApplication.quit)

            # Rebuilds reuse the same QMenu (signals stay connected once), so
            # pstate checkmarks must be restored from the saved config.
            self._mark_active_pstate(str(self._cfg.get("pstate", PSTATE_AUTO)))

        def _submenu(self, title: str) -> QtWidgets.QMenu:
            menu = self._menu.addMenu(title)
            menu.triggered.connect(self._on_menu_trigger)
            self._submenus.append(menu)
            return menu

        def _section(self, menu, title: str) -> None:
            header = menu.addAction(title)
            header.setEnabled(False)
            font = header.font()
            font.setBold(True)
            header.setFont(font)

        def _info_item(self, menu, text: str):
            action = menu.addAction(text)
            action.setEnabled(False)
            return action

        # ── sched_ext ────────────────────────────────────────

        @staticmethod
        def _sched_signature(status: dict) -> tuple:
            return (
                status.get("scheduler"),
                status.get("mode"),
                tuple(status.get("modes") or ()),
            )

        def _build_sched_ext_section(self, menu) -> None:
            status = self._sched_ext
            if not status.get("available"):
                unavailable = menu.addAction("not available (install scx-scheds)")
                unavailable.setEnabled(False)
                return
            schedulers = status.get("schedulers") or []
            for name in schedulers:
                display = name if name.startswith("scx_") else f"scx_{name}"
                action = menu.addAction(display)
                action.setData(("sched_ext", name))
                action.setCheckable(True)
                self._sched_actions.append(action)
            default_action = menu.addAction("Default (kernel EEVDF)")
            default_action.setData(("sched_ext", "default"))
            default_action.setCheckable(True)
            self._sched_actions.append(default_action)

            modes = status.get("modes") or []
            if status.get("running") and modes:
                menu.addSeparator()
                self._section(menu, "Mode")
                for mode_name in modes:
                    action = menu.addAction(mode_name)
                    action.setData(("sched_ext_mode", mode_name))
                    action.setCheckable(True)
                    self._sched_mode_actions.append(action)

        def _refresh_sched_ext(self, rebuild: bool = False) -> None:
            """Re-read scxctl state; rebuild the menu when sched/modes changed."""
            status = sched_ext_status()
            self._sched_ext = status
            signature = self._sched_signature(status)
            if rebuild or signature != self._sched_sig:
                self._sched_sig = signature
                self._build_menu()
                self._update_display()
            else:
                self._update_sched_checks()

        def _on_menu_about_to_show(self) -> None:
            self._refresh_sched_ext()

        def _update_sched_checks(self) -> None:
            status = self._sched_ext
            scheduler = (status.get("scheduler") or "").lower()
            running = bool(status.get("running"))
            for action in self._sched_actions:
                name = action.data()[1]
                if name == "default":
                    action.setChecked(not running)
                else:
                    action.setChecked(
                        running and name.lower() == scheduler
                    )
            mode = (status.get("mode") or "").lower()
            for action in self._sched_mode_actions:
                action.setChecked(
                    bool(mode) and action.data()[1].lower() == mode
                )

        def _apply_sched_ext(self, value: str) -> None:
            binary = _binary("scxctl")
            if binary is None:
                self._notify(
                    "sched_ext", "scxctl not found (install scx-scheds)", False
                )
                return
            if value == "default":
                self._sched_runner.run([binary, "stop"])
            else:
                self._sched_runner.run([binary, "switch", "--sched", value])

        def _apply_sched_ext_mode(self, mode: str) -> None:
            binary = _binary("scxctl")
            if binary is None:
                self._notify(
                    "sched_ext", "scxctl not found (install scx-scheds)", False
                )
                return
            self._sched_runner.run([binary, "switch", "--mode", mode])

        def _on_sched_ext_done(self, output: str) -> None:
            self._refresh_sched_ext(rebuild=True)
            self._notify("sched_ext", output or "done", True)

        def _on_sched_ext_fail(self, message: str) -> None:
            self._notify("sched_ext", message, False)
            self._refresh_sched_ext()

        # ── actions ───────────────────────────────────────────

        def _on_menu_trigger(self, action) -> None:
            data = action.data()
            if not isinstance(data, tuple):
                return
            kind, value = data
            if kind == "profile":
                ok, message = set_power_profile(value)
                self._notify("Power Profile", message, ok)
            elif kind == "governor":
                self._pin_governor(value)
            elif kind == "unpin":
                self._unpin_governor()
            elif kind == "pstate":
                self._set_pstate(value)
            elif kind == "power_limit":
                self._set_power_limit(value)
            elif kind == "sched_ext":
                self._apply_sched_ext(value)
            elif kind == "sched_ext_mode":
                self._apply_sched_ext_mode(value)

        def _pin_governor(self, governor: str) -> None:
            ok, message = apply_governor(governor)
            if not ok:
                self._notify("CPU Governor", message, False)
                return
            written, pin_message = write_pin(governor)
            if written:
                self._pin = governor
            else:
                # Pin file is not writable (missing root install). Enforce locally.
                self._pin = governor
                self._notify("CPU Governor", f"{pin_message}\nEnforcing from the tray.", True)
            self._cfg["governor"] = governor
            save_config(self._cfg)
            self._current_governor = governor
            self._update_display()

        def _unpin_governor(self) -> None:
            write_pin(None)
            self._pin = None
            self._cfg["governor"] = None
            save_config(self._cfg)
            self._update_display()

        def _set_pstate(self, pstate: str) -> None:
            self._cfg["pstate"] = pstate
            save_config(self._cfg)
            self._mark_active_pstate(pstate)
            self._pstate_runner.run([self._pstate_bin, "-ps", pstate])

        def _toggle_autostart(self, checked: bool) -> None:
            set_autostart(checked)
            self._cfg["autostart"] = checked
            save_config(self._cfg)

        def _notify(self, title: str, message: str, ok: bool = True) -> None:
            icon = (
                QtWidgets.QSystemTrayIcon.MessageIcon.Information
                if ok
                else QtWidgets.QSystemTrayIcon.MessageIcon.Warning
            )
            self.showMessage(title, message, icon, 4000)

        def _on_pstate_failure(self, message: str) -> None:
            self._notify("GPU P-State", f"Failed to set P-state:\n{message}", False)

        def _set_power_limit(self, key: str) -> None:
            preset = POWER_LIMIT_PRESETS.get(key)
            if preset is None:
                return
            ok, message = apply_power_limits(*preset)
            if ok:
                self._cfg["power_limit"] = key
                save_config(self._cfg)
            else:
                self._notify("CPU Power Limit", message, False)
            self._update_display()

        # ── refresh loop ──────────────────────────────────────

        def _refresh(self) -> None:
            self._smi_runner.run([
                self._smi_bin, "--query-gpu=index,power.draw,pstate",
                "--format=csv,noheader,nounits",
            ])
            # The pin can also be set by the CLI or by another session.
            self._pin = read_pin()
            counters = read_rapl_counters()
            if counters is not None:
                self._power.add_rapl_samples(*counters)
            self._current_governor = read_governor()
            if self._gdbus:
                self._profile_runner.run(power_profile_command())
            self._enforce_pin()
            self._enforce_power_limit()
            self._update_display()

        def _on_profile_ok(self, raw: str) -> None:
            self._current_profile = parse_gdbus_string(raw)
            self._update_display()

        def _enforce_pin(self) -> None:
            """Re-apply a pinned governor that external automation flipped back."""
            pin = self._pin
            if not pin or self._current_governor == pin:
                return
            if time.monotonic() - self._last_pin_apply < PIN_RETRY_SECONDS:
                return
            self._last_pin_apply = time.monotonic()
            apply_governor(pin)
            self._current_governor = read_governor()

        def _enforce_power_limit(self) -> None:
            """Re-apply the saved preset when firmware or powerd reset it.

            Skipped for limits set through the CLI to a non-preset pair: that
            path stores ``None`` in the config, so the user's explicit choice
            is never fought over.
            """
            key = self._cfg.get("power_limit")
            if key not in POWER_LIMIT_PRESETS:
                return
            wanted = POWER_LIMIT_PRESETS[key]
            limits = read_power_limits()
            if limits is None or limits == wanted:
                self._limit_failed = False
                return
            if time.monotonic() - self._last_limit_apply < LIMIT_RETRY_SECONDS:
                return
            self._last_limit_apply = time.monotonic()
            ok, message = apply_power_limits(*wanted)
            if ok:
                self._limit_failed = False
            elif not self._limit_failed:
                # Notify once per failure streak, not every two seconds.
                self._limit_failed = True
                self._notify("CPU Power Limit", message, False)

        def _on_smi_ok(self, raw: str) -> None:
            power, pstate, count = parse_smi(raw)
            self._power.add_gpu_sample(power, count)
            if pstate != self._current_label:
                self._current_label = pstate
                self.setIcon(_render_icon(pstate))
            self._update_display()

        def _mark_active_pstate(self, active: str) -> None:
            for action in self._pstate_actions:
                action.setChecked(action.data()[1] == active)

        def _update_display(self) -> None:
            governor = self._current_governor or "N/A"
            profile = self._current_profile or "N/A"
            pin = self._pin or "—"

            self._info_governor.setText(f"CPU: {governor}")
            self._info_pin.setText(f"Pin: {pin}")
            self._info_profile.setText(f"Profile: {profile}")
            for action in self._governor_actions:
                action.setChecked(action.data()[1] == governor)
            for action in self._profile_actions:
                action.setChecked(action.data()[1] == profile)

            limits = read_power_limits()
            if limits is None:
                self._info_power.setText(format_power_limit_status(None, None))
                limit_title = "—"
            else:
                pl1_w, pl2_w = limits
                bounds = power_limit_bounds()
                window = read_power_limit_window()
                self._info_power.setText(
                    format_power_limit_status(limits, bounds, window)
                )
                for action in self._power_actions:
                    preset = POWER_LIMIT_PRESETS.get(action.data()[1])
                    action.setChecked(preset == limits)
                preset_key = next(
                    (
                        key
                        for key, preset in POWER_LIMIT_PRESETS.items()
                        if preset == limits
                    ),
                    None,
                )
                limit_title = POWER_LIMIT_LABELS.get(
                    preset_key, f"{pl1_w}/{pl2_w} W"
                )
                if window is not None:
                    limit_title += f" · {_short_seconds(window[0])} s"

            power = self._power
            gpu_text = f"GPU: {power.gpu_now:.1f} W / avg {power.gpu_avg:.1f} W"
            if power.gpu_count > 1:
                gpu_text += f" ({power.gpu_count} GPUs)"
            self._info_gpu.setText(gpu_text)
            pkg_now = f"{power.pkg[-1]:.1f}" if power.pkg else "--"
            dram_now = f"{power.dram[-1]:.1f}" if power.dram else "--"
            self._info_pkg.setText(f"PKG: {pkg_now} W / avg {power.pkg_avg:.1f} W")
            self._info_dram.setText(f"DRAM: {dram_now} W / avg {power.dram_avg:.1f} W")
            self._info_total.setText(
                f"TOTAL: {power.total_now:.1f} W / avg {power.total_avg:.1f} W"
            )
            self._info_energy.setText(
                f"Energy: {power.total_wh:.1f} Wh (PKG {power.pkg_wh:.1f} · "
                f"DRAM {power.dram_wh:.1f})"
            )

            se = self._sched_ext
            if not se.get("available"):
                sched_text = "unavailable"
            elif se.get("running"):
                sched_text = f"{se.get('scheduler')} · {se.get('mode')}"
            else:
                sched_text = "default"
            self._info_schedext.setText(f"sched_ext: {sched_text}")
            self._update_sched_checks()

            # Top-level submenu titles carry the live value of each domain,
            # so the collapsed menu already answers "what is active now".
            self._profile_menu.setTitle(
                f"Profile: {PROFILE_LABELS.get(profile, profile)}"
            )
            self._governor_menu.setTitle(
                f"Governor: {governor}" + (" 📌" if self._pin else "")
            )
            self._sched_menu.setTitle(f"sched_ext: {sched_text}")
            self._gpu_menu.setTitle(f"GPU: {self._current_label}")
            self._limit_menu.setTitle(f"Power Limit: {limit_title}")

            self.setToolTip(
                f"P-State Anchor {__version__}\n"
                f"GPU P{self._current_label}  {power.gpu_now:.1f} W\n"
                f"CPU {governor}   Pin: {pin}\n"
                f"Profile {profile}\n"
                f"PKG {pkg_now} W   DRAM {dram_now} W   "
                f"TOTAL {power.total_now:.1f} W\n"
                f"Energy {power.total_wh:.1f} Wh this session"
            )

        # ── IPC ───────────────────────────────────────────────

        def _on_ipc_connection(self) -> None:
            while (connection := self._ipc.nextPendingConnection()) is not None:
                connection.readyRead.connect(
                    lambda conn=connection: self._on_ipc_data(conn)
                )
                connection.disconnected.connect(connection.deleteLater)

        def _on_ipc_data(self, connection) -> None:
            if connection.readAll().data() == b"show_menu":
                self._notify(
                    "P-State Anchor",
                    "Already running — check the system tray.",
                )
            connection.close()

        def _repaint_icon(self) -> None:
            _icon_cache.clear()
            self.setIcon(_render_icon(self._current_label))

        def _on_activate(self, reason) -> None:
            if reason == QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick:
                self._refresh()


else:  # pragma: no cover - Qt-less fallbacks
    class CommandRunner:  # type: ignore[no-redef]
        pass

    class TrayApp:  # type: ignore[no-redef]
        pass


# ── entry points ──────────────────────────────────────────

def _notify_running_instance() -> bool:
    """Poke an already running tray so it explains itself."""
    import socket as _socket

    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(IPC_SOCKET)
            sock.sendall(b"show_menu")
        return True
    except OSError:
        return False


def run_tray() -> int:
    if not HAVE_QT:
        print(
            "PyQt6 is required for the tray. Install it (python3-pyqt6) or use "
            "--dump-state / --pstate / --governor.",
            file=sys.stderr,
        )
        return 2
    if _notify_running_instance():
        return 0
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("pstate-anchor")
    app.setQuitOnLastWindowClosed(False)
    _tray = TrayApp()  # noqa: F841 - must stay referenced
    return app.exec()


def pstate_value(raw: str) -> str:
    """argparse type for P-state ids: 0-15, or 16 for "driver decides"."""
    if not raw.isdigit() or int(raw) > 16:
        raise argparse.ArgumentTypeError(
            f"invalid P-state {raw!r}: expected 0-15 or {PSTATE_AUTO} (auto)"
        )
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pstate-anchor",
        description=(
            "System tray for NVIDIA GPU P-states, CPU governor and "
            "KDE Power Profiles. Without arguments it starts the tray."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--dump-state", action="store_true",
        help="print machine-readable state as JSON and exit",
    )
    parser.add_argument(
        "--pstate", "--oneshot", dest="pstate", metavar="P", type=pstate_value,
        help=f"set GPU P-state and exit (0-15, {PSTATE_AUTO} = auto)",
    )
    parser.add_argument(
        "--governor", metavar="NAME",
        help="set and pin the CPU governor (e.g. schedutil, performance)",
    )
    parser.add_argument(
        "--unpin-governor", action="store_true",
        help="clear the governor pin so automation regains control",
    )
    parser.add_argument(
        "--profile", choices=sorted(PROFILE_MAP),
        help="set the KDE power profile and exit",
    )
    parser.add_argument(
        "--power-limit", metavar="SPEC",
        help=(
            "set RAPL package limits and exit: eco, stock, or PL1/PL2 in watts "
            "(PL1 cannot exceed the firmware ceiling)"
        ),
    )
    parser.add_argument(
        "--autostart", choices=("on", "off"),
        help="enable or disable autostart and exit",
    )
    parser.add_argument(
        "--sched-ext", metavar="SCHED",
        help=(
            "switch sched_ext scheduler and exit: name from `scxctl list`, "
            "or 'default'/'off' to stop sched_ext (kernel EEVDF)"
        ),
    )
    parser.add_argument(
        "--sched-ext-mode", metavar="MODE",
        help="switch the mode of the running sched_ext scheduler and exit",
    )
    args = parser.parse_args(argv)

    if args.dump_state:
        print(json.dumps(collect_state(), indent=2))
        return 0

    if args.pstate is not None:
        binary = _binary("nvidia-pstate")
        if binary is None:
            print("nvidia-pstate not found: pip install nvidia-pstate", file=sys.stderr)
            return 2
        result = subprocess.run(
            [binary, "-ps", args.pstate], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            print((result.stderr or result.stdout).strip(), file=sys.stderr)
            return result.returncode
        cfg = load_config()
        cfg["pstate"] = args.pstate
        save_config(cfg)
        return 0

    if args.governor is not None:
        ok, message = apply_governor(args.governor)
        if not ok:
            print(message, file=sys.stderr)
            return 1
        written, pin_message = write_pin(args.governor)
        cfg = load_config()
        cfg["governor"] = args.governor
        save_config(cfg)
        print(message if written else f"{message}; {pin_message}", file=sys.stderr)
        return 0

    if args.unpin_governor:
        _, message = write_pin(None)
        cfg = load_config()
        cfg["governor"] = None
        save_config(cfg)
        print(message, file=sys.stderr)
        return 0

    if args.autostart is not None:
        cleanup_legacy_entries()
        enabled = args.autostart == "on"
        set_autostart(enabled)
        cfg = load_config()
        cfg["autostart"] = enabled
        save_config(cfg)
        return 0

    if args.power_limit is not None:
        try:
            limits = parse_power_limit_spec(args.power_limit)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        ok, message = apply_power_limits(*limits)
        if not ok:
            print(message, file=sys.stderr)
            return 1
        key = args.power_limit.strip().lower()
        cfg = load_config()
        cfg["power_limit"] = key if key in POWER_LIMIT_PRESETS else None
        save_config(cfg)
        print(message, file=sys.stderr)
        return 0

    if args.sched_ext is not None:
        value = args.sched_ext.strip().lower()
        if value in ("default", "off", "none", "stop"):
            ok, message = apply_sched_ext(stop=True)
        else:
            ok, message = apply_sched_ext(sched=value)
        print(message, file=sys.stderr)
        return 0 if ok else 1

    if args.sched_ext_mode is not None:
        ok, message = apply_sched_ext(mode=args.sched_ext_mode)
        print(message, file=sys.stderr)
        return 0 if ok else 1

    if args.profile is not None:
        ok, message = set_power_profile(args.profile)
        if not ok:
            print(message, file=sys.stderr)
            return 1
        return 0

    return run_tray()


if __name__ == "__main__":
    sys.exit(main())
