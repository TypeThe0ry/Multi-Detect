"""CPU-only proof tests and an opt-in replay of the archived Iowa case C."""

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_executor import RuntimeFrame, X1Envelope, decode_decision
from multidetect.autonomy.x1_plan import one_waypoint_primitives_input
from multidetect.sim_teacher.streams import MavlinkState
from multidetect.sim_teacher.x1_fw_native_rtl import PX4_RTL_COMMIT, FwRtlBounds, _REQUIRED
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from multidetect.sim_teacher.x1_scenario import HOLD_REVALIDATION_LEAD_MS, CollectorScenario
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions
from multidetect.terrain_dem import load_local_dem_grid
from scripts import generate_x1_assumed_sim_bounds as iowa
from test_x1_continue_after_retask import installed  # noqa: F401
from test_x1_live_retask import inputs  # noqa: F401
from test_x1_mission import _build, _plan, profile, terrain  # noqa: F401

MS = 1_000_000
HOLD = "hold:yard-orbit"
ARCHIVE_COMMIT = "83c4f4e580558816f0e398918f66358988004696"


def bounds(scope):
    # Test hypotheses only: not flight evidence and never an installed image.
    return FwRtlBounds(
        scope=scope, px4_commit=PX4_RTL_COMMIT, image_id="sha256:" + "b" * 64,
        validation_sha256="c" * 64,
        params={"SYS_AUTOSTART": 4051, "VT_TYPE": 0, "RTL_TYPE": 0, "RTL_CONE_ANG": 0,
                "RTL_LAND_DELAY": 0, "RTL_APPR_FORCE": 0, "RTL_PLD_MD": 0, "MPC_XY_CRUISE": 5,
                "MPC_Z_V_AUTO_DN": 1.5, "MPC_LAND_SPEED": 0.7, "NAV_ACC_RAD": 2,
                "RTL_RETURN_ALT": 60, "RTL_DESCEND_ALT": 73, "RTL_LOITER_RAD": 80,
                "NAV_LOITER_RAD": 80, "VT_B_TRANS_DUR": 4, "VT_B_DEC_MSS": 2, "FW_R_LIM": 35,
                "FW_AIRSPD_MIN": 12, "FW_AIRSPD_TRIM": 15, "FW_AIRSPD_MAX": 18},
        fw_tracking_m=20, fw_vertical_m=3, fw_acceptance_m=10, move_to_land_vertical_m=2,
        transition_vertical_m=4, mc_tracking_m=12, mc_vertical_m=2, landing_tracking_m=4,
        landing_vertical_m=2, max_ground_speed_mps=27, phase_time_s=(15, 50, 30, 20, 5, 45, 110),
        fw_peak_power_w=344, hover_peak_power_w=750, max_bank_deg=35, max_climb_mps=3,
        max_descent_mps=2, max_acceleration_mps2=2, max_angular_rate_dps=60,
        home_only_verified=True, configuration_locked=True,
    )


def assumed(rt, sha256=None):
    return {"capacity_wh": 72, "image_id": iowa.IMAGE_ID, "fw_rtl": rt, "parameters": {},
            "sha256": sha256 or rt.scope}


