from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.runtime import RuntimeMode
from multidetect.simulation import VehicleSpawnPose, load_runtime_scenario
from multidetect.simulator.stack import (
    SimulatorStackPorts,
    StackPlan,
    StackPlanError,
    build_stack_plan,
    read_world_coordinates,
    resolve_simulator_assets,
    stack_plan_from_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "configs" / "scenarios"


@pytest.mark.parametrize(
    ("name", "vehicle"),
    [
        ("river_rescue_vtol_001", "vtol"),
        ("coastal_patrol_fixed_wing_001", "fixed-wing"),
        ("urban_inspection_multirotor_001", "multirotor"),
    ],
)
def test_stack_plan_resolves_repository_assets(name: str, vehicle: str) -> None:
    plan = stack_plan_from_scenario(SCENARIOS / f"{name}.scenario.json", repo_root=ROOT)
    assert isinstance(plan, StackPlan)
    assert plan.vehicle_class == vehicle
    assert plan.world_path.is_file() and plan.model_path.is_file()
    assert plan.map_path.is_file() and plan.dem_path.is_file()
    assert len(plan.assets_sha256) == 64
    document = plan.to_mapping()
    assert document["repository"]["truth_loaded"] is False
    # Flat aliases are intentionally kept alongside the nested schema so
    # launchers can consume the plan without duplicating field-resolution code.
    assert document["scenario_id"] == name
    assert document["scenario_manifest_hash"] == plan.scenario_manifest_hash
    assert document["vehicle_model"] == plan.vehicle_model
    assert document["world_path"] == document["world"]["path"]
    assert document["map_path"] == document["map"]["path"]
    assert document["dem_path"] == document["dem"]["path"]
    assert document["assets"]["map"]["role"] == "map_geojson"
    assert document["assets"]["dem"]["role"] == "terrain_dem"
    assert document["validation"]["map_validated"] is True
    assert document["validation"]["dem_validated"] is True
    assert document["assets_sha256"] == plan.assets_sha256
    assert document["px4_command"] == document["commands"]["px4"]
    assert document["gazebo_command"] == document["commands"]["gazebo"]


def test_stack_plan_hashes_are_stable_and_serializable() -> None:
    source = SCENARIOS / "river_rescue_vtol_001.scenario.json"
    one = stack_plan_from_scenario(
        source,
        repo_root=ROOT,
        tool_capabilities={"gz": False, "px4": False},
    )
    two = stack_plan_from_scenario(
        source,
        repo_root=ROOT,
        tool_capabilities={"gazebo": False, "px4": False},
    )
    assert one.evidence_hash == two.evidence_hash
    mapping = one.to_mapping_with_hash()
    assert mapping["sha256"] == one.evidence_hash
    assert mapping["runtime"]["dry_run"] is True
    assert mapping["ports"]["loopback_only"] is True


def test_map_and_dem_content_are_bound_into_aggregate_hash(tmp_path: Path) -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    simulation = tmp_path / "simulation"
    shutil.copytree(ROOT / "simulation", simulation)
    original = resolve_simulator_assets(runtime, repository_root=tmp_path)
    map_path = simulation / "maps" / "river_rescue_vtol_001.geojson"
    document = json.loads(map_path.read_text(encoding="utf-8"))
    document["features"][0]["properties"]["revision_probe"] = 1
    map_path.write_text(json.dumps(document), encoding="utf-8")

    changed = resolve_simulator_assets(runtime, repository_root=tmp_path)

    assert changed.world.sha256 == original.world.sha256
    assert changed.dem.sha256 == original.dem.sha256
    assert changed.map.sha256 != original.map.sha256
    assert changed.aggregate_sha256 != original.aggregate_sha256


def test_missing_repository_map_fails_closed(tmp_path: Path) -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    simulation = tmp_path / "simulation"
    shutil.copytree(ROOT / "simulation" / "gazebo", simulation / "gazebo")

    with pytest.raises(StackPlanError, match="maps asset is not a file"):
        resolve_simulator_assets(runtime, repository_root=tmp_path)


def test_dem_origin_must_match_world_origin(tmp_path: Path) -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    simulation = tmp_path / "simulation"
    shutil.copytree(ROOT / "simulation", simulation)
    dem_path = simulation / "terrain" / "river_rescue_vtol_001.dem.json"
    document = json.loads(dem_path.read_text(encoding="utf-8"))
    document["origin"]["elevation_m"] = 500.0
    document["elevation_m"] = [[value + 495.0 for value in row] for row in document["elevation_m"]]
    dem_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StackPlanError, match="origin does not match"):
        resolve_simulator_assets(runtime, repository_root=tmp_path)


