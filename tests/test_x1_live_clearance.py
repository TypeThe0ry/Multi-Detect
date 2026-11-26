"""CPU replay of CollectorScenario against current (response-time) PX4 samples."""
import hashlib
import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.x1_contingency_replay import Fault
from multidetect.sim_teacher import x1_scenario
from multidetect.sim_teacher.streams import MavlinkState
from multidetect.sim_teacher.x1_scenario import SitlScenarioTransport
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions
from multidetect.target_geolocation import wgs84_forward_from_ned
from scripts import run_x1_autonomy_demo as demo
from test_x1_mission import _build, _plan, _raise_vertex, profile, terrain  # noqa: F401
from test_x1_planner import _fixture

MS = 1_000_000
SCOPE = "d" * 64
FAULT = Fault(0, 30_000, "link_loss", "link", {"gcs": "down"})


def _leg(phase, end, seconds):
    return dict(phase=phase, end=end, padding_ne_m=2, padding_alt_m=1, duration_s=seconds, peak_power_w=500)


def _document(terrain):
    doc = _fixture("Fly over the yard at 80 m AGL then return and land.", target=(400, 0)).document
    doc["places"]["poi:beacon"] = doc["places"].pop("poi:yard")
    doc["ref_labels"] = {}
    doc["budget"]["battery_capacity_wh"] = 74
    entry = dict(entry=[199, 211, -2, 2, 178, 182], max_entry_speed_mps=12,
                 legs=[_leg("mc", [0, 0, 180], 45), _leg("landing", [0, 0, 100], 50)],
                 dynamics=dict(max_bank_deg=20, max_climb_mps=2, max_descent_mps=2,
                               max_acceleration_mps2=1, max_angular_rate_dps=30),
                 airspeed=None, failure_revalidation_enforced=False)
    fw = {**entry, "max_entry_speed_mps": 30, "airspeed": [14, 18], "failure_revalidation_enforced": True,
          "legs": [_leg("fw", [400, 0, 180], 18), _leg("transition", [430, 0, 180], 9),
                   _leg("mc", [0, 0, 180], 75), _leg("landing", [0, 0, 100], 50)]}
    doc["x1_live_evidence"] = {
        "scope_sha256": SCOPE, "dem_sha256": terrain.source_sha256, "dem_error_m": 1,
        "obstacle_coverage": [-10, 500, -10, 10, 95, 190], "obstacles": [],
        "landing_pad": {"radius_m": 8, "elevation_error_m": 2},
        "battery": {"max_voltage_v": 17, "soc_error_pct": 1, "consumption_error_wh": 0.25},
        "suffixes": {"3": entry, "1": fw},
    }
    return doc


