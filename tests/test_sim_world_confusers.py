"""Checks for Fuel identity-hard-negative world generation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from multidetect.simulator.world_gen import generate_fuel_worlds

ROOT = Path(__file__).resolve().parents[1]


def test_confusers_are_placed_and_flagged(tmp_path: Path) -> None:
    generate_fuel_worlds(
        count=1,
        seed=20261016,
        split_ratios=(1.0, 0.0, 0.0),
        out_worlds=tmp_path / "worlds",
        out_scenarios=tmp_path / "scenarios",
        catalog_path=ROOT / "configs/sim-assets/fuel-catalog.json",
        resolved_root=ROOT / "artifacts/cache/gz-fuel/resolved",
        segmentation_labels=True,
        unambiguous_tasks=True,
        world_prefix="gen_cfs_w",
        count_slugs=("backpack",),
        confusers={"backpack": ("electrical_box", "cardboard_box")},
        confuser_copies=2,
        spread_m=(10.0, 50.0),
    )
    truth = json.loads((tmp_path / "scenarios/gen_cfs_w0000.truth.json").read_text())
    confusers = [row for row in truth["targets"] if row.get("confuser")]
    assert [row["asset_slug"] for row in confusers] == [
        "electrical_box",
        "electrical_box",
        "cardboard_box",
        "cardboard_box",
    ]
    confuser_ids = {row["target_id"] for row in confusers}
    assert all(
        truth["segmentation_labels"][label]["role"] == "confuser"
        for label in truth["segmentation_labels"]
        if truth["segmentation_labels"][label]["target_id"] in confuser_ids
    )


def test_confuser_cli_dry_run() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_confuser_worlds.py"), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["world_prefix"] == "gen_cfs_w"
    assert payload["count"] == 12
