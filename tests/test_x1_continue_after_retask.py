"""Response-time MAVLink snapshots from the Iowa evidence-follow cadence run."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_executor import (
    Admission, RuntimeFrame, X1AirbornePlanAdapter, X1ContingencyExecutor, X1Envelope, decode_decision,
)
from multidetect.autonomy.x1_contingency_state import STATE_SCHEMA, VTOL_STATES
from multidetect.autonomy.x1_mission import build_x1_mission_steps
from multidetect.autonomy.x1_plan import one_waypoint_primitives_input
from multidetect.sim_teacher.streams import MavlinkState
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from multidetect.sim_teacher.x1_scenario import CollectorScenario
from scripts.run_x1_autonomy_demo import PROFILE, load_airframe_profile, load_local_dem_grid


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _scenario(archive):
    doc = archive["package"]
    terrain = load_local_dem_grid("simulation/terrain/rw_farm_iowa_w6km__clear_noon.dem.json")
    profile = load_airframe_profile(PROFILE)
    package = load_mission_package(doc, mode="sim", allow_assumed_sim=True)
    capabilities = frozenset(doc["capabilities"])
    plan = validate_mission_plan(archive["original_plan"], capabilities=capabilities)
    view, adapted, radius = one_waypoint_primitives_input(plan, package, capabilities)
    steps = build_x1_mission_steps(view, plan=adapted, terrain=terrain, profile=profile, mission_radius_m=radius)
    # Use the exact template bytes pinned by the recorded episode.
    bounds_path = Path(__file__).parent / "fixtures/x1_continue_after_retask/iowa-assumed-bounds.json"
    bounds = _read(bounds_path)
    bounds_sha = hashlib.sha256(bounds_path.read_bytes()).hexdigest()
    assert bounds_sha == doc["x1_live_evidence"]["assumed_bounds_sha256"]
    evidence = LiveContinueEvidence(package, profile, terrain, steps, scope=doc["x1_live_evidence"]["scope_sha256"],
                                    assumed_sim={"sha256": bounds_sha, "capacity_wh": 72,
                                                 "airborne_retask": bounds["airborne_retask"]["value"]})
    scenario = CollectorScenario((), clearance=None, motion_evidence=evidence, terrain=terrain)
    scenario.target_peer = (1, 1)
    scenario.decision = SimpleNamespace(revision=1, plan_digest=canonical_sha256(archive["original_plan"]))
    return scenario, X1Envelope(profile)


def _frame(scenario, snapshot, *, index, event_id, **fields):
    rows, now = snapshot["rows"], snapshot["now_ns"]
    state = MavlinkState(messages=rows, vehicle_time_boot_ms=snapshot["boot"],
                         received_monotonic_ns={kind: row["receive_monotonic_ns"] for kind, row in rows.items()},
                         received_vehicle_ms=snapshot["stamps"])
    index, sha, key = scenario._continue_motion_context(index, None)
    boot = rows["LOCAL_POSITION_NED"]["time_boot_ms"]
    point, energy, remaining = scenario._motion_data(index, boot, state, now_ns=now, installed_key=key)
    samples = {kind: scenario._fresh_sample(kind, state=state, now_ns=now) or {} for kind in rows}
    heartbeat, vtol = samples["HEARTBEAT"], samples["EXTENDED_SYS_STATE"]
    return RuntimeFrame(
        now, now // 1_000_000, scenario.motion_evidence.builder.package.mission_id, scenario.decision.revision,
        event_id, heartbeat.get("system_status") != 4, bool(heartbeat.get("base_mode", 0) & 128)
        and vtol.get("landed_state") == 2, VTOL_STATES.get(vtol.get("vtol_state")),
        scenario._home_height_m(state=state, now_ns=now), scenario._airspeed_mps(samples),
        scenario._position_ok(samples, state=state, now_ns=now), {}, position_ne_amsl=point,
        active_motion_index=index, remaining_energy_wh=energy, remaining_time_s=remaining,
        installed_plan_sha256=sha, **fields,
    )


def _install(scenario, envelope, archive):
    evidence = scenario.motion_evidence
    package = evidence.builder.package
    choice = decode_decision(archive["retask_wire"], capabilities=frozenset(package.document["capabilities"]))
    finding = archive["finding"]
    frame = _frame(scenario, archive["admission"], index=2, event_id=choice.event_id, retask_boundary=True,
                   retask_finding_ne=scenario.terrain.local_from_wgs84(finding["lat_deg"], finding["lon_deg"]),
                   retask_finding_error_m=finding["error_bound_m"])
    scenario.retask.enter_tick(2)
    proof = evidence.build_proof(choice, frame)
    assert envelope.veto(choice, frame, proof, package,
                         check_airborne_plan=X1AirbornePlanAdapter.check_airborne_plan) is None
    assert scenario.retask.replace_remaining(choice.plan, 1)
    scenario.decision.revision = 2
    scenario.decision.plan_digest = canonical_sha256(choice.params["plan"])
    assert scenario.decision.plan_digest == "995a1e75b39540b87ca7390219f53b175cd8153a8887241218a4233b96b486a4"


def _continue(scenario, snapshot, *, revision=2):
    event_id = int(snapshot["label"])
    package = scenario.motion_evidence.builder.package
    decision = decode_decision(json.dumps(dict(schema="x1.contingency.decision.v1", action="continue", params={},
                                               request_id=f"tick:{event_id}", event_id=event_id,
                                               mission_id=package.mission_id, revision=revision)))
    frame = _frame(scenario, snapshot, index=snapshot["index"], event_id=event_id, retask_boundary=False)
    return decision, frame, scenario.motion_evidence.build_proof(decision, frame)


@pytest.fixture
def installed():
    # The checked-in sample uses a controlled empty inventory; do not treat it
    # as the Iowa survey. The offline lane replay uses the full archived package.
    archive = _read(Path(__file__).parent / "fixtures/x1_continue_after_retask/episode-0000-68bf5c36.json")
    scenario, envelope = _scenario(archive)
    _install(scenario, envelope, archive)
    return scenario, envelope, archive


@pytest.mark.parametrize(("label", "seconds"), [("54", 269), ("58", 229), ("120", 175)],
                         ids=["approach", "loiter_observe", "mc_return"])
def test_installed_continue_uses_remaining_route(installed, label, seconds):
    scenario, envelope, archive = installed
    sample = next(s for s in archive["snapshots"] if s["label"] == label)
    evidence = scenario.motion_evidence
    package = evidence.builder.package
    # An object without vehicle methods guarantees there is no usable command port.
    executor = X1ContingencyExecutor(SimpleNamespace(plan_version=2), object(), package, envelope,
                                     claim_native=lambda: pytest.fail("continue must not claim the route"))
    t_ms = sample["now_ns"] // 1_000_000
    request = executor.open_request(dict(schema=STATE_SCHEMA, mission_id=package.mission_id, revision=2, t_ms=t_ms),
                                    run_id="archive", mission_progress={})
    wire = json.dumps(dict(schema="x1.contingency.decision.v1", request_id=request["request_id"],
                           event_id=request["event_id"], mission_id=package.mission_id, revision=2,
                           action="continue", params={}))
    frame = _frame(scenario, sample, index=sample["index"], event_id=request["event_id"], retask_boundary=False)
    proof = evidence.build_proof(decode_decision(wire), frame)
    assert frame.position_ne_amsl is not None
    assert proof.plan_revision == 2
    assert proof.plan_sha256 == scenario.decision.plan_digest
    assert proof.landing_site_clear
    assert proof.required_time_s == seconds
    assert executor.apply(wire, frame, now_ms=t_ms, proof=proof) == Admission("continued", "no_new_command", "continue")


@pytest.mark.parametrize("stale", [dict(plan_revision=1), dict(plan_sha256="a" * 64),
                                   dict(plan_revision=None, plan_sha256=None)])
def test_installed_continue_rejects_old_plan_proof_even_if_rewrapped(installed, stale):
    scenario, envelope, archive = installed
    sample = next(s for s in archive["snapshots"] if s["label"] == "54")
    decision, frame, proof = _continue(scenario, sample)
    assert proof.geofence_clear
    assert envelope.veto(decision, frame, replace(proof, **stale), scenario.motion_evidence.builder.package) == (
        "missing_or_unbound_motion_proof")


def test_installed_continue_rejects_orbit_disk_outside_corridor(installed):
    scenario, envelope, archive = installed
    evidence = scenario.motion_evidence
    sample = next(s for s in archive["snapshots"] if s["index"] == 3)
    decision, frame, proof = _continue(scenario, sample)
    assert proof.geofence_clear and frame.position_ne_amsl is not None
    _, e, _ = scenario.retask.at(3).step.command.target_ned
    # Aircraft and orbit centre are inside; the far side of the actual ring is not.
    edge = max(e, frame.position_ne_amsl[1]) + 1
    assert edge < e + scenario.retask.at(3).step.command.radius_m
    evidence.builder.corridor["east_max_m"] = edge
    proof = evidence.build_proof(decision, frame)
    assert not proof.geofence_clear
    assert envelope.veto(decision, frame, proof, evidence.builder.package) == "unverified_clearance"


@pytest.mark.parametrize("label", ["52", "61"], ids=["approach", "loiter_observe"])
def test_installed_continue_keeps_fw_airspeed_veto(installed, label):
    scenario, envelope, archive = installed
    sample = next(s for s in archive["snapshots"] if s["label"] == label)
    decision, frame, proof = _continue(scenario, sample)
    assert frame.airspeed_mps == sample["rows"]["VFR_HUD"]["airspeed"] > 18
    assert envelope.veto(decision, frame, proof, scenario.motion_evidence.builder.package) == "current_fw_airspeed_limit"