def exercise(terrain, profile, monkeypatch, *, case="complete", cpu_replay=True, fw=False,
             fault_kind="link_loss", package_override=None, assumed_sim=None, archived_battery=None):
    doc = _document(terrain)
    evidence = doc["x1_live_evidence"]
    step = 1 if fw else 3
    suffix = evidence["suffixes"][str(step)]
    package = load_mission_package({k: v for k, v in doc.items() if k != "x1_live_evidence"}, mode="sim")
    steps = _build(replace(package, plan=validate_mission_plan(_plan())), terrain, profile)
    if case == "missing_evidence":
        doc.pop("x1_live_evidence")
    elif case == "wrong_scope":
        evidence["scope_sha256"] = "f" * 64
    elif case == "wrong_dem":
        evidence["dem_sha256"] = "f" * 64
    elif case == "missing_dem_bound":
        evidence.pop("dem_error_m")
    elif case == "dem_uncertainty":
        evidence["dem_error_m"] = 100
        evidence["landing_pad"]["elevation_error_m"] = 101
    elif case == "terrain_ahead":
        terrain = _raise_vertex(terrain, north=100, east=0, elevation=190)
    elif case == "missing_coverage":
        evidence.pop("obstacle_coverage")
    elif case == "coverage_gap":
        evidence["obstacle_coverage"][0] = 10
    elif case == "obstacle":
        evidence["obstacles"].append([130, 140, -2, 2, 179, 181])
    elif case == "missing_capacity":
        doc["budget"].pop("battery_capacity_wh")
    elif case == "time_reserve":
        doc["budget"]["time_s"] = 145
    elif case == "too_fast_predicted":
        suffix["dynamics"]["max_bank_deg"] = 95
    elif case == "unbounded_failure":
        suffix["failure_revalidation_enforced"] = False
    elif case == "wrong_terminal":
        suffix["legs"][-1]["end"] = [4, 0, 100]
    package = package_override(package, doc) if package_override is not None else load_mission_package(doc, mode="sim")
    if archived_battery is not None:
        # Use the same envelope and clearance builder with portable route geometry.
        doc["x1_live_evidence"]["native"] = {"return": {"3": suffix}}
        package = load_mission_package(doc, mode="sim", allow_assumed_sim=True)
    choice = "return" if archived_battery is not None else "continue"
    now = [10_000]
    monkeypatch.setattr(x1_scenario.time, "monotonic_ns", lambda: now[0] * MS)
    monkeypatch.setattr(demo, "llama_decisions", lambda *_: recorded_decisions(({"action": choice, "params": {}},)))
    monkeypatch.setattr(demo, "load_airframe_profile", lambda _: profile)
    if cpu_replay:
        def replay(*args, **kwargs):
            package, profile, backend, _gimbal, emit = args
            return DecisionTick(package, profile, backend, None, emit, cpu_replay=True,
                                now_ms=lambda: now[0], **kwargs)

        monkeypatch.setattr(demo, "DecisionTick", replay)
    fault = {
        "link_loss": FAULT,
        "wind_step": Fault(0, 30_000, "wind_step", "wind", dict(north_mps=8, east_mps=-4, down_mps=0)),
        "battery_drop": Fault(0, 30_000, "battery_drop", "battery", {"pct": 70}),
    }[fault_kind]
    transport = SitlScenarioTransport(None, wind=("/world/fake/wind", (0, 0, 0)))
    monkeypatch.setattr(x1_scenario, "wind_baseline", lambda _world: transport.wind)
    monkeypatch.setattr(SitlScenarioTransport, "in_container", lambda *args, **kwargs: transport)
    receipts = []
    scenario = demo.make_scenario(SimpleNamespace(decision_port=9911, sentence=doc["task_text"]),
                                  {"scenario_faults": (fault,), "scenario_package": package,
                                   "terrain": terrain, "scenario_revision": 1, "assumed_sim": assumed_sim}, steps,
                                  {"compiled_mission_sha256": "a" * 64,
                                   "contingency_scenario": {"motion_scope_sha256": SCOPE}})
    reader = SimpleNamespace(state=MavlinkState())
    reader.latest_control_state = lambda: reader.state
    boot = [now[0]]
    component = 0 if archived_battery is not None else 1
    scenario.bind(None, None, None, reader, SimpleNamespace(target_system=1, target_component=component), receipts.append,
                  "live-replay", clock=lambda: boot[0])
    scenario.worker.timeline = SimpleNamespace(origin=9000)
    if fault_kind == "link_loss":
        transport.link_suppressed.set()
    scenario.last_heartbeat_ms = 8500
    scenario.markers = deque([{"fault_index": 0, "receive_monotonic_ns": 9100 * MS}])
    built = []
    original_proof = scenario.decision.build_proof

    def capture(decision, frame):
        proof = original_proof(decision, frame)
        built.append((decision, frame, proof))
        if case == "wrong_frame":
            return replace(proof, frame_id=proof.frame_id - 1)
        if case == "wrong_digest":
            return replace(proof, decision_digest="e" * 64)
        return proof

    scenario.decision.build_proof = capture

    def tick(now_ms, *, fault=None):
        now[0] = boot[0] = now_ms
        latitude, longitude = wgs84_forward_from_ned(latitude_deg=52, longitude_deg=5, north_offset_m=205, east_offset_m=0)
        rows = {
            "HEARTBEAT": {"system_status": 4, "base_mode": 128},
            "EXTENDED_SYS_STATE": {"vtol_state": 4 if fw else 3, "landed_state": 2},
            "LOCAL_POSITION_NED": {"time_boot_ms": now_ms, "x": 205, "y": 0, "z": -80,
                                   "vx": 20 if fw else 5, "vy": 0, "vz": 0},
            "GLOBAL_POSITION_INT": {"time_boot_ms": now_ms, "lat": round(latitude * 1e7),
                                    "lon": round(longitude * 1e7), "alt": 180_000, "relative_alt": 80_000},
            "GPS_RAW_INT": {"fix_type": 3},
            "VFR_HUD": {"airspeed": 16},
            "BATTERY_STATUS": {"id": 0, "charge_state": 1, "fault_bitmask": 0, "battery_remaining": 93,
                               "energy_consumed": 144, "current_consumed": -1},
        }
        if assumed_sim is not None:
            rows["BATTERY_STATUS"].update(energy_consumed=-1, current_consumed=0)
        if fault_kind == "wind_step":
            rows["WIND_COV"] = {"wind_x": 8, "wind_y": -4, "wind_z": 0}
        elif fault_kind == "battery_drop":
            rows["BATTERY_STATUS"]["battery_remaining"] = 71
        stale = {"stale_global": "GLOBAL_POSITION_INT", "stale_local": "LOCAL_POSITION_NED",
                 "stale_battery": "BATTERY_STATUS"}.get(fault)
        if fault == "missing_global":
            rows.pop("GLOBAL_POSITION_INT")
        elif fault == "missing_local":
            rows.pop("LOCAL_POSITION_NED")
        elif fault == "missing_battery":
            rows.pop("BATTERY_STATUS")
        elif fault == "wrong_peer":
            rows["GLOBAL_POSITION_INT"]["_source_system"] = 2
        elif fault == "invalid_local":
            rows["LOCAL_POSITION_NED"]["vx"] = float("nan")
        elif fault == "entry_speed":
            rows["LOCAL_POSITION_NED"]["vx"] = 90
        elif fault == "entry_altitude":
            rows["GLOBAL_POSITION_INT"]["alt"] = 99_000
        elif fault == "boot_skew":
            rows["GLOBAL_POSITION_INT"]["time_boot_ms"] -= 501
        elif fault == "missing_mission_clock":
            boot[0] = None
        elif fault == "regressed_boot":
            boot[0] = 9999
        elif fault == "regressed_energy":
            rows["BATTERY_STATUS"]["energy_consumed"] = 143
        elif fault == "unknown_energy":
            rows["BATTERY_STATUS"].update(energy_consumed=-1, current_consumed=0)
        elif fault == "valid_charge_counter":
            rows["BATTERY_STATUS"].update(energy_consumed=-1, current_consumed=200)
        elif fault == "battery_reserve":
            rows["BATTERY_STATUS"]["battery_remaining"] = 19
        elif fault == "battery_17":
            rows["BATTERY_STATUS"]["battery_remaining"] = 17
        elif fault == "battery_fault":
            rows["BATTERY_STATUS"]["fault_bitmask"] = 1
        elif fault == "px4_failsafe":
            rows["HEARTBEAT"]["system_status"] = 5
        elif fault == "fw_airspeed":
            rows["VFR_HUD"]["airspeed"] = 13
        reader.state = MavlinkState(vehicle_time_boot_ms=now_ms)
        for name, row in rows.items():
            reader.state.received_monotonic_ns[name] = (now_ms - (501 if name == stale else 0)) * MS
            reader.state.received_vehicle_ms[name] = now_ms - (501 if fault == "stale_battery_sim" else 0)
            reader.state.messages[name] = {"_source_system": 1, "_source_component": 1, **row}
        if archived_battery is not None:
            phase = archived_battery["request" if fault is None else "reply"]
            battery = phase["battery"].copy()
            if fault == "battery_20":
                battery["battery_remaining"] = 20
            if "BATTERY_STATUS" in rows:
                receipt_age = phase["tick_monotonic_ns"] - battery["receive_monotonic_ns"]
                if stale != "BATTERY_STATUS":
                    reader.state.received_monotonic_ns["BATTERY_STATUS"] -= receipt_age
                pose, wire = phase["pose_boot_ms"], phase["wire_boot_ms"]
                reader.state.vehicle_time_boot_ms = now_ms + wire - pose
                reader.state.received_vehicle_ms["BATTERY_STATUS"] = now_ms + phase["battery_at_wire_boot_ms"] - pose
                if fault == "stale_battery_sim":
                    reader.state.received_vehicle_ms["BATTERY_STATUS"] = reader.state.vehicle_time_boot_ms - 501
                elif fault == "future_battery_sim":
                    reader.state.received_vehicle_ms["BATTERY_STATUS"] = reader.state.vehicle_time_boot_ms + 1
                elif fault == "skewed_wire_clock":
                    reader.state.vehicle_time_boot_ms = now_ms + 501
                elif fault == "regressed_wire_clock":
                    reader.state.vehicle_time_boot_ms = now_ms - 20
                    reader.state.received_vehicle_ms["BATTERY_STATUS"] = now_ms - 501
                reader.state.messages["BATTERY_STATUS"] = battery
        scenario.tick(step, None)

    try:
        tick(10_000)
        scenario.decision.future.result(timeout=2)
        # The second sample is the only one eligible to authorize the reply.
        tick(10_100, fault=case)
    finally:
        scenario.decision.close()
    requests = [row for row in receipts if row["message_type"] == "scenario_decision_request"]
    results = [row for row in receipts if row["message_type"] == "scenario_decision_receipt"]
    assert len(requests) == len(results) == 1, receipts
    assert requests[0]["request"]["request_id"] == results[0]["request_id"]
    assert requests[0]["request"]["state"]["events"][-1]["type"] == fault_kind
    if archived_battery is not None:
        assert requests[0]["request"]["state"]["telemetry"]["battery"]["pct"] == 95
    return results[0], built, scenario.decision.replay_commands


