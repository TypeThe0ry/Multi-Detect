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
    assert commands == [16, 189, 21]
    # pymavlink only knows the bare PX4 mode name.
    assert link.modes == ["MISSION"]


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
        {"message_type": "LOCAL_POSITION_NED", "z": -8.0},
        {"message_type": "VFR_HUD", "airspeed": 15.0},
        {"message_type": "HEARTBEAT", "custom_mode": "AUTO.RTL"},
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
