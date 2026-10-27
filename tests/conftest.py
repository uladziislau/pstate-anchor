"""Pytest fixtures: load the single-file script under a module name.

The script ships with a dash in its filename (so that `curl -o` produces a
ready-to-run command), which means it cannot be imported with a plain
`import`. `spec_from_file_location` loads it as a normal module instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "nvidia-pstate-switcher.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pstate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pstate"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pstate():
    return _load_module()


@pytest.fixture
def fake_sysfs(tmp_path):
    """Temporary stand-in for the cpufreq/RAPL files the script reads."""
    available = tmp_path / "scaling_available_governors"
    available.write_text("schedutil powersave performance\n")
    governor = tmp_path / "scaling_governor"
    governor.write_text("schedutil\n")
    pkg = tmp_path / "energy_uj_pkg"
    pkg.write_text("1000000\n")
    dram = tmp_path / "energy_uj_dram"
    dram.write_text("500000\n")
    return {
        "available": str(available),
        "governor": str(governor),
        "pkg": str(pkg),
        "dram": str(dram),
    }
