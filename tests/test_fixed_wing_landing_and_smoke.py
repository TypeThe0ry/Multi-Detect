import json
from pathlib import Path
from types import SimpleNamespace

from multidetect.autonomy.airframe_profile import AirframeProfile
from scripts import collect_sim_teacher_episodes as collector
from scripts.smoke_airframe_flight import evaluate_episode


class _State:
    def __init__(self):
        self.messages = {
            "GLOBAL_POSITION_INT": {"lat": 135000000, "lon": 1038000000},
            "LOCAL_POSITION_NED": {"z": -100.0},
            "ATTITUDE": {"yaw": 0.0},
            # Home 0.5 km away, runway heading east (yaw 90 deg).
            "HOME_POSITION": {"latitude": 135045000, "longitude": 1038000000,
                              "q": [0.7071067811865476, 0.0, 0.0, 0.7071067811865476]},
        }
        self.received_monotonic_ns = {}

    def get(self, name):
        return self.messages.get(name)


class _Reader:
    def __init__(self, messages):
        self.state = _State()
        self.state.messages.update(messages)
        self._requests = iter([{"seq": 0}, {"seq": 1}, {"seq": 2}])
        self._ack = {"type": 0}

    def wait_message(self, names, timeout_s, *, after_ns=0):
        if "MISSION_REQUEST_INT" in names:
            try:
                return next(self._requests)
            except StopIteration:
                return None
        if "HEARTBEAT" in names:
            # PX4 rejects the first MISSION request while it validates the upload.
            self.mode_polls = getattr(self, "mode_polls", 0) + 1
            return {"custom_mode": (4 << 16) | (4 << 24) if self.mode_polls > 1 else (4 << 16) | (5 << 24)}
        return self._ack


class _Mav:
    def __init__(self):
        self.sent = []

    def mission_count_send(self, *args):
        self.sent.append(("MISSION_COUNT", args))

    def mission_item_int_send(self, *args):
        self.sent.append(("MISSION_ITEM_INT", args))


class _Link:
    target_system = 1
    target_component = 1

    def __init__(self, reader):
        self.mav = _Mav()
        self.reader = reader
        self.modes = []

    def wait_message(self, names, timeout_s, *, after_ns=0):
        return self.reader.wait_message(names, timeout_s, after_ns=after_ns)

    def set_mode(self, mode):
        self.modes.append(mode)


def test_fixed_wing_landing_uploads_approach_land_start_and_land():
    reader = _Reader({})
    link = _Link(reader)
    result = collector._upload_fixed_wing_landing_mission(
        link, reader, landing_method="runway", timeout_s=1.0
    )
    assert result.ok
    assert [entry[0] for entry in link.mav.sent] == [
        "MISSION_COUNT", "MISSION_ITEM_INT", "MISSION_ITEM_INT", "MISSION_ITEM_INT"
    ]
    commands = [entry[1][4] for entry in link.mav.sent[1:]]
    assert commands == [189, 31, 21]
    # Touchdown at home, approach loiter upwind (west of home for an east runway).
    land = link.mav.sent[3][1]
    loiter = link.mav.sent[2][1]
    assert (land[11], land[12]) == (135045000, 1038000000)
    assert loiter[11] == 135045000 and loiter[12] < 1038000000
    assert loiter[13] == collector.FIXED_WING_LANDING_APPROACH_ALT_M
    assert abs(result.runway_heading_deg - 90.0) < 1e-6
    # pymavlink only knows the bare PX4 mode name; retried until PX4 reports MISSION.
    assert link.modes == ["MISSION", "MISSION"]


def test_fixed_wing_landing_reports_request_timeout_and_unverified_parachute():
    reader = _Reader({})
    reader._requests = iter(())
    link = _Link(reader)
    timeout = collector._upload_fixed_wing_landing_mission(
        link, reader, landing_method="runway", timeout_s=0.05
    )
    assert timeout.status == "timeout"
    parachute = collector._upload_fixed_wing_landing_mission(
        link, reader, landing_method="parachute", timeout_s=0.05
    )
    assert parachute.status == "failed"
    assert "not verified" in parachute.detail


def test_vtol_transition_to_mc_uses_command_and_land_state():
    calls = []

    class Vehicle:
        def vtol_transition(self, target, **kwargs):
            calls.append(("transition", target))
            return SimpleNamespace(status="accepted", ok=True)

        def fw_land(self, method, **kwargs):
            calls.append(("land", method))
            return SimpleNamespace(status="accepted", ok=True)

    executor = __import__("multidetect.sim_teacher.fw_executor", fromlist=["FixedWingVtolExecutor"]).FixedWingVtolExecutor(
        Vehicle(), airframe="standard_vtol", landing_method="vtol",
        wait_for_state=lambda phase, timeout: True,
    )
    result = executor.land()
    assert result.status == "success"
    assert calls == [("transition", "MC"), ("land", "vtol")]


def test_smoke_evaluation_reads_recorded_artifacts(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/advanced_plane.json"))
    (tmp_path / "episode.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    rows = [
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 1, "vtol_state": 0},
        {"message_type": "LOCAL_POSITION_NED", "x": 300.0, "y": 400.0, "z": -8.0},
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 2, "vtol_state": 0},
        {"message_type": "VFR_HUD", "airspeed": 15.0},
        {"message_type": "HEARTBEAT", "custom_mode": (4 << 16) | (5 << 24)},
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 1, "vtol_state": 0},
    ]
    (tmp_path / "mavlink.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    step = {"action": {"kind": "translate"}, "outcome": {
        "commanded_delta_ned": [10.0, 0.0, 0.0],
        "achieved_delta_ned": [9.0, 0.0, 0.0],
    }}
    (tmp_path / "steps.jsonl").write_text(json.dumps(step), encoding="utf-8")
    result = evaluate_episode(tmp_path, profile, 5.0)
    assert result["takeoff_success"] is True
    assert result["loiter_airspeed_in_range"] is True
    assert result["rtl_entered"] is True
    assert result["landed"] is True
    assert result["reposition_error_m"] == [1.0]
    assert result["max_distance_from_origin_m"] == 500.0 and result["stayed_in_area"] is True


def test_smoke_evaluation_needs_touchdown_after_flight_and_a_real_rtl(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/advanced_plane.json"))
    rows = [
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 1, "vtol_state": 0},
        {"message_type": "LOCAL_POSITION_NED", "x": 0.0, "y": 4.0e6, "z": -30.0},
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 2, "vtol_state": 0},
        {"message_type": "HEARTBEAT", "custom_mode": (4 << 16) | (3 << 24)},
    ]
    (tmp_path / "mavlink.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    result = evaluate_episode(tmp_path, profile, 5.0)
    assert result["landed"] is False  # on the ground only before takeoff
    assert result["rtl_entered"] is False  # AUTO.LOITER is not RTL
    assert result["stayed_in_area"] is False


def test_fixed_wing_profiles_disable_terrain_abort_without_rangefinder():
    # The aircraft has no rangefinder; PX4's default terrain abort holds at
    # MIS_LND_ABRT_ALT instead of landing.
    for airframe in ("advanced_plane", "rc_cessna_runway", "rc_cessna_hand", "rc_cessna_catapult"):
        params = dict(collector._profile_landing_parameters(airframe))
        assert params == {"FW_LND_ABORT": "0", "FW_LND_USETER": "0"}
    assert collector._profile_landing_parameters("x500_mono_cam") == ()
    assert collector._profile_landing_parameters("quadtailsitter") == ()
