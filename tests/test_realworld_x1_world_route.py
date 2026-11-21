from __future__ import annotations

import math
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.realworld import long_range_flight as flight
from scripts.realworld import wide_collect_batch as batch
from scripts.realworld import x1_world_route as route


WORLD_ID = "rw_fixture_x1_w6km__clear_noon"
WORLD = '''<sdf version="1.9"><world name="default"><spherical_coordinates>
<surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation>
<latitude_deg>42.015</latitude_deg><longitude_deg>-93.71</longitude_deg><elevation>300.0</elevation>
</spherical_coordinates></world></sdf>'''


@pytest.fixture
def flat_layers():
    shape = (route.COUNT, route.COUNT)
    return {"ground_low": np.zeros(shape), "ground_high": np.zeros(shape),
            "buildings_and_boxes": np.full(shape, -np.inf), "canopy": np.full(shape, -np.inf),
            "water_surface": np.full(shape, -np.inf), "water": np.zeros(shape, dtype=bool)}


@pytest.fixture
def asset_world(tmp_path, flat_layers, monkeypatch):
    asset = tmp_path / "v1"
    asset.mkdir()
    (asset / "models").mkdir()
    dem = asset / "dem.npy"
    np.save(dem, np.full((101, 101), 300.0))
    world = asset / f"{WORLD_ID}.sdf"
    world.write_text(WORLD)
    scene = {"lat": 42.015, "lon": -93.71, "elevation": 300.0}
    inputs = {dem.name: route.file_sha256(dem)}
    monkeypatch.setattr(route, "load_layers", lambda *_: (scene, dem, flat_layers, {}, inputs))
    return asset, world


def test_pad_uses_physical_ground_but_still_rejects_collisions_and_water(flat_layers):
    ground = np.zeros((route.COUNT + 1, route.COUNT + 1))
    collision = flat_layers["buildings_and_boxes"]
    trees = flat_layers["canopy"]
    water = flat_layers["water"]
    home = route.COUNT // 2
    route.validate_pad(ground, collision, trees, water)
    ground[home, home] = 0.5
    with pytest.raises(route.PadRejected, match="terrain"):
        route.validate_pad(ground, collision, trees, water)
    ground[home, home] = 0
    collision[home, home] = 1
    with pytest.raises(route.PadRejected, match="obstructed"):
        route.validate_pad(ground, collision, trees, water)
    collision[home, home] = -np.inf
    water[home, home + 4] = True
    with pytest.raises(route.PadRejected, match="water at home"):
        route.validate_pad(ground, collision, trees, water)


def test_route_cli_classifies_only_pad_errors(monkeypatch, tmp_path):
    argv = ["--asset", str(tmp_path), "--world", str(tmp_path / "world.sdf")]
    for error, code in [(route.PadRejected("wet pad"), route.PAD_REJECTED), (ValueError("no route"), 1)]:
        def fail(*_):
            raise error

        monkeypatch.setattr(route, "preflight", fail)
        with pytest.raises(SystemExit) as exc:
            route.main(argv)
        assert exc.value.code == code


def test_corridor_checks_capture_range_edge_water_and_obstacles(flat_layers):
    points = route.corner_route(route.QUADRANTS["NW"], 1)
    passing = route.evaluate(points, flat_layers)
    assert passing["passed"]
    assert passing["max_departure_witness_m"] > 4005
    assert passing["minimum_boundary_margin_m"] >= 20
    assert passing["maximum_modeled_capture_offset_m"] < 15
    assert passing["agl_m"] == [85, 85]

    middle = route.COUNT // 2
    flat_layers["water"][middle, middle] = True
    flat_layers["buildings_and_boxes"][middle, middle] = 145
    blocked = route.evaluate(points, flat_layers)
    assert not blocked["passed"]
    assert {"clearance", "water at home"} <= set(blocked["failures"])


