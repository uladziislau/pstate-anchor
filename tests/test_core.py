"""Behavioural tests for the parts of the script that machines care about.

Nothing here needs a display: the tray, DBus and nvidia-smi are exercised only
through the parsers and through the pure state writers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pstate-anchor.py"


# ── config ────────────────────────────────────────────────

def test_load_config_defaults_when_missing(pstate, tmp_path):
    cfg = pstate.load_config(str(tmp_path / "absent.conf"))
    assert cfg == {"pstate": pstate.PSTATE_AUTO, "autostart": True, "governor": None}


def test_load_config_survives_broken_json(pstate, tmp_path):
    broken = tmp_path / "broken.conf"
    broken.write_text('{"pstate": ')
    assert pstate.load_config(str(broken))["pstate"] == pstate.PSTATE_AUTO


def test_load_config_ignores_unknown_keys_and_merges_partial(pstate, tmp_path):
    conf = tmp_path / "conf.json"
    conf.write_text(json.dumps({"pstate": "5", "bogus": 1}))
    cfg = pstate.load_config(str(conf))
    assert cfg["pstate"] == "5"
    assert "bogus" not in cfg
    assert cfg["autostart"] is True


def test_save_config_roundtrip_creates_parents(pstate, tmp_path):
    conf = tmp_path / "deep" / "nested" / "conf.json"
    pstate.save_config({"pstate": "8", "autostart": False, "governor": "performance"}, str(conf))
    assert pstate.load_config(str(conf))["governor"] == "performance"


def test_load_config_falls_back_to_legacy_file(pstate, tmp_path, monkeypatch):
    legacy = tmp_path / "nvidia-pstate-switcher.conf"
    legacy.write_text('{"pstate": "5", "autostart": false}')
    monkeypatch.setattr(pstate, "CONFIG_FILE", str(tmp_path / "pstate-anchor.conf"))
    monkeypatch.setattr(pstate, "LEGACY_CONFIG_FILE", str(legacy))

    cfg = pstate.load_config(str(tmp_path / "pstate-anchor.conf"))
    assert cfg["pstate"] == "5"
    assert cfg["autostart"] is False


def test_own_config_wins_over_legacy(pstate, tmp_path, monkeypatch):
    legacy = tmp_path / "nvidia-pstate-switcher.conf"
    legacy.write_text('{"pstate": "5"}')
    own = tmp_path / "pstate-anchor.conf"
    own.write_text('{"pstate": "8"}')
    monkeypatch.setattr(pstate, "CONFIG_FILE", str(own))
    monkeypatch.setattr(pstate, "LEGACY_CONFIG_FILE", str(legacy))
    assert pstate.load_config(str(own))["pstate"] == "8"


def test_cleanup_legacy_entries_removes_only_old_artefacts(pstate, tmp_path, monkeypatch):
    old_autostart = tmp_path / "autostart-old.desktop"
    old_autostart.write_text("Exec=/usr/local/bin/nvidia-pstate-switcher\n")
    old_desktop = tmp_path / "desktop-old.desktop"
    old_desktop.write_text("Exec=/usr/local/bin/nvidia-pstate-switcher\n")
    user_file = tmp_path / "user-edit.desktop"
    user_file.write_text("Exec=/opt/custom/pstate-anchor --tray\n")

    monkeypatch.setattr(pstate, "LEGACY_AUTOSTART_FILE", str(old_autostart))
    monkeypatch.setattr(pstate, "LEGACY_DESKTOP_FILE", str(user_file))

    removed = pstate.cleanup_legacy_entries()
    assert str(old_autostart) in removed
    assert not old_autostart.exists()
    assert user_file.exists(), "hand-edited file must survive"


# ── cpufreq ───────────────────────────────────────────────

def test_available_governors_parses_sysfs(pstate, fake_sysfs):
    assert pstate.available_governors(fake_sysfs["available"]) == [
        "schedutil", "powersave", "performance",
    ]


def test_available_governors_falls_back_when_sysfs_missing(pstate, tmp_path):
    assert pstate.available_governors(str(tmp_path / "nope")) == list(
        pstate.FALLBACK_GOVERNORS
    )


def test_read_governor_returns_none_without_cpufreq(pstate, tmp_path):
    assert pstate.read_governor(str(tmp_path / "nope")) is None


def test_apply_governor_rejects_unknown_value(pstate, fake_sysfs):
    ok, message = pstate.apply_governor(
        "turbo",
        governor_path=fake_sysfs["governor"],
        available_path=fake_sysfs["available"],
    )
    assert ok is False
    assert "not available" in message


def test_apply_governor_is_noop_when_already_active(pstate, fake_sysfs):
    ok, message = pstate.apply_governor(
        "schedutil",
        governor_path=fake_sysfs["governor"],
        available_path=fake_sysfs["available"],
    )
    assert ok is True
    assert message == "already active"


# ── governor pin ──────────────────────────────────────────

def test_write_pin_creates_parent_directory(pstate, tmp_path, fake_sysfs):
    pin = tmp_path / "etc" / "pstate-anchor" / "governor-pin"
    ok, message = pstate.write_pin(
        "performance", path=str(pin), available_path=fake_sysfs["available"]
    )
    assert ok is True
    assert pin.read_text().strip() == "performance"
    assert pstate.read_pin(str(pin)) == "performance"
    assert message == f"pin set to performance ({pin})"


def test_active_pin_file_prefers_system_dir_when_writable(pstate, tmp_path, monkeypatch):
    system = tmp_path / "etc" / "pstate-anchor" / "governor-pin"
    system.parent.mkdir(parents=True)
    monkeypatch.setattr(pstate, "PIN_FILE", str(system))
    monkeypatch.setattr(pstate, "USER_PIN_FILE", str(tmp_path / "user-pin"))
    assert pstate.active_pin_file() == str(system)


def test_active_pin_file_falls_back_to_user_dir(pstate, tmp_path, monkeypatch):
    missing_system = tmp_path / "no-such-etc" / "governor-pin"
    user_pin = tmp_path / "config" / "pstate-anchor" / "governor-pin"
    monkeypatch.setattr(pstate, "PIN_FILE", str(missing_system))
    monkeypatch.setattr(pstate, "USER_PIN_FILE", str(user_pin))
    assert pstate.active_pin_file() == str(user_pin)


def test_write_pin_uses_fallback_location_and_roundtrips(pstate, tmp_path, monkeypatch):
    monkeypatch.setattr(pstate, "PIN_FILE", str(tmp_path / "nope" / "governor-pin"))
    monkeypatch.setattr(pstate, "USER_PIN_FILE", str(tmp_path / "user" / "governor-pin"))
    available = _write_available(tmp_path, "schedutil powersave")

    ok, _ = pstate.write_pin("powersave", available_path=available)
    assert ok is True
    assert (tmp_path / "user" / "governor-pin").read_text().strip() == "powersave"
    assert pstate.read_pin() == "powersave"

    ok, message = pstate.write_pin(None)
    assert ok is True and message == "pin cleared"
    assert pstate.read_pin() is None


def _write_available(tmp_path, content):
    path = tmp_path / "available"
    path.write_text(content + "\n")
    return str(path)


def test_write_pin_rejects_unavailable_governor(pstate, tmp_path, fake_sysfs):
    pin = tmp_path / "pin"
    ok, _ = pstate.write_pin(
        "turbo", path=str(pin), available_path=fake_sysfs["available"]
    )
    assert ok is False
    assert not pin.exists()


def test_write_pin_none_clears(pstate, tmp_path, fake_sysfs):
    pin = tmp_path / "pin"
    pstate.write_pin("powersave", path=str(pin), available_path=fake_sysfs["available"])
    ok, message = pstate.write_pin(None, path=str(pin))
    assert ok is True
    assert message == "pin cleared"
    assert not pin.exists()
    assert pstate.write_pin(None, path=str(pin))[0] is True  # idempotent


# ── nvidia-smi parsing ────────────────────────────────────

def test_parse_smi_single_gpu(pstate):
    power, pstate_label, count = pstate.parse_smi("0, 43.14, P0\n")
    assert (round(power, 2), pstate_label, count) == (43.14, "P0", 1)


def test_parse_smi_sums_power_across_gpus(pstate):
    raw = "0, 40.0, P0\n1, 10.5, P8\n"
    power, pstate_label, count = pstate.parse_smi(raw)
    assert (power, pstate_label, count) == (50.5, "P0", 2)


def test_parse_smi_degrades_on_na_and_garbage(pstate):
    power, pstate_label, count = pstate.parse_smi("0, [N/A], P0\nnonsense\n")
    assert (power, pstate_label, count) == (0.0, "P0", 1)


def test_parse_smi_empty_output(pstate):
    assert pstate.parse_smi("") == (0.0, "—", 0)


# ── RAPL maths ────────────────────────────────────────────

def test_rapl_watts_converts_microjoule_delta(pstate):
    # 2_000_000 µJ over 2 s == 1 W
    assert pstate.rapl_watts(1_000_000, 3_000_000, 2.0) == 1.0


def test_power_monitor_first_sample_is_zero(pstate):
    monitor = pstate.PowerMonitor(interval=2.0)
    monitor.add_rapl_samples(1_000_000, 500_000)
    assert monitor.pkg[-1] == 0.0 and monitor.dram[-1] == 0.0


def test_power_monitor_second_sample_computes_watts(pstate):
    monitor = pstate.PowerMonitor(interval=2.0)
    monitor.add_rapl_samples(1_000_000, 500_000)
    monitor.add_rapl_samples(1_000_000 + 20_000_000, 500_000 + 4_000_000)
    assert monitor.pkg[-1] == 10.0
    assert monitor.dram[-1] == 2.0


def test_power_monitor_clamps_outliers_and_counter_wrap(pstate):
    monitor = pstate.PowerMonitor(interval=1.0)
    monitor.add_rapl_samples(9_000_000, 900_000)
    monitor.add_rapl_samples(1_000_000, 100_000)  # counter wrap: negative delta
    assert monitor.pkg[-1] == 0.0 and monitor.dram[-1] == 0.0


def test_power_monitor_total_and_average(pstate):
    monitor = pstate.PowerMonitor(interval=2.0)
    monitor.add_gpu_sample(40.0, 1)
    monitor.add_rapl_samples(0, 0)
    monitor.add_rapl_samples(20_000_000, 4_000_000)
    assert monitor.pkg[-1] == 10.0
    assert monitor.total_now == 52.0
    assert round(monitor.total_avg, 2) == round(40.0 + 5.0 + 1.0, 2)


def test_power_monitor_window_is_bounded(pstate):
    monitor = pstate.PowerMonitor(window=6, interval=2.0)  # 3 slots
    for value in (1.0, 2.0, 3.0, 4.0):
        monitor.add_gpu_sample(value)
    assert list(monitor.gpu) == [2.0, 3.0, 4.0]


def test_read_rapl_counters_reads_both_files(pstate, fake_sysfs):
    assert pstate.read_rapl_counters(fake_sysfs["pkg"], fake_sysfs["dram"]) == (
        1_000_000, 500_000,
    )


def test_read_rapl_counters_returns_none_when_unreadable(pstate, tmp_path):
    assert pstate.read_rapl_counters(
        str(tmp_path / "x"), str(tmp_path / "y")
    ) is None


# ── desktop integration ───────────────────────────────────

def test_autostart_toggle_writes_and_removes_entry(pstate, tmp_path, monkeypatch):
    autostart = tmp_path / "autostart" / "pstate-anchor.desktop"
    monkeypatch.setattr(pstate.sys, "argv", ["/opt/bin/pstate-anchor"])
    pstate.set_autostart(True, str(autostart))
    content = autostart.read_text()
    assert "X-KDE-autostart-condition=true" in content
    assert "Exec=/opt/bin/pstate-anchor" in content
    assert pstate.autostart_enabled(str(autostart)) is True

    pstate.set_autostart(False, str(autostart))
    assert pstate.autostart_enabled(str(autostart)) is False
    pstate.set_autostart(False, str(autostart))  # idempotent


def test_ensure_desktop_entry_is_idempotent(pstate, tmp_path):
    desktop = tmp_path / "applications" / "pstate-anchor.desktop"
    pstate.ensure_desktop_entry(str(desktop))
    first = desktop.read_text()
    assert "X-KDE-autostart-condition" not in first
    desktop.write_text("# user edit\n")
    pstate.ensure_desktop_entry(str(desktop))
    assert desktop.read_text() == "# user edit\n"



# ── guard contract ────────────────────────────────────────

GUARD = SCRIPT.parent / "contrib" / "pstate-anchor-guard"


def _guard_tree(tmp_path, current, available="schedutil powersave performance"):
    governor = tmp_path / "scaling_governor"
    governor.write_text(current + "\n")
    avail = tmp_path / "scaling_available_governors"
    avail.write_text(available + "\n")
    return governor, avail


def _run_guard(env, *args):
    return subprocess.run(
        ["bash", str(GUARD), *args], capture_output=True, text=True, env=env, timeout=30,
    )


def _guard_env(governor, avail, pin=None, default="schedutil"):
    env = os.environ.copy()
    env.update({
        "PSTATE_GOVERNOR_PATH": str(governor),
        "PSTATE_AVAILABLE_GOVERNORS_PATH": str(avail),
        "PSTATE_PIN_FILE": str(pin) if pin else str(governor.parent / "absent-pin"),
        "DEFAULT_GOVERNOR": default,
    })
    return env


def test_guard_applies_the_pin(tmp_path):
    governor, avail = _guard_tree(tmp_path, "schedutil")
    pin = tmp_path / "governor-pin"
    pin.write_text("powersave\n")
    result = _run_guard(_guard_env(governor, avail, pin))
    assert result.returncode == 0, result.stderr
    assert governor.read_text().strip() == "powersave"


def test_guard_falls_back_to_default_without_pin(tmp_path):
    governor, avail = _guard_tree(tmp_path, "performance")
    result = _run_guard(_guard_env(governor, avail, default="schedutil"))
    assert result.returncode == 0, result.stderr
    assert governor.read_text().strip() == "schedutil"


def test_guard_ignores_a_pin_the_kernel_does_not_offer(tmp_path):
    governor, avail = _guard_tree(tmp_path, "performance")
    pin = tmp_path / "governor-pin"
    pin.write_text("turbo\n")
    result = _run_guard(_guard_env(governor, avail, pin, default="schedutil"))
    assert result.returncode == 0, result.stderr
    assert governor.read_text().strip() == "schedutil"


def test_guard_check_reports_the_decision_without_changing_anything(tmp_path):
    governor, avail = _guard_tree(tmp_path, "performance")
    pin = tmp_path / "governor-pin"
    pin.write_text("powersave\n")
    result = _run_guard(_guard_env(governor, avail, pin), "--check")
    assert result.returncode == 0, result.stderr
    assert "current=performance" in result.stdout
    assert "target=powersave" in result.stdout
    assert "source=pin" in result.stdout
    assert governor.read_text().strip() == "performance"


def test_guard_is_a_noop_when_already_pinned(tmp_path):
    governor, avail = _guard_tree(tmp_path, "powersave")
    pin = tmp_path / "governor-pin"
    pin.write_text("powersave\n")
    result = _run_guard(_guard_env(governor, avail, pin))
    assert result.returncode == 0, result.stderr
    assert governor.read_text().strip() == "powersave"

# ── invariants ────────────────────────────────────────────

def test_profile_map_uses_offered_governors_and_pstates(pstate):
    for profile, settings in pstate.PROFILE_MAP.items():
        assert settings["governor"] in pstate.FALLBACK_GOVERNORS, profile
        assert settings["pstate"] in pstate.DEFAULT_PSTATES, profile


def test_balanced_profile_is_auto_pstate(pstate):
    assert pstate.PROFILE_MAP["balanced"]["pstate"] == pstate.PSTATE_AUTO


def test_every_governor_has_no_duplicate_entries(pstate):
    governors = pstate.available_governors()
    assert len(governors) == len(set(governors))


# ── CLI contract ──────────────────────────────────────────

def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, check=False,
        env=env or os.environ.copy(),
    )


def test_cli_version_matches_module(pstate):
    result = _run("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == pstate.__version__


def test_version_is_calendar_scheme(pstate):
    parts = pstate.__version__.split(".")
    assert len(parts) == 4, "expected 0.YYYY.MM.DD"
    major, year, month, day = parts
    assert major == "0"
    assert len(year) == 4 and 2000 <= int(year) <= 2999
    assert 1 <= int(month) <= 12
    assert 1 <= int(day) <= 31


def test_cli_dump_state_is_valid_json_without_gui(monkeypatch):
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = _run("--dump-state", env=env)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["version"] == "0.2026.10.27"
    assert isinstance(state["available_governors"], list)
    assert "governor_pin" in state and "dependencies" in state
    assert set(state["gpu"]) == {"count", "pstate", "power_w"}


def test_cli_dump_state_works_without_pyqt():
    blocker = (
        "import sys, runpy\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name.split('.')[0] == 'PyQt6' else None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'PyQt6':\n"
        "            raise ImportError('blocked for test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        f"sys.argv = [{str(SCRIPT)!r}, '--dump-state']\n"
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", blocker], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["dependencies"]["PyQt6"] is False


def test_tray_refuses_to_start_without_pyqt():
    blocker = (
        "import sys, runpy\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'PyQt6':\n"
        "            raise ImportError('blocked for test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        f"sys.argv = [{str(SCRIPT)!r}]\n"
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", blocker], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "PyQt6 is required" in result.stderr


def test_cli_pstate_without_nvidia_pstate_fails_cleanly(tmp_path):
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)  # no nvidia-pstate on PATH
    env["HOME"] = str(tmp_path)
    result = _run("--pstate", "5", env=env)
    assert result.returncode == 2
    assert "pip install nvidia-pstate" in result.stderr
