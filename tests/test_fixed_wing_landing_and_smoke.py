import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile
from scripts import collect_sim_teacher_episodes as collector
from scripts import smoke_airframe_flight as smoke
from scripts.smoke_airframe_flight import evaluate_episode


class _State:
    def __init__(self):
        self.messages = {
            "GLOBAL_POSITION_INT": {"lat": 135000000, "lon": 1038000000},
            "LOCAL_POSITION_NED": {"z": -100.0},
            "ATTITUDE": {"yaw": 0.0},
            # Home 0.5 km away, runway heading east (yaw 90 deg).
            "HOME_POSITION": {
                "latitude": 135045000,
                "longitude": 1038000000,
                "q": [0.7071067811865476, 0.0, 0.0, 0.7071067811865476],
            },
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
            return {
                "custom_mode": (4 << 16) | (4 << 24)
                if self.mode_polls > 1
                else (4 << 16) | (5 << 24)
            }
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
        "MISSION_COUNT",
        "MISSION_ITEM_INT",
        "MISSION_ITEM_INT",
        "MISSION_ITEM_INT",
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

    executor = __import__(
        "multidetect.sim_teacher.fw_executor", fromlist=["FixedWingVtolExecutor"]
    ).FixedWingVtolExecutor(
        Vehicle(),
        airframe="standard_vtol",
        landing_method="vtol",
        wait_for_state=lambda phase, timeout: True,
    )
    result = executor.land()
    assert result.status == "success"
    assert calls == [("transition", "MC"), ("land", "vtol")]


def test_vtol_takeoff_syncs_guided_translation_origin_to_measured_fw_state():
    from multidetect.sim_teacher.fw_executor import FixedWingVtolExecutor
    from multidetect.sim_teacher.primitives import ActionPrimitive

    state = {"position": (0.0, 0.0, 0.0), "yaw": 0.0}
    reposition_calls = []

    class Vehicle:
        def arm(self):
            return SimpleNamespace(status="success", ok=True)

        def fw_takeoff(self, *_args, **_kwargs):
            state.update(position=(2.0, 3.0, -20.0), yaw=5.0)
            return SimpleNamespace(status="accepted", ok=True)

        def vtol_transition(self, _target, **_kwargs):
            state.update(position=(12.0, 23.0, -42.0), yaw=90.0)
            return SimpleNamespace(status="accepted", ok=True)

        def reposition(self, point, altitude, speed, **_kwargs):
            reposition_calls.append((point, altitude, speed))
            return SimpleNamespace(status="accepted", ok=True)

    executor = FixedWingVtolExecutor(
        Vehicle(),
        airframe="vtol",
        position=lambda: state["position"],
        yaw_deg=lambda: state["yaw"],
        wait_for_state=lambda _phase, _timeout: True,
    )

    assert executor.arm_and_takeoff(40.0).status == "success"
    assert executor._commanded == pytest.approx((12.0, 23.0, -40.0))
    assert executor._commanded_yaw == pytest.approx(90.0)

    result = executor.execute(ActionPrimitive.translate(35.0, 0.0))

    assert result.status == "success"
    assert reposition_calls[0][0] == pytest.approx((12.0, 58.0))
    assert reposition_calls[0][1] == pytest.approx(40.0)


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
    (tmp_path / "mavlink.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    step = {
        "action": {"kind": "translate"},
        "outcome": {
            "commanded_delta_ned": [10.0, 0.0, 0.0],
            "achieved_delta_ned": [9.0, 0.0, 0.0],
        },
    }
    (tmp_path / "steps.jsonl").write_text(json.dumps(step), encoding="utf-8")
    result = evaluate_episode(tmp_path, profile, 5.0)
    assert result["takeoff_success"] is True
    assert result["loiter_airspeed_in_range"] is True
    assert result["rtl_entered"] is True
    assert result["landed"] is True
    assert result["reposition_error_m"] == [1.0]
    assert result["max_distance_from_origin_m"] == 500.0 and result["stayed_in_area"] is True


def test_smoke_takeoff_uses_measured_altitude_not_commanded_altitude(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/advanced_plane.json"))
    (tmp_path / "episode.json").write_text(
        json.dumps({"takeoff_altitude_m": 5.0}), encoding="utf-8"
    )
    assert evaluate_episode(tmp_path, profile, 5.0)["takeoff_success"] is False


def test_smoke_requires_an_episode_artifact_even_when_collector_exits_zero(tmp_path, monkeypatch, capsys):
    class _Child:
        returncode = 0

        def communicate(self):
            return "", ""

    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: _Child())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke_airframe_flight.py",
            "--airframe",
            "delta_tailsitter_mono_cam",
            "--out",
            str(tmp_path),
        ],
    )

    assert smoke.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "fail"
    assert result["judgement"]["failure_reason"] == "collector produced no episode.json artifact"
    assert result["judgement"]["failure_class"] == "flight"


