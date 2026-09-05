from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "simulation/gazebo/models/rc_cessna_mono_cam"
MODEL_SDF = MODEL_DIR / "model.sdf"
MODEL_CONFIG = MODEL_DIR / "model.config"
WORLD = ROOT / "simulation/gazebo/worlds/coastal_patrol_fixed_wing_001.sdf"
SCENARIO = ROOT / "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json"


def _vector(text: str | None, length: int) -> tuple[float, ...]:
    assert text is not None
    values = tuple(float(value) for value in text.split())
    assert len(values) == length
    assert all(math.isfinite(value) for value in values)
    return values


def test_fixed_wing_model_is_repository_owned_px4_composition() -> None:
    assert MODEL_SDF.is_file()
    assert MODEL_CONFIG.is_file()

    config = ET.parse(MODEL_CONFIG).getroot()
    assert config.findtext("name") == "rc_cessna_mono_cam"
    assert config.findtext("sdf") == "model.sdf"
    config_sdf = config.find("sdf")
    assert config_sdf is not None
    assert config_sdf.attrib.get("version") == "1.9"

    root = ET.parse(MODEL_SDF).getroot()
    assert root.tag == "sdf"
    assert root.attrib.get("version") == "1.9"
    model = root.find("model")
    assert model is not None
    assert model.attrib == {"name": "rc_cessna_mono_cam"}

    includes = {
        include.findtext("uri")
        for include in model.findall("include")
        if include.findtext("uri") is not None
    }
    assert includes == {"model://rc_cessna", "model://mono_cam"}
    camera_include = next(
        include
        for include in model.findall("include")
        if include.findtext("uri") == "model://mono_cam"
    )
    camera_pose = camera_include.find("pose")
    assert camera_pose is not None
    assert camera_pose.attrib == {"relative_to": "base_link"}
    assert _vector(camera_pose.text, 6) == (0.48, 0.0, -0.10, 0.0, 0.0, 0.0)

    joint = model.find("joint[@name='CameraJoint']")
    assert joint is not None
    assert joint.attrib.get("type") == "fixed"
    assert joint.findtext("parent") == "base_link"
    assert joint.findtext("child") == "camera_link"
    assert _vector(joint.findtext("pose"), 6) == (0.48, 0.0, -0.10, 0.0, 0.0, 0.0)

    # The composition may resolve only the two pinned PX4 model dependencies;
    # it must not pull arbitrary network resources or evaluator channels.
    serialized = MODEL_SDF.read_text(encoding="utf-8").lower()
    assert "http://" not in serialized
    assert "https://" not in serialized
    for token in ("ground_truth", "expected_answer", "false_geolocations"):
        assert token not in serialized


def test_fixed_wing_world_has_runway_wgs84_wind_and_local_primitives() -> None:
    assert WORLD.is_file()
    root = ET.parse(WORLD).getroot()
    assert root.tag == "sdf"
    assert root.attrib.get("version") == "1.9"
    world = root.find("world")
    assert world is not None
    assert world.attrib.get("name") == "default"
    assert world.find("gui") is None
    assert not world.findall(".//include")

    physics = world.find("physics")
    assert physics is not None
    assert physics.attrib.get("type") == "ode"
    assert float(physics.findtext("max_step_size", "nan")) == 0.004
    assert float(physics.findtext("real_time_factor", "nan")) == 1.0
    assert float(physics.findtext("real_time_update_rate", "nan")) == 250.0
    assert _vector(world.findtext("gravity"), 3) == (0.0, 0.0, -9.80665)
    assert _vector(world.findtext("wind/linear_velocity"), 3) == (2.0, -0.8, 0.0)

    spherical = world.find("spherical_coordinates")
    assert spherical is not None
    assert spherical.findtext("surface_model") == "EARTH_WGS84"
    assert spherical.findtext("world_frame_orientation") == "ENU"
    latitude = float(spherical.findtext("latitude_deg", "nan"))
    longitude = float(spherical.findtext("longitude_deg", "nan"))
    assert -90.0 <= latitude <= 90.0
    assert -180.0 <= longitude <= 180.0

    models = {item.attrib["name"]: item for item in world.findall("model")}
    assert {
        "ground_plane",
        "fixed_wing_runway",
        "fixed_wing_apron",
        "fixed_wing_home_pad",
        "approach_beacon_west",
        "approach_beacon_east",
        "coastal_hangar",
        "coastal_water_strip",
        "rescue_buoy_orange",
    }.issubset(models)
    runway = models["fixed_wing_runway"]
    assert runway.findtext("static") == "true"
    runway_size = _vector(
        runway.findtext("link/visual[@name='runway_visual']/geometry/box/size"), 3
    )
    assert runway_size == (220.0, 24.0, 0.06)
    assert runway.find("link/collision/geometry/box") is not None
    home = models["fixed_wing_home_pad"]
    assert home.findtext("static") == "true"
    assert home.find("link/collision/geometry/cylinder") is not None
    buoy = models["rescue_buoy_orange"]
    assert buoy.findtext("static") == "true"
    buoy_pose = _vector(buoy.findtext("pose"), 6)
    assert math.isclose(buoy_pose[0], 249.0)
    assert math.isclose(buoy_pose[1], 180.0)
    assert buoy.find("link/visual[@name='buoy_body']/geometry/cylinder") is not None

    serialized = WORLD.read_text(encoding="utf-8").lower()
    for token in (
        "http://",
        "https://",
        "fuel://",
        "model://",
        "target_id",
        "ground_truth",
        "expected_answer",
        "false_geolocations",
    ):
        assert token not in serialized


def test_fixed_wing_scenario_channels_are_hash_bound_and_truth_separated() -> None:
    manifest = load_scenario_manifest(SCENARIO)
    runtime = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)

    assert manifest.scenario_id == "coastal_patrol_fixed_wing_001"
    assert runtime.scenario_manifest_hash == manifest.canonical_hash
    assert runtime.vehicle.model == "px4_fixed_wing_standard"
    assert runtime.vehicle.airframe_profile_id == "px4_fixed_wing_gazebo_v1"
    assert runtime.world.world_asset == "builtin://gazebo/coastal_patrol_fixed_wing_001"
    assert tuple(fault.kind.value for fault in runtime.faults) == (
        "gps_drift",
        "airspeed_freeze",
        "wind_gust",
        "camera_dropout",
    )
    assert tuple(fault.at_s for fault in runtime.faults) == (45.0, 90.0, 120.0, 180.0)
    assert manifest.truth.targets[0].target_id == "orange-marker-001"
    assert manifest.truth.targets[0].label == "orange_rescue_marker"
    assert manifest.expectations.maximum_false_geolocations == 0

    channel_dir = SCENARIO.parent
    raw_manifest = json.loads(SCENARIO.read_text(encoding="utf-8"))
    for _channel_name, channel in raw_manifest["channels"].items():
        raw_channel = json.loads((channel_dir / channel["path"]).read_text(encoding="utf-8"))
        assert canonical_sha256(raw_channel) == channel["sha256"]

    runtime_text = json.dumps(runtime.to_channel_mapping(), ensure_ascii=False).lower()
    for token in (
        "target_id",
        "orange-marker-001",
        "orange_rescue_marker",
        "ground_truth",
        "expected_answer",
    ):
        assert token not in runtime_text

    # A scenario manifest is a SIM/REPLAY input only; REAL is fail-closed.
    try:
        load_runtime_scenario(SCENARIO, mode=RuntimeMode.REAL)
    except Exception as exc:  # keep assertion independent from concrete contract subclass
        assert "REAL" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("REAL mode unexpectedly loaded a fixed-wing scenario")