@pytest.mark.parametrize(("cpu_replay", "fw", "case", "fault_kind"), [
    (False, False, "complete", "link_loss"), (True, False, "complete", "link_loss"),
    (True, True, "complete", "wind_step"), (True, False, "valid_charge_counter", "link_loss"),
    (True, False, "complete", "battery_drop"),
])
def test_continue_from_live_samples(terrain, profile, monkeypatch, cpu_replay, fw, case, fault_kind):  # noqa: F811
    receipt, [(decision, frame, proof)], commands = exercise(terrain, profile, monkeypatch, case=case,
                                                            cpu_replay=cpu_replay, fw=fw, fault_kind=fault_kind)
    assert (receipt["action"], receipt["status"], receipt["reason"]) == ("continue", "continued", "no_new_command")
    assert commands == ([] if cpu_replay else None)
    assert frame.frame_id == proof.frame_id == 10_100 * MS
    assert proof.decision_digest == decision.digest
    assert frame.position_ne_amsl == pytest.approx((205, 0, 180), abs=0.02)
    assert proof.terrain_margin_m > 0 and proof.obstacle_margin_m > 0
    assert proof.available_time_s == pytest.approx(989.9)
    assert proof.available_energy_wh == pytest.approx(51.8 if fault_kind == "battery_drop" else 68.08)


