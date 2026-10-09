"""CPU original-wire/open-writer fixtures; never a simulator or flight grant."""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_sim_navigation_reference import (
    OriginalMessage,
    Sender,
    binding,
    document,
    listener,
    message_set,
    native_binding,
)
from test_sim_navigation_reference import fixture as retained_fixture
from test_sim_planning_inputs import _model

from multidetect.autonomy.serialization import AutonomyContractError, canonical_json
from multidetect.autonomy.sim_planning_inputs import qualify_sim_planning_context
from multidetect.autonomy.teacher_live_navigation_checkpoint import (
    LiveTeacherNavigationCheckpoint,
    capture_teacher_committed_prefix,
    decode_context_packet,
    navigation_parameter_baseline_argv,
    navigation_reference_phase_argv,
    qualify_teacher_live_navigation_checkpoint,
    validate_live_teacher_navigation_checkpoint,
)
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3


class Clock:
    session_id = "CPU-QPC"

    def __init__(self):
        self.value = 100.39

    def monotonic(self):
        return self.value

    def utc_now(self):
        return datetime(2026, 10, 8, tzinfo=timezone.utc)


class ArrayMessage(OriginalMessage):
    def to_dict(self):
        value = super().to_dict()
        for name, size in (("voltages", 10), ("voltages_ext", 4)):
            value[name] = [value.pop(f"{name}{i}") for i in range(size)]
        return value


def health_messages(*, battery_remaining=90, current=-1, endurance=0, airspeed=0):
    bits = 1 | 2 | 32 | 33554432
    return [
        OriginalMessage(
            "HEARTBEAT",
            0,
            50,
            "<IBBBBB",
            "custom_mode type autopilot base_mode system_status mavlink_version",
            (0, 2, 12, 0, 3, 3),
        ),
        OriginalMessage("EXTENDED_SYS_STATE", 245, 130, "<BB", "vtol_state landed_state", (0, 1)),
        OriginalMessage(
            "GPS_RAW_INT",
            24,
            24,
            "<QiiiHHHHBBiIIIIH",
            "time_usec lat lon alt eph epv vel cog fix_type satellites_visible "
            "alt_ellipsoid h_acc v_acc vel_acc hdg_acc yaw",
            (10300000, 13521000, 1038198000, 18240, 15, 16, 0, 0, 3, 10, 18240, 150, 160, 0, 0, 0),
        ),
        ArrayMessage(
            "BATTERY_STATUS",
            147,
            154,
            "<iih10HhBBBbiB4HBI",
            "current_consumed energy_consumed temperature "
            + " ".join(f"voltages{i}" for i in range(10))
            + " current_battery id battery_function type battery_remaining "
            "time_remaining charge_state "
            + " ".join(f"voltages_ext{i}" for i in range(4))
            + " mode fault_bitmask",
            (
                0,
                0,
                2500,
                12000,
                *([65535] * 9),
                current,
                0,
                1,
                3,
                battery_remaining,
                endurance,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
            ),
        ),
        OriginalMessage(
            "SYS_STATUS",
            1,
            124,
            "<IIIHHhHHHHHHb",
            "onboard_control_sensors_present onboard_control_sensors_enabled "
            "onboard_control_sensors_health load voltage_battery current_battery "
            "drop_rate_comm errors_comm errors_count1 errors_count2 errors_count3 "
            "errors_count4 battery_remaining",
            (bits, bits, bits, 100, 12000, current, 0, 0, 0, 0, 0, 0, battery_remaining),
        ),
        OriginalMessage(
            "VFR_HUD",
            74,
            20,
            "<ffffhH",
            "airspeed groundspeed alt climb heading throttle",
            (airspeed, 0, 18.24, 0, 90, 0),
        ),
    ]


def update_output(*, timestamp=8000000, instance=1, set_count=2):
    return (
        f"TOPIC: parameter_update\n    timestamp: {timestamp}\n"
        f"    instance: {instance}\n    set_count: {set_count}\n"
    ).encode()