@pytest.mark.parametrize(("case", "reason"), [
    ("valid", None), ("corridor", "unverified_clearance"), ("terrain", "unverified_clearance"),
    ("coverage", "unverified_clearance"), ("obstacle", "unverified_clearance"),
    ("energy", "energy_or_reserve_limit"), ("time", "time_or_reserve_limit"),
    ("version", "unverified_clearance"), ("params", "unverified_clearance"),
    ("no_readback", "unverified_clearance"), ("unknown_hold", "unverified_clearance"),
    ("expired_hold", "unverified_clearance"), ("home", "unverified_clearance"),
    ("land-now", "land_now_requires_mc"), ("loiter", "unverified_clearance"),
])
def test_fw_rtl_envelope(inputs, profile, terrain, monkeypatch, case, reason):
    base, _, _, _ = inputs
    doc = dict(base.document)
    data = doc["x1_live_evidence"]
    scope = data["scope_sha256"]
    raw = iowa.generate(mode="sim", world=iowa.WORLD, image=iowa.IMAGE,
                        profile_path=Path("configs/airframes/delta_tailsitter_gimbal.json"))
    hold = raw["native"]["holds"][HOLD]["value"]
    hold["centre"][2] = 185
    hold["entry"][4:6] = [175, 195]
    data["native"] = {"holds": {HOLD: hold}}
    rt = bounds(scope)
    if case == "corridor":
        doc["x1_planning"]["corridor"]["east_min_m"] = -125
    if case == "terrain":
        rt = replace(rt, params={**rt.params, "RTL_DESCEND_ALT": 30})
        assert not rt.valid_for(scope, profile)
        # Independently exercise the legacy low-altitude geometry veto.
        monkeypatch.setitem(_REQUIRED, "RTL_DESCEND_ALT", 30)
    if case == "coverage":
        data["obstacle_coverage"][0] = -125
    if case == "obstacle":
        data["obstacles"] = [[-30, -25, -50, -45, 166, 170]]
    if case == "version":
        rt = replace(rt, px4_commit=ARCHIVE_COMMIT)
    if case == "params":
        rt = replace(rt, params={**rt.params, "RTL_LAND_DELAY": -1})
    package = load_mission_package(doc, mode="sim", allow_assumed_sim=True)
    steps = _build(replace(package, plan=validate_mission_plan(_plan(85))), terrain, profile)
    evidence = LiveContinueEvidence(package, profile, terrain, steps, scope=scope, assumed_sim=assumed(rt))
    evidence.fw_rtl_ready = evidence.fw_rtl is not None and case != "no_readback"
    start = (398, 8, 185)
    evidence.check_entry(1, start, dict(x=398, y=8, z=-85, vx=0, vy=15, vz=0),
                         confirmed_hold_ref=None if case == "unknown_hold" else HOLD,
                         observed_home=(80 if case == "home" else 0, 0, 100))
    frame = RuntimeFrame(1, 10_000, package.mission_id, 1, 1, False, True, "fw", 85, 15, True,
                         evidence.hold_sites(1, "fw"), position_ne_amsl=start, active_motion_index=1,
                         remaining_energy_wh=20 if case == "energy" else 65,
                         remaining_time_s=100 if case == "time" else 800, native_hold_active=case != "expired_hold")
    action = case if case in {"land-now", "loiter"} else "return"
    decision = decode_decision(json.dumps(dict(schema="x1.contingency.decision.v1", request_id="case-c:1",
                                               event_id=1, mission_id=package.mission_id, revision=1,
                                               action=action, params={"hold_ref": HOLD} if action == "loiter" else {})))
    proof = evidence.build_proof(decision, frame)
    assert X1Envelope(profile).veto(decision, frame, proof, package) == reason
    if reason is None:
        assert proof.landing_site_clear and proof.min_transition_agl_m > 40
        assert proof.required_time_s == sum(rt.phase_time_s)
    if case == "terrain":
        assert proof.geofence_clear and proof.min_transition_agl_m < 40 and proof.terrain_margin_m < 0
    if case == "corridor":
        assert not proof.geofence_clear and proof.terrain_margin_m > 0


@pytest.mark.parametrize(("pin", "value"), [
    ("FW_AIRSPD_MIN", 11), ("FW_AIRSPD_TRIM", 16), ("FW_AIRSPD_MAX", 23), ("FW_AIRSPD_MAX", None),
])
def test_fw_rtl_bounds_require_profile_speed_pins(profile, pin, value):
    rt = bounds(iowa.SCOPES["wind_step"])
    assert rt.valid_for(rt.scope, profile)
    params = dict(rt.params)
    if value is None:
        del params[pin]
    else:
        params[pin] = value
    assert not replace(rt, params=params).valid_for(rt.scope, profile)


@pytest.mark.parametrize("readback", ["matches", "commit", "parameter", "legacy_descend",
                                      "airspeed_min", "airspeed_trim", "airspeed_max"])
def test_candidate_requires_exact_prearm_readback(readback):
    rt = bounds("a" * 64)
    records = []
    observed = dict(rt.params)
    if readback == "parameter":
        observed["RTL_LAND_DELAY"] = -1
    if readback == "legacy_descend":
        observed["RTL_DESCEND_ALT"] = 30
    if readback.startswith("airspeed_"):
        observed["FW_AIRSPD_" + readback.removeprefix("airspeed_").upper()] += 1
    scenario = CollectorScenario((), clearance=None, motion_evidence=SimpleNamespace(assumed_sim=assumed(rt), fw_rtl=rt))
    scenario.record = records.append
    scenario.transport = SimpleNamespace(
        prepare=lambda _: None,
        command=lambda *_: f"PX4 git-hash: {ARCHIVE_COMMIT if readback == 'commit' else rt.px4_commit}\n",
        _get_param=observed.__getitem__,
    )
    if readback == "matches":
        scenario.prepare()
    else:
        with pytest.raises(RuntimeError, match="commit or parameter readback mismatch"):
            scenario.prepare()
    assert records[-1]["verified"] is (readback == "matches")
    if readback == "matches":
        assert records[-1]["observed_parameters"] == rt.params