@pytest.mark.parametrize(("case", "reason"), [
    ("missing_evidence", "unverified_clearance"), ("wrong_scope", "unverified_clearance"),
    ("wrong_dem", "unverified_clearance"), ("missing_dem_bound", "unverified_clearance"),
    ("dem_uncertainty", "unverified_clearance"), ("terrain_ahead", "unverified_clearance"),
    ("missing_coverage", "unverified_clearance"), ("coverage_gap", "unverified_clearance"),
    ("obstacle", "unverified_clearance"), ("wrong_terminal", "unverified_clearance"),
    ("stale_global", "unverified_clearance"), ("missing_global", "unverified_clearance"),
    ("stale_local", "unverified_clearance"), ("missing_local", "flight_mode_or_position_unverified"),
    ("wrong_peer", "unverified_clearance"), ("invalid_local", "unverified_clearance"),
    ("entry_speed", "unverified_clearance"), ("entry_altitude", "unverified_clearance"),
    ("boot_skew", "unverified_clearance"), ("missing_mission_clock", "superseded_mission_or_event"),
    ("regressed_boot", "unverified_clearance"), ("regressed_energy", "energy_or_reserve_limit"),
    ("missing_capacity", "energy_or_reserve_limit"), ("stale_battery", "energy_or_reserve_limit"),
    ("missing_battery", "energy_or_reserve_limit"), ("unknown_energy", "energy_or_reserve_limit"),
    ("battery_reserve", "energy_or_reserve_limit"), ("battery_fault", "energy_or_reserve_limit"),
    ("time_reserve", "time_or_reserve_limit"), ("too_fast_predicted", "predicted_dynamics_limit"),
    ("px4_failsafe", "px4_owns_or_flight_state_unknown"), ("fw_airspeed", "unverified_clearance"),
    ("unbounded_failure", "unverified_clearance"), ("wrong_frame", "missing_or_unbound_motion_proof"),
    ("wrong_digest", "missing_or_unbound_motion_proof"),
])
def test_live_replay_fails_closed(terrain, profile, monkeypatch, case, reason):  # noqa: F811
    receipt, _proofs, commands = exercise(terrain, profile, monkeypatch, case=case,
                                          fw=case in {"fw_airspeed", "unbounded_failure"})
    assert (receipt["action"], receipt["status"], receipt["reason"]) == ("continue", "rejected", reason)
    assert commands == []


