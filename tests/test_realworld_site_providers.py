from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("mapbox_earcut")
from scripts.realworld.build_scene import Terrain, vegetation_index
from scripts.realworld.city_models import parse_citygml
from pyproj import Transformer


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


def test_citygml_surfaces_normalize_to_local_coordinates(tmp_path) -> None:
    path = tmp_path / "tile.gml"
    path.write_text("""<CityModel xmlns:gml=\"http://www.opengis.net/gml\" xmlns:bldg=\"http://www.opengis.net/citygml/building/2.0\"><bldg:Building gml:id=\"b1\"><bldg:boundedBy><bldg:RoofSurface><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList srsDimension=\"3\">100 200 10 110 200 10 110 210 10 100 210 10 100 200 10</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></bldg:RoofSurface></bldg:boundedBy></bldg:Building></CityModel>""")
    rows = parse_citygml(path, Transformer.from_crs("EPSG:25833", "EPSG:25833", always_xy=True))
    assert len(rows) == 1
    assert rows[0]["surfaces"][0]["kind"] == "RoofSurface"
    assert rows[0]["surfaces"][0]["vertices"][1] == [110.0, 200.0, 10.0]