def test_tool_capabilities_can_mark_plan_launchable() -> None:
    plan = stack_plan_from_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json",
        repo_root=ROOT,
        tool_capabilities={"gazebo": True, "px4": True},
    )
    assert plan.to_mapping()["runtime"]["dry_run"] is False
    assert plan.to_mapping()["tools"]["gazebo"]["available"] is True


def test_multirotor_stack_selects_x500_and_px4_autostart() -> None:
    plan = stack_plan_from_scenario(
        SCENARIOS / "urban_inspection_multirotor_001.scenario.json",
        repo_root=ROOT,
        tool_capabilities={"gazebo": False, "px4": False},
    )
    document = plan.to_mapping()
    assert document["vehicle"]["class"] == "multirotor"
    assert document["vehicle"]["model_name"] == "x500_mono_cam"
    assert document["environment"]["PX4_SYS_AUTOSTART"] == "4001"
    assert document["environment"]["PX4_SIM_MODEL"] == "gz_x500_mono_cam"


def test_hash_bound_spawn_pose_is_forwarded_to_px4_gazebo_environment() -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "urban_inspection_multirotor_001.scenario.json",
        mode=RuntimeMode.SIM,
    )
    positioned = replace(
        runtime,
        vehicle=replace(
            runtime.vehicle,
            spawn_pose=VehicleSpawnPose(
                x_east_m=1.25,
                y_north_m=-2.5,
                z_up_m=6.0,
                roll_rad=-0.0,
                pitch_rad=0.125,
                yaw_rad=1.5707963267948966,
            ),
        ),
    )
    plan = build_stack_plan(
        positioned,
        repository_root=ROOT,
        tool_capabilities={"gazebo": False, "px4": False},
    )

    assert plan.environment_mapping["PX4_GZ_MODEL_POSE"] == (
        "1.25,-2.5,6,0,0.125,1.5707963267948966"
    )
    assert "PX4_GZ_MODEL_POSE" not in build_stack_plan(
        runtime,
        repository_root=ROOT,
        tool_capabilities={"gazebo": False, "px4": False},
    ).environment_mapping


def test_px4_sitl_command_alias_is_accepted() -> None:
    plan = stack_plan_from_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json",
        repo_root=ROOT,
        tool_capabilities={"gazebo": False, "px4-sitl": True},
    )
    # The explicit capability alias is accepted even when the executable is
    # represented by the safe default command name in this host.
    assert plan.to_mapping()["tool_capabilities"]["px4"] is True


def test_custom_job_telemetry_port_rebinds_canonical_manifest_endpoint() -> None:
    ports = SimulatorStackPorts(
        px4_control_port=15640,
        px4_telemetry_port=15641,
        qgc_output_port=15642,
        qgc_input_port=15643,
        metadata_port=15644,
        rtsp_port=18554,
    )
    plan = build_stack_plan(
        SCENARIOS / "river_rescue_vtol_001.scenario.json",
        repository_root=ROOT,
        ports=ports,
        tool_capabilities={"gazebo": False, "px4": False},
    )
    mapping = plan.to_mapping()
    assert mapping["environment"]["PX4_MAVLINK_UDP_PORT"] == "15640"
    assert mapping["environment"]["PX4_MAVLINK_TELEMETRY_PORT"] == "15641"
    assert mapping["sensor_port_bindings"] == [
        {
            "sensor_id": "px4-telemetry",
            "manifest_port": 14541,
            "bound_port": 15641,
            "reason": "canonical_px4_telemetry_port_override",
        }
    ]


