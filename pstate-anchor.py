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
import shutil
import subprocess
import sys
import time
from collections import deque
from statistics import mean

__version__ = "0.2026.10.27"

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

DEFAULT_CONFIG = {"pstate": PSTATE_AUTO, "autostart": True, "governor": None}


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

    def add_gpu_sample(self, watts: float, count: int = 1) -> None:
        self.gpu_now = watts
        self.gpu_count = count
        self.gpu.append(watts)

    def add_rapl_samples(self, pkg_uj: int, dram_uj: int) -> None:
        if self._last is None:
            pkg_w = dram_w = 0.0
        else:
            pkg_w = rapl_watts(self._last[0], pkg_uj, self._interval)
            dram_w = rapl_watts(self._last[1], dram_uj, self._interval)
            # Counter wrap-around yields a negative delta: treat as zero.
            pkg_w = max(0.0, min(pkg_w, 300.0))
            dram_w = max(0.0, min(dram_w, 50.0))
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
        "gpu": {
            "count": gpu_count,
            "pstate": gpu_pstate,
            "power_w": round(gpu_power, 2),
        },
        "rapl_readable": rapl is not None,
        "dependencies": {
            "nvidia-smi": smi,
            "nvidia-pstate": _binary("nvidia-pstate"),
            "cpupower": _binary("cpupower"),
            "gdbus": _binary("gdbus"),
            "PyQt6": _pyqt_available(),
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

            # Read the KDE profile asynchronously: a blocking gdbus call in the
            # refresh loop could stall the tray for up to its 5 s timeout.
            self._profile_runner = CommandRunner()
            self._profile_runner.finished.connect(self._on_profile_ok)
            self._gdbus = shutil.which("gdbus")

            self._build_menu()

            saved = self._cfg.get("pstate", PSTATE_AUTO)
            self._mark_active_pstate(str(saved))
            if str(saved) != PSTATE_AUTO:
                self._set_pstate(str(saved))

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
            self._menu = QtWidgets.QMenu()
            self._profile_actions = []
            self._governor_actions = []
            self._pstate_actions = []

            self._section("Power Profile")
            for profile_id, label in PROFILE_LABELS.items():
                action = self._menu.addAction(label)
                action.setData(("profile", profile_id))
                action.setCheckable(True)
                self._profile_actions.append(action)

            self._menu.addSeparator()
            self._section("CPU Governor")
            for governor in selectable_governors():
                action = self._menu.addAction(governor)
                action.setData(("governor", governor))
                action.setCheckable(True)
                self._governor_actions.append(action)
            unpin = self._menu.addAction("Follow system (unpin)")
            unpin.setData(("unpin", None))

            self._menu.addSeparator()
            self._section("GPU P-State")
            for pstate, label in self._pstates.items():
                text = f"P{pstate} — {label}" if label else f"P{pstate}"
                action = self._menu.addAction(text)
                action.setData(("pstate", pstate))
                action.setCheckable(True)
                self._pstate_actions.append(action)

            self._menu.addSeparator()
            self._section("Status")
            self._info_governor = self._info_item("CPU: —")
            self._info_pin = self._info_item("Pin: —")
            self._info_profile = self._info_item("Profile: —")
            self._info_gpu = self._info_item("GPU: — / —")
            self._info_pkg = self._info_item("PKG: —")
            self._info_dram = self._info_item("DRAM: —")
            self._info_total = self._info_item("TOTAL: —")

            self._menu.addSeparator()
            autostart = self._menu.addAction("Run at startup")
            autostart.setCheckable(True)
            autostart.setChecked(autostart_enabled())
            autostart.triggered.connect(self._toggle_autostart)

            self._menu.addSeparator()
            self._menu.addAction("Refresh", self._refresh)
            self._menu.addAction("Quit", QtWidgets.QApplication.quit)

            self._menu.triggered.connect(self._on_menu_trigger)
            self.setContextMenu(self._menu)

        def _section(self, title: str) -> None:
            header = self._menu.addAction(title)
            header.setEnabled(False)
            font = header.font()
            font.setBold(True)
            header.setFont(font)

        def _info_item(self, text: str):
            action = self._menu.addAction(text)
            action.setEnabled(False)
            return action

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

            self.setToolTip(
                f"P-State Anchor {__version__}\n"
                f"GPU P{self._current_label}  {power.gpu_now:.1f} W\n"
                f"CPU {governor}   Pin: {pin}\n"
                f"Profile {profile}\n"
                f"PKG {pkg_now} W   DRAM {dram_now} W   "
                f"TOTAL {power.total_now:.1f} W"
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
        "--autostart", choices=("on", "off"),
        help="enable or disable autostart and exit",
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

    if args.profile is not None:
        ok, message = set_power_profile(args.profile)
        if not ok:
            print(message, file=sys.stderr)
            return 1
        return 0

    return run_tray()


if __name__ == "__main__":
    sys.exit(main())
