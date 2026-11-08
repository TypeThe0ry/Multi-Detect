from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/profile_onboard_association_scaling.py"
SPEC = importlib.util.spec_from_file_location("profile_onboard_association_scaling", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)


def test_association_scaling_records_all_sizes_and_exact_outputs() -> None:
    report = PROFILE.run(repeats=3, warmup=1)

    assert [row["candidates"] for row in report["scales"]] == [16, 32, 64, 128]
    assert all(row["tracks"] == row["output_items"] for row in report["scales"])
    assert all(len(row["output_sha256"]) == 64 for row in report["scales"])
    assert report["input"]["original_rgb_frames"] == 0
    assert "not measured" in report["input"]["rgb_preprocessing"]
