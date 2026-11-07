import json
from pathlib import Path

from multidetect.sim_teacher.vehicle_loss import VehicleLossDetector, replay_vehicle_loss


def _observation(altitude_m: float, *, roll_deg: float = 0.0, landed_state: int = 2):
    return {
        "pose": {"local_ned_m": [0.0, 0.0, -altitude_m]},
        "attitude": {"roll_deg": roll_deg, "pitch_deg": 0.0},
        "landed": landed_state == 1,
        "flight_state": {"landed_state": landed_state},
    }


def test_landing_descent_and_landed_tumble_are_not_vehicle_loss():
    detector = VehicleLossDetector()
    high = _observation(10.0)
    for altitude in (6.0, 3.0, 1.5, 0.4):
        low = _observation(altitude, roll_deg=130.0)
        assert detector.update(high, low, {"kind": "translate", "up_m": -5.0}, [0, 0, 5]) is None
        high = low
    assert detector.update(high, _observation(0.2, roll_deg=170.0, landed_state=1),
                           {"kind": "land", "up_m": 0.0}, [0, 0, 0]) is None


def test_near_ground_tumble_requires_consecutive_steps():
    detector = VehicleLossDetector()
    current = _observation(1.0)
    for _ in range(2):
        after = _observation(1.0, roll_deg=120.0)
        assert detector.update(current, after, {"kind": "hold", "up_m": 0.0}, [0, 0, 0]) is None
        current = after
    after = _observation(1.0, roll_deg=120.0)
    event = detector.update(current, after, {"kind": "hold", "up_m": 0.0}, [0, 0, 0])
    assert event and event["reason"] == "sustained_tumble_near_ground"


def test_replays_operator_abort_fall_from_read_only_artifact():
    episode = Path(
        "artifacts/benchmarks/nightly-linux-r21idr3-r24plan-28-head/main/operator-abort/"
        "campaign/gen_cnt2_w0003/episodes/episode-0000-06a1a47d/steps.jsonl"
    )
    steps = [json.loads(line) for line in episode.read_text(encoding="utf-8").splitlines()]
    assert replay_vehicle_loss(steps) == 38
