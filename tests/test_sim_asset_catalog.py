import json
import math
from pathlib import Path

CATALOG = Path(__file__).parents[1] / "configs/sim-assets/fuel-catalog.json"


def test_fuel_catalog_bounds_are_finite_and_plausible():
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert data["asset_mode"] == "fuel_v1"
    assert len(data["assets"]) == 39
    for asset in data["assets"]:
        size = asset["measured_size_m"]
        assert len(size) == 3 and all(math.isfinite(v) and 0 < v < 1000 for v in size)
        assert all(math.isfinite(v) for v in asset["bbox_min_m"] + asset["bbox_max_m"])
    by_source = {a["source"]: a for a in data["assets"]}
    assert 3.5 <= max(by_source["Hatchback"]["measured_size_m"]) <= 6.0
    assert 1.5 <= by_source["Walking_person"]["measured_size_m"][2] <= 2.0
    assert 0.3 <= by_source["Construction_Cone"]["measured_size_m"][2] <= 1.2