def test_departure_certificate_can_rule_out_every_far_endpoint(flat_layers):
    axis = (np.arange(route.COUNT) + 0.5) * route.CELL - route.HALF
    far = np.hypot(axis[:, None], axis) > 3600
    flat_layers["ground_high"][far] = 320
    certificate = route.departure_certificate(flat_layers)
    assert 0 < certificate["maximum_possible_distance_m"]["30"] < 3600
    assert certificate["best_possible_minimum_agl_at_gate_m"] == -170
    assert certificate["best_possible_minimum_clearance_at_gate_m"] == -170


def test_departure_certificate_does_not_require_a_specific_route(flat_layers):
    assert route.departure_certificate(flat_layers)["maximum_possible_distance_m"]["40"] >= route.DEPARTURE


def test_terrain_altitudes_propagate_asymmetric_rates_and_tie_hover_return():
    lower = np.array([40., 40., 95., 40., 40.])
    upper = np.array([47., 80., 95., 80., 80.])
    heights = route.terrain_altitudes(lower, upper, np.full(4, 180.), (2.5, 2.0))
    assert heights.tolist() == [45, 70, 95, 75, 75]


def test_valley_follows_terrain_and_rejects_a_rate_violating_profile(flat_layers):
    axis = (np.arange(route.COUNT) + 0.5) * route.CELL - route.HALF
    flat_layers["ground_low"][:] = -axis / 50 - 0.1
    flat_layers["ground_high"][:] = -axis / 50 + 0.1
    centreline = np.array([(0, 0), (-440, 175), (-1000, 490), (-1670, 860)], dtype=float)
    points = route.valley_route(centreline, 140, 1)
    metrics = route.evaluate(points, flat_layers, terrain_rates=(2.5, 2.0))
    assert metrics["failures"] == ["departure"]
    heights = np.array(metrics["waypoints_enu_m"])[:, 2]
    assert np.ptp(heights) > 25
    assert heights[-1] == heights[-2]
    assert 0 < metrics["maximum_commanded_climb_mps"] <= 2.5
    assert 0 < metrics["maximum_commanded_descent_mps"] <= 2.0
    heights[4] += 30
    rejected = route.evaluate(points, flat_layers, heights, terrain_rates=(2.5, 2.0))
    assert "vertical rate" in rejected["failures"]


