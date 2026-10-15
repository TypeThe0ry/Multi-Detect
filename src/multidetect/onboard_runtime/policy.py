"""Coordinate adaptation around the shared SIM/aircraft OnboardPolicy."""

from __future__ import annotations

from typing import Any


class HomeRelativePolicy:
    """Keep the existing policy's altitude origin at the surveyed home plane."""

    def __init__(self, policy: Any, home_down_m: float, proposer: Any = None) -> None:
        self.policy = policy
        self.home_down_m = home_down_m
        self.proposer = proposer

    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        position = (pose_ned[0], pose_ned[1], pose_ned[2] - self.home_down_m)
        observation = {
            **observation, "pose": {**observation["pose"], "local_ned_m": position},
        }
        return self.policy.act(observation, pose_ned=position, yaw_deg=yaw_deg)

    def close(self) -> None:
        if self.proposer is not None:
            stopped = getattr(self.proposer, "stopped", None)
            if stopped is not None:
                stopped.set()
        self.policy.close()
