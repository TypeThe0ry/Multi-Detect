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


def test_citygml_keeps_vertical_walls_all_polygons_and_separate_parts(tmp_path) -> None:
    from shapely.geometry import box
    path = tmp_path / "walls.gml"
    polygon = "<gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>0 0 0 4 0 0 4 0 8 0 0 8 0 0 0</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
    path.write_text(f"<CityModel xmlns:gml='http://www.opengis.net/gml' xmlns:b='http://www.opengis.net/citygml/building/2.0'><b:Building gml:id='parent'><b:WallSurface>{polygon}{polygon}</b:WallSurface><b:consistsOfBuildingPart><b:BuildingPart gml:id='child'><b:WallSurface>{polygon}</b:WallSurface></b:BuildingPart></b:consistsOfBuildingPart></b:Building></CityModel>")
    rows = parse_citygml(path, Transformer.from_crs("EPSG:25833", "EPSG:25833", always_xy=True), area=box(-1, -1, 5, 1))
    assert [(row["id"], len(row["surfaces"])) for row in rows] == [("parent", 2), ("child", 1)]


def test_obstacle_boxes_use_native_dem_spacing_and_do_not_fill_takeoff_pad(monkeypatch, tmp_path) -> None:
    import xml.etree.ElementTree as ET
    from scripts.realworld import make_world

    raw = tmp_path / "sites/test/raw"
    raw.mkdir(parents=True)
    dem = np.tile(np.arange(9, dtype=float), (9, 1))
    np.save(raw / "dem.npy", dem)
    monkeypatch.setattr(make_world, "DATA_ROOT", tmp_path)
    scene = {"size_m": 400, "elevation": 4, "footprints": [
        [-40, -40, 40, 40, 20], [90, -10, 110, 10, 12],
    ]}
    root = ET.fromstring("<root>" + make_world.obstacle_boxes(scene, "test") + "</root>")
    assert len(root) == 1  # A coarse box may not block the clear launch area.
    pose = list(map(float, root[0].findtext("pose").split()))
    assert pose[:3] == [100.0, 0.0, 8.0]


def test_merged_asset_is_centered_and_grounded_once() -> None:
    from scripts.realworld.instancing import Merger, _normalize_parts
    vertices = np.array([[-2., -1., -3.], [2., -1., 1.], [0., 1., -3.]])
    parts = [{"v": vertices, "uv": np.zeros((3, 2)), "tex": None, "color": None}]
    _normalize_parts(parts, "test-uncatalogued-asset")
    assert np.allclose(parts[0]["v"].min(0), [-2., -1., 0.])
    merger = Merger("test", None)
    merger.parts["test-uncatalogued-asset"] = parts
    merger.add("test-uncatalogued-asset", 20, 40, 7, 0)
    placed = next(iter(merger.groups.values()))["v"][0]
    assert placed[:, 2].min() == 7
    assert placed[:, 0].min() == 18
    assert placed[:, 0].max() == 22


def test_city_collision_preserves_a_concave_surface(tmp_path) -> None:
    from types import SimpleNamespace
    from scripts.realworld.build_scene import add_city_model
    (tmp_path / "meshes").mkdir()
    ring = [[0, 0, 10], [4, 0, 10], [4, 1, 10], [1, 1, 10], [1, 4, 10], [0, 4, 10], [0, 0, 10]]
    rows = [{"surfaces": [{"vertices": ring}]}]
    add_city_model(rows, tmp_path, SimpleNamespace(elev0=0), 100)
    vertices, faces = [], []
    for line in (tmp_path / "meshes/city_buildings_collision.obj").read_text().splitlines():
        bits = line.split()
        if bits[0] == "v":
            vertices.append(list(map(float, bits[1:])))
        elif bits[0] == "f":
            faces.append([int(x.split("/")[0]) - 1 for x in bits[1:]])
    pts = np.array(vertices)[faces]
    area = np.linalg.norm(np.cross(pts[:, 1] - pts[:, 0], pts[:, 2] - pts[:, 0]), axis=1).sum() / 2
    assert area == pytest.approx(7)
    assert not np.any((pts.mean(1)[:, 0] > 1) & (pts.mean(1)[:, 1] > 1))


def test_private_visual_copy_repairs_texture_paths_without_changing_source(monkeypatch, tmp_path) -> None:
    from scripts.realworld import make_world
    source = tmp_path / "car"
    (source / "meshes").mkdir(parents=True)
    (source / "materials/textures").mkdir(parents=True)
    Image = pytest.importorskip("PIL.Image")
    Image.new("RGB", (2, 2)).save(source / "materials/textures/body.png")
    material = source / "meshes/body.mtl"
    material.write_text("newmtl body\nmap_Kd body.png\n")
    (source / "meshes/body.dae").write_text('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema"><library_images><image><init_from>body.png</init_from></image></library_images></COLLADA>')
    (source / "model.sdf").write_text("<sdf><model name='car'><link name='l'><visual name='v'><geometry><mesh><uri>meshes/body.obj</uri></mesh></geometry></visual></link></model></sdf>")
    monkeypatch.setattr(make_world, "RESOLVED", tmp_path)
    visuals, _ = make_world.visuals_of("car")
    assert material.read_text() == "newmtl body\nmap_Kd body.png\n"
    assert (tmp_path / "rw_prop_car/meshes/body.mtl").read_text() == "newmtl body\nmap_Kd ../materials/textures/body.png\n"
    assert visuals[0].findtext("geometry/mesh/uri") == "model://rw_prop_car/meshes/body.obj"
    dae = (tmp_path / "rw_prop_car/meshes/body.dae").read_text()
    assert "<COLLADA xmlns=" in dae
    assert "../materials/textures/body.png" in dae
