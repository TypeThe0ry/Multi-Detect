from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import profile_onboard_cpu_budget as profiler  # noqa: E402


def test_cpu_profile_has_repeatable_stages_and_fixed_input_hashes() -> None:
    first = profiler.run(repeats=3, iterations=3, warmup=1)
    second = profiler.run(repeats=3, iterations=3, warmup=1)

    assert first["input"] == second["input"]
    assert first["measurement"]["total_samples_per_stage"] == 9
    assert set(first["stages"]) == {
        "replay_decode_fixed_episode", "detector_preprocess_synthetic", "planner_policy_primitive",
        "geometry_association_iou", "serialization_logging_json",
    }
    assert all(stage["samples"] == 9 for stage in first["stages"].values())
    assert all(stage["p50_ms"] >= 0 and stage["p95_ms"] >= 0 for stage in first["stages"].values())
