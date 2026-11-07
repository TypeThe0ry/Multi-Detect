from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("mapbox_earcut")
from scripts.realworld.build_scene import Terrain, vegetation_index


def test_rgb_only_imagery_uses_visible_vegetation_index() -> None:
    rgba = np.zeros((2, 2, 4), dtype=np.uint8)
    rgba[:, 0, :3] = (40, 170, 40)
    rgba[:, 1, :3] = (170, 40, 40)
    rgba[..., 3] = 255

    index, method = vegetation_index(rgba)

    assert method == "vari"
    assert np.all(index[:, 0] > 0.5)
    assert np.all(index[:, 1] < 0)


def test_nir_imagery_keeps_ndvi_and_terrain_uses_dem_grid_spacing(tmp_path) -> None:
    rgba = np.zeros((1, 2, 4), dtype=np.uint8)
    rgba[0, 0] = (30, 80, 30, 190)
    rgba[0, 1] = (80, 80, 80, 80)
    index, method = vegetation_index(rgba)
    assert method == "ndvi"
    assert index[0, 0] > index[0, 1]

    np.save(tmp_path / "dem.npy", np.zeros((5, 5), dtype=np.float32))
    terrain = Terrain({"size_m": 4}, tmp_path)
    assert terrain.res == 1.0
