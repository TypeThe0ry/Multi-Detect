"""Cheap, complete fixtures for onboard autonomy unit tests.

The production constructors intentionally own a fair amount of optional state.
Tests that need to exercise one method should use these factories instead of
``Class.__new__`` and hand-picking attributes: this keeps test doubles aligned
with the constructor contract as that contract evolves.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from multidetect.autonomy.mission_executor import MissionExecutor, plan_for_find_task
from multidetect.autonomy.onboard_loop import CameraModel, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts.eval_slow_system import LlamaServerRunner
from scripts.onboard_policy import OnboardPolicy


class _StoppedProcess:
    """Process-shaped object used by the no-server Llama runner fixture."""

    returncode = 0

    def poll(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def make_llama_runner(**overrides: Any) -> LlamaServerRunner:
    """Return a complete ``LlamaServerRunner`` without starting llama-server.

    The real constructor is deliberately not called: starting an external
    inference server would make a unit test depend on local model artifacts.
    Every field initialized by ``LlamaServerRunner.__init__`` is populated with
    the same documented default, and callers may override any field.
    """

    runner = object.__new__(LlamaServerRunner)
    defaults: dict[str, Any] = {
        "port": 9933,
        "max_new_tokens": 128,
        "crop_policy": "none",
        "max_side": 448,
        "fast_identity": False,
        "_verdict_tokens": {},
        "_verdict_pieces": {},
        "identity_variant": None,
        "cache_prompt": True,
        "identity_only": False,
        "logprobs": False,
        "last_logprobs": None,
        "last_usage": None,
        "server_log": SimpleNamespace(flush=lambda: None, name="<test-runner>"),
        "process": _StoppedProcess(),
        "max_restarts": 0,
        "restarts": [],
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        setattr(runner, name, value)
    return runner


def make_policy(
    *,
    work_dir: Path | None = None,
    config: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> OnboardPolicy:
    """Construct an ``OnboardPolicy`` with model resources replaced by stubs."""

    work_dir = Path.cwd() / ".pytest-onboard-policy" if work_dir is None else Path(work_dir)
    constructor_names = {
        "task_text", "world_path", "altitude_m", "camera_info", "image_size",
        "config", "proposer", "slow_runner", "ground_projector", "home_ned",
    }
    constructor = {
        "task_text": "Find the target.",
        "world_path": None,
        "altitude_m": 20.0,
        "camera_info": {"fx": 320.0, "fy": 320.0, "cx": 160.0, "cy": 120.0},
        "image_size": (320, 240),
        "work_dir": work_dir,
        "config": dict(config or {}),
        "proposer": lambda frame: [],
        "slow_runner": make_llama_runner(),
    }
    for name in tuple(overrides):
        if name in constructor_names:
            constructor[name] = overrides.pop(name)
    policy = OnboardPolicy(**constructor)
    for name, value in overrides.items():
        setattr(policy, name, value)
    return policy


def _default_loop(*args: Any, **kwargs: Any) -> OnboardLoop:
    return OnboardLoop(
        camera=CameraModel(fx=320.0, fy=320.0, cx=160.0, cy=120.0, width=320, height=240),
        search_action=lambda pose, yaw: ActionPrimitive.hold(1.0),
        segment_clear=lambda pose, north, east: True,
    )


def make_executor(
    plan: Any | None = None,
    *,
    loop_factory: Any = _default_loop,
    **overrides: Any,
) -> MissionExecutor:
    """Construct a ``MissionExecutor`` with a deterministic lightweight loop."""

    kwargs = {"loop_factory": loop_factory}
    kwargs.update(overrides)
    return MissionExecutor(plan or plan_for_find_task("target"), **kwargs)


__all__ = ["make_executor", "make_llama_runner", "make_policy"]
