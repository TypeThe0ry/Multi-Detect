from __future__ import annotations

import test_mission_executor as helpers

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor


def _executor(radius: float, *, maximum: int = 220) -> MissionExecutor:
    plan = helpers._count_plan(10)
    plan.steps[0].params["region"] = "zone:test"
    return MissionExecutor(
        plan,
        loop_factory=helpers._factory(),
        refs={"zone:test": (0.0, 0.0, radius)},
        config=ExecutorConfig(step_budget=140, count_budget_max=maximum, runtime_step_limit=220),
    )


def test_count_budget_covers_large_region_and_registered_revisits() -> None:
    executor = _executor(55.0)
    run = executor._run
    assert run is not None
    executor._pending_rejection_places = lambda _run: [{"centre": (1, 1)}] * 3
    executor._pending_small_unjudged_places = lambda _run: [{"centre": (2, 2)}] * 2
    budget = executor._budget(executor.current_step, run)
    assert budget > 140
    assert budget <= 220


def test_runtime_limit_leaves_return_home_tail() -> None:
    plan = helpers._count_plan(10)
    plan.steps[0].params["region"] = "zone:test"
    executor = MissionExecutor(
        plan,
        loop_factory=helpers._factory(),
        refs={"zone:test": (0.0, 0.0, 55.0)},
        config=ExecutorConfig(step_budget=140, count_budget_max=4096, runtime_step_limit=150),
    )
    assert executor._budget(executor.current_step, executor._run) + 10 <= 150


def test_missing_runtime_limit_keeps_legacy_fixed_budget() -> None:
    plan = helpers._count_plan(10)
    plan.steps[0].params["region"] = "zone:test"
    executor = MissionExecutor(
        plan,
        loop_factory=helpers._factory(),
        refs={"zone:test": (0.0, 0.0, 55.0)},
        config=ExecutorConfig(step_budget=140, count_budget_max=4096),
    )
    assert executor._budget(executor.current_step, executor._run) == 140


def test_small_region_does_not_inherit_large_fixed_budget() -> None:
    small_executor = _executor(5.0)
    small = small_executor._budget(small_executor.current_step, small_executor._run)
    large_executor = _executor(55.0)
    large = large_executor._budget(large_executor.current_step, large_executor._run)
    assert small < 140
    assert small < large


def test_energy_margin_blocks_adaptive_expansion() -> None:
    executor = _executor(55.0)
    run = executor._run
    assert run is not None
    executor._pending_rejection_places = lambda _run: [{"centre": (1, 1)}] * 4
    executor.energy_margin_s = 0.0
    assert executor._budget(executor.current_step, run) <= executor.config.step_budget