def test_diagnostic_lengths_are_local_not_full_route(flat_layers):
    flat_layers["buildings_and_boxes"][route.COUNT // 2, route.COUNT // 2] = 145
    points = route.corner_route(route.QUADRANTS["NW"], 1)
    report = route.evaluate(points, flat_layers, np.full(len(points), 85), diagnostics=True)
    assert report["minimum_vertical_clearance_m"] == -60
    assert set(report["violations_by_turn_radius_m"]) == {"0", "80", "100", "120"}
    for model in report["violations_by_turn_radius_m"].values():
        assert model["agl_below_m"] == model["agl_above_m"] == 0
        assert 100 < model["clearance_below_m"] < 300


def test_alpine_certificate_fails_closed_without_publishing(asset_world, flat_layers, monkeypatch):
    asset, world = asset_world
    load = route.load_layers

    def alpine(*args):
        scene, *rest = load(*args)
        return {**scene, "site": route.ALPINE_SITE, "lat": route.ALPINE_ORIGIN[0], "lon": route.ALPINE_ORIGIN[1]}, *rest

    monkeypatch.setattr(route, "load_layers", alpine)
    flat_layers["ground_high"][:] = 200
    with pytest.raises(ValueError, match=r"bound departure to 0.0 m"):
        route.preflight(asset, world)
    assert not (asset / "x1").exists()


@pytest.mark.parametrize("failure", ["geometry", "cpu"])
def test_failed_preflight_leaves_no_receipt(asset_world, monkeypatch, failure):
    asset, world = asset_world
    monkeypatch.setattr(route, "QUADRANTS", {"NW": (-1, 1)})
    invoked = []

    def cpu(*args):
        invoked.append(args)
        raise ValueError("mock CPU rejection")

    monkeypatch.setattr(route, "cpu_plan", cpu)
    if failure == "geometry":
        monkeypatch.setattr(route, "evaluate", lambda *_: {"passed": False, "failures": ["terrain rejection"]})
    with pytest.raises(ValueError, match="no route passed"):
        route.preflight(asset, world)
    assert bool(invoked) == (failure == "cpu")
    assert not (asset / "x1").exists()


def test_publication_binds_real_cpu_plan_and_batch(asset_world, tmp_path, monkeypatch):
    asset, world = asset_world
    monkeypatch.setattr(route, "RUNNER_PYTHON", Path(sys.executable))
    cpu_plan = route.cpu_plan
    plans = []

    def check_stage(scenario, staged_world, models):
        assert staged_world.is_relative_to(route.ROOT / ".tmp/x1-worlds")
        assert models == asset / "models"
        plans.append(cpu_plan(scenario, staged_world, models))

    monkeypatch.setattr(route, "cpu_plan", check_stage)
    receipt_path = route.preflight(asset, world)
    receipt = route.read_json(receipt_path)
    scenario = receipt_path.parent / receipt["scenario"]
    sidecar = flight.sidecar_path(scenario)
    files = {"scenario": scenario, "sidecar": sidecar, "world": world,
             "map": scenario.parent / f"{WORLD_ID}.geojson", "dem": scenario.parent / f"{WORLD_ID}.dem.json"}
    assert receipt == {"scenario": scenario.name, "passed": True,
                       **{f"{key}_sha256": route.file_sha256(path) for key, path in files.items()}}
    plan, = plans
    assert plan["fw_guidance"] == "offboard_path" and plan["flight_started"] is False
    assert plan["collector_plan"]["docker_command"] is None
    assert len(plan["fw_waypoints_home_ned_m"]) == 16
    assert all(math.isclose(point[2], -85) for point in plan["fw_waypoints_home_ned_m"])

    spec = {"batch_id": "fixture", "output_root": str(tmp_path / "batch"), "airframe": route.AIRFRAME,
            "gpu": 3, "slots": [5], "resolved_root": str(asset / "models"),
            "variant_world_root": str(world.parent), "variant_map_root": str(scenario.parent),
            "variant_terrain_root": str(scenario.parent),
            "jobs": [{"site": "rw_fixture_x1", "scenario": str(scenario), "seed": 42,
                      "time_of_day": "noon", "weather": "clear", "altitude_m": 91}]}
    job, = batch.expand(spec)["jobs"]
    staged, _ = batch.prepare(job, tmp_path / "snapshot")
    assert flight.sidecar_path(staged).read_bytes() == sidecar.read_bytes()
    assert route.read_json(staged.parent / f"{WORLD_ID}.runtime_input.json")["seed"] == 42

    with tempfile.TemporaryDirectory(dir=route.ROOT / ".tmp/x1-worlds") as temporary:
        stage = Path(temporary)
        shutil.copytree(scenario.parent, stage, dirs_exist_ok=True)
        staged_world = stage / world.name
        shutil.copyfile(world, staged_world)
        staged_map = stage / files["map"].name
        staged_map.write_text(staged_map.read_text() + "\n")
        with pytest.raises(ValueError, match="preflight map SHA-256 mismatch"):
            cpu_plan(stage / scenario.name, staged_world, asset / "models")

    sidecar_original = sidecar.read_bytes()
    altered = route.read_json(sidecar)
    del altered["route_preflight"]
    route.write_json(sidecar, altered)
    with pytest.raises(ValueError, match="staged asset identity mismatch"):
        batch.prepare(job, tmp_path / "downgrade")
    sidecar.write_bytes(sidecar_original)

    world.write_text(WORLD + "\n")
    with pytest.raises(ValueError, match="staged asset identity mismatch"):
        batch.prepare(job, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_unsafe_world_structure_is_rejected(tmp_path):
    world = tmp_path / f"{WORLD_ID}.sdf"
    world.write_text(WORLD.replace("</world>", "<include><uri>model://unknown</uri></include></world>"))
    with pytest.raises(ValueError, match="flattened calm world"):
        route.load_layers(tmp_path, world)


def test_long_range_wait_uses_horizontal_capture_disk(monkeypatch):
    from scripts import collect_sim_teacher_episodes as collector
    from multidetect.sim_teacher.streams import MavlinkState

    mission = flight.load_mission(flight.DEFAULT_SCENARIO)
    now = 0.0
    telemetry = iter([(12, 7, 2), (128, 7, -70), (127, 7, -70)])

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(collector, "_home_reference", lambda _: (None, (12, 7, 2)))
    monkeypatch.setattr(collector, "_state_position", lambda _: next(telemetry))
    monkeypatch.setattr(collector, "_state_yaw", lambda _: 0)
    monkeypatch.setattr(collector, "_pose_sim_stamp_ns", lambda _: int(now * 1e9))
    monkeypatch.setattr(collector, "_flight_state_snapshot", lambda _: {"vtol_state": 4})
    monkeypatch.setattr(flight, "_terrain_agl", lambda *_: 72)
    state = MavlinkState(messages={"HEARTBEAT": {"custom_mode": 6 << 16}},
                         received_monotonic_ns={"HEARTBEAT": 0})
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    streamer = SimpleNamespace(raise_if_failed=lambda: None)
    command = flight.MotionCommand("reposition", target_ned=(100, 0, -85), mode="FW",
                                   fly_through_radius_m=15)
    result = flight.wait_for_leg(command, reader, collector=collector, mission=mission,
                                 cruise_mps=15, offboard_streamer=streamer, wall=lambda: now, sleep=sleep)
    assert result["waited"] == "arrived"
    assert result["distance_m"] == 15 and now == 0.1


def test_long_range_offboard_lifecycle_stops_and_restores_on_mode_exit(monkeypatch):
    from scripts import collect_sim_teacher_episodes as collector
    from multidetect.sim_teacher.streams import MavlinkState

    events = []
    state = MavlinkState()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)

    class Streamer:
        def __init__(self, vehicle):
            self.last_target = None

        def set_path_target(self, position, tangent):
            self.last_target = (position, tangent)
            events.append("target")

        def start(self):
            assert self.last_target is not None
            events.append("start")

        def stop(self):
            events.append("stop")

        def raise_if_failed(self):
            pass

    def enter(vehicle, mode_reader, streamer, *, after_ns):
        assert mode_reader is reader and events[-1] == "start"
        state.messages["HEARTBEAT"] = {"custom_mode": 6 << 16}
        state.received_monotonic_ns["HEARTBEAT"] = after_ns
        events.append("enter")

    mission = {**flight.load_mission(flight.DEFAULT_SCENARIO), "route_preflight": {"fly_through_radius_m": 15}}
    monkeypatch.setattr(collector, "SetpointStreamer", Streamer)
    monkeypatch.setattr(collector, "_wait_for_offboard", enter)
    monkeypatch.setattr(collector, "_flight_state_snapshot", lambda _: {"vtol_state": 4})
    original_executor = collector.FixedWingVtolExecutor
    original_wait = collector._wait_motion_command
    with pytest.raises(RuntimeError, match="left Offboard mode"):
        with flight.collector_overrides(collector, mission):
            executor = collector.FixedWingVtolExecutor(
                object(), airframe="vtol", telemetry_reader=reader,
                position=lambda: (112, -43, -82), geo_reference=lambda: (None, (12, 7, 3)),
            )
            policy = flight.LongRangePolicy([(200, -50, -85), (200, 0, -85)], 80, vtol=True,
                                            fly_through_radius_m=15)
            first = executor.execute(policy.next_action())
            assert first.status == "success"
            assert first.ack["offboard_path"] == {"target_ned_m": (212, -43, -82), "tangent_ne": (1, 0)}
            second = executor.execute(policy.next_action())
            assert second.ack["offboard_path"]["tangent_ne"] == (0, 1)
            assert events == ["target", "start", "enter", "target"]
            state.messages["HEARTBEAT"] = {"custom_mode": 4 << 16 | 3 << 24}
            executor.execute(flight.MotionCommand("transition", mode="MC"))
    assert events[-1] == "stop"
    assert collector.FixedWingVtolExecutor is original_executor
    assert collector._wait_motion_command is original_wait