def test_live_evidence_is_explicitly_sim_only(terrain):  # noqa: F811
    doc = _document(terrain)
    with pytest.raises(ValueError, match="requires an X1 simulation package"):
        load_mission_package(doc)
    assert "x1_live_evidence" in load_mission_package(doc, mode="sim").document


@pytest.mark.parametrize(("fault_kind", "fw", "case", "status", "reason"), [
    ("wind_step", True, "complete", "continued", "no_new_command"),
    ("link_loss", False, "complete", "continued", "no_new_command"),
    ("battery_drop", False, "battery_17", "rejected", "energy_or_reserve_limit"),
    ("link_loss", False, "unknown_energy", "rejected", "energy_or_reserve_limit"),
    ("link_loss", False, "missing_evidence", "rejected", "unverified_clearance"),
])
def test_generated_package_replay(terrain, profile, monkeypatch, fault_kind, fw, case, status, reason):  # noqa: F811
    from multidetect.sim_teacher.x1_live_evidence_builder import build_live_evidence

    def package_override(package, doc):
        supplied = doc.pop("x1_live_evidence", None)
        digest = "c" * 64
        bounds = {**supplied, "base_mission_sha256": digest} if supplied is not None else None
        evidence, missing = build_live_evidence(package, terrain=terrain, scope_sha256=SCOPE,
                                                base_mission_sha256=digest, surveyed_bounds=bounds)
        if supplied is None:
            assert "obstacle_coverage" in missing and "suffixes" in missing
        else:
            assert not missing
        return load_mission_package({**package.document, "x1_live_evidence": evidence}, mode="sim")

    receipt, [(_, _, proof)], commands = exercise(terrain, profile, monkeypatch, case=case, fw=fw,
                                         fault_kind=fault_kind, package_override=package_override)
    assert (receipt["action"], receipt["status"], receipt["reason"]) == ("continue", status, reason)
    assert commands == []
    if case == "battery_17":
        assert proof.available_energy_wh == pytest.approx(11.84)