@pytest.mark.parametrize(("trigger", "home_case"), [
    ("event", "fresh"), ("cadence", "fresh"), ("cadence", "stale"), ("cadence", "foreign_peer"),
])
def test_installed_continue_then_fw_hold_then_return(installed, monkeypatch, trigger, home_case):
    scenario, _, archive = installed
    evidence = scenario.motion_evidence
    package, profile = evidence.builder.package, evidence.builder.profile
    rt = bounds(package.document["x1_live_evidence"]["scope_sha256"])
    # Counterfactual bounds and native readback; the installed route itself is
    # from the checked-in #75 replay and was validated/installed by its fixture.
    evidence.fw_rtl = replace(rt, phase_time_s=(15, 50, 25, 20, 5, 32, 95))
    evidence.fw_rtl_ready = True
    original_sha = scenario.decision.plan_digest
    route = scenario.retask.installed_at(2)
    scenario.cadence_steps = route.steps
    scenario.worker.timeline = SimpleNamespace(origin=1)
    scenario.markers = []
    scenario.transport = SimpleNamespace(target_observation=lambda _: None)
    trace, requests = [], []
    scenario.record = trace.append
    choices = recorded_decisions([dict(action="continue", params={}), dict(action="loiter", params={"hold_ref": HOLD}),
                                  dict(action="return", params={})])

    def backend(request):
        requests.append(request)
        return choices(request)

    now = 10_000
    monkeypatch.setattr("multidetect.sim_teacher.x1_scenario.time.monotonic_ns", lambda: now * MS)
    scenario.clock = lambda: state.vehicle_time_boot_ms
    scenario.reader = SimpleNamespace(latest_control_state=lambda: state)
    tick = DecisionTick(package, profile, backend, None, trace.append, run_id="installed-fw-rtl",
                        plan_digest=original_sha, revision=2, cpu_replay=True, now_ms=lambda: now,
                        build_proof=evidence.build_proof, decision_cadence_s=1)
    scenario.decision = tick

    def cycle(label, *, holding=False):
        nonlocal state, now
        sample = next(item for item in archive["snapshots"] if item["label"] == label)
        rows = {kind: dict(row) for kind, row in sample["rows"].items()}
        if holding:
            rows["HEARTBEAT"]["custom_mode"] = (3 << 24) | (4 << 16)  # AUTO.LOITER, synthetic.
            gps = rows["GLOBAL_POSITION_INT"]
            rows["HOME_POSITION"] = {"_source_system": 2 if home_case == "foreign_peer" else 1,
                                     "_source_component": 1,
                                     "latitude": round(evidence.terrain.origin_latitude_deg * 1e7),
                                     "longitude": round(evidence.terrain.origin_longitude_deg * 1e7),
                                     "altitude": gps["alt"] - gps["relative_alt"]}
        if label == "52":
            rows["VFR_HUD"]["airspeed"] = 16  # Exercise the reviewed Hold entry band.
        offset = 4_000 if holding else 3_000 if label == "52" else 0
        for kind in ("LOCAL_POSITION_NED", "GLOBAL_POSITION_INT"):
            rows[kind]["time_boot_ms"] += offset
        for _ in range(2):
            stamps = {kind: (now - (3_001 if kind == "HOME_POSITION" and home_case == "stale" else 1)) * MS
                      for kind in rows}
            state = MavlinkState(messages=rows, received_monotonic_ns=stamps,
                                 received_vehicle_ms={k: v + offset for k, v in sample["stamps"].items()},
                                 vehicle_time_boot_ms=sample["boot"] + offset)
            scenario.tick(2, None)
            if tick.future is not None:
                tick.future.result(timeout=2)
            now += 10

    state = None
    try:
        cycle("54")
        assert tick.recent_receipts[-1]["status"] == "continued", str(tick.last_admission[:2])
        assert tick.last_admission[1].plan_sha256 == route.plan_sha256
        assert tick.replay_commands == []
        now += 2_000
        cycle("52")
        assert tick.recent_receipts[-1]["status"] == "accepted_unconfirmed", str(tick.last_admission[:2])
        assert [call[0] for call in tick.replay_commands] == ["loiter_at"]
        assert tick.executor.native_owner == "loiter" and scenario.hold["hold_ref"] == HOLD
        assert requests[-1]["hold_refs"] == [HOLD]
        assert requests[-1]["remaining_plan_sha256"] == canonical_sha256({
            "compiled_plan_sha256": route.plan_sha256, "steps": [step.source_step_id for step in route.steps],
        })
        scenario.native = SimpleNamespace(action="loiter", status="confirmed", poll=lambda: None,
                                          confirmed=lambda action: action == "loiter")
        now = scenario.hold["deadline_ms"] - HOLD_REVALIDATION_LEAD_MS if trigger == "event" else now + 2_000
        cycle("52", holding=True)
        assert requests[-1].get("trigger", "event") == trigger
        assert requests[-1]["state"]["revision"] == route.revision
        if trigger == "event":
            assert requests[-1]["state"]["events"][-1]["type"] == "hold_revalidation"
        if home_case == "fresh":
            assert tick.recent_receipts[-1]["status"] == "sent_unconfirmed", str(tick.last_admission[:2])
            assert [call[0] for call in tick.replay_commands] == ["loiter_at", "rtl"]
            assert tick.executor.native_owner == "return" and scenario.hold is None
        else:
            assert tick.recent_receipts[-1]["reason"] == "unverified_clearance"
            assert [call[0] for call in tick.replay_commands] == ["loiter_at"]
            assert tick.executor.native_owner == "loiter" and scenario.hold["hold_ref"] == HOLD
    finally:
        tick.close()


