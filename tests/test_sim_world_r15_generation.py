"""Focused checks for r15 Fuel world set."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_r15_index_and_real_assets():
    d = json.loads(
        (ROOT / "configs/scenarios/generated-r15/index.json").read_text(encoding="utf-8")
    )
    assert len(d["worlds"]) == 20
    assert sum(x["target_combo"]["slug"] == "backpack" for x in d["worlds"]) >= 10
    assert all(x["asset_mode"] == "fuel_v1" for x in d["worlds"])


def test_r15_validation():
    subprocess.run([sys.executable, str(ROOT / "scripts/validate_r15_worlds.py")], check=True)


def test_r15_cli_dry_run():
    out = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/generate_r15_worlds.py"), "--dry-run"], text=True
    )
    assert '"count": 20' in out


def test_r15_cli_gimbal_sidecar_options_are_explicit():
    out = subprocess.check_output(
        [
            sys.executable,
            str(ROOT / "scripts/generate_r15_worlds.py"),
            "--dry-run",
            "--gimbal-sidecar",
            "--gimbal-yaw-rad",
            "0.25",
            "--gimbal-roll-rad",
            "-0.1",
            "--stabilization-mode",
            "follow-yaw",
            "--propeller-spin-rad-s",
            "10",
            "-10",
            "10",
            "-10",
        ],
        text=True,
    )
    payload = json.loads(out)
    assert payload["actuator_state_sidecar"] == {
        "schema_version": 1,
        "gimbal": {
            "yaw_rad": 0.25,
            "roll_rad": -0.1,
            "stabilization_mode": "follow-yaw",
        },
        "propeller_spin_rad_s": [10.0, -10.0, 10.0, -10.0],
    }

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/generate_r15_worlds.py"),
            "--dry-run",
            "--gimbal-yaw-rad",
            "0.25",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "require --gimbal-sidecar" in result.stderr