@pytest.mark.parametrize(("fault_kind", "fw", "case", "status", "reason"), [
    ("wind_step", True, "complete", "continued", "no_new_command"),
    ("link_loss", True, "complete", "continued", "no_new_command"),
    ("battery_drop", True, "battery_17", "rejected", "energy_or_reserve_limit"),
    ("link_loss", True, "stale_battery", "rejected", "energy_or_reserve_limit"),
    ("link_loss", True, "stale_battery_sim", "rejected", "energy_or_reserve_limit"),
    ("link_loss", True, "missing_battery", "rejected", "energy_or_reserve_limit"),
    ("link_loss", True, "stale_global", "rejected", "unverified_clearance"),
    ("link_loss", True, "missing_global", "rejected", "unverified_clearance"),
])
def test_assumed_sim_replay(terrain, profile, monkeypatch, tmp_path, fault_kind, fw, case, status, reason):  # noqa: F811
    import json

    raw = json.loads((demo.ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa.json").read_text(encoding="utf-8"))
    evidence_hash = demo.canonical_sha256(raw)
    battery = raw["battery"]["value"]
    state = {"sha256": evidence_hash, "capacity_wh": raw["battery_capacity_wh"]["value"]}

    def package_override(_package, doc):
        doc["budget"]["battery_capacity_wh"] = state["capacity_wh"]
        evidence = doc["x1_live_evidence"]
        evidence.update(basis="ASSUMED_SIM", assumed_bounds_sha256=evidence_hash)
        key = "fault_soc_error_pct" if fault_kind == "battery_drop" else "nominal_soc_error_pct"
        evidence["battery"]["soc_error_pct"] = battery[key]
        return load_mission_package(doc, mode="sim", allow_assumed_sim=True)

    receipt, [(_, _, proof)], commands = exercise(
        terrain, profile, monkeypatch, case=case, fw=fw, fault_kind=fault_kind,
        package_override=package_override, assumed_sim=state,
    )
    assert (receipt["action"], receipt["status"], receipt["reason"]) == ("continue", status, reason)
    assert commands == []
    assert receipt["basis"] == receipt["motion_proof"]["basis"] == proof.basis == "ASSUMED_SIM"
    assert receipt["assumed_bounds_sha256"] == evidence_hash
    if case == "complete" and fault_kind == "wind_step":
        # Exercise the same episode-to-result path the SITL runner uses.
        (tmp_path / "episode.json").write_text('{"status":"complete"}', encoding="utf-8")
        (tmp_path / "mavlink.jsonl").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        record = {"flight": {}, "markers": [], "safety": {"observed": {}}, "contingency_scenario": {
            "live_evidence": {"basis": "ASSUMED_SIM"},
        }}
        assert demo.finish_flight(tmp_path, (), record) is False  # No takeoff/landing in the CPU fixture.
        output = tmp_path / "result.json"
        demo.write_result(output, record)
        saved = demo.read_object(output)["contingency_scenario"]
        assert saved["live_evidence"]["basis"] == saved["markers"][0]["motion_proof"]["basis"] == "ASSUMED_SIM"
    if case == "battery_17":
        assert proof.available_energy_wh == 0
    if case == "complete":
        assert proof.available_energy_wh == pytest.approx(64.08)


def test_iowa_case_b_mc_return_geometry_matches_archived_bounds():
    root = demo.ROOT
    current = json.loads((root / "configs/simulation/x1-assumed-sim-bounds-iowa.json").read_bytes())
    archived = json.loads((Path(__file__).with_name("fixtures") / "x1_assumed_sim_bounds_iowa_archived.json").read_bytes())
    assert current["px4_native_parameters"]["value"] == {
        **archived["px4_native_parameters"]["value"], "RTL_DESCEND_ALT": 73,
        "FW_AIRSPD_MIN": 12, "FW_AIRSPD_TRIM": 15, "FW_AIRSPD_MAX": 18,
    }
    assert current["native"]["return"]["3"] == archived["native"]["return"]["3"]


@pytest.mark.parametrize(("case", "available_energy_wh"), [
    ("complete", 65.52), ("battery_20", 11.52), ("missing_battery", None),
    ("stale_battery", None), ("stale_battery_sim", None), ("future_battery_sim", None),
    ("skewed_wire_clock", None), ("regressed_wire_clock", None),
])
def test_iowa_mc_return_battery_replay(terrain, profile, monkeypatch, case, available_energy_wh):  # noqa: F811
    fixture = Path(__file__).with_name("fixtures") / "x1_iowa_mc_return_battery.json"
    archived = json.loads(fixture.read_bytes())
    # The archived episode ran with the bounds file of that day; keep a byte-identical copy so later
    # additions to the repository bounds (e.g. evidence-follow) do not invalidate the replay.
    path = Path(__file__).with_name("fixtures") / "x1_assumed_sim_bounds_iowa_archived.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == archived["bounds_sha256"]
    raw = json.loads(path.read_bytes())
    battery = raw["battery"]["value"]
    state = {"sha256": archived["bounds_sha256"], "capacity_wh": raw["battery_capacity_wh"]["value"]}

    def package_override(_package, doc):
        doc["budget"]["battery_capacity_wh"] = state["capacity_wh"]
        evidence = doc["x1_live_evidence"]
        evidence.update(basis="ASSUMED_SIM", assumed_bounds_sha256=state["sha256"])
        evidence["battery"] = {"max_voltage_v": battery["max_voltage_v"],
                               "soc_error_pct": battery["nominal_soc_error_pct"],
                               "consumption_error_wh": battery["consumption_error_wh"]}
        return load_mission_package(doc, mode="sim", allow_assumed_sim=True)

    receipt, [(_, _, proof)], commands = exercise(
        terrain, profile, monkeypatch, case=case, package_override=package_override,
        assumed_sim=state, archived_battery=archived,
    )
    assert receipt["action"] == "return"
    if case == "complete":
        assert (receipt["status"], receipt["reason"], commands) == ("sent_unconfirmed", "readback_required", [("rtl",)])
    else:
        assert (receipt["status"], receipt["reason"], commands) == ("rejected", "energy_or_reserve_limit", [])
    if available_energy_wh is None:
        assert proof.available_energy_wh is None
    else:
        assert proof.available_energy_wh == pytest.approx(available_energy_wh)


@pytest.mark.parametrize(("target_component", "expected"), [(0, 1), (1, 1), (42, 42)])
def test_bind_keeps_explicit_component_and_filters_foreign_telemetry(monkeypatch, target_component, expected):
    monkeypatch.setattr(SitlScenarioTransport, "in_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(x1_scenario.time, "monotonic_ns", lambda: MS)
    state = MavlinkState(received_monotonic_ns={"BATTERY_STATUS": MS})
    reader = SimpleNamespace(latest_control_state=lambda: state)
    scenario = x1_scenario.CollectorScenario((), clearance=lambda *_: None)
    scenario.bind(None, None, None, reader, SimpleNamespace(target_system=1, target_component=target_component),
                  lambda _: None, "peer-replay", clock=lambda: 1)
    assert scenario.target_peer == (1, expected)
    state.messages["BATTERY_STATUS"] = {"_source_system": 1, "_source_component": expected,
                                        "id": 0, "battery_remaining": 95}
    assert scenario.battery_pct() == 95
    state.messages["BATTERY_STATUS"]["_source_component"] = 255
    assert scenario.battery_pct() is None
