from __future__ import annotations

import hashlib
import io
import math
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.backends.gazebo_sim import (
    GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION,
    GAZEBO_SIM_FLAG_CAMERA_INFO,
    GAZEBO_SIM_FLAG_NAVSAT,
    GAZEBO_SIM_FLAG_ODOMETRY,
    GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG,
    GAZEBO_SIM_SOURCE_ROW_ORDER,
    GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
    GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
    GAZEBO_SIM_WIRE_HEADER,
    GAZEBO_SIM_WIRE_HEADER_BYTES,
    GAZEBO_SIM_WIRE_ROW_ORDER,
    GazeboSimBackend,
    GazeboSimTelemetryProvider,
    gazebo_sim_attitude_sigmas_deg,
    read_gazebo_sim_wire_frame,
    reconstruct_gazebo_sim_wire_frame,
)
from multidetect.multimodal_ranging import CameraCalibration
from multidetect.runtime import RuntimeMode, RuntimeProfile
from multidetect.simulation import SensorRuntimeInput, load_runtime_scenario
from multidetect.terrain_dem import load_local_dem_grid
from scripts.gazebo_sensor_stream import build_wire_frame

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
TERRAIN_DEM = ROOT / "simulation/terrain/river_rescue_vtol_001.dem.json"
URBAN_TERRAIN_DEM = ROOT / "simulation/terrain/urban_inspection_multirotor_001.dem.json"


def _camera_calibration(
    calibration_id: str = "gazebo-front-camera-v1",
) -> CameraCalibration:
    return CameraCalibration(
        calibration_id=calibration_id,
        width_px=2,
        height_px=1,
        fx_px=1.0,
        fy_px=1.0,
        cx_px=1.0,
        cy_px=0.5,
    )


def _wire_frame(
    sequence: int,
    *,
    stamp_ns: int,
    width: int = 2,
    height: int = 1,
    camera_info_stamp_ns: int | None = None,
    navsat_stamp_ns: int | None = None,
    odometry_stamp_ns: int | None = None,
    flags: int = (
        GAZEBO_SIM_FLAG_CAMERA_INFO
        | GAZEBO_SIM_FLAG_NAVSAT
        | GAZEBO_SIM_FLAG_ODOMETRY
        | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG
    ),
    payload: bytes | None = None,
    digest: bytes | None = None,
    latitude_deg: float = 47.397971,
    longitude_deg: float = 8.546164,
    altitude_m: float = 488.2,
    position_east_m: float = 1.0,
    position_north_m: float = 2.0,
    position_up_m: float = 3.0,
    orientation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    velocity_flu_mps: tuple[float, float, float] = (4.0, 5.0, 6.0),
    attitude_covariance_enu_rad2: tuple[float, ...] = (
        0.0001, 0.0, 0.0,
        0.0, 0.0004, 0.0,
        0.0, 0.0, 0.0009,
    ),
) -> bytes:
    selected = payload if payload is not None else bytes((1, 2, 3, 4, 5, 6))
    packet = build_wire_frame(
        sequence=sequence,
        sensor_stamp_ns=stamp_ns,
        width=width,
        height=height,
        camera_info_stamp_ns=(
            stamp_ns - 3 if camera_info_stamp_ns is None else camera_info_stamp_ns
        ),
        navsat_stamp_ns=stamp_ns - 2 if navsat_stamp_ns is None else navsat_stamp_ns,
        odometry_stamp_ns=stamp_ns - 1 if odometry_stamp_ns is None else odometry_stamp_ns,
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        altitude_m=altitude_m,
        position_east_m=position_east_m,
        position_north_m=position_north_m,
        position_up_m=position_up_m,
        orientation_x=orientation[0],
        orientation_y=orientation[1],
        orientation_z=orientation[2],
        orientation_w=orientation[3],
        velocity_forward_mps=velocity_flu_mps[0],
        velocity_left_mps=velocity_flu_mps[1],
        velocity_up_mps=velocity_flu_mps[2],
        attitude_covariance_enu_rad2=attitude_covariance_enu_rad2,
        bgr_payload=selected,
    )
    if (
        flags
        != (
            GAZEBO_SIM_FLAG_CAMERA_INFO
            | GAZEBO_SIM_FLAG_NAVSAT
            | GAZEBO_SIM_FLAG_ODOMETRY
            | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG
        )
        or digest is not None
    ):
        fields = list(GAZEBO_SIM_WIRE_HEADER.unpack(packet[:GAZEBO_SIM_WIRE_HEADER_BYTES]))
        fields[-2] = flags
        if digest is not None:
            fields[-1] = digest
        packet = GAZEBO_SIM_WIRE_HEADER.pack(*fields) + selected
    return packet


def _replace_wire_field(
    packet: bytes,
    index: int,
    value: object,
    *,
    recompute_digest: bool = True,
) -> bytes:
    """Mutate one packed field, optionally preserving a valid record digest."""

    fields = list(GAZEBO_SIM_WIRE_HEADER.unpack(packet[:GAZEBO_SIM_WIRE_HEADER_BYTES]))
    fields[index] = value
    payload = packet[GAZEBO_SIM_WIRE_HEADER_BYTES:]
    if recompute_digest:
        fields[-1] = b"\x00" * 32
        unsigned_header = GAZEBO_SIM_WIRE_HEADER.pack(*fields)
        fields[-1] = hashlib.sha256(unsigned_header + payload).digest()
    return GAZEBO_SIM_WIRE_HEADER.pack(*fields) + payload