@pytest.fixture(scope="module")
def case_c():
    archive = os.environ.get("X1_CASE_C_ARCHIVE")
    if not archive:
        pytest.skip("set X1_CASE_C_ARCHIVE to the archived live-wind_step-hold directory")
    root = Path(archive)
    result = json.loads((root / "result.json").read_text())
    requests = [json.loads(line) for line in (root / "decision-requests.jsonl").read_text().splitlines()]
    episode, = (root / "flight/episodes").iterdir()
    markers = result["contingency_scenario"]["markers"]
    receipts = [r for r in markers if r["message_type"] == "scenario_decision_receipt"]
    times = [r["motion_proof"]["frame_id"] for r in receipts]
    telemetry = [{} for _ in times]
    wanted = {"GLOBAL_POSITION_INT", "LOCAL_POSITION_NED", "HOME_POSITION", "HEARTBEAT",
              "GPS_RAW_INT", "EXTENDED_SYS_STATE", "VFR_HUD", "BATTERY_STATUS"}
    with (episode / "mavlink.jsonl").open() as f:
        for line in f:
            if not any(f'"message_type": "{name}"' in line for name in wanted):
                continue
            row = json.loads(line)
            stamp = row["receive_monotonic_ns"]
            if row.get("_source_system") == row.get("_source_component") == 1:
                for tick_ns, rows in zip(times, telemetry, strict=True):
                    if 0 <= tick_ns - stamp <= 3_000 * MS:
                        rows[row["message_type"]] = row
    return root, result, requests, receipts, telemetry


