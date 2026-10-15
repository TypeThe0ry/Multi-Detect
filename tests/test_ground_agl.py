from __future__ import annotations

import math
from dataclasses import replace

import pytest

from multidetect.autonomy.ground_model import FlatGround, GroundProjection
from multidetect.sim_teacher.streams import (
    MavlinkReader,
    MavlinkState,
    SensorSample,
    build_observation,
)


def sample():
    return SensorSample(
        rgb=b"\x00\x00\x00",
        width=1,
        height=1,
        depth=None,
        camera_info={"fx": 1, "fy": 1},
        pose={},
        velocity=[0, 0, 0],
        sensor_stamp_ns=2_000_000_000,
        received_monotonic_ns=2_000_000_000,
        received_wall_time=2,
    )


def state(**messages):
    return MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"x": 0, "y": 0, "z": -20},
            "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
            "GLOBAL_POSITION_INT": {"relative_alt": 999_000},
            "GPS_RAW_INT": {"fix_type": 3},
            **messages,
        },
        received_monotonic_ns={key: 1_800_000_000 for key in (*messages, "ATTITUDE")},
    )


def distance(**fields):
    return {
        "orientation": 25,
        "current_distance": 1200,
        "min_distance": 10,
        "max_distance": 2000,
        "signal_quality": 100,
        **fields,
    }


def test_downward_range_is_centimetres_and_verticalized_and_opt_in():
    telemetry = state(DISTANCE_SENSOR=distance())
    telemetry.messages["ATTITUDE"]["roll"] = math.radians(30)
    before = build_observation(sample(), telemetry)
    assert "ground_agl" not in before
    observation = build_observation(sample(), telemetry, ground_agl_max_age_s=1)
    assert observation["ground_agl"]["height_m"] == pytest.approx(12 * math.cos(math.pi / 6))
    ground = GroundProjection(use_agl=True).for_observation(observation)
    assert ground == FlatGround(20 - observation["ground_agl"]["height_m"])
    assert "DISTANCE_SENSOR" in MavlinkReader.TRACKED
    assert "ALTITUDE" in MavlinkReader.TRACKED


@pytest.mark.parametrize(
    "message",
    [
        distance(orientation=0),
        distance(current_distance=3000),
        distance(current_distance=0),
        distance(signal_quality=1),
        distance(current_distance=math.nan),
    ],
)
def test_invalid_range_falls_back_to_terrain_agl(message):
    observation = build_observation(
        sample(),
        state(DISTANCE_SENSOR=message, ALTITUDE={"altitude_terrain": 8}),
        ground_agl_max_age_s=1,
    )
    assert observation["ground_agl"] == {"height_m": 8, "age_s": 0.2, "source": "ALTITUDE"}
    assert GroundProjection(use_agl=True).for_observation(observation) == FlatGround(12)


@pytest.mark.parametrize(
    "fields",
    [
        {"altitude_terrain": 7},
        {"altitude_terrain": math.nan, "bottom_clearance": 7},
        {"altitude_terrain": -1000, "bottom_clearance": 7},
    ],
)
def test_altitude_terrain_is_already_agl_not_msl(fields):
    observation = build_observation(sample(), state(ALTITUDE=fields), ground_agl_max_age_s=1)
    assert observation["ground_agl"]["height_m"] == 7
    assert GroundProjection(use_agl=True).for_observation(observation) == FlatGround(13)


@pytest.mark.parametrize(
    "message",
    [{}, {"altitude_terrain": -1000}, {"altitude_terrain": math.nan}, {"bottom_clearance": -1}],
)
def test_missing_or_invalid_altitude_does_not_infer_agl_from_relative_alt(message):
    observation = build_observation(sample(), state(ALTITUDE=message), ground_agl_max_age_s=1)
    assert "ground_agl" not in observation


def test_stale_future_or_unstamped_measurement_is_ignored():
    telemetry = state(DISTANCE_SENSOR=distance())
    for stamp in (None, 1, 3_000_000_000):
        telemetry.received_monotonic_ns = {} if stamp is None else {"DISTANCE_SENSOR": stamp}
        observation = build_observation(sample(), telemetry, ground_agl_max_age_s=1)
        assert "ground_agl" not in observation
    telemetry.received_monotonic_ns = {"DISTANCE_SENSOR": 1_800_000_000}
    old_frame = replace(sample(), received_monotonic_ns=1_000_000_000)
    assert "ground_agl" not in build_observation(old_frame, telemetry, ground_agl_max_age_s=1)


def test_zero_advertised_minimum_accepts_positive_range():
    observation = build_observation(
        sample(), state(DISTANCE_SENSOR=distance(min_distance=0)), ground_agl_max_age_s=1
    )
    assert observation["ground_agl"]["height_m"] == 12


@pytest.mark.parametrize("attitude_stamp", [None, 1, 3_000_000_000, 1_400_000_000])
def test_range_requires_fresh_synchronized_attitude(attitude_stamp):
    telemetry = state(DISTANCE_SENSOR=distance(), ALTITUDE={"altitude_terrain": 9})
    telemetry.messages["ATTITUDE"]["roll"] = math.radians(60)
    if attitude_stamp is None:
        del telemetry.received_monotonic_ns["ATTITUDE"]
    else:
        telemetry.received_monotonic_ns["ATTITUDE"] = attitude_stamp
    observation = build_observation(sample(), telemetry, ground_agl_max_age_s=1)
    assert observation["ground_agl"] == {"height_m": 9, "age_s": 0.2, "source": "ALTITUDE"}