def test_vtol_transition_requires_fw_then_later_mc(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/delta_tailsitter.json"))

    def judge(states):
        rows = [
            {"message_type": "EXTENDED_SYS_STATE", "vtol_state": state, "landed_state": 2}
            for state in states
        ]
        (tmp_path / "episode.json").write_text("{}", encoding="utf-8")
        (tmp_path / "mavlink.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
        )
        (tmp_path / "steps.jsonl").write_text("", encoding="utf-8")
        return evaluate_episode(tmp_path, profile, 5.0)["transition_success"]

    assert judge([3, 4]) is False
    assert judge([3, 4, 2, 3]) is True


def test_smoke_reposition_must_meet_plan_tolerance(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/advanced_plane.json"))
    (tmp_path / "steps.jsonl").write_text(
        json.dumps(
            {
                "action": {"kind": "translate"},
                "outcome": {
                    "commanded_delta_ned": [10.0, 0.0, 0.0],
                    "achieved_delta_ned": [8.0, 0.0, 0.0],
                },
            }
        ),
        encoding="utf-8",
    )
    assert evaluate_episode(tmp_path, profile, 5.0, 1.0)["reposition_success"] is False
    assert evaluate_episode(tmp_path, profile, 2.0, 2.0)["reposition_success"] is True


def test_smoke_evaluation_takes_rtl_from_px4_statustext(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/advanced_plane.json"))
    rows = [
        {"message_type": "STATUSTEXT", "text": "RTL: start return at 101 m (100 m above destinatio"}
    ]
    (tmp_path / "mavlink.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    assert evaluate_episode(tmp_path, profile, 5.0)["rtl_entered"] is True


def test_smoke_evaluation_needs_touchdown_after_flight_and_a_real_rtl(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/advanced_plane.json"))
    rows = [
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 1, "vtol_state": 0},
        {"message_type": "LOCAL_POSITION_NED", "x": 0.0, "y": 4.0e6, "z": -30.0},
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 2, "vtol_state": 0},
        {"message_type": "HEARTBEAT", "custom_mode": (4 << 16) | (3 << 24)},
    ]
    (tmp_path / "mavlink.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
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


def test_delta_smoke_policy_has_long_orbit_and_keeps_generic_route_unchanged():
    strict = collector._SmokeFlightPolicy(strict_delta_smoke=True)
    strict_actions = [strict.next_action() for _ in range(3)]
    assert [action.kind for action in strict_actions] == ["translate", "hold", "stop"]
    assert strict_actions[1].seconds == collector.DELTA_SMOKE_FW_MIN_DURATION_S == 120.0

    generic = collector._SmokeFlightPolicy()
    generic_actions = [generic.next_action() for _ in range(7)]
    assert [action.kind for action in generic_actions] == [
        "translate",
        "hold",
        "translate",
        "turn",
        "translate",
        "hold",
        "stop",
    ]


def test_delta_smoke_reposition_waits_for_measured_arrival_in_sim_time(monkeypatch):
    class State:
        def __init__(self):
            self.messages = {
                "LOCAL_POSITION_NED": {"x": 0.0, "y": 0.0, "z": -5.0, "time_boot_ms": 0},
                "EXTENDED_SYS_STATE": {"vtol_state": 4},
            }

        def get(self, name):
            return self.messages.get(name)

    class Reader:
        def __init__(self):
            self.state = State()

        def raise_if_failed(self):
            return None

    reader = Reader()

    def advance_sim(_seconds):
        position = reader.state.messages["LOCAL_POSITION_NED"]
        position["time_boot_ms"] += 1000
        position["x"] = min(35.0, position["x"] + 7.0)

    monkeypatch.setattr(collector.time, "sleep", advance_sim)
    result = collector._wait_smoke_reposition(
        (35.0, 0.0, -5.0), reader, category="vtol", timeout_s=5.0
    )

    assert result["waited"] == "arrived"
    assert result["distance_m"] <= collector.MOTION_ARRIVAL_TOLERANCE_M
    assert result["sim_elapsed_s"] == 4.0
    assert result["sim_clock_available"] is True


def test_delta_smoke_reposition_fails_closed_without_sim_clock():
    class State:
        def get(self, name):
            if name == "LOCAL_POSITION_NED":
                return {"x": 0.0, "y": 0.0, "z": -5.0}
            return {"vtol_state": 4} if name == "EXTENDED_SYS_STATE" else None

    reader = SimpleNamespace(state=State(), raise_if_failed=lambda: None)
    result = collector._wait_smoke_reposition(
        (35.0, 0.0, -5.0), reader, category="vtol", timeout_s=5.0
    )

    assert result["waited"] == "timeout"
    assert result["sim_clock_available"] is False


def _write_delta_fw_judgement_fixture(episode_dir: Path, saturated_indices=()):
    import numpy as np

    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "depth").mkdir()
    (episode_dir / "episode.json").write_text(
        json.dumps({"status": "complete", "takeoff_altitude_m": 5.0}), encoding="utf-8"
    )
    rows = [
        {"message_type": "fixed_wing_takeoff_complete", "sim_time_s": 1.0},
        {"message_type": "EXTENDED_SYS_STATE", "time_boot_ms": 8000, "vtol_state": 3},
        {"message_type": "SERVO_OUTPUT_RAW", "time_boot_ms": 8000, **{f"servo{i}_raw": 5 for i in range(1, 5)}},
        {
            "message_type": "delta_smoke_fw_sustain_start",
            "sim_time_s": 10.0,
            "commanded_altitude_m": 5.0,
        },
    ]
    q = _axis_quaternion((0, 1, 0), -90)
    for index in range(31):
        time_ms = (10 + index * 4) * 1000
        rows.extend(
            [
                {"message_type": "EXTENDED_SYS_STATE", "time_boot_ms": time_ms, "vtol_state": 4},
                {"message_type": "VFR_HUD", "time_boot_ms": time_ms, "airspeed": 15.0},
                {
                    "message_type": "ATTITUDE_QUATERNION",
                    "time_boot_ms": time_ms,
                    **{f"q{number}": value for number, value in enumerate(q, start=1)},
                },
                {
                    "message_type": "LOCAL_POSITION_NED",
                    "time_boot_ms": time_ms,
                    "x": 0.0,
                    "y": 0.0,
                    "z": -5.0,
                },
                {
                    "message_type": "SERVO_OUTPUT_RAW",
                    "time_boot_ms": time_ms,
                    **{
                        f"servo{motor}_raw": 5
                        if index in saturated_indices and motor == 1
                        else 500
                        for motor in range(1, 5)
                    },
                },
            ]
        )
    rows.extend(
        [
            {"message_type": "delta_smoke_fw_sustain_end", "sim_time_s": 132.0,
             "status": "complete", "continuous_fw": True, "fw_duration_s": 120.0,
             "orbit_signed_degrees": 400.0},
            {"message_type": "EXTENDED_SYS_STATE", "time_boot_ms": 133000, "vtol_state": 3},
            {"message_type": "SERVO_OUTPUT_RAW", "time_boot_ms": 133000, **{f"servo{i}_raw": 1100 for i in range(1, 5)}},
            {"message_type": "delta_smoke_post_transition_end", "sim_time_s": 140.0,
             "status": "complete", "climb_m": 5.0},
        ]
    )
    (episode_dir / "mavlink.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    steps = []
    pre_takeoff_name = "000000_500000000.npy"
    np.save(
        episode_dir / "depth" / pre_takeoff_name,
        np.full((10, 10), 0.5, dtype=np.float32),
    )
    for index in range(1, 6):
        stamp_ns = index * 1_000_000_000 + 1_000_000_000
        name = f"{index:06d}_{stamp_ns}.npy"
        depth = np.full((10, 10), 2.0, dtype=np.float32)
        np.save(episode_dir / "depth" / name, depth)
        steps.append(
            {
                "action": {"kind": "translate"},
                "ack": {
                    "smoke_reposition_wait": {
                        "waited": "arrived",
                        "distance_m": 7.0,
                    }
                },
                "obs_after": {
                    "depth_path": f"depth/{name}",
                    "frame_sensor_stamp_ns": stamp_ns,
                },
            }
        )
    (episode_dir / "steps.jsonl").write_text(
        "\n".join(json.dumps(row) for row in steps), encoding="utf-8"
    )


def _axis_quaternion(axis, angle_deg):
    import math

    angle = math.radians(angle_deg) / 2
    scale = math.sin(angle)
    return (math.cos(angle), *(component * scale for component in axis))


def test_delta_smoke_metrics_use_only_continuous_fw_and_post_takeoff_depth(tmp_path):
    profile = AirframeProfile.from_json(Path("configs/airframes/delta_tailsitter.json"))
    _write_delta_fw_judgement_fixture(tmp_path)

    result = evaluate_episode(tmp_path, profile, 5.0, 8.0)

    assert result["fw_sustain_success"] is True
    assert result["fw_metrics_pass"] is True
    assert result["fw_airspeed_p5_mps"] == pytest.approx(15.0)
    assert result["fw_airspeed_p95_mps"] == pytest.approx(15.0)
    assert result["fw_bank_abs_p95_deg"] == pytest.approx(0.0, abs=1e-8)
    assert result["fw_pitch_p5_deg"] == pytest.approx(0.0, abs=1e-8)
    assert result["fw_height_error_p95_m"] == pytest.approx(0.0)
    assert result["fw_motor_saturation_fraction"] == 0.0
    assert result["fw_reposition_success"] is True
    assert result["post_transition_climb_success"] is True
    assert result["self_occlusion_frame_count"] == 5
    assert result["self_occlusion_fraction"] == pytest.approx(0.0)
    assert result["self_occlusion_success"] is True


def test_delta_smoke_fails_when_fw_motor_saturation_exceeds_fifteen_percent(tmp_path):
    profile = AirframeProfile.from_json(Path("configs/airframes/delta_tailsitter.json"))
    _write_delta_fw_judgement_fixture(tmp_path, saturated_indices=range(5))

    result = evaluate_episode(tmp_path, profile, 5.0, 8.0)

    assert result["fw_motor_saturation_fraction"] == pytest.approx(5 / 31)
    assert result["fw_metrics_pass"] is False


def test_command_ack_wait_survives_a_later_ack_for_another_command(monkeypatch):
    # RTL (176) and the VTOL back-transition (3000) are sent back to back; PX4's
    # RTL ACK lands right after the transition ACK and replaces the latest COMMAND_ACK.
    import time as _time

    state = _State()
    started_ns = _time.monotonic_ns()
    stamp = started_ns + 1
    monkeypatch.setattr(collector.time, "monotonic_ns", lambda: started_ns)
    state.command_acks = {3000: ({"command": 3000, "result": 0}, stamp)}
    state.messages["COMMAND_ACK"] = {"command": 176, "result": 0}
    state.received_monotonic_ns["COMMAND_ACK"] = stamp
    link = collector._ReaderAckLink(SimpleNamespace(), SimpleNamespace(state=state))
    assert link.wait_command_ack(3000, 0.2) == {"command": 3000, "result": 0}


def test_rtl_is_confirmed_from_a_fresh_heartbeat_before_landing():
    import time as _time

    state = _State()
    reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
    after = _time.monotonic_ns()
    # A stale RTL heartbeat from before the request does not count.
    state.messages["HEARTBEAT"] = {"custom_mode": (4 << 16) | (5 << 24)}
    state.received_monotonic_ns["HEARTBEAT"] = after - 1
    assert (
        collector._wait_px4_mode(reader, (4, 5), timeout_s=0.2, after_ns=after)["confirmed"]
        is False
    )
    state.received_monotonic_ns["HEARTBEAT"] = after + 1
    assert (
        collector._wait_px4_mode(reader, (4, 5), timeout_s=0.2, after_ns=after)["confirmed"] is True
    )


def test_loiter_airspeed_allows_a_brief_overshoot_but_not_a_stall(tmp_path: Path):
    profile = AirframeProfile.from_json(Path("configs/airframes/quadtailsitter.json"))  # 14..22 m/s

    def judge(speed):
        step = {"action": {"kind": "hold"}, "obs_after": {"airspeed_mps": speed}}
        (tmp_path / "steps.jsonl").write_text(json.dumps(step), encoding="utf-8")
        (tmp_path / "mavlink.jsonl").write_text("", encoding="utf-8")
        return evaluate_episode(tmp_path, profile, 5.0)["loiter_airspeed_in_range"]

    assert judge(22.1) is True  # post-transition overshoot
    assert judge(25.0) is False
    assert judge(13.5) is False  # below the minimum: stall margin stays strict