@pytest.fixture
def live_fixture(tmp_path):
    # Reuse independent wire/model fixture assets only. Its unrelated sealed
    # capture is not passed to this consumer or used as current evidence.
    old_args, retained = retained_fixture.__wrapped__(tmp_path, SimpleNamespace(param=None))
    root = tmp_path
    world_path = root / "world.sdf"
    world_path.write_bytes(
        world_path.read_bytes().replace(
            b"</world>", b"<wind><linear_velocity>1 -.4 0</linear_velocity></wind></world>"
        )
    )
    world = binding(root, "world.sdf")
    profile = {**retained["profile"], "stack_plan_sha256": "4" * 64}
    profile_source = document(root, "profile.json", profile)
    runtime_source = document(
        root,
        "live-runtime.json",
        {
            "scenario_id": "fixture-scenario",
            "vehicle": {"airframe_profile_id": profile["airframe_profile_id"]},
            "world": {"wind_north_mps": 1, "wind_east_mps": -0.4},
        },
    )
    container = "1" * 64
    plan = {
        "docker_plan": {
            "docker_executable": "docker",
            "container_name": "CPU-teacher",
            "image": "fixture/px4@sha256:" + "2" * 64,
            "scenario_id": "fixture-scenario",
            "scenario_manifest_hash": "3" * 64,
            "ports": {"control": {"host": 14550}},
            "mounts": {
                name: {"container": f"/assets/{name}", "host": str(root / name)}
                for name in ("world.sdf", "profile.json", "live-runtime.json")
            },
        }
    }
    inspect = {
        "Id": container,
        "Name": "/CPU-teacher",
        "State": {"Running": True},
        "Config": {
            "Image": plan["docker_plan"]["image"],
            "Env": ["PX4_SIM_MODEL=gz_x500_mono_cam"],
            "Labels": {
                "multidetect.purpose": "simulator-docker",
                "multidetect.teacher-session-owner": "CPU-owner",
                "multidetect.teacher-session-purpose": "truth-free-teacher-observation",
            },
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "Devices": [],
            "PortBindings": {"14550/udp": [{"HostIp": "127.0.0.1", "HostPort": "14550"}]},
        },
        "Mounts": [
            {"Destination": item["container"], "Source": item["host"], "RW": False}
            for item in plan["docker_plan"]["mounts"].values()
        ],
    }

    def native(name, raw, start, end, argv):
        source = binding(root, f"native-{name}.bin", raw)
        return {
            "command": list(argv),
            "clock_id": Clock.session_id,
            "container_id": container,
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

    from multidetect.autonomy.sim_navigation_reference import PARAMETERS

    def parameter_value(name, expected):
        return expected if expected is not None else (1 if name == "SIM_GZ_EN_ODOM" else 0)

    baseline = (
        b"MD-LIVE-PARAMETER-UPDATE-BEFORE-V1\n"
        + update_output()
        + b"MD-LIVE-PARAMETERS-V1\n"
        + b"".join(
            f"{name}={parameter_value(name, expected)}\n".encode()
            for name, expected in PARAMETERS.items()
        )
        + b"MD-LIVE-PARAMETER-UPDATE-V1\n"
        + update_output()
        + b"MD-LIVE-NAV-END-V1\n"
    )
    baseline_row = native(
        "baseline",
        baseline,
        99,
        99.5,
        ("docker", "exec", container, *navigation_parameter_baseline_argv()),
    )
    phases = []
    for role, timestamp, start, end in (
        ("before", 10000000, 100, 100.05),
        ("after", 10400000, 100.4, 100.45),
    ):
        phase = (
            b"MD-LIVE-PARAMETER-UPDATE-V1\n"
            + update_output()
            + b"MD-LIVE-LOCAL-POSITION-V1\n"
            + listener(timestamp)
            + b"MD-LIVE-NAV-END-V1\n"
        )
        phases.append(
            native(
                role,
                phase,
                start,
                end,
                ("docker", "exec", container, *navigation_reference_phase_argv()),
            )
        )
    inspects = [
        native(
            f"inspect-{role}",
            json.dumps([inspect]).encode(),
            start,
            end,
            ("docker", "container", "inspect", container),
        )
        for role, start, end in (("before", 99.7, 99.9), ("after", 100.46, 100.49))
    ]
    clock = Clock()
    cap = TeacherTelemetryCaptureV3(
        root / "live-journals",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id="CPU-live-transaction",
        local_origin_id="CPU-live-origin",
    )
    sender = Sender(None)
    for batch, boot in enumerate((10100, 10300)):
        samples = []
        for index, message in enumerate(message_set(boot) + health_messages()):
            message.pack(sender)
            sender.seq = (sender.seq + 1) % 256
            samples.append((message, 100.16 + batch * 0.2 + index * 0.001))
        cap.append_batch(samples)
    clock.value = 100.51
    prefix = capture_teacher_committed_prefix(
        cap, artifact_root=root, output_directory=root / "prefix", clock=clock
    )
    args = dict(
        artifact_root=root,
        output_directory=root / "checkpoint",
        prefix_source=prefix,
        profile_source=profile_source,
        runtime_source=runtime_source,
        world_source=world,
        model_sources=old_args["model_sources"],
        camera_updates=retained["camera_frames"],
        parameter_baseline=baseline_row,
        navigation_before=phases[0],
        navigation_after=phases[1],
        owned_inspect_before=inspects[0],
        owned_inspect_after=inspects[1],
        plan=plan,
        container_id=container,
        owner_token="CPU-owner",
        now_s=clock.value,
        clock_id=clock.session_id,
        live_capture=cap,
        live_clock=clock,
    )
    yield args, cap, clock, native
    if not cap._closed:
        cap.abort("CPU-fixture cleanup")


def test_open_original_prefix_to_current_context_and_origin(live_fixture):
    args, cap, clock, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    assert isinstance(checkpoint, LiveTeacherNavigationCheckpoint)
    assert not cap._closed and checkpoint.clock_id == clock.session_id
    context = checkpoint.observed_context
    assert context.schema_version == 2 and context.captured_at_s == 100.36
    assert context.battery_remaining_pct == 90 and context.estimated_flight_time_s is None
    assert context.airspeed_mps is None and context.flight_capability is None
    assert context.camera_available and not any(
        (
            context.perception_available,
            context.geofence_valid,
            context.map_cache_available,
            context.dem_available,
        )
    )
    assert (context.wind_north_mps, context.wind_east_mps) == (-0.4, 1)
    report = checkpoint.to_mapping()
    assert not report["configured_wind"]["runtime_direction_matches"]
    assert not any(report["claims"].values())
    assert checkpoint.runtime_attestation.telemetry_qualified
    assert checkpoint.origin.to_mapping()["verified_home_enu_m"] == pytest.approx([0, 0, 0.24])
    assert checkpoint.expires_at_s == 102.15
    assert checkpoint.inspected_at_s == 100.46
    assert report["observed_context_receipt"]["qualification_details"][
        "original_record_indices_preserved"
    ]
    assert checkpoint.observation_source.json_pointer == "/observed_context_receipt"
    clock.value = 100.6
    assert (
        validate_live_teacher_navigation_checkpoint(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=clock.session_id,
            expected_origin_continuity_token=checkpoint.origin_continuity_token,
        )
        is checkpoint
    )


def test_observation_pointer_consumed_by_shared_explicit_SIM_budget(live_fixture):
    args, cap, _, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    model = _model()
    model["airframe_profile_hash"] = checkpoint.observed_context.airframe_profile_hash
    model_source = document(args["artifact_root"], "live-model.json", model)
    context, receipt = qualify_sim_planning_context(
        checkpoint.observed_context,
        runtime_attestation=checkpoint.runtime_attestation,
        observation_source=checkpoint.observation_source,
        model_source=model_source,
        artifact_root=args["artifact_root"],
        now_s=checkpoint.qualified_at_s,
        attested_at_s=checkpoint.inspected_at_s,
        clock_id=checkpoint.clock_id,
    )
    assert context.estimated_flight_time_s == pytest.approx(540 - 0.15)
    assert context.battery_remaining_pct == 90
    assert receipt.observed_context_sha256 == checkpoint.observed_context.canonical_hash
    assert not receipt.to_mapping()["reserve_deducted"]


def test_sealed_receipt_cannot_become_current_checkpoint(live_fixture):
    args, cap, clock, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    historical = LiveTeacherNavigationCheckpoint(
        canonical_json(checkpoint.to_mapping()), checkpoint.checkpoint_source
    )
    assert (
        validate_live_teacher_navigation_checkpoint(
            historical,
            artifact_root=args["artifact_root"],
            now_s=checkpoint.qualified_at_s,
            clock_id=clock.session_id,
        )
        is historical
    )
    clock.value = 100.6
    with pytest.raises(AutonomyContractError, match="historical receipt"):
        validate_live_teacher_navigation_checkpoint(
            historical,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=clock.session_id,
        )
    cap.seal()
    with pytest.raises((AutonomyContractError, RuntimeError, ValueError)):
        validate_live_teacher_navigation_checkpoint(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=clock.session_id,
        )


def test_append_after_prefix_is_legal_and_does_not_renew_checkpoint(live_fixture):
    args, cap, clock, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    original = checkpoint.canonical_hash
    clock.value = 100.6
    message = health_messages()[0]
    message.pack(Sender(None))
    cap.append_batch([(message, 100.59)])
    validate_live_teacher_navigation_checkpoint(
        checkpoint,
        artifact_root=args["artifact_root"],
        now_s=clock.value,
        clock_id=clock.session_id,
    )
    assert (
        checkpoint.canonical_hash == original
        and checkpoint.observed_context.captured_at_s == 100.36
    )
    clock.value = checkpoint.expires_at_s
    with pytest.raises(AutonomyContractError, match="expired"):
        validate_live_teacher_navigation_checkpoint(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=clock.session_id,
        )


@pytest.mark.parametrize(
    "kind",
    ["HEARTBEAT", "EXTENDED_SYS_STATE", "GPS_RAW_INT", "BATTERY_STATUS", "SYS_STATUS", "VFR_HUD"],
)
def test_health_wire_CRC_independent_and_payload_fields(kind):
    message = next(m for m in health_messages() if m.kind == kind)
    packet = message.pack(Sender(None))
    decoded = decode_context_packet(packet, kind)
    for key, value in message.to_dict().items():
        if key != "mavpackettype":
            assert decoded[key] == value
    altered = bytearray(packet)
    altered[-3] ^= 1
    with pytest.raises(AutonomyContractError, match="CRC"):
        decode_context_packet(bytes(altered), kind)


@pytest.mark.parametrize(
    "case",
    [
        "parameter-change",
        "reset-change",
        "native-incomplete",
        "owner-change",
        "foreign-clock",
        "wind-source-tamper",
    ],
)
def test_original_source_or_native_continuity_refusal(live_fixture, case):
    args, _, _, native = live_fixture
    root = args["artifact_root"]
    args = copy.copy(args)
    if case == "parameter-change":
        raw = (root / "native-after.bin").read_bytes().replace(b"set_count: 2", b"set_count: 3")
        args["navigation_after"] = native(
            "changed-after", raw, 100.4, 100.45, args["navigation_after"]["command"]
        )
    elif case == "reset-change":
        raw = (
            (root / "native-after.bin")
            .read_bytes()
            .replace(b"xy_reset_counter: 1", b"xy_reset_counter: 2")
        )
        args["navigation_after"] = native(
            "changed-after", raw, 100.4, 100.45, args["navigation_after"]["command"]
        )
    elif case == "native-incomplete":
        args["navigation_after"] = {**args["navigation_after"], "complete_output_observed": False}
    elif case == "owner-change":
        args["owner_token"] = "another-owner"
    elif case == "foreign-clock":
        args["clock_id"] = "another-clock"
    else:
        (root / "world.sdf").write_bytes(
            (root / "world.sdf").read_bytes().replace(b"1 -.4 0", b"9 -.4 0")
        )
    with pytest.raises(AutonomyContractError):
        qualify_teacher_live_navigation_checkpoint(**args)


def test_plan_origin_token_change_refused(live_fixture):
    args, _, clock, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    changed = checkpoint.origin_continuity_token
    changed["ref_timestamp"] += 1
    clock.value = 100.6
    with pytest.raises(AutonomyContractError, match="token changed"):
        validate_live_teacher_navigation_checkpoint(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=clock.value,
            clock_id=clock.session_id,
            expected_origin_continuity_token=changed,
        )


@pytest.mark.parametrize("case", ["overwrite", "truncate", "replace"])
def test_growing_prefix_original_hash_and_file_identity(live_fixture, case):
    args, _, _, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    path = args["artifact_root"] / "live-journals/raw-packets.journal"
    raw = path.read_bytes()
    if case == "overwrite":
        path.write_bytes(raw[:20] + bytes([raw[20] ^ 1]) + raw[21:])
    elif case == "truncate":
        path.write_bytes(raw[:-1])
    else:
        # Windows prevents renaming an open writer handle. Closing is not a
        # source renewal; the historical identity checker must still reject
        # the replacement inode even when its bytes happen to match exactly.
        live_fixture[1].seal()
        path.rename(path.with_suffix(".original"))
        path.write_bytes(raw)
    with pytest.raises(AutonomyContractError):
        validate_live_teacher_navigation_checkpoint(
            checkpoint,
            artifact_root=args["artifact_root"],
            now_s=checkpoint.qualified_at_s,
            clock_id=checkpoint.clock_id,
        )


def test_prefix_export_refuses_missing_actual_nine_packets(tmp_path):
    clock = Clock()
    cap = TeacherTelemetryCaptureV3(
        tmp_path / "raw",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id="missing",
        local_origin_id="missing",
    )
    message = health_messages()[0]
    message.pack(Sender(None))
    cap.append_batch([(message, clock.value)])
    with pytest.raises(ValueError, match="missing/stale"):
        capture_teacher_committed_prefix(
            cap, artifact_root=tmp_path, output_directory=tmp_path / "prefix", clock=clock
        )
    cap.abort("CPU cleanup")


def refresh_health(live_fixture, messages):
    args, cap, clock, _ = live_fixture
    samples = []
    sender = Sender(None)
    for index, message in enumerate(messages):
        message.pack(sender)
        sender.seq += 1
        samples.append((message, 100.50 + index * 0.0001))
    cap.append_batch(samples)
    prefix = capture_teacher_committed_prefix(
        cap,
        artifact_root=args["artifact_root"],
        output_directory=args["artifact_root"] / "prefix-fresh",
        clock=clock,
    )
    return {**args, "prefix_source": prefix}


@pytest.mark.parametrize(
    "kind,index,value",
    [
        ("HEARTBEAT", 3, 128),
        ("HEARTBEAT", 4, 4),
        ("EXTENDED_SYS_STATE", 1, 2),
        ("GPS_RAW_INT", 8, 2),
        ("GPS_RAW_INT", 11, 0),
        ("GPS_RAW_INT", 11, 2001),
        ("SYS_STATUS", 2, 0),
        ("BATTERY_STATUS", 17, -1),
        ("VFR_HUD", 1, 0.5),
    ],
)
def test_actual_unhealthy_ground_context_is_not_admitted(live_fixture, kind, index, value):
    messages = health_messages()
    message = next(m for m in messages if m.kind == kind)
    values = list(message.values)
    values[index] = value
    message.values = tuple(values)
    args = refresh_health(live_fixture, messages)
    with pytest.raises(AutonomyContractError):
        qualify_teacher_live_navigation_checkpoint(**args)


def test_actual_unknown_air_and_endurance_remain_unknown(live_fixture):
    args = refresh_health(live_fixture, health_messages(airspeed=math.nan))
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    context = checkpoint.observed_context
    assert context.airspeed_mps is None and context.estimated_flight_time_s is None
    assert context.provenance.airspeed.kind == context.provenance.endurance.kind == "unknown"


def test_reported_endurance_age_not_reserve_or_battery_is_deducted(live_fixture):
    args = refresh_health(live_fixture, health_messages(current=100, endurance=500))
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    assert checkpoint.observed_context.estimated_flight_time_s == pytest.approx(499.9903)
    assert checkpoint.observed_context.battery_remaining_pct == 90
    assert checkpoint.observed_context.provenance.endurance.kind == "autopilot_reported"


def test_health_projection_cannot_replace_unhealthy_original_wire(live_fixture):
    messages = health_messages()
    message = messages[0]
    values = list(message.values)
    values[4] = 4
    message.values = tuple(values)
    original_projection = message.to_dict
    message.to_dict = lambda: {**original_projection(), "system_status": 3}
    args = refresh_health(live_fixture, messages)
    with pytest.raises(AutonomyContractError, match="projection differs from wire"):
        qualify_teacher_live_navigation_checkpoint(**args)


def test_final_clock_expiry_after_source_recheck_refused(live_fixture, monkeypatch):
    import multidetect.autonomy.teacher_live_navigation_checkpoint as module

    args, _, clock, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    recompute = module._recompute

    def source_cost(*a):
        result = recompute(*a)
        clock.value = checkpoint.expires_at_s
        return result

    monkeypatch.setattr(module, "_recompute", source_cost)
    clock.value = 100.6
    with pytest.raises(AutonomyContractError, match="expired while"):
        validate_live_teacher_navigation_checkpoint(
            checkpoint, artifact_root=args["artifact_root"], now_s=100.6, clock_id=clock.session_id
        )


def test_actual_16k_instance_preserves_original_indices_above_8191(live_fixture):
    from scripts.collect_onboard_teacher_gazebo_origin_window import WindowTelemetryCapture

    args, _, clock, _ = live_fixture
    root = args["artifact_root"]
    cap = WindowTelemetryCapture(
        root / "high-index-journals",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id="CPU-high-index",
        local_origin_id="CPU-high-index-origin",
    )
    try:
        sender = Sender(None)
        for _ in range(512):
            batch = []
            for _ in range(16):
                message = health_messages()[0]
                message.pack(sender)
                sender.seq = (sender.seq + 1) % 256
                batch.append((message, 100.1))
            cap.append_batch(batch)
        for batch, boot in enumerate((10100, 10300)):
            samples = []
            for index, message in enumerate(message_set(boot) + health_messages()):
                message.pack(sender)
                sender.seq = (sender.seq + 1) % 256
                samples.append((message, 100.16 + batch * 0.2 + index * 0.001))
            cap.append_batch(samples)
        prefix = capture_teacher_committed_prefix(
            cap, artifact_root=root, output_directory=root / "high-prefix", clock=clock
        )
        checkpoint = qualify_teacher_live_navigation_checkpoint(
            **{**args, "prefix_source": prefix, "live_capture": cap}
        )
        assert cap.max_records == 16384
        assert min(checkpoint.record_indices.values()) >= 8192
        assert checkpoint.record_indices["HOME_POSITION"] == 8204
        assert checkpoint.observed_context.captured_at_s == 100.36
    finally:
        if not cap._closed:
            cap.abort("CPU high-index cleanup")


def test_same_named_clock_ids_do_not_substitute_another_actual_writer(live_fixture):
    args, _, clock, _ = live_fixture
    checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
    root = args["artifact_root"]
    other = TeacherTelemetryCaptureV3(
        root / "another-writer",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id="CPU-live-transaction",
        local_origin_id="CPU-live-origin",
    )
    try:
        substituted = LiveTeacherNavigationCheckpoint(
            canonical_json(checkpoint.to_mapping()), checkpoint.checkpoint_source, other, clock
        )
        clock.value = 100.6
        with pytest.raises(AutonomyContractError, match="not owned by current writer"):
            validate_live_teacher_navigation_checkpoint(
                substituted, artifact_root=root, now_s=clock.value, clock_id=clock.session_id
            )
    finally:
        other.abort("CPU substitute cleanup")
