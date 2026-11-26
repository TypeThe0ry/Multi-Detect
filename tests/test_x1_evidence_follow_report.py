import gzip
import hashlib
import json
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.verify_x1_evidence_follow import main, verify_evidence_follow


@pytest.fixture
def episode():
    mavlink, steps = [], []

    def mark(kind, t_ms, **fields):
        mavlink.append({"message_type": kind, "receive_monotonic_ns": t_ms * 1_000_000, **fields})

    peer = {"_source_system": 1, "_source_component": 1}
    mark("HEARTBEAT", 1, autopilot=12, **peer)
    detection = {
        "class": "vehicle", "track_ref": "lead_1", "bearing_deg": 20, "elevation_deg": -10,
        "position_estimate": {"lat_deg": 41, "lon_deg": -93, "alt_amsl_m": 300, "error_bound_m": 15},
    }
    perception = {"detection": detection, "source": "fault", "t_ms": 200, "age_ms": 0,
                  "target_status": {"visible": True, "track_ref": "lead_1", "track_state": "tentative"}}
    mark("scenario_observed", 10, fault="target_found", source="fault", observed_value=perception)
    mark("scenario_decision_request", 20, request={"request_id": "follow:1", "event_id": 1,
         "scenario_observation": deepcopy(perception), "state": {"revision": 1, "mission_id": "mission:1",
         "events": [{"id": 1, "type": "target_found", "source": "fault"}]}})
    wire = json.dumps({"action": "retask", "request_id": "follow:1", "event_id": 1,
                       "mission_id": "mission:1", "revision": 1, "params": {"plan": {"steps": [
                           {"id": f"step{i}", "skill": skill} for i, skill in enumerate(
                               ("fw_goto_waypoint", "loiter_observe", "back_transition", "return_home"), 3)
                       ]}}}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    mark("scenario_decision_receipt", 35, action="retask", status="installed", request_id="follow:1",
         basis="ASSUMED_SIM", assumed_bounds_sha256="0" * 64, wire=wire,
         injected_tool_results=[{"schema": "x1.contingency.tool.v1", "ok": True, "source": "fault",
                                 "error": None, "tool": "target_status", "age_ms": 24,
                                 "data": {"captured_ms": 200, "track_id": "lead_1", "visible": True,
                                          "last_seen_age_ms_at_snapshot": 0, "error_bound_m": 15,
                                          "position_wgs84_amsl": [41, -93, 300]}}],
         motion_proof={"basis": "ASSUMED_SIM", "geofence_clear": True,
                       "decision_digest": hashlib.sha256(wire.encode()).hexdigest()})
    mark("scenario_plan_replaced", 37, previous_revision=1, revision=2, step_index=3, request_id="follow:1")

    def primitive(index, start, end, name, command, wait, **fields):
        label = {"revision": 2, "step_index": index, "source_step_id": f"step{index}",
                 "primitive": name, "command": command, **fields}
        mark("collector_x1_retask_primitive_start", start, **label)
        mark("collector_x1_retask_primitive_result", end, **label, status="success", motion_wait=wait)
        steps.append({"step_index": index, "receive_monotonic_ns": (end + 2) * 1_000_000,
                      "labels": {"x1_retask": label}, "outcome": {"status": "success"},
                      "ack": {"motion_wait": wait}})

    primitive(3, 40, 50, "fw_goto_waypoint", {"kind": "reposition", "mode": "FW"}, {
        "waited": "arrived", "x1_passage": {"pre_plane_sim_stamp_ns": 1, "crossing_sim_stamp_ns": 2,
                                           "crossing_cross_track_m": 2, "crossing_cross_track_limit_m": 10},
    })
    orbit = {"kind": "orbit", "mode": "FW", "radius_m": 100, "duration_s": 1.5}
    primitive(4, 54, 1600, "loiter_observe", {
        **orbit, "target_ned": [190, 380, -70], "metadata": {"long_range_home_relative": True},
    }, {"waited": "duration", "orbit_capture_sim_ns": 3_000_000_000, "orbit_residence_sim_s": 1.5,
        "target_ned_m": [200, 400, -70], "radius_m": 100, "fw_reposition_success": True},
        executed_command={**orbit, "target_ned": [200, 400, -70], "metadata": {"long_range_home_relative": False}})
    mark("EXTENDED_SYS_STATE", 55, vtol_state=4, landed_state=2, **peer)
    for i in range(6):
        angle = math.radians(5 * i)
        mark("LOCAL_POSITION_NED", 60 + i * 300, x=200 + 100 * math.cos(angle),
             y=400 + 100 * math.sin(angle), z=-70, time_boot_ms=3000 + i * 300, **peer)
    primitive(5, 1610, 1700, "back_transition", {"kind": "transition", "mode": "MC"},
              {"waited": "transitioned"})
    mark("EXTENDED_SYS_STATE", 1650, vtol_state=3, landed_state=2, **peer)
    primitive(6, 1710, 1800, "mc_return_to_home", {
        "kind": "mc_reposition", "mode": "MC", "target_ned": [0, 0, -70],
    }, {"waited": "settled"})
    mark("LOCAL_POSITION_NED", 1790, x=0.4, y=0.1, vx=0.1, vy=0.1, vz=0.1, time_boot_ms=5000, **peer)
    mark("collector_x1_retask_landing_handoff", 1810, revision=2, step_index=7,
         source_step_id="step6", primitive="vtol_land", command={"kind": "land", "mode": "MC"})
    mark("EXTENDED_SYS_STATE", 1900, vtol_state=3, landed_state=1, **peer)
    mark("collector_landing_complete", 1905, status="landed", revision=2,
         flight_state={"landed_state": 1, "vtol_state": 3})
    return deepcopy((mavlink, steps))


def _row(rows, name, **match):
    return next(row for row in rows if row["message_type"] == name
                and all(row.get(key) == value for key, value in match.items()))


def test_accepts_evidence_follow(episode):
    passed, evidence = verify_evidence_follow(*episode)
    assert passed, evidence
    assert evidence["revision"] == 2
    assert evidence["basis"] == "ASSUMED_SIM"
    assert evidence["orbit"]["dwell_s"] == pytest.approx(1.5)
    assert evidence["orbit"]["swept_deg"] == pytest.approx(25)
    assert [row["waited"] for row in evidence["steps"]] == ["arrived", "duration", "transitioned", "settled"]
    assert evidence["landed_monotonic_ns"] == 1_900_000_000


def test_finish_flight_distinguishes_base_prefix_and_installed_suffix(episode, tmp_path):
    from scripts.run_x1_autonomy_demo import finish_flight

    mavlink, rows = episode
    mavlink.append({"message_type": "fixed_wing_takeoff_complete", "receive_monotonic_ns": 2_000_000})
    original = [SimpleNamespace(primitive=primitive, source_step_id=primitive,
                                command=SimpleNamespace(kind=kind, to_dict=lambda k=kind: {"kind": k}))
                for primitive, kind in (("rise", "mc_reposition"), ("fw_transition", "transition"),
                                        ("fw_goto_waypoint", "reposition"), ("back_transition", "transition"))]
    waited = ("settled", "transitioned", "arrived")
    prefix = [{"step_index": i, "receive_monotonic_ns": (3 + i) * 1_000_000,
               "labels": {"motion": step.command.to_dict()}, "outcome": {"status": "success"},
               "ack": {"motion_wait": {"waited": waited[i], "x1_passage": True}}}
              for i, step in enumerate(original[:3])]

    def write_rows(name, data):
        (tmp_path / name).write_text("".join(json.dumps(row) + "\n" for row in data), encoding="utf-8")

    (tmp_path / "episode.json").write_text('{"status":"complete"}', encoding="utf-8")
    write_rows("mavlink.jsonl", mavlink)
    write_rows("steps.jsonl", prefix + rows)
    record = {"markers": [], "safety": {"observed": {}}, "flight": {}, "contingency_scenario": {}}
    assert finish_flight(tmp_path, original, record)
    assert record["flight"]["steps"] == {step.primitive: True for step in original[:3]}
    assert record["contingency_scenario"]["evidence_follow"]["revision"] == 2
    prefix[1]["outcome"]["status"] = "failure"
    write_rows("steps.jsonl", prefix + rows)
    assert not finish_flight(tmp_path, original, record)


@pytest.mark.parametrize(("kind", "match", "update", "reason"), [
    ("scenario_observed", {}, {"observed_value": {}}, "target_found_observation_missing"),
    ("scenario_observed", {}, {"receive_monotonic_ns": 26_000_000}, "target_found_out_of_order"),
    ("scenario_decision_receipt", {}, {"basis": "REAL"}, "assumed_sim_proof_missing"),
    ("scenario_decision_receipt", {}, {"status": "rejected"}, "installed_retask_receipt_missing_or_ambiguous"),
    ("scenario_decision_receipt", {}, {"wire": "{}"}, "replacement_revision_or_order_mismatch"),
    ("scenario_plan_replaced", {}, {"request_id": "other:1"}, "replacement_revision_or_order_mismatch"),
    ("scenario_plan_replaced", {}, {"step_index": 4}, "replacement_revision_or_order_mismatch"),
    ("scenario_plan_replaced", {}, {"previous_revision": 0}, "replacement_revision_or_order_mismatch"),
    ("collector_x1_retask_primitive_start", {"step_index": 3}, {"revision": 3},
     "replacement_revision_or_order_mismatch"),
    ("collector_x1_retask_primitive_start", {"step_index": 4}, {"source_step_id": "original"},
     "loiter_observe_execution_mismatch"),
    ("collector_x1_retask_primitive_start", {"step_index": 5}, {"receive_monotonic_ns": 1_500_000_000},
     "back_transition_execution_mismatch"),
    ("EXTENDED_SYS_STATE", {"vtol_state": 3, "landed_state": 2}, {"vtol_state": 4},
     "px4_mc_transition_unconfirmed"),
    ("EXTENDED_SYS_STATE", {"landed_state": 1}, {"_source_system": 99}, "px4_landing_unconfirmed"),
])
def test_rejects_bad_evidence(episode, kind, match, update, reason):
    mavlink, steps = episode
    _row(mavlink, kind, **match).update(update)
    passed, evidence = verify_evidence_follow(mavlink, steps)
    assert not passed
    assert evidence["reason"] == reason


def test_fw_passage_must_confirm_crossing(episode):
    mavlink, steps = episode
    for wait in (steps[0]["ack"]["motion_wait"],
                 _row(mavlink, "collector_x1_retask_primitive_result", step_index=3)["motion_wait"]):
        wait["x1_passage"]["passage_failure"] = "sample_gap"
    assert verify_evidence_follow(mavlink, steps)[1]["reason"] == "fw_passage_missing"


def test_retask_requires_the_published_observation(episode):
    mavlink, steps = episode
    _row(mavlink, "scenario_decision_request")["request"]["scenario_observation"]["detection"]["track_ref"] = "different"
    assert verify_evidence_follow(mavlink, steps)[1]["reason"] == "retask_request_does_not_reference_detection"


def test_cadence_ticket_has_its_own_id_and_keeps_original_observation_time(episode):
    mavlink, steps = episode
    request = _row(mavlink, "scenario_decision_request")["request"]
    request.update(trigger="cadence", event_id=2**52 + 1)
    request["state"]["latest_event_id"] = 1
    request["scenario_observation"]["age_ms"] = 17
    receipt = _row(mavlink, "scenario_decision_receipt")
    receipt.update(trigger="cadence", event_id=request["event_id"])
    receipt["wire"] = json.dumps({**json.loads(receipt["wire"]), "event_id": request["event_id"]},
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    receipt["motion_proof"]["decision_digest"] = hashlib.sha256(receipt["wire"].encode()).hexdigest()
    passed, evidence = verify_evidence_follow(mavlink, steps)
    assert passed and evidence["trigger"] == "cadence", evidence
    receipt["injected_tool_results"][0]["data"]["captured_ms"] += 1
    assert verify_evidence_follow(mavlink, steps)[1]["reason"] == "retask_tool_evidence_mismatch"


@pytest.mark.parametrize(("kind", "match", "update", "reason"), [
    ("LOCAL_POSITION_NED", {"x": 0.4}, {"vx": 4}, "return_home_settle_unconfirmed"),
    ("collector_landing_complete", {}, {"status": "timeout"}, "px4_landing_unconfirmed"),
    ("collector_landing_complete", {}, {"revision": 1}, "px4_landing_unconfirmed"),
])
def test_rejects_home_or_landing_label_without_matching_readback(episode, kind, match, update, reason):
    mavlink, steps = episode
    _row(mavlink, kind, **match).update(update)
    assert verify_evidence_follow(mavlink, steps)[1]["reason"] == reason


def test_rejects_unlabelled_step_after_replacement(episode):
    mavlink, steps = episode
    steps.append({**steps[-1], "labels": {}})
    assert verify_evidence_follow(mavlink, steps)[1]["reason"] == "replacement_step_rows_missing_or_ambiguous"


def test_retask_requires_an_explicit_install_marker(episode):
    mavlink, steps = episode
    mavlink.remove(_row(mavlink, "scenario_plan_replaced"))
    assert verify_evidence_follow(mavlink, steps)[1]["reason"] == "replacement_marker_missing_or_ambiguous"


@pytest.mark.parametrize("failure", ["off_ring", "gap", "wrong_vehicle", "wrong_fw_state", "no_sweep", "short_dwell",
                                     "wrong_altitude"])
def test_orbit_requires_continuous_px4_ring_telemetry(episode, failure):
    mavlink, steps = episode
    for row in mavlink:
        if failure == "wrong_altitude" and row.get("message_type") == "LOCAL_POSITION_NED" and row.get("z") == -70:
            row["z"] = -170
        if failure == "wrong_fw_state" and row.get("vtol_state") == 4:
            row["vtol_state"] = 3
        if row["message_type"] != "LOCAL_POSITION_NED":
            continue
        if failure == "off_ring":
            row.update(x=200, y=400)
        elif failure == "gap" and 3000 < row["time_boot_ms"] < 4500:
            row["time_boot_ms"] = 1
        elif failure == "wrong_vehicle":
            row["_source_system"] = 2
        elif failure == "no_sweep":
            row.update(x=300, y=400)
        elif failure == "short_dwell" and row["time_boot_ms"] == 4500:
            row["time_boot_ms"] = 4200
    passed, evidence = verify_evidence_follow(mavlink, steps)
    assert not passed
    assert evidence["reason"] == "orbit_radius_residence_unconfirmed"


@pytest.mark.parametrize("compressed", [False, True])
def test_cli_files_and_exit_status(episode, tmp_path, capsys, compressed):
    mavlink, steps = episode
    mavlink_path = tmp_path / ("mavlink.jsonl.gz" if compressed else "mavlink.jsonl")
    data = "".join(json.dumps(row) + "\n" for row in mavlink)
    mavlink_path.write_bytes(gzip.compress(data.encode()) if compressed else data.encode())
    step_path = tmp_path / "steps.jsonl"
    step_path.write_text("".join(json.dumps(row) + "\n" for row in steps), encoding="utf-8")
    assert main([str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    step_path.write_text("{truncated\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["evidence"]["reason"] == "episode_evidence_unreadable"