def _telemetry_from_packet(packet: bytes):
    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    provider = GazeboSimTelemetryProvider()
    provider.update(wire, observed_at_s=10.0)
    return provider.snapshot(now_s=10.0)


class _FakeProcess:
    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(b"")
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1


class _EofThenExitedProcess(_FakeProcess):
    def __init__(self, *, stderr: bytes) -> None:
        super().__init__(b"")
        self.stderr = io.BytesIO(stderr)
        self._poll_count = 0

    def poll(self) -> int | None:
        self._poll_count += 1
        if self._poll_count == 1:
            return None
        self.returncode = 2
        return self.returncode


def test_gazebo_sim_wire_round_trip_is_integrity_and_readiness_bound() -> None:
    frame = read_gazebo_sim_wire_frame(
        io.BytesIO(_wire_frame(0, stamp_ns=1_000_000_000)),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    assert frame.sequence == 0
    assert frame.payload_bgr24 == bytes((1, 2, 3, 4, 5, 6))
    assert len(frame.record_sha256) == 64
    assert frame.latitude_deg == pytest.approx(47.397971)
    assert frame.odometry_present is True
    assert (frame.position_east_m, frame.position_north_m, frame.position_up_m) == (
        1.0,
        2.0,
        3.0,
    )


def test_gazebo_frame_source_preserves_helper_stderr_after_eof_exit() -> None:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    process = _EofThenExitedProcess(
        stderr=b'{"event":"gazebo_sensor_stream_finished","error":"odometry rejected"}\n'
    )
    backend = GazeboSimBackend(
        scenario_input=runtime_input,
        camera_calibration=_camera_calibration(),
        docker_executable=Path(sys.executable).resolve(),
        container_name="owned-gazebo-sim",
        frame_count=1,
        airframe_profile_hash="a" * 64,
        container_image_sha256="b" * 64,
        stream_script_sha256="c" * 64,
        model_assets_sha256="d" * 64,
        expected_width=2,
        expected_height=1,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    backend.frame_source.open()
    with pytest.raises(RuntimeError) as raised:
        backend.frame_source.read()
    message = str(raised.value)
    assert "helper closed its framed stream" in message
    assert "exit=2" in message
    assert "odometry rejected" in message
    assert "unexpected EOF while reading Gazebo SIM header" in message
    backend.close()


def test_gazebo_sim_wire_requires_full_odometry() -> None:
    flags = (
        GAZEBO_SIM_FLAG_CAMERA_INFO | GAZEBO_SIM_FLAG_NAVSAT | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG
    )
    with pytest.raises(RuntimeError, match="readiness"):
        read_gazebo_sim_wire_frame(
            io.BytesIO(
                _wire_frame(
                    0,
                    stamp_ns=1_000_000_000,
                    flags=flags,
                )
            ),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            _wire_frame(
                0,
                stamp_ns=1_000_000_000,
                flags=(
                    GAZEBO_SIM_FLAG_CAMERA_INFO
                    | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG
                    | GAZEBO_SIM_FLAG_ODOMETRY
                ),
            ),
            "readiness",
        ),
        (
            _wire_frame(0, stamp_ns=1_000_000_000, digest=b"x" * 32),
            "SHA-256",
        ),
        (_wire_frame(1, stamp_ns=1_000_000_000), "sequence"),
        (
            _wire_frame(
                0,
                stamp_ns=1_000_000_000,
                flags=(
                    GAZEBO_SIM_FLAG_CAMERA_INFO | GAZEBO_SIM_FLAG_NAVSAT | GAZEBO_SIM_FLAG_ODOMETRY
                ),
            ),
            "pixel format",
        ),
        (
            _wire_frame(0, stamp_ns=1_000_000_000, flags=0x80010007),
            "unknown flags",
        ),
    ),
)
def test_gazebo_sim_wire_fails_closed(payload: bytes, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        read_gazebo_sim_wire_frame(
            io.BytesIO(payload),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


@pytest.mark.parametrize(
    ("flags", "odometry_stamp_ns", "message"),
    (
        (
            GAZEBO_SIM_FLAG_CAMERA_INFO
            | GAZEBO_SIM_FLAG_NAVSAT
            | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG,
            999_999_999,
            "readiness",
        ),
        (
            GAZEBO_SIM_FLAG_CAMERA_INFO
            | GAZEBO_SIM_FLAG_NAVSAT
            | GAZEBO_SIM_FLAG_ODOMETRY
            | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG,
            0,
            "timestamps",
        ),
    ),
)
def test_gazebo_sim_wire_rejects_missing_odometry_flag_or_timestamp(
    flags: int,
    odometry_stamp_ns: int,
    message: str,
) -> None:
    packet = _wire_frame(
        0,
        stamp_ns=1_000_000_000,
        flags=flags,
    )
    if odometry_stamp_ns == 0:
        packet = _replace_wire_field(packet, 11, 0)
    with pytest.raises(RuntimeError, match=message):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


def test_gazebo_sim_wire_rejects_non_increasing_sensor_time() -> None:
    with pytest.raises(RuntimeError, match="strictly increasing"):
        read_gazebo_sim_wire_frame(
            io.BytesIO(_wire_frame(1, stamp_ns=1_000_000_000)),
            expected_sequence=1,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=1_000_000_000,
        )


@pytest.mark.parametrize("latitude", (-90.0, 90.0))
def test_gazebo_sim_wire_rejects_exact_pole_coordinates(latitude: float) -> None:
    packet = _replace_wire_field(
        _wire_frame(0, stamp_ns=1_000_000_000),
        12,
        latitude,
    )
    with pytest.raises(RuntimeError, match="latitude_deg is invalid"):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


def test_gazebo_sim_wire_digest_covers_odometry_header_values() -> None:
    packet = _replace_wire_field(
        _wire_frame(0, stamp_ns=1_000_000_000),
        15,
        123.0,
        recompute_digest=False,
    )
    with pytest.raises(RuntimeError, match="record SHA-256"):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


@pytest.mark.parametrize(
    ("field_index", "value", "message"),
    (
        (15, math.inf, "non-finite"),
        (15, 1_000_000.1, "position exceeds"),
        (22, 2_000.1, "velocity exceeds"),
        (21, 0.8, "quaternion is not normalized"),
    ),
)
def test_gazebo_sim_wire_rejects_invalid_odometry_values(
    field_index: int,
    value: float,
    message: str,
) -> None:
    packet = _replace_wire_field(
        _wire_frame(0, stamp_ns=2_000_000_000),
        field_index,
        value,
    )
    with pytest.raises(RuntimeError, match=message):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


@pytest.mark.parametrize(
    ("field_updates", "message"),
    (
        (((25, math.nan),), "9 finite"),
        (((26, 0.01),), "symmetric"),
        (((25, 1.0), (26, 2.0), (28, 2.0), (29, 1.0)), "semidefinite"),
    ),
)
def test_gazebo_sim_wire_rejects_invalid_attitude_covariance(
    field_updates: tuple[tuple[int, float], ...],
    message: str,
) -> None:
    packet = _wire_frame(0, stamp_ns=2_000_000_000)
    for index, value in field_updates:
        packet = _replace_wire_field(packet, index, value)
    with pytest.raises(RuntimeError, match=message):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


@pytest.mark.parametrize(
    ("field_index", "source_name"),
    ((10, "NavSat"), (11, "Odometry")),
)
def test_gazebo_sim_wire_rejects_stale_or_future_skewed_sources(
    field_index: int,
    source_name: str,
) -> None:
    sensor_stamp_ns = 2_000_000_000
    packet = _replace_wire_field(
        _wire_frame(0, stamp_ns=sensor_stamp_ns),
        field_index,
        sensor_stamp_ns - 1_500_000_001,
    )
    with pytest.raises(RuntimeError, match=rf"{source_name} timestamp skew exceeds"):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


def test_gazebo_sim_wire_allows_session_static_camera_info_timestamp() -> None:
    sensor_stamp_ns = 8_000_000_000
    packet = _replace_wire_field(
        _wire_frame(0, stamp_ns=sensor_stamp_ns),
        9,
        1,
    )
    frame = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    assert frame.camera_info_stamp_ns == 1
    assert frame.sensor_stamp_ns - frame.camera_info_stamp_ns > 1_500_000_000


@pytest.mark.parametrize(
    ("field_index", "source_name"),
    ((9, "CameraInfo"), (10, "NavSat"), (11, "Odometry")),
)
def test_gazebo_sim_wire_rejects_any_required_source_from_the_future(
    field_index: int,
    source_name: str,
) -> None:
    sensor_stamp_ns = 2_000_000_000
    packet = _replace_wire_field(
        _wire_frame(0, stamp_ns=sensor_stamp_ns),
        field_index,
        sensor_stamp_ns + 1,
    )
    with pytest.raises(RuntimeError, match=rf"{source_name} timestamp is from the future"):
        read_gazebo_sim_wire_frame(
            io.BytesIO(packet),
            expected_sequence=0,
            expected_width=2,
            expected_height=1,
            previous_sensor_stamp_ns=None,
        )


def test_gazebo_telemetry_preserves_navsat_and_odometry_source_ages() -> None:
    sensor_stamp_ns = 10_000_000_000
    packet = _wire_frame(
        0,
        stamp_ns=sensor_stamp_ns,
        navsat_stamp_ns=sensor_stamp_ns - 750_000_000,
        odometry_stamp_ns=sensor_stamp_ns - 250_000_000,
    )
    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    provider = GazeboSimTelemetryProvider(maximum_age_s=1.0)

    provider.update(wire, observed_at_s=100.0)
    telemetry = provider.snapshot(now_s=100.0)

    assert telemetry.gps_observed_at_s == pytest.approx(99.25)
    assert telemetry.position_observed_at_s == pytest.approx(99.25)
    assert telemetry.attitude_observed_at_s == pytest.approx(99.75)
    assert telemetry.velocity_observed_at_s == pytest.approx(99.75)
    assert telemetry.local_position_observed_at_s == pytest.approx(99.75)
    # The provider-wide freshness gate follows the oldest required telemetry
    # source, rather than the wire read time.
    provider.snapshot(now_s=100.25)
    with pytest.raises(RuntimeError, match="telemetry is stale"):
        provider.snapshot(now_s=100.250_000_001)


def test_gazebo_attitude_covariance_is_transformed_and_floored_with_provenance() -> None:
    zero_wire = read_gazebo_sim_wire_frame(
        io.BytesIO(
            _wire_frame(
                0,
                stamp_ns=1_000_000_000,
                attitude_covariance_enu_rad2=(0.0,) * 9,
            )
        ),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    provider = GazeboSimTelemetryProvider(
        minimum_roll_sigma_deg=0.4,
        minimum_pitch_sigma_deg=0.5,
        minimum_heading_sigma_deg=1.2,
    )
    provider.update(zero_wire, observed_at_s=10.0)
    floored = provider.snapshot(now_s=10.0)
    assert floored.attitude_roll_sigma_deg == pytest.approx(0.4)
    assert floored.attitude_pitch_sigma_deg == pytest.approx(0.5)
    assert floored.attitude_heading_sigma_deg == pytest.approx(1.2)
    assert (
        floored.attitude_uncertainty_source
        == "gazebo_odometry_covariance_with_configured_floor"
    )

    covariance = (
        0.0004, 0.0001, 0.0,
        0.0001, 0.0009, 0.0,
        0.0, 0.0, 0.0016,
    )
    measured_wire = replace(zero_wire, attitude_covariance_enu_rad2=covariance)
    transformed = gazebo_sim_attitude_sigmas_deg(measured_wire)
    provider.update(measured_wire, observed_at_s=11.0)
    measured = provider.snapshot(now_s=11.0)
    assert transformed == pytest.approx(tuple(math.degrees(value) for value in (0.02, 0.03, 0.04)))
    assert measured.attitude_roll_sigma_deg == pytest.approx(transformed[0])
    assert measured.attitude_pitch_sigma_deg == pytest.approx(transformed[1])
    assert measured.attitude_heading_sigma_deg == pytest.approx(transformed[2])


def test_gazebo_telemetry_accepts_exact_age_limit_and_rejects_one_ns_over() -> None:
    sensor_stamp_ns = 10_000_000_000
    exact_packet = _wire_frame(
        0,
        stamp_ns=sensor_stamp_ns,
        navsat_stamp_ns=sensor_stamp_ns - 1_000_000_000,
        odometry_stamp_ns=sensor_stamp_ns,
    )
    exact_wire = read_gazebo_sim_wire_frame(
        io.BytesIO(exact_packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    exact_provider = GazeboSimTelemetryProvider(maximum_age_s=1.0)
    exact_provider.update(exact_wire, observed_at_s=100.0)
    assert exact_provider.snapshot(now_s=100.0).gps_observed_at_s == pytest.approx(99.0)

    stale_packet = _wire_frame(
        0,
        stamp_ns=sensor_stamp_ns,
        navsat_stamp_ns=sensor_stamp_ns - 1_000_000_001,
        odometry_stamp_ns=sensor_stamp_ns,
    )
    stale_wire = read_gazebo_sim_wire_frame(
        io.BytesIO(stale_packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    stale_provider = GazeboSimTelemetryProvider(maximum_age_s=1.0)
    with pytest.raises(RuntimeError, match="source is stale at frame capture"):
        stale_provider.update(stale_wire, observed_at_s=100.0)
    with pytest.raises(RuntimeError, match="not ready"):
        stale_provider.snapshot(now_s=100.0)


def test_gazebo_telemetry_provider_rejects_future_source_atomically() -> None:
    sensor_stamp_ns = 10_000_000_000
    packet = _wire_frame(0, stamp_ns=sensor_stamp_ns)
    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    provider = GazeboSimTelemetryProvider()
    future = replace(wire, navsat_stamp_ns=sensor_stamp_ns + 1)

    with pytest.raises(RuntimeError, match="NavSat timestamp is from the future"):
        provider.update(future, observed_at_s=100.0)
    with pytest.raises(RuntimeError, match="not ready"):
        provider.snapshot(now_s=100.0)


def test_gazebo_enu_flu_to_ned_frd_conversion_is_sign_invariant() -> None:
    half_sqrt = math.sqrt(0.5)
    positive = _telemetry_from_packet(
        _wire_frame(
            0,
            stamp_ns=1_000_000_000,
            orientation=(0.0, 0.0, half_sqrt, half_sqrt),
            velocity_flu_mps=(10.0, 0.0, 0.0),
        )
    )
    negative = _telemetry_from_packet(
        _wire_frame(
            0,
            stamp_ns=1_000_000_000,
            orientation=(0.0, 0.0, -half_sqrt, -half_sqrt),
            velocity_flu_mps=(10.0, 0.0, 0.0),
        )
    )
    assert positive.heading_deg == pytest.approx(0.0, abs=1.0e-12)
    assert positive.velocity_north_mps == pytest.approx(10.0)
    assert positive.velocity_east_mps == pytest.approx(0.0, abs=1.0e-12)
    assert negative.roll_deg == pytest.approx(positive.roll_deg)
    assert negative.pitch_deg == pytest.approx(positive.pitch_deg)
    assert negative.heading_deg == pytest.approx(positive.heading_deg)
    assert negative.velocity_north_mps == pytest.approx(positive.velocity_north_mps)
    assert negative.velocity_east_mps == pytest.approx(positive.velocity_east_mps)


def test_gazebo_attitude_conversion_is_finite_and_canonical_at_gimbal_lock() -> None:
    telemetry = _telemetry_from_packet(
        _wire_frame(
            0,
            stamp_ns=1_000_000_000,
            orientation=(0.5, -0.5, 0.5, 0.5),
            velocity_flu_mps=(0.0, 0.0, 0.0),
        )
    )
    assert telemetry.roll_deg == pytest.approx(0.0)
    assert telemetry.pitch_deg == pytest.approx(90.0)
    assert telemetry.heading_deg == pytest.approx(0.0)
    assert all(
        math.isfinite(value)
        for value in (telemetry.roll_deg, telemetry.pitch_deg, telemetry.heading_deg)
    )

    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(
            _wire_frame(
                0,
                stamp_ns=1_000_000_000,
                orientation=(0.5, -0.5, 0.5, 0.5),
            )
        ),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    assert gazebo_sim_attitude_sigmas_deg(wire) == (180.0, 90.0, 180.0)


def test_gazebo_agl_uses_navsat_msl_minus_scenario_dem_not_local_up() -> None:
    terrain_dem = load_local_dem_grid(TERRAIN_DEM)
    packet = _wire_frame(
        0,
        stamp_ns=1_000_000_000,
        latitude_deg=terrain_dem.origin_latitude_deg,
        longitude_deg=terrain_dem.origin_longitude_deg,
        altitude_m=12.25,
        position_up_m=123.0,
    )
    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    provider = GazeboSimTelemetryProvider(terrain_dem=terrain_dem)
    provider.update(wire, observed_at_s=10.0)

    telemetry = provider.snapshot(now_s=10.0)

    assert telemetry.altitude_agl_m == pytest.approx(7.25)
    assert telemetry.altitude_agl_m != pytest.approx(wire.position_up_m)
    assert telemetry.altitude_reference == GAZEBO_SIM_TERRAIN_AGL_REFERENCE
    assert telemetry.altitude_observed_at_s == pytest.approx(10.0)
    assert telemetry.local_down_m == pytest.approx(-123.0)


@pytest.mark.parametrize(
    "provider",
    (
        GazeboSimTelemetryProvider(),
        GazeboSimTelemetryProvider(
            terrain_dem=load_local_dem_grid(TERRAIN_DEM),
            simulated_gps_fix_type=2,
        ),
    ),
)
def test_gazebo_agl_fails_closed_without_dem_or_qualified_gps(
    provider: GazeboSimTelemetryProvider,
) -> None:
    terrain_dem = load_local_dem_grid(TERRAIN_DEM)
    packet = _wire_frame(
        0,
        stamp_ns=1_000_000_000,
        latitude_deg=terrain_dem.origin_latitude_deg,
        longitude_deg=terrain_dem.origin_longitude_deg,
        altitude_m=12.25,
        position_up_m=123.0,
    )
    wire = read_gazebo_sim_wire_frame(
        io.BytesIO(packet),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
        previous_sensor_stamp_ns=None,
    )
    provider.update(wire, observed_at_s=10.0)

    telemetry = provider.snapshot(now_s=10.0)

    assert math.isnan(telemetry.altitude_agl_m)
    assert math.isnan(telemetry.altitude_observed_at_s)
    assert telemetry.altitude_reference is None
    assert telemetry.local_down_m == pytest.approx(-123.0)


def test_gazebo_backend_rejects_terrain_dem_from_another_scenario() -> None:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    with pytest.raises(ValueError, match="terrain_dem scenario_id"):
            GazeboSimBackend(
                scenario_input=runtime_input,
                camera_calibration=_camera_calibration(),
            docker_executable=Path(sys.executable).resolve(),
            container_name="owned-gazebo-sim",
            frame_count=1,
            airframe_profile_hash="a" * 64,
            container_image_sha256="b" * 64,
            stream_script_sha256="c" * 64,
            model_assets_sha256="d" * 64,
            terrain_dem=load_local_dem_grid(URBAN_TERRAIN_DEM),
            expected_width=2,
            expected_height=1,
        )


@pytest.mark.parametrize(
    ("mutate_sensors", "message"),
    (
        (
            lambda sensors: tuple(sensor for sensor in sensors if sensor.kind != "rgb_camera"),
            "exactly one scenario RGB camera",
        ),
        (
            lambda sensors: (
                *sensors,
                next(sensor for sensor in sensors if sensor.kind == "rgb_camera"),
            ),
            "exactly one scenario RGB camera",
        ),
        (
            lambda sensors: tuple(
                replace(sensor, sensor_id="rear-camera") if sensor.kind == "rgb_camera" else sensor
                for sensor in sensors
            ),
            "sensor_id must be 'front-camera'",
        ),
        (
            lambda sensors: tuple(
                replace(sensor, calibration_id=None) if sensor.kind == "rgb_camera" else sensor
                for sensor in sensors
            ),
            "must bind a calibration_id",
        ),
        (
            lambda sensors: tuple(
                replace(sensor, stream_uri="gazebo://another-scenario/front-camera")
                if sensor.kind == "rgb_camera"
                else sensor
                for sensor in sensors
            ),
            "stream_uri does not match",
        ),
    ),
)
def test_gazebo_backend_fails_closed_on_invalid_scenario_rgb_binding(
    mutate_sensors: Callable[
        [tuple[SensorRuntimeInput, ...]],
        tuple[SensorRuntimeInput, ...],
    ],
    message: str,
) -> None:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    invalid_input = replace(runtime_input, sensors=mutate_sensors(runtime_input.sensors))

    with pytest.raises(ValueError, match=message):
            GazeboSimBackend(
                scenario_input=invalid_input,
                camera_calibration=_camera_calibration(),
            docker_executable=Path(sys.executable).resolve(),
            container_name="owned-gazebo-sim",
            frame_count=1,
            airframe_profile_hash="a" * 64,
            container_image_sha256="b" * 64,
            stream_script_sha256="c" * 64,
            model_assets_sha256="d" * 64,
            expected_width=2,
            expected_height=1,
        )


def test_gazebo_sensor_binding_hash_covers_camera_calibration_and_exact_topics() -> None:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    alternate_calibration_input = replace(
        runtime_input,
        sensors=tuple(
            replace(sensor, calibration_id="alternate-front-camera-v1")
            if sensor.kind == "rgb_camera"
            else sensor
            for sensor in runtime_input.sensors
        ),
    )
    common = {
        "docker_executable": Path(sys.executable).resolve(),
        "container_name": "owned-gazebo-sim",
        "frame_count": 1,
        "airframe_profile_hash": "a" * 64,
        "container_image_sha256": "b" * 64,
        "stream_script_sha256": "c" * 64,
        "model_assets_sha256": "d" * 64,
        "expected_width": 2,
        "expected_height": 1,
        "camera_calibration": _camera_calibration(),
    }

    baseline = GazeboSimBackend(scenario_input=runtime_input, **common)
    alternate_calibration = GazeboSimBackend(
        scenario_input=alternate_calibration_input,
        camera_calibration=_camera_calibration("alternate-front-camera-v1"),
        **{key: value for key, value in common.items() if key != "camera_calibration"},
    )
    alternate_topics = GazeboSimBackend(
        scenario_input=runtime_input,
        model_instance="standard_vtol_mono_cam_1",
        **common,
    )

    assert baseline.sensor_binding_hash != alternate_calibration.sensor_binding_hash
    assert baseline.sensor_binding_hash != alternate_topics.sensor_binding_hash
    assert baseline.topic_binding_hash != alternate_topics.topic_binding_hash


@pytest.mark.parametrize(
    ("scenario_relative", "model_instance", "calibration_id"),
    (
        (
            "configs/scenarios/river_rescue_vtol_001.scenario.json",
            "standard_vtol_mono_cam_0",
            "gazebo-front-camera-v1",
        ),
        (
            "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json",
            "rc_cessna_mono_cam_0",
            "gazebo-fixed-wing-front-camera-v1",
        ),
        (
            "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
            "x500_mono_cam_0",
            "gazebo-multirotor-front-camera-v1",
        ),
        (
            "configs/scenarios/validation/terrain_ranging_validation_001.scenario.json",
            "x500_mono_cam_0",
            "gazebo-multirotor-front-camera-v1",
        ),
        (
            "configs/scenarios/validation/terrain_pixel_validation_001.scenario.json",
            "x500_mono_cam_0",
            "gazebo-multirotor-front-camera-v1",
        ),
    ),
)
def test_gazebo_sensor_binding_supports_every_checked_in_rgb_scenario(
    scenario_relative: str,
    model_instance: str,
    calibration_id: str,
) -> None:
    runtime_input = load_runtime_scenario(ROOT / scenario_relative, mode=RuntimeMode.SIM)

    backend = GazeboSimBackend(
        scenario_input=runtime_input,
        camera_calibration=_camera_calibration(calibration_id),
        docker_executable=Path(sys.executable).resolve(),
        container_name="owned-gazebo-sim",
        frame_count=1,
        airframe_profile_hash="a" * 64,
        container_image_sha256="b" * 64,
        stream_script_sha256="c" * 64,
        model_assets_sha256="d" * 64,
        model_instance=model_instance,
        expected_width=2,
        expected_height=1,
    )

    binding = backend.sensor_binding
    assert binding["sensor_id"] == "front-camera"
    assert binding["calibration_id"] == calibration_id
    assert binding["stream_uri"] == (f"gazebo://{runtime_input.scenario_id}/front-camera")
    assert binding["topic_binding_sha256"] == backend.topic_binding_hash


def test_gazebo_sim_backend_declares_authentic_sim_with_full_kinematics() -> None:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    terrain_dem = load_local_dem_grid(TERRAIN_DEM)
    stream = _wire_frame(
        0,
        stamp_ns=1_000_000_000,
        latitude_deg=terrain_dem.origin_latitude_deg,
        longitude_deg=terrain_dem.origin_longitude_deg,
        altitude_m=terrain_dem.origin_elevation_m + 10.0,
    ) + _wire_frame(
        1,
        stamp_ns=1_033_333_333,
        latitude_deg=terrain_dem.origin_latitude_deg,
        longitude_deg=terrain_dem.origin_longitude_deg,
        altitude_m=terrain_dem.origin_elevation_m + 10.0,
        position_east_m=1.5,
        position_north_m=2.5,
        position_up_m=4.0,
        velocity_flu_mps=(7.0, 0.0, 0.0),
    )
    process = _FakeProcess(stream)
    backend = GazeboSimBackend(
        scenario_input=runtime_input,
        camera_calibration=_camera_calibration(),
        docker_executable=Path(sys.executable).resolve(),
        container_name="owned-gazebo-sim",
        frame_count=2,
        airframe_profile_hash="a" * 64,
        container_image_sha256="b" * 64,
        stream_script_sha256="c" * 64,
        model_assets_sha256="d" * 64,
        terrain_dem=terrain_dem,
        expected_width=2,
        expected_height=1,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    assert backend.descriptor.mode is RuntimeMode.SIM
    assert backend.descriptor.profile == RuntimeProfile.sim()
    assert backend.descriptor.recording_hash is None
    assert backend.descriptor.scenario_id == runtime_input.scenario_id
    assert backend.descriptor.scenario_manifest_hash == runtime_input.scenario_manifest_hash
    assert backend.descriptor.scenario_seed == runtime_input.seed
    expected_topics = {
        "schema_version": 1,
        "world": "default",
        "model_instance": "standard_vtol_mono_cam_0",
        "image_topic": (
            "/world/default/model/standard_vtol_mono_cam_0/link/camera_link/sensor/camera/image"
        ),
        "camera_info_topic": (
            "/world/default/model/standard_vtol_mono_cam_0/"
            "link/camera_link/sensor/camera/camera_info"
        ),
        "navsat_topic": (
            "/world/default/model/standard_vtol_mono_cam_0/"
            "link/base_link/sensor/navsat_sensor/navsat"
        ),
        "odometry_topic": "/model/standard_vtol_mono_cam_0/odometry_with_covariance",
    }
    expected_topic_hash = canonical_sha256(expected_topics)
    binding = backend.sensor_binding
    assert binding == {
        "schema_version": 1,
        "sensor_id": "front-camera",
        "kind": "rgb_camera",
        "calibration_id": "gazebo-front-camera-v1",
        "camera_calibration_sha256": canonical_sha256(asdict(_camera_calibration())),
        "stream_uri": "gazebo://river_rescue_vtol_001/front-camera",
        "topic_binding": expected_topics,
        "topic_binding_sha256": expected_topic_hash,
        "source_row_order": GAZEBO_SIM_SOURCE_ROW_ORDER,
        "wire_row_order": GAZEBO_SIM_WIRE_ROW_ORDER,
        "vertical_flip_applied": GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        "camera_info_intrinsics_semantic_validation": (
            GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
        ),
    }
    assert GAZEBO_SIM_SOURCE_ROW_ORDER == "top_to_bottom"
    assert GAZEBO_SIM_WIRE_ROW_ORDER == "top_to_bottom"
    assert GAZEBO_SIM_VERTICAL_FLIP_APPLIED is False
    assert backend.topic_binding_hash == expected_topic_hash
    command = backend.frame_source.command
    for option, expected in (
        ("--image-topic", expected_topics["image_topic"]),
        ("--camera-info-topic", expected_topics["camera_info_topic"]),
        ("--navsat-topic", expected_topics["navsat_topic"]),
        ("--odometry-topic", expected_topics["odometry_topic"]),
    ):
        index = command.index(option)
        assert command[index + 1] == expected
    assert not hasattr(backend, "ground_truth")
    assert not hasattr(backend, "truth")
    backend.frame_source.open()
    first = backend.frame_source.read()
    second = backend.frame_source.read()
    assert first.image_bgr.shape == (1, 2, 3)
    assert second.frame_id == "gazebo-sim-000001"
    provenance = backend.frame_source.frame_provenance
    assert isinstance(provenance, tuple)
    assert len(provenance) == 2
    first_provenance = provenance[0]
    assert first_provenance.frame_id == first.frame_id
    assert first_provenance.sequence == 0
    assert first_provenance.captured_at_s == first.captured_at_s
    assert first_provenance.captured_at_clock == "host_monotonic"
    assert first_provenance.sensor_stamp_ns == 1_000_000_000
    assert first_provenance.camera_info_stamp_ns == 999_999_997
    assert first_provenance.navsat_stamp_ns == 999_999_998
    assert first_provenance.odometry_stamp_ns == 999_999_999
    assert first_provenance.camera_info_source_skew_ns == 3
    assert first_provenance.navsat_source_skew_ns == 2
    assert first_provenance.odometry_source_skew_ns == 1
    assert (
        first_provenance.payload_sha256
        == hashlib.sha256(memoryview(first.image_bgr).cast("B")).hexdigest()
    )
    assert len(first_provenance.record_sha256) == 64
    assert (first_provenance.width, first_provenance.height) == (2, 1)
    assert first_provenance.stride == 6
    assert first_provenance.payload_bytes == 6
    assert first_provenance.flags == (
        GAZEBO_SIM_FLAG_CAMERA_INFO
        | GAZEBO_SIM_FLAG_NAVSAT
        | GAZEBO_SIM_FLAG_ODOMETRY
        | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG
    )
    assert first_provenance.pixel_format == "BGR24"
    assert first_provenance.wire_unsigned_header_hex is not None
    assert first_provenance.wire_telemetry is not None
    assert first_provenance.frame_telemetry is not None
    reconstructed = reconstruct_gazebo_sim_wire_frame(
        unsigned_header_hex=first_provenance.wire_unsigned_header_hex,
        payload_bgr24=memoryview(first.image_bgr).cast("B"),
        expected_sequence=0,
        expected_width=2,
        expected_height=1,
    )
    assert reconstructed.record_sha256 == first_provenance.record_sha256
    assert first_provenance.wire_telemetry.latitude_deg == pytest.approx(
        terrain_dem.origin_latitude_deg
    )
    assert first_provenance.frame_telemetry.altitude_agl_m == pytest.approx(10.0)
    assert first_provenance.frame_telemetry.heading_deg == pytest.approx(90.0)
    assert first_provenance.frame_telemetry.attitude_roll_sigma_deg == pytest.approx(
        math.degrees(0.01)
    )
    assert first_provenance.frame_telemetry.attitude_pitch_sigma_deg == pytest.approx(
        math.degrees(0.02)
    )
    assert first_provenance.frame_telemetry.attitude_heading_sigma_deg == pytest.approx(
        math.degrees(0.03)
    )
    assert (
        first_provenance.frame_telemetry.attitude_uncertainty_source
        == "gazebo_odometry_covariance_with_configured_floor"
    )
    assert not hasattr(first_provenance, "payload_bgr24")
    assert not hasattr(first_provenance, "image_bgr")
    with pytest.raises(FrozenInstanceError):
        first_provenance.sequence = 99  # type: ignore[misc]
    assert backend.frame_source.latest_frame_provenance == provenance[-1]
    telemetry = backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic())
    assert telemetry.latitude_deg == pytest.approx(terrain_dem.origin_latitude_deg)
    assert telemetry.position_healthy is True
    assert telemetry.flight_mode_allows_deploy is False
    assert telemetry.gps_absolute_altitude_m == pytest.approx(terrain_dem.origin_elevation_m + 10.0)
    assert telemetry.altitude_agl_m == pytest.approx(10.0)
    assert telemetry.altitude_reference == GAZEBO_SIM_TERRAIN_AGL_REFERENCE
    assert telemetry.gps_fix_type == 3
    assert telemetry.satellites_visible == 12
    assert telemetry.gps_horizontal_accuracy_m == pytest.approx(1.5)
    assert telemetry.gps_vertical_accuracy_m == pytest.approx(2.5)
    assert telemetry.local_north_m == pytest.approx(2.5)
    assert telemetry.local_east_m == pytest.approx(1.5)
    assert telemetry.local_down_m == pytest.approx(-4.0)
    # Identity Gazebo FLU orientation points forward along ENU east, which is
    # a canonical NED heading of 90 degrees.
    assert telemetry.heading_deg == pytest.approx(90.0)
    assert telemetry.roll_deg == pytest.approx(0.0)
    assert telemetry.pitch_deg == pytest.approx(0.0)
    assert telemetry.velocity_north_mps == pytest.approx(0.0)
    assert telemetry.velocity_east_mps == pytest.approx(7.0)
    assert telemetry.ground_speed_mps == pytest.approx(7.0)
    backend.frame_source.close()
    stats = backend.frame_source.require_complete()
    assert stats.complete is True
    assert stats.odometry_timestamp_frame_count == 2
    assert stats.odometry_required is True
    assert stats.full_odometry_pose_velocity_fusion_claimed is True
    assert stats.simulated_gps_fix_type == 3
    assert stats.simulated_satellites_visible == 12
    assert stats.sensor_id == "front-camera"
    assert stats.calibration_id == "gazebo-front-camera-v1"
    assert stats.stream_uri == "gazebo://river_rescue_vtol_001/front-camera"
    assert stats.image_topic == expected_topics["image_topic"]
    assert stats.camera_info_topic == expected_topics["camera_info_topic"]
    assert stats.navsat_topic == expected_topics["navsat_topic"]
    assert stats.odometry_topic == expected_topics["odometry_topic"]
    assert stats.topic_binding_sha256 == expected_topic_hash
    assert stats.sensor_binding_sha256 == backend.sensor_binding_hash
    assert stats.source_row_order == GAZEBO_SIM_SOURCE_ROW_ORDER
    assert stats.wire_row_order == GAZEBO_SIM_WIRE_ROW_ORDER
    assert stats.vertical_flip_applied is GAZEBO_SIM_VERTICAL_FLIP_APPLIED
    assert (
        stats.camera_info_intrinsics_semantic_validation
        == GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
    )
    backend.close()