@pytest.mark.parametrize("variant", ["archive", "default_descent", "terrain_and_energy", "pinned_energy", "admitted_hypothesis"])
def test_case_c_replay(case_c, profile, monkeypatch, variant):
    root, result, requests, receipts, telemetry = case_c
    assert [(r["action"], r["status"]) for r in receipts] == [("loiter", "accepted_unconfirmed"), ("return", "rejected")]
    assert receipts[-1]["reason"] == "unverified_clearance"
    assert any(row.get("reason") == "mode_and_hold_target_observed" for row in result["contingency_scenario"]["markers"])
    dem = load_local_dem_grid(Path("simulation/terrain") / Path(result["prepared"]["dem_path"]).name)
    package = load_mission_package(root / "mission-package.json", mode="sim", allow_assumed_sim=True)
    capabilities = frozenset(package.document["capabilities"])
    plan = validate_mission_plan(result["raw_planner_output"], capabilities=capabilities)
    view, adapted, radius = one_waypoint_primitives_input(plan, package, capabilities)
    steps = _build(replace(view, plan=adapted), dem, profile, mission_radius_m=radius)
    scope = result["contingency_scenario"]["motion_scope_sha256"]
    rt = bounds(scope)
    if variant == "archive":
        rt = replace(rt, px4_commit=ARCHIVE_COMMIT, image_id=iowa.IMAGE_ID)
    elif variant == "default_descent":
        rt = replace(rt, params={**rt.params, "RTL_DESCEND_ALT": 30})
        assert not rt.valid_for(scope, profile)
        # Independently exercise the legacy low-altitude geometry veto.
        monkeypatch.setitem(_REQUIRED, "RTL_DESCEND_ALT", 30)
    elif variant == "terrain_and_energy":
        # Preserve the archived 68 m hypothesis as a geometry regression.
        rt = replace(rt, params={**rt.params, "RTL_DESCEND_ALT": 68, "RTL_RETURN_ALT": 75})
        monkeypatch.setitem(_REQUIRED, "RTL_DESCEND_ALT", 68)
    elif variant == "admitted_hypothesis":
        # Counterfactual completion bounds for this CPU-only replay.
        rt = replace(rt, phase_time_s=(15, 50, 25, 20, 5, 32, 95))
    data = package.document["x1_live_evidence"]
    evidence = LiveContinueEvidence(package, profile, dem, steps, scope=scope,
                                   assumed_sim=assumed(rt, data["assumed_bounds_sha256"]))
    evidence.fw_rtl_ready = evidence.fw_rtl is not None  # Test-only firmware attestation.
    scenario = CollectorScenario((), clearance=None, terrain=dem, motion_evidence=evidence)
    scenario.target_peer = (1, 1)
    scenario.native = SimpleNamespace(confirmed=lambda action: action == "loiter")
    now, trace = [0], []
    tick = DecisionTick(package, profile, recorded_decisions([r["reply"] for r in requests]), None, trace.append,
                        run_id="case-c", plan_digest=result["compiled_mission_sha256"], cpu_replay=True,
                        now_ms=lambda: now[0], build_proof=evidence.build_proof)
    try:
        for i, (request, receipt, rows) in enumerate(zip(requests, receipts, telemetry, strict=True)):
            now_ns = receipt["motion_proof"]["frame_id"]
            local = rows["LOCAL_POSITION_NED"]
            boot = local["time_boot_ms"]
            stamps = {k: row["receive_monotonic_ns"] for k, row in rows.items()}
            battery_boot = boot - round((now_ns - stamps["BATTERY_STATUS"]) / MS)
            mav = MavlinkState(messages=rows, received_monotonic_ns=stamps,
                               received_vehicle_ms={"BATTERY_STATUS": battery_boot}, vehicle_time_boot_ms=boot)
            if i:
                scenario.hold = {"hold_ref": HOLD}
            point, energy, time = scenario._motion_data(1, boot, mav, now_ns=now_ns)
            assert point is not None and energy is not None and time is not None
            frame = RuntimeFrame(now_ns, now_ns // MS, package.mission_id, 1, 0, False, True, "fw",
                                 rows["GLOBAL_POSITION_INT"]["relative_alt"] / 1000, rows["VFR_HUD"]["airspeed"],
                                 True, evidence.hold_sites(1, "fw"), position_ne_amsl=point, active_motion_index=1,
                                 remaining_energy_wh=energy, remaining_time_s=time, native_hold_active=bool(i))
            event, = request["request"]["state"]["events"]
            now[0] = event["t_ms"]
            tick.tick(frame, events=[(event["type"], event["source"])])
            tick.future.result(timeout=2)
            now[0] = frame.t_ms
            tick.tick(frame)
        hold_site = evidence.holds[HOLD].site
        commands = [("loiter_at", (hold_site.lat_deg, hold_site.lon_deg), hold_site.radius_m,
                     hold_site.alt_amsl_m, hold_site.tangential_speed_mps)]
        proof = tick.last_admission[1]
        if variant == "admitted_hypothesis":
            assert tick.recent_receipts[-1]["status"] == "sent_unconfirmed", str(tick.last_admission[:2])
            commands.append(("rtl",))
        elif variant == "pinned_energy":
            assert tick.recent_receipts[-1]["reason"] == "energy_or_reserve_limit"
            assert proof.min_transition_agl_m >= 40 and proof.terrain_margin_m > 0
            assert proof.required_energy_wh + 14.8 > proof.available_energy_wh
        else:
            assert tick.recent_receipts[-1]["reason"] == "unverified_clearance"
        if variant == "archive":
            assert proof.required_energy_wh is None
        elif variant == "default_descent":
            assert proof.min_transition_agl_m < 40
        elif variant == "terrain_and_energy":
            assert proof.terrain_margin_m < 0
            assert proof.required_energy_wh + 14.8 > proof.available_energy_wh
        assert tick.replay_commands == commands
    finally:
        tick.close()