def test_mavlink_sensor_cannot_bind_an_unrelated_owned_port() -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    unsafe = replace(
        runtime,
        sensors=(replace(runtime.sensors[1], stream_uri="udp://127.0.0.1:14649"),),
    )
    with pytest.raises(StackPlanError, match="selected PX4 telemetry port"):
        build_stack_plan(unsafe, repository_root=ROOT)


def test_missing_tool_is_advisory_not_asset_failure() -> None:
    plan = stack_plan_from_scenario(
        SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json", repo_root=ROOT
    )
    assert plan.to_mapping()["runtime"]["dry_run"] is True
    assert {entry[0] for entry in plan.tool_capabilities} == {"gazebo", "px4"}


def test_rejects_unknown_tool_capability() -> None:
    with pytest.raises(StackPlanError, match="unknown field"):
        stack_plan_from_scenario(
            SCENARIOS / "river_rescue_vtol_001.scenario.json",
            repo_root=ROOT,
            tool_capabilities={"gazebo": False, "px4": False, "docker": True},
        )


def test_rejects_network_world_asset() -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    unsafe = replace(
        runtime, world=replace(runtime.world, world_asset="https://example.invalid/world.sdf")
    )
    with pytest.raises(StackPlanError):
        resolve_simulator_assets(unsafe, repository_root=ROOT)


def test_rejects_unknown_vehicle_model() -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    unsafe = replace(
        runtime,
        vehicle=replace(runtime.vehicle, model="rocket", airframe_profile_id="unknown"),
    )
    with pytest.raises(StackPlanError, match="unsupported SIM vehicle"):
        resolve_simulator_assets(unsafe, repository_root=ROOT)


def test_rejects_non_loopback_sensor_endpoint() -> None:
    runtime = load_runtime_scenario(
        SCENARIOS / "river_rescue_vtol_001.scenario.json", mode=RuntimeMode.SIM
    )
    unsafe = replace(
        runtime, sensors=(replace(runtime.sensors[0], stream_uri="udp://8.8.8.8:14540"),)
    )
    with pytest.raises(StackPlanError, match="loopback"):
        build_stack_plan(unsafe, repository_root=ROOT)


def test_rejects_path_escape_repository_root(tmp_path: Path) -> None:
    with pytest.raises(StackPlanError, match="repository root"):
        stack_plan_from_scenario(
            SCENARIOS / "river_rescue_vtol_001.scenario.json", repo_root=tmp_path
        )


def test_ports_are_distinct_and_protected_port_is_untouched() -> None:
    plan = stack_plan_from_scenario(
        SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json", repo_root=ROOT
    )
    ports = plan.to_mapping()["ports"]
    assert len(ports["all_ports"]) == len(set(ports["all_ports"]))
    assert 14550 not in ports["all_ports"]


def test_world_coordinate_reader_is_fail_closed_and_accepts_world_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sdf"
    assert read_world_coordinates(missing) is None

    malformed = tmp_path / "malformed.sdf"
    malformed.write_text("<sdf>", encoding="utf-8")
    assert read_world_coordinates(malformed) is None

    standalone = tmp_path / "standalone-world.sdf"
    standalone.write_text(
        """<world>
          <spherical_coordinates>
            <latitude_deg>47.397742</latitude_deg>
            <longitude_deg>8.545594</longitude_deg>
            <elevation>488.0</elevation>
          </spherical_coordinates>
        </world>""",
        encoding="utf-8",
    )
    assert read_world_coordinates(standalone) == pytest.approx((47.397742, 8.545594, 488.0))


@pytest.mark.parametrize("latitude", (-90.0, 90.0))
def test_world_coordinate_reader_rejects_exact_poles(tmp_path: Path, latitude: float) -> None:
    pole = tmp_path / f"pole-{latitude}.sdf"
    pole.write_text(
        f"""<world>
          <spherical_coordinates>
            <latitude_deg>{latitude}</latitude_deg>
            <longitude_deg>8.5</longitude_deg>
            <elevation>400.0</elevation>
          </spherical_coordinates>
        </world>""",
        encoding="utf-8",
    )
    assert read_world_coordinates(pole) is None
