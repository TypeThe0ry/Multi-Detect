import json
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from scripts.realworld import x1_site_dem as site


def test_bilinear_interpolation_matches_runtime_cross_term_and_upper_edge():
    grid = np.array([[4, 10], [8, 20]], dtype=np.float32)
    assert site.bilinear(grid, 0.25, 0.5) == pytest.approx(8.75)
    assert site.bilinear(grid, 1, 0.5) == pytest.approx(14)
    assert site.bilinear(grid, 1, 1) == pytest.approx(20)


@pytest.mark.parametrize(("row", "col"), [(-0.001, 0), (0, 1.001), (np.nan, 0), (0, np.inf)])
def test_bilinear_rejects_out_of_coverage_or_nonfinite_coordinates(row, col):
    with pytest.raises(ValueError, match="outside grid coverage"):
        site.bilinear(np.zeros((2, 2)), row, col)


def test_residual_uses_subgrid_nodes_and_inclusive_north_east_box():
    raw = np.full((3, 3), 100.0)
    raw[1, 2] = 103
    raw[2, 2] = 98
    coarse = np.full((2, 2), 100.0)
    result = site.lattice_stats(raw, coarse, np.array([-1, 0, 1]), 2, (0, 1, 0, 1))
    assert result == {
        "count": 4, "box_ne_m": [0, 1, 0, 1], "min_m": -3, "max_m": 2, "mean_m": -0.25,
        "rmse_m": pytest.approx(np.sqrt(13 / 4)), "max_abs_m": 3, "max_abs_at_ne_m": [0, 1],
    }


def test_residual_rejects_incomplete_rectangles():
    with pytest.raises(ValueError, match="outside the raw DEM"):
        site.lattice_stats(np.zeros((3, 3)), np.zeros((2, 2)), np.array([-1, 0, 1]), 2, (0, 1, 0, 2))


def test_pad_uses_breakpoints_and_conservative_closed_collision_cells():
    axis = np.arange(-2, 3)
    raw = np.full((5, 5), 100.0)
    raw[2, 2] = 104  # Interior surface extremum.
    ground = np.zeros((5, 5))
    ground[0, 0] = 0.6  # In a cell touching the closed neighborhood at a corner.
    buildings = np.full((4, 4), -np.inf)
    buildings[-1, 0] = 8
    result = site.pad_neighborhood(raw, axis, raw[::2, ::2], axis[::2], ground, axis, buildings, 1)
    assert result["raw_bilinear_export_m"] == {"min_m": 100, "max_m": 104, "spread_m": 4}
    assert result["runtime_bilinear_msl_m"] == {"min_m": 101, "max_m": 104, "spread_m": 3}
    assert result["ground_mesh_full_cell_vertices_enu_up_m"]["max_m"] == 0.6
    assert result["ground_mesh_clipped_square_enu_up_m"]["max_m"] == 0
    assert result["building_aabb_cells_examined"] == 16
    assert result["building_aabb_cells_with_hits"] == 1
    assert result["building_aabb_top_enu_up_m"] == 8


def test_triangle_extrema_include_diagonal_intersections_with_clipped_boundary():
    axis = np.array([-3.0, 0.0, 3.0])
    ground = np.zeros((3, 3))
    ground[-1, -1] = 9
    ground[-2, -1] = -9
    ground[-1, -2] = -9
    # In the NE cell the point (1, 1) sits on the production diagonal.
    assert site.clipped_ground_range(ground, axis, np.array([0, 1]), 1) == {
        "min_m": -3, "max_m": 3, "spread_m": 6,
    }


def test_pin_mismatch_stops_cli_before_publishing(tmp_path, monkeypatch, capsys):
    original = tmp_path / "data"
    original.write_bytes(b"original")
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({"schema_version": 1, "inputs": {
        key: {"path": str(original), "sha256": site.sha256(original)} for key in site.ROLES
    }}))
    original.write_bytes(b"replacement")
    monkeypatch.setattr("sys.argv", ["x1_site_dem", "--inputs", str(manifest)])
    with pytest.raises(SystemExit) as exc:
        site.main()
    streams = capsys.readouterr()
    assert exc.value.code == 2
    assert streams.out == ""
    assert "digest mismatch" in streams.err


def test_incomplete_manifest_rejected():
    with pytest.raises(ValueError, match="complete schema-v1"):
        site.verified_inputs({"schema_version": 1, "inputs": {}})


@pytest.mark.parametrize("unsupported", ["heightmap", "include", "plugin"])
def test_alternate_world_representation_rejected(tmp_path, unsupported):
    world = tmp_path / "world.sdf"
    root = ET.fromstring("<sdf><world/></sdf>")
    ET.SubElement(root.find("world"), unsupported)
    ET.ElementTree(root).write(world)
    with pytest.raises(ValueError, match="flattened Iowa mesh world"):
        site.world_geometry({"world": world}, {}, None)


def test_world_vertical_origin_mismatch_rejected_before_collision_files(tmp_path):
    world = tmp_path / "world.sdf"
    world.write_text("""<sdf><world><spherical_coordinates>
        <surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation>
        <latitude_deg>42.015</latitude_deg><longitude_deg>-93.71</longitude_deg><elevation>318.23</elevation>
        </spherical_coordinates></world></sdf>""")
    dem = SimpleNamespace(origin_latitude_deg=42.015, origin_longitude_deg=-93.71, origin_elevation_m=319.23)
    with pytest.raises(ValueError, match="origins disagree"):
        site.world_geometry({"world": world}, {"lat": 42.015, "lon": -93.71, "elevation": 318.23}, dem)
