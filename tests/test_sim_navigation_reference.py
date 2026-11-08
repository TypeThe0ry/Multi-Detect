"""Original-byte coordinate fixtures only; no SIM process, GPU or flight."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from gimbal_collision_fixtures import gimbal_sources
from test_sim_planning_inputs import _attestation, _context

from multidetect.autonomy.contracts import GeoPoint
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import AutonomyContractError, canonical_sha256
from multidetect.autonomy.sim_navigation_reference import (
    CAPTURE_CONTRACT,
    PARAMETERS,
    _qrotate,
    audit_sim_navigation_reference,
    audit_sim_navigation_window,
    decode_navigation_packet,
    parse_local_position_listener,
)
from multidetect.autonomy.sim_static_route_clearance import ExplicitSimOriginBinding
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3


class Sender:
    def __init__(self, ignored, srcSystem=1, srcComponent=1):
        self.system, self.component, self.seq = srcSystem, srcComponent, 0


class OriginalMessage:
    """CPU common-dialect encoder, independent of the consumer decoder."""

    def __init__(self, kind, message_id, extra, layout, fields, values):
        self.kind, self.message_id, self.extra = kind, message_id, extra
        self.layout, self.names, self.values = layout, fields.split(), values

    def pack(self, sender):
        payload = struct.pack(self.layout, *self.values)
        self.system, self.component, self.sequence = sender.system, sender.component, sender.seq
        header = bytes(
            [len(payload), 0, 0, self.sequence, self.system, self.component]
        ) + self.message_id.to_bytes(3, "little")
        crc = 65535
        for item in header + payload + bytes([self.extra]):
            crc ^= item
            for _ in range(8):
                crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
        self.payload = b"\xfd" + header + payload + crc.to_bytes(2, "little")
        self.fields = dict(zip(self.names, struct.unpack(self.layout, payload), strict=True))
        return self.payload

    def get_srcSystem(self):
        return self.system

    def get_srcComponent(self):
        return self.component

    def get_seq(self):
        return self.sequence

    def get_type(self):
        return self.kind

    def get_msgbuf(self):
        return self.payload

    def to_dict(self):
        return {"mavpackettype": self.kind, **self.fields}


def make_message(kind, values):
    definitions = {
        "HOME_POSITION": (
            242,
            104,
            "<iii10fQ",
            "latitude longitude altitude x y z q1 q2 q3 q4 "
            "approach_x approach_y approach_z time_usec",
        ),
        "GPS_GLOBAL_ORIGIN": (49, 39, "<iiiQ", "latitude longitude altitude time_usec"),
        "LOCAL_POSITION_NED": (32, 185, "<I6f", "time_boot_ms x y z vx vy vz"),
        "GLOBAL_POSITION_INT": (
            33,
            104,
            "<I4i3hH",
            "time_boot_ms lat lon alt relative_alt vx vy vz hdg",
        ),
        "ATTITUDE": (30, 39, "<I6f", "time_boot_ms roll pitch yaw rollspeed pitchspeed yawspeed"),
        "ATTITUDE_QUATERNION": (
            31,
            246,
            "<I11f",
            "time_boot_ms q1 q2 q3 q4 rollspeed pitchspeed yawspeed "
            "repr_offset_q1 repr_offset_q2 repr_offset_q3 repr_offset_q4",
        ),
    }
    return OriginalMessage(kind, *definitions[kind], values)


mav = SimpleNamespace(MAVLink=Sender)


def binding(root, path, raw=None, pointer=None):
    if raw is not None:
        (root / path).write_bytes(raw)
    raw = (root / path).read_bytes()
    return EvidenceBinding(path, len(raw), hashlib.sha256(raw).hexdigest(), "CPU-fixture", pointer)


def native_binding(root, source):
    return {"path": (root / source.path).as_posix(), "bytes": source.bytes, "sha256": source.sha256}


def document(root, path, value):
    return binding(root, path, json.dumps(value, allow_nan=False).encode(), "")


def listener(timestamp):
    fields = {
        "timestamp": timestamp,
        "timestamp_sample": timestamp,
        "ref_timestamp": 8000000,
        "x": 0,
        "y": 0,
        "z": 0,
        "vx": 0,
        "vy": 0,
        "vz": 0,
        "ref_lat": 1.3521,
        "ref_lon": 103.8198,
        "ref_alt": 18.24,
        "eph": 0.15,
        "epv": 0.16,
        "heading": math.pi / 2,
        **{
            k: 1
            for k in (
                "xy_reset_counter",
                "z_reset_counter",
                "vxy_reset_counter",
                "vz_reset_counter",
                "heading_reset_counter",
            )
        },
        **{
            k: True
            for k in ("xy_valid", "z_valid", "v_xy_valid", "v_z_valid", "xy_global", "z_global")
        },
        "heading_good_for_control": False,
        "dead_reckoning": False,
    }
    return (
        "TOPIC: vehicle_local_position\n"
        + "\n".join(f"    {k}: {v}" for k, v in fields.items())
        + "\n"
    ).encode()


def message_set(boot_ms):
    return [
        make_message(
            "HOME_POSITION", (13521000, 1038198000, 18240, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 8000000)
        ),
        make_message("GPS_GLOBAL_ORIGIN", (13521000, 1038198000, 18240, 8000000)),
        make_message("LOCAL_POSITION_NED", (boot_ms, 0, 0, 0, 0, 0, 0)),
        make_message(
            "GLOBAL_POSITION_INT", (boot_ms, 13521000, 1038198000, 18240, 0, 0, 0, 0, 9000)
        ),
        make_message("ATTITUDE", (boot_ms, 0, 0, math.pi / 2, 0, 0, 0)),
        make_message(
            "ATTITUDE_QUATERNION",
            (boot_ms, math.sqrt(0.5), 0, 0, math.sqrt(0.5), 0, 0, 0, 0, 0, 0, 0),
        ),
    ]


@pytest.fixture
def fixture(tmp_path, request):
    root = tmp_path
    world = binding(
        root,
        "world.sdf",
        b"""<sdf version="1.9"><world name="default">
