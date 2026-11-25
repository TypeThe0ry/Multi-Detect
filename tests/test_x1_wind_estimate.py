from __future__ import annotations

import math
import random

import pytest

from multidetect.autonomy.x1_wind_estimate import WindEstimator

MS = 1_000_000


def rows(t_ms, *, heading=0.0, pitch=0.0, bank=0.0, speed=16.0,
         wind=(8, -4), noise=(0, 0), error=0, tailsitter=False, global_only=False):
    def at(offset=0, **fields):
        return {"receive_monotonic_ns": (t_ms + offset) * MS, **fields}

    # In the FW frame a roll about the forward axis must not affect air velocity.
    p, q = math.sin(pitch), math.cos(pitch)
    c, s = math.cos(heading), math.sin(heading)
    forward = (c * q, s * q, -p)
    if tailsitter:
        # FW->NED rotation applied to MC basis X=FW Z, Y=FW Y, Z=-FW X.
        cb, sb = math.cos(bank), math.sin(bank)
        mc_x = (c * p * cb + s * sb, s * p * cb - c * sb, q * cb)
        mc_y_down = q * sb
        mc_z_down = -forward[2]
        roll = math.atan2(mc_y_down, mc_z_down)
        pitch_mc = math.asin(-mc_x[2])
        yaw = math.atan2(mc_x[1], mc_x[0])
    else:
        roll, pitch_mc, yaw = bank, pitch, heading
    result = {
        "VFR_HUD": at(airspeed=speed + error, heading=round(math.degrees(yaw)) % 360),
        "ATTITUDE": at(-15, roll=roll, pitch=pitch_mc, yaw=yaw),
        "EXTENDED_SYS_STATE": at(vtol_state=4, landed_state=2),
    }
    vn, ve = (wind[i] + speed * forward[i] + noise[i] for i in range(2))
    vd = speed * forward[2]
    if global_only:
        result["GLOBAL_POSITION_INT"] = at(
            -25, vx=round(100 * vn), vy=round(100 * ve), vz=round(100 * vd),
        )
    else:
        result["LOCAL_POSITION_NED"] = at(-25, vx=vn, vy=ve, vz=vd)
    return result


@pytest.mark.parametrize("tailsitter,global_only", [(False, False), (True, False), (True, True)])
def test_seeded_turns_with_pitch_and_outliers_are_deterministic(tailsitter, global_only):
    def run():
        rng = random.Random(913)
        estimator = WindEstimator(tailsitter=tailsitter)
        trace = []
        for i in range(60):
            t = 1000 + 200 * i
            noise = tuple(rng.gauss(0, 0.25) for _ in range(2)) if i % 11 != 7 else (7, -6)
            sample = rows(t, heading=i * 0.10, pitch=0.14 * math.cos(i / 7), bank=0.3,
                          noise=noise, error=rng.gauss(0, 0.2), tailsitter=tailsitter,
                          global_only=global_only)
            trace.append(estimator.update(sample, now_ns=t * MS))
        return trace

    trace = run()
    assert trace == run()
    assert trace[:4] == [None] * 4
    assert all(reading is not None for reading in trace[4:])
    assert any(reading.confidence < 1 for reading in trace[4:])
    assert max(math.dist(reading.ned_mps[:2], (8, -4)) for reading in trace[4:]) <= 0.5
    assert all(reading.ned_mps[2] is None for reading in trace[4:])


def test_same_airspeed_packet_cannot_be_counted_repeatedly_or_refresh_age():
    estimator = WindEstimator()
    for i in range(5):
        sample = rows(1000 + 40 * i)
        sample["VFR_HUD"]["receive_monotonic_ns"] = 1000 * MS
        assert estimator.update(sample, now_ns=(1000 + 40 * i) * MS) is None
    for i in range(1, 5):
        t = 1200 + 100 * i
        sample = rows(t)
        reading = estimator.update(sample, now_ns=t * MS)
    assert reading is not None
    assert reading.telemetry(t) == {"north_mps": 8.0, "east_mps": -4.0, "down_mps": None,
                                   "age_ms": 25, "source": "mavlink"}
    assert estimator.update(sample, now_ns=(t + 300) * MS) == reading
    assert reading.age_ms(t + 300) == 325
    assert estimator.update(sample, now_ns=(t + 1001) * MS) is None


@pytest.mark.parametrize("invalid", ["hover", "transition", "landed", "stale_mode", "bad_speed",
                                     "low_speed", "bad_attitude", "bad_velocity", "skew",
                                     "pitot_unhealthy"])
def test_unusable_input_returns_unknown_even_after_lock(invalid):
    estimator = WindEstimator()
    for t in range(1000, 2001, 200):
        reading = estimator.update(rows(t), now_ns=t * MS)
    assert reading is not None
    sample = rows(2200)
    if invalid in {"hover", "transition"}:
        sample["EXTENDED_SYS_STATE"]["vtol_state"] = 3 if invalid == "hover" else 1
    elif invalid == "landed":
        sample["EXTENDED_SYS_STATE"]["landed_state"] = 1
    elif invalid == "stale_mode":
        sample["EXTENDED_SYS_STATE"]["receive_monotonic_ns"] = 699 * MS
    elif invalid in {"bad_speed", "low_speed"}:
        sample["VFR_HUD"]["airspeed"] = float("nan") if invalid == "bad_speed" else 11.9
    elif invalid == "bad_attitude":
        del sample["ATTITUDE"]["pitch"]
    elif invalid == "bad_velocity":
        sample["LOCAL_POSITION_NED"]["vz"] = float("inf")
    elif invalid == "skew":
        sample["ATTITUDE"]["receive_monotonic_ns"] = 1949 * MS
    elif invalid == "pitot_unhealthy":
        sample["SYS_STATUS"] = {"receive_monotonic_ns": 2200 * MS,
                                "onboard_control_sensors_present": 16,
                                "onboard_control_sensors_health": 0}
    assert estimator.update(sample, now_ns=2200 * MS) is None
    if invalid not in {"bad_velocity", "skew"}:
        assert estimator.update(rows(2400), now_ns=2400 * MS) is None


def test_filter_abstains_on_inconsistent_samples_and_converges_after_step():
    estimator = WindEstimator()
    for t in range(1000, 3400, 200):
        assert estimator.update(rows(t, wind=(8 if t % 400 else 0, 0)), now_ns=t * MS) is None
    for t in range(3400, 5800, 200):
        reading = estimator.update(rows(t), now_ns=t * MS)
    assert reading.ned_mps == pytest.approx((8.0, -4.0, None))
    assert reading.support_since_ns > 3400 * MS


def test_small_wind_is_not_rounded_to_zero_or_replaced_by_the_scenario_command():
    estimator = WindEstimator()
    for t in range(1000, 2001, 200):
        reading = estimator.update(rows(t, wind=(0.4, -0.3)), now_ns=t * MS)
    assert reading.ned_mps[:2] == pytest.approx((0.4, -0.3))
