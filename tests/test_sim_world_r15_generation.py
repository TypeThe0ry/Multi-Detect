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