<spherical_coordinates><surface_model>EARTH_WGS84</surface_model>
<world_frame_orientation>ENU</world_frame_orientation><latitude_deg>1.3521</latitude_deg>
<longitude_deg>103.8198</longitude_deg><elevation>18</elevation></spherical_coordinates>
</world></sdf>""",
    )
    models = {}
    for name, text in {
        "x500_mono_cam": (
            '<include merge="true"><uri>model://x500</uri></include>'
            '<plugin name="gz::sim::systems::OdometryPublisher" '
            'filename="gz-sim-odometry-publisher-system"><dimensions>3</dimensions>'
            "<odom_publish_frequency>50</odom_publish_frequency></plugin>"
        ),
        "x500": '<include merge="true"><uri>model://x500_base</uri></include>',
        "x500_base": (
            '<pose>0 0 .24 0 0 0</pose><link name="base_link">'
            '<sensor name="imu_sensor" type="imu"><gz_frame_id>base_link</gz_frame_id></sensor>'
            '<sensor name="navsat_sensor" type="navsat">'
            "<gz_frame_id>base_link</gz_frame_id></sensor></link>"
        ),
    }.items():
        models[name] = binding(
            root,
            f"{name}.sdf",
            f'<sdf version="1.9"><model name="{name}">{text}</model></sdf>'.encode(),
        )
    capture = {
        "contract": CAPTURE_CONTRACT,
        "status": "origin-window-recorded",
        "error": None,
        "close_errors": [],
        "clock": {"session_id": "CPU-QPC"},
        "transaction_id": "CPU-transaction",
        "local_origin_id": "unresolved-CPU-origin",
        "session": {"container_id": "1" * 64},
        "plan": {
            "docker_plan": {
                "image": "fixture/px4@sha256:" + "2" * 64,
                "scenario_id": "fixture-scenario",
                "scenario_manifest_hash": "3" * 64,
            }
        },
        "profile": {
            "schema_version": 1,
            "airframe_profile_id": "px4_multirotor_gazebo_v1",
            "model_name": "x500_mono_cam",
            "vehicle_model": "px4_multirotor_standard",
        },
        "listener_snapshots": [],
        "camera_frames": [],
        "parameters_before": {},
        "parameters_after": {},
    }
    runtime = document(
        root,
        "runtime.json",
        {
            "scenario_id": "fixture-scenario",
            "vehicle": {"airframe_profile_id": "px4_multirotor_gazebo_v1"},
        },
    )
    capture["runtime_source"] = runtime.to_mapping()

    def native(name, role, raw, start, end, arguments):
        source = binding(root, f"{role}-{name}.bin", raw)
        return {
            "role": role,
            "name": name,
            "command": ["docker", "exec", "1" * 64, *arguments],
            "clock_id": "CPU-QPC",
            "container_id": "1" * 64,
            "started_at_s": start,
            "finished_at_s": end,
            "native_exit_code": 0,
            "API_attempted": True,
            "API_returned": True,
            "error": None,
            "owned_process_exited": True,
            "reader_threads_exited": True,
            "complete_output_observed": True,
            "timeout": False,
            "streams": {
                "stdout": {
                    "original_bytes": native_binding(root, source),
                    "all_bytes_retained": True,
                    "EOF": True,
                    "error": None,
                    "observed_bytes": source.bytes,
                    "full_output_sha256": source.sha256,
                }
            },
        }

    args = ["/opt/px4-gazebo/bin/px4-listener", "vehicle_local_position", "-i", "0", "-n", "1"]
    capture["listener_snapshots"] = [
        native("listener", "before", listener(10000000), 100, 100.05, args),
        native("listener", "after", listener(10400000), 100.4, 100.45, args),
    ]
    for phase, start, end in (("before", 99.5, 99.9), ("after", 100.46, 100.49)):
        for name, expected in PARAMETERS.items():
            value = expected if expected is not None else (1 if name == "SIM_GZ_EN_ODOM" else 0)
            row = native(
                name,
                phase,
                f"{value}\n".encode(),
                start,
                end,
                ["/opt/px4-gazebo/bin/px4-param", "show", "-q", name],
            )
            row["actual_quiet_parameter_value"] = value
            capture[f"parameters_{phase}"][name] = row
    wire = struct.Struct("<8sHHIQIIIIQQQ22dI32s")
    for i, stamp in enumerate((10100000000, 10300000000)):
        host_ns = int((100.15 + i * 0.2) * 1e9)
        payload = b"\x00\x01\x02"
        raw = wire.pack(
            b"MDGZSIM3",
            3,
            wire.size,
            i,
            stamp,
            1,
            1,
            3,
            3,
            stamp,
            stamp,
            stamp,
            1.3521,
            103.8198,
            18.24,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            0,
            0,
            0,
            *([0] * 9),
            65543,
            bytes(32),
        )
        rgb = binding(root, f"rgb-{i}.bin", payload)
        camera = document(
            root,
            f"camera-{i}.json",
            {
                "host_return_stamp": {"session_id": "CPU-QPC", "value_ns": host_ns},
                "provenance": {
                    "wire_unsigned_header_hex": raw.hex(),
                    "record_sha256": hashlib.sha256(raw + payload).hexdigest(),
                },
                "persisted_payloads": {"rgb-bgr24": native_binding(root, rgb)},
                "rgb_bgr24_sha256": rgb.sha256,
            },
        )
        capture["camera_frames"].append(
            {
                "window_index": 0,
                "frame_index": i,
                "camera": native_binding(root, camera),
                "host_return_at_s": host_ns / 1e9,
                "source_sensor_stamp_ns": stamp,
            }
        )
    timer = [100.39]
    cap = TeacherTelemetryCaptureV3(
        root / "journals",
        clock=lambda: timer[0],
        clock_id="CPU-QPC",
        transaction_id="CPU-transaction",
        local_origin_id="unresolved-CPU-origin",
    )
    sender = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    rows = []
    if getattr(request, "param", None) == "pre-window-home-change":
        earlier = make_message(
            "HOME_POSITION", (13521000, 1038198000, 18900, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 7500000)
        )
        stable = message_set(10000)[0]
        for message, received in ((earlier, 99.1), (stable, 99.8)):
            message.pack(sender)
            sender.seq += 1
            rows.extend(cap.append_batch([(message, received)]))
    for i, boot in enumerate((10100, 10300)):
        samples = []
        for j, message in enumerate(message_set(boot)):
            if getattr(request, "param", None) == "within-window-home-change" and i == 1 and j == 0:
                values = list(message.values)
                values[2] += 50
                values[-1] = 10200000
                message = make_message("HOME_POSITION", tuple(values))
            message.pack(sender)
            sender.seq += 1
            samples.append((message, 100.16 + i * 0.2 + j * 0.001))
        rows.extend(cap.append_batch(samples))
    capture["selected_telemetry"] = {
        kind: [r for r in rows if r["message"]["message_type"] == kind]
        for kind in (
            "HOME_POSITION",
            "GPS_GLOBAL_ORIGIN",
            "LOCAL_POSITION_NED",
            "GLOBAL_POSITION_INT",
            "ATTITUDE",
            "ATTITUDE_QUATERNION",
        )
    }
    capture["snapshot_rows"] = cap.materialize_snapshot(
        ["HOME_POSITION", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT"],
        root / "snapshot",
        now_s=100.39,
    )
    capture["snapshot_manifest"] = native_binding(
        root, binding(root, "snapshot/snapshot-manifest.json")
    )
    timer[0] = 100.5
    capture["raw_telemetry"] = cap.seal()
    capture_source = document(root, "origin-window.json", capture)
    context = _context(
        captured_at_s=100.46,
        airframe_profile_hash=canonical_sha256(capture["profile"]),
        aircraft_position=GeoPoint(1.3521, 103.8198, 0, "relative_home"),
        home_position=GeoPoint(1.3521, 103.8198),
    )
    return {
        "artifact_root": root,
        "capture_source": capture_source,
        "world_source": world,
        "model_sources": models,
        "observed_context": context,
        "runtime_attestation": _attestation(),
        "now_s": 100.51,
        "clock_id": "CPU-QPC",
    }, capture


def update_capture(fixture, mutate):
    args, capture = fixture
    mutate(capture)
    args["capture_source"] = document(args["artifact_root"], "origin-window.json", capture)
    return args


def test_current_source_qualification_preserves_raw_context(fixture):
    args, _ = fixture
    original = args["observed_context"].canonical_hash
    result = audit_sim_navigation_reference(**args)
    report = result.to_mapping()
    assert result.origin_qualified and report["window_coordinates_verified"]
    assert report["exact_synchronization_claimed"] is False
    assert args["observed_context"].canonical_hash == original
    assert not any(report["claims"].values())
    assert report["actual_parameters"]["SIM_GZ_EN_ODOM"] == 1
    derived = ExplicitSimOriginBinding.from_mapping(result.derived_origin).to_mapping()
    assert derived["verified_home_enu_m"] == pytest.approx([0, 0, 0.24])
    assert derived["expires_at_s"] <= 102.363


def test_gimbal_profile_uses_pinned_collision_closure_for_navigation_reference(fixture):
    args, _ = fixture
    root = args["artifact_root"]
    gimbal_bindings = gimbal_sources(root)
    runtime_source = document(
        root,
        "runtime.json",
        {
            "scenario_id": "fixture-scenario",
            "vehicle": {"airframe_profile_id": "x500_gimbal"},
        },
    )
    runtime_binding = native_binding(root, runtime_source)
    args.pop("model_sources")
    args.update(
        gimbal_sources=gimbal_bindings,
        observed_context=None,
        runtime_attestation=None,
    )

    def use_gimbal(capture):
        capture["profile"]["airframe_profile_id"] = "x500_gimbal"
        capture["profile"]["model_name"] = "x500_gimbal"
        capture["runtime_source"] = runtime_binding

    args = update_capture((args, _), use_gimbal)
    result = audit_sim_navigation_reference(**args).to_mapping()
    assert result["model_to_navigation_body_offset_flu_m"] == pytest.approx([0, 0, 0.24])
    assert result["gimbal_collision_geometry"]["collision_count"] == 10
    assert len(result["gimbal_collision_geometry"]["source_expanded_collision_definitions"]) == 10
    assert any(
        collision["collision_id"] == "camera_link/cgo3_camera_collision"
        and collision["kind"] == "sphere"
        and collision["source_sha256"] == gimbal_bindings["gimbal_model.sdf"].sha256
        for collision in result["gimbal_collision_geometry"][
            "source_expanded_collision_definitions"
        ]
    )
    assert result["gimbal_collision_geometry"]["source_joint_limits_rad"]["cgo3_camera_joint"] == {
        "lower_rad": -2.35619,
        "upper_rad": 0.7854,
    }
    assert {source["path"] for source in result["source_evidence"]} >= {
        binding.path for binding in gimbal_bindings.values()
    }


def test_sealed_historical_window_never_renews_origin(fixture):
    args, _ = fixture
    result = audit_sim_navigation_reference(**{**args, "now_s": 110})
    assert result.to_mapping()["replay_verified"]
    assert not result.origin_qualified and result.derived_origin is None
    assert result.to_mapping()["blocking_gates"] == [
        "original-observation-or-listener-window-expired"
    ]


def test_raw_replay_does_not_invent_context_or_telemetry_admission(fixture):
    args, _ = fixture
    result = audit_sim_navigation_window(
        **{k: v for k, v in args.items() if k not in {"observed_context", "runtime_attestation"}}
    )
    assert result.to_mapping()["window_coordinates_verified"]
    assert result.to_mapping()["observed_context_sha256"] is None
    assert result.to_mapping()["runtime_attestation_sha256"] is None
    assert not result.origin_qualified and result.derived_origin is None


@pytest.mark.parametrize("fixture", ["pre-window-home-change"], indirect=True)
def test_earlier_home_settling_is_retained_but_not_a_co_window_reset(fixture):
    args, _ = fixture
    result = audit_sim_navigation_reference(**args)
    assert result.origin_qualified
    scope = result.to_mapping()["reference_scope"]["HOME_POSITION"]
    assert scope["window_records_checked"] == 2
    assert scope["out_of_window_identity_change_records"] == 1
    assert scope["out_of_window_changed_record_indices"] == [0]


@pytest.mark.parametrize("fixture", ["within-window-home-change"], indirect=True)
def test_genuine_co_window_home_change_is_not_masked(fixture):
    args, _ = fixture
    with pytest.raises(AutonomyContractError, match="identity changed within window"):
        audit_sim_navigation_reference(**args)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(contract="multidetect.teacher-SIM-navref-late-probe-v1"),
        lambda c: c["listener_snapshots"][1].update(clock_id="other-clock"),
        lambda c: c["listener_snapshots"][1].update(native_exit_code=1),
        lambda c: c["listener_snapshots"][1].update(complete_output_observed=False),
        lambda c: c["listener_snapshots"][1].update(started_at_s=100.2),
        lambda c: c["listener_snapshots"][1]["command"].append("--other"),
        lambda c: c["parameters_after"]["EKF2_EV_CTRL"].update(actual_quiet_parameter_value=1),
        lambda c: c["camera_frames"][0].update(source_sensor_stamp_ns=10),
        lambda c: c["camera_frames"][0].update(host_return_at_s=101),
        lambda c: c["selected_telemetry"]["GLOBAL_POSITION_INT"][0]["message"].update(alt=100),
        lambda c: c["selected_telemetry"]["GLOBAL_POSITION_INT"][0].update(clock_id="foreign"),
        lambda c: c["selected_telemetry"]["GLOBAL_POSITION_INT"][0]["raw_record"].update(
            offset=10**9
        ),
        lambda c: c["selected_telemetry"]["LOCAL_POSITION_NED"].clear(),
    ],
)
def test_source_projection_identity_window_refusal(fixture, mutate):
    args = update_capture(fixture, mutate)
    with pytest.raises(AutonomyContractError):
        audit_sim_navigation_reference(**args)


@pytest.mark.parametrize(
    "replacement",
    [
        b"    xy_reset_counter: 2",
        b"    ref_timestamp: 8000001",
        b"    xy_valid: False",
        b"    dead_reckoning: True",
        b"    z: 1_0",
    ],
)
def test_listener_reset_unknown_reference_refuses(fixture, replacement):
    args, capture = fixture
    row = capture["listener_snapshots"][1]
    path = Path(row["streams"]["stdout"]["original_bytes"]["path"])
    raw = path.read_bytes()
    name = replacement.strip().split(b":")[0]
    raw = b"\n".join(
        replacement if line.strip().startswith(name + b":") else line for line in raw.split(b"\n")
    )
    bound = binding(args["artifact_root"], path.relative_to(args["artifact_root"]).as_posix(), raw)
    row["streams"]["stdout"].update(
        original_bytes=native_binding(args["artifact_root"], bound),
        observed_bytes=bound.bytes,
        full_output_sha256=bound.sha256,
    )
    args["capture_source"] = document(args["artifact_root"], "origin-window.json", capture)
    with pytest.raises(AutonomyContractError):
        audit_sim_navigation_reference(**args)


@pytest.mark.parametrize(
    "path",
    [
        "world.sdf",
        "x500_base.sdf",
        "rgb-0.bin",
        "camera-0.json",
        "after-listener.bin",
        "journals/telemetry.raw.bin",
    ],
)
def test_bound_original_source_tamper_denied(fixture, path):
    args, capture = fixture
    target = args["artifact_root"] / path
    if not target.exists():
        target = Path(capture["raw_telemetry"]["journal_sources"]["raw"]["path"])
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(AutonomyContractError):
        audit_sim_navigation_reference(**args)


def test_context_snapshot_not_newer_stream_tail(fixture):
    args, _ = fixture
    args["observed_context"] = replace(
        args["observed_context"], aircraft_position=GeoPoint(1.3521, 103.8198, 0.1, "relative_home")
    )
    with pytest.raises(AutonomyContractError, match="specific original snapshot"):
        audit_sim_navigation_reference(**args)


@pytest.mark.parametrize(
    "index,kind",
    enumerate(
        (
            "HOME_POSITION",
            "GPS_GLOBAL_ORIGIN",
            "LOCAL_POSITION_NED",
            "GLOBAL_POSITION_INT",
            "ATTITUDE",
            "ATTITUDE_QUATERNION",
        )
    ),
)
def test_independent_packet_layout_crc_and_boot_units(index, kind):
    message = message_set(12345)[index]
    packet = bytes(message.pack(mav.MAVLink(None, srcSystem=1, srcComponent=1)))
    decoded = decode_navigation_packet(packet, kind)
    if "time_boot_ms" in decoded:
        assert decoded["time_boot_ms"] == 12345
    else:
        assert decoded["time_usec"] == 8000000
    corrupt = bytearray(packet)
    corrupt[-1] ^= 1
    with pytest.raises(AutonomyContractError, match="CRC"):
        decode_navigation_packet(bytes(corrupt), kind)


@pytest.mark.parametrize(
    "change", [lambda p: p + b"\0", lambda p: b"\0" + p[1:], lambda p: p[:2] + b"\1" + p[3:]]
)
def test_extra_packet_unknown_magic_signed_flag_denied(change):
    packet = bytes(message_set(12345)[2].pack(mav.MAVLink(None, srcSystem=1, srcComponent=1)))
    with pytest.raises(AutonomyContractError):
        decode_navigation_packet(change(packet), "LOCAL_POSITION_NED")


def test_foreign_original_source_packet_denied():
    packet = bytes(message_set(12345)[2].pack(mav.MAVLink(None, srcSystem=2, srcComponent=1)))
    with pytest.raises(AutonomyContractError, match="source"):
        decode_navigation_packet(packet, "LOCAL_POSITION_NED")


@pytest.mark.parametrize("invalid", [b"1_0", "１".encode(), b"nan", b"inf", b"1e999"])
def test_listener_numeric_lexical_refuses(invalid):
    raw = listener(10000000).replace(b"    x: 0", b"    x: " + invalid)
    with pytest.raises(AutonomyContractError):
        parse_local_position_listener(raw)


def test_actual_retained_late_listener_keeps_old_reference_and_ground_heading():
    path = (
        Path(__file__).resolve().parents[1]
        / "artifacts/diagnostics/onboard-teacher-origin-probe-20261008-r1"
        / "navref-native/native-17.stdout.bin"
    )
    if not path.is_file():
        pytest.skip("optional retained readonly late diagnostic not present")
    parsed = parse_local_position_listener(path.read_bytes())
    assert parsed["ref_timestamp"] < parsed["timestamp_sample"]
    assert parsed["heading_good_for_control"] is False
    assert parsed["xy_reset_counter"] == 4


def test_model_lever_arm_rotates_with_real_quaternion():
    assert _qrotate((0, math.sqrt(0.5), 0, math.sqrt(0.5)), (0, 0, 0.24)) == pytest.approx(
        (0.24, 0, 0)
    )
    with pytest.raises(AutonomyContractError):
        _qrotate((0, 0, 0, 2), (0, 0, 0.24))
