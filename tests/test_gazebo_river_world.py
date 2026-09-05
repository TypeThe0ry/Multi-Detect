from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORLD_PATH = ROOT / "simulation" / "gazebo" / "worlds" / "river_rescue_vtol_001.sdf"


def _load_world() -> tuple[ET.Element, ET.Element]:
    root = ET.parse(WORLD_PATH).getroot()
    world = root.find("world")
    assert world is not None
    return root, world


def _float_vector(text: str | None, *, length: int) -> tuple[float, ...]:
    assert text is not None
    values = tuple(float(value) for value in text.split())
    assert len(values) == length
    assert all(math.isfinite(value) for value in values)
    return values


def test_river_world_is_repository_owned_sdf_with_default_topic_namespace() -> None:
    assert WORLD_PATH.is_file()
    root, world = _load_world()

    assert root.tag == "sdf"
    assert root.attrib["version"] == "1.9"
    assert [item.attrib.get("name") for item in root.findall("world")] == ["default"]
    assert world.attrib["name"] == "default"
    assert world.find("gui") is None

    serialized = WORLD_PATH.read_text(encoding="utf-8").lower()
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "fuel://" not in serialized
    assert "model://" not in serialized
    assert not world.findall(".//include")


def test_river_world_has_px4_rate_physics_sun_and_navsat_reference() -> None:
    _, world = _load_world()
    physics = world.find("physics")
    assert physics is not None
    assert physics.attrib["type"] == "ode"
    assert float(physics.findtext("max_step_size", "nan")) == 0.004
    assert float(physics.findtext("real_time_factor", "nan")) == 1.0
    assert float(physics.findtext("real_time_update_rate", "nan")) == 250.0
    assert _float_vector(world.findtext("gravity"), length=3) == (0.0, 0.0, -9.80665)
    assert _float_vector(world.findtext("wind/linear_velocity"), length=3) == (3.0, -1.5, 0.0)

    sun = world.find("light[@name='sun']")
    assert sun is not None
    assert sun.attrib["type"] == "directional"
    assert sun.findtext("cast_shadows") == "true"
    assert float(sun.findtext("intensity", "0")) > 0.0

    spherical = world.find("spherical_coordinates")
    assert spherical is not None
    assert spherical.findtext("surface_model") == "EARTH_WGS84"
    assert spherical.findtext("world_frame_orientation") == "ENU"
    latitude = float(spherical.findtext("latitude_deg", "nan"))
    longitude = float(spherical.findtext("longitude_deg", "nan"))
    assert -90.0 <= latitude <= 90.0
    assert -180.0 <= longitude <= 180.0


def test_river_world_has_collision_ground_dry_home_and_visible_scene_primitives() -> None:
    _, world = _load_world()
    models = {model.attrib["name"]: model for model in world.findall("model")}
    assert {
        "ground_plane",
        "river_surface",
        "river_banks",
        "vtol_home_pad",
        "rescue_marker_red",
        "shore_marker_alpha",
        "shore_marker_bravo",
    }.issubset(models)

    ground = models["ground_plane"]
    assert ground.findtext("static") == "true"
    assert ground.find(".//collision/geometry/plane") is not None
    assert ground.find(".//visual/geometry/plane") is not None

    home = models["vtol_home_pad"]
    assert home.findtext("static") == "true"
    assert home.find(".//collision/geometry/cylinder") is not None
    assert len(home.findall(".//visual")) >= 4

    river = models["river_surface"]
    river_links = river.findall("link")
    assert len(river_links) == 3
    assert all(link.find("visual/geometry/box") is not None for link in river_links)
    for link in river_links:
        pose = _float_vector(link.findtext("pose"), length=6)
        size = _float_vector(link.findtext("visual/geometry/box/size"), length=3)
        assert size[0] >= 100.0
        assert size[1] >= 20.0
        # Each river centre stays away from the origin / default VTOL pad.
        assert math.hypot(pose[0], pose[1]) > 40.0

    marker = models["rescue_marker_red"]
    assert marker.findtext("static") == "true"
    assert marker.find(".//visual/geometry/cylinder") is not None
    assert marker.find(".//visual/geometry/sphere") is not None
    assert marker.find(".//visual/geometry/box") is not None
    assert len(models["river_banks"].findall("link")) == 6


def test_runtime_world_contains_no_evaluator_channel_or_answer_metadata() -> None:
    serialized = WORLD_PATH.read_text(encoding="utf-8").lower()
    forbidden = (
        "target_id",
        "life-jacket-001",
        "red_life_jacket",
        "ground_truth",
        "expected_answer",
        "allowed_terminal_states",
        "false_geolocations",
    )
    for token in forbidden:
        assert token not in serialized
