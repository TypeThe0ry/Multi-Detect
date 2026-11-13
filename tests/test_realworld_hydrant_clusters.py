import json
import math
from pathlib import Path

import pytest

pytest.importorskip("shapely")
from scripts.realworld.hydrant_clusters import SPACINGS_M, triangle  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("side", SPACINGS_M)
def test_triangle_gaps_equal_side(side):
    p = triangle(12.0, -7.0, side, 0.7)
    gaps = [math.dist(p[i], p[j]) for i in range(3) for j in range(i + 1, 3)]
    assert gaps == pytest.approx([side] * 3)


def test_index_truth_matches_spacing():
    index = json.loads((ROOT / "configs/scenarios/realworld-clusters/index.json").read_text())
    assert index["worlds"]
    for world in index["worlds"]:
        truth = json.loads((ROOT / "configs/scenarios/realworld-clusters" / f"{world['world_id']}.truth.json").read_text())
        points = [(t["pose"]["x_east_m"], t["pose"]["y_north_m"]) for t in truth["targets"]]
        assert len(points) == world["expected_count"] == 3
        assert all(abs(math.dist(a, b) - world["cluster_spacing_m"]) < 0.01 for i, a in enumerate(points) for b in points[i + 1:])
        assert sum(t["true_target"] for t in truth["targets"]) == 1
