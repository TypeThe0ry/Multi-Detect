"""CPU-only coverage checks for the geometric SIM search teacher."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pytest

from multidetect.sim_teacher import ExpertSearchPolicy, body_to_ned
from multidetect.sim_teacher.policies import target_in_fov
from scripts.collect_sim_teacher_episodes import DEFAULT_CAMERA_INFO

SEARCH_RADIUS_M = 120.0
STANDOFF_M = 15.0
TRIALS = 200
STEP_BUDGET = 1200


@dataclass
class KinematicVehicle:
    """Setpoint-like plant: action endpoints are achieved with bounded error."""

    rng: random.Random
    noisy: bool
    north: float = 0.0
    east: float = 0.0
    down: float = -5.0
    yaw_deg: float = 0.0
    commanded_north: float = 0.0
    commanded_east: float = 0.0
    commanded_down: float = -5.0
    commanded_yaw_deg: float = 0.0

    @property
    def pose(self) -> tuple[float, float, float]:
        return self.north, self.east, self.down

    def apply(self, action) -> None:
        if action.kind == "translate":
            assert math.hypot(action.forward_m, action.right_m) <= 15.0 + 1e-6
            dn, de, dd = body_to_ned(
                action.forward_m, action.right_m, action.up_m, self.commanded_yaw_deg
            )
            self.commanded_north += dn
            self.commanded_east += de
            self.commanded_down += dd
        elif action.kind == "turn":
            self.commanded_yaw_deg += action.yaw_delta_deg
        assert math.hypot(self.commanded_north, self.commanded_east) <= SEARCH_RADIUS_M + 1e-6
        perturbation = 0.3 if self.noisy else 0.0
        angle_error = 5.0 if self.noisy else 0.0
        self.north = self.commanded_north + self.rng.uniform(-perturbation, perturbation)
        self.east = self.commanded_east + self.rng.uniform(-perturbation, perturbation)
        self.down = self.commanded_down + self.rng.uniform(-perturbation, perturbation)
        self.yaw_deg = self.commanded_yaw_deg + self.rng.uniform(-angle_error, angle_error)
        assert math.hypot(self.north, self.east) <= SEARCH_RADIUS_M + 1e-6
        assert self.commanded_down == pytest.approx(-5.0)
        assert abs(self.down + 5.0) <= perturbation + 1e-6


def _policy(target: tuple[float, float, float]) -> ExpertSearchPolicy:
    return ExpertSearchPolicy(
        target_ned=target,
        intrinsics=DEFAULT_CAMERA_INFO,
        image_size=(1280, 960),
        standoff_m=STANDOFF_M,
        search_radius_m=SEARCH_RADIUS_M,
        max_detection_range_m=80.0,
    )


def _trial(target: tuple[float, float, float], *, seed: int, noisy: bool) -> tuple[bool, bool]:
    policy = _policy(target)
    vehicle = KinematicVehicle(random.Random(seed), noisy)
    acquired = False
    for _ in range(STEP_BUDGET):
        labels = policy.labels(pose_ned=vehicle.pose, yaw_deg=vehicle.yaw_deg)
        assert {"phase", "target_in_fov", "target_bearing_deg", "target_range_m"} <= labels.keys()
        in_fov, bearing, distance = target_in_fov(
            target,
            vehicle.pose,
            vehicle.yaw_deg,
            DEFAULT_CAMERA_INFO,
            (1280, 960),
            max_range_m=80.0,
        )
        assert labels["target_in_fov"] == in_fov
        assert labels["target_bearing_deg"] == pytest.approx(bearing)
        assert labels["target_range_m"] == pytest.approx(distance)
        acquired |= in_fov
        action = policy.next_action(pose_ned=vehicle.pose, yaw_deg=vehicle.yaw_deg)
        vehicle.apply(action)
        if action.kind == "stop":
            break
    final_range = math.dist(vehicle.pose, target)
    return acquired, action.kind == "stop" and abs(final_range - STANDOFF_M) <= 3.0


@pytest.mark.parametrize("noisy", [False, True], ids=["perfect", "tracking_error"])
def test_random_target_search_acquisition_and_standoff(noisy: bool) -> None:
    placement_rng = random.Random(20261010)
    targets = []
    for _ in range(TRIALS):
        radius = SEARCH_RADIUS_M * math.sqrt(placement_rng.random())
        theta = placement_rng.uniform(-math.pi, math.pi)
        targets.append((radius * math.cos(theta), radius * math.sin(theta), -5.0))
    results = [
        _trial(target, seed=1000 + index, noisy=noisy)
        for index, target in enumerate(targets)
    ]
    acquired = sum(result[0] for result in results)
    at_standoff = sum(result[1] for result in results)
    print(f"{noisy=}: acquired={acquired}/{TRIALS}; standoff={at_standoff}/{TRIALS}")
    assert acquired / TRIALS >= 0.95
    assert at_standoff / TRIALS >= 0.90


def test_hidden_target_does_not_steer_search() -> None:
    """Search commands share the same prior and must ignore hidden target coordinates."""
    policies = [_policy((-115.0, 0.0, -5.0)), _policy((0.0, -115.0, -5.0))]
    vehicle = KinematicVehicle(random.Random(11), False)
    for _ in range(20):
        if any(
            target_in_fov(
                policy.target_ned,
                vehicle.pose,
                vehicle.yaw_deg,
                DEFAULT_CAMERA_INFO,
                (1280, 960),
                max_range_m=80.0,
            )[0]
            for policy in policies
        ):
            break
        actions = [
            policy.next_action(pose_ned=vehicle.pose, yaw_deg=vehicle.yaw_deg)
            for policy in policies
        ]
        assert actions[0] == actions[1]
        vehicle.apply(actions[0])
