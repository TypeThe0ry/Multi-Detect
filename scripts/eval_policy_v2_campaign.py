#!/usr/bin/env python3
"""Evaluate policy-v2 on a held-out SIM scenario list.

The evaluator owns campaign bookkeeping only.  One episode is delegated to
``scripts.rollout_policy_v2_sim`` (or to a runner supplied by a test).  This
keeps the collector/rollout lifecycle in one place while making aggregation
and baseline comparisons deterministic and usable without Docker.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import math
import statistics
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
DEFAULT_INDEX = ROOT / "configs" / "scenarios" / "generated" / "index.json"
DEFAULT_ROLLOUT = ROOT / "scripts" / "rollout_policy_v2_sim.py"
POLICY_NAMES = ("policy_v2", "scripted_expert", "constant_forward", "lawnmower")
POLICY_ALIASES = {"simple_lawnmower": "lawnmower", "constant-forward": "constant_forward"}


@dataclass(frozen=True, slots=True)
class Scenario:
    """The non-privileged campaign description for one generated world."""

    scenario_id: str
    scenario_path: Path
    split: str
    task_text: str | None = None
    truth_path: Path | None = None
    world_path: Path | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario": str(self.scenario_path),
            "split": self.split,
            "task_text": self.task_text,
            "truth": None if self.truth_path is None else str(self.truth_path),
            "world": None if self.world_path is None else str(self.world_path),
        }


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _scenario_id(path: Path) -> str:
    suffix = ".scenario.json"
    return path.name[: -len(suffix)] if path.name.endswith(suffix) else path.stem


def load_scenarios(index_path: Path = DEFAULT_INDEX, *, split: str = "test") -> list[Scenario]:
    """Load scenarios from generated/index.json, resolving paths relative to it."""

    document = _load_json(index_path)
    worlds = document.get("worlds") if isinstance(document, Mapping) else None
    if not isinstance(worlds, list):
        raise ValueError(f"scenario index has no worlds list: {index_path}")
    base = index_path.parent
    scenarios: list[Scenario] = []
    for raw in worlds:
        if not isinstance(raw, Mapping) or raw.get("split") != split:
            continue
        files = raw.get("files")
        files = files if isinstance(files, Mapping) else {}

        def file_path(
            name: str, fallback: str | None = None, *, source_files: Mapping[str, Any] = files
        ) -> Path | None:
            item = source_files.get(name)
            value = item.get("path") if isinstance(item, Mapping) else item
            if not isinstance(value, str):
                value = fallback
            if not value:
                return None
            candidate = (base / value).resolve()
            return candidate

        scenario_file = file_path("scenario", raw.get("world_file"))
        if scenario_file is None or not scenario_file.name.endswith(".scenario.json"):
            # Some indexes only provide world_file; retain useful behavior by
            # looking next to the index for the conventional generated name.
            world_id = raw.get("world_id")
            if not isinstance(world_id, str):
                continue
            scenario_file = (base / f"{world_id}.scenario.json").resolve()
        world_id = raw.get("world_id")
        if not isinstance(world_id, str) or not world_id.strip():
            world_id = _scenario_id(scenario_file)
        scenarios.append(
            Scenario(
                scenario_id=world_id,
                scenario_path=scenario_file,
                split=split,
                task_text=str(raw["task_text"]) if raw.get("task_text") is not None else None,
                truth_path=file_path("truth"),
                world_path=file_path("world", raw.get("world_file")),
            )
        )
    if not scenarios:
        raise ValueError(f"no scenarios with split={split!r} in {index_path}")
    return scenarios


# Friendly aliases used by small orchestration wrappers and tests.
load_scenario_index = load_scenarios
select_scenarios = load_scenarios


def scenarios_from_paths(paths: Sequence[str | Path], *, split: str = "test") -> list[Scenario]:
    """Build a campaign list from explicit scenario files or shell globs."""
    found: list[Scenario] = []
    seen: set[Path] = set()
    for raw in paths:
        text = str(raw)
        matches = (
            sorted(glob.glob(text, recursive=True)) if any(c in text for c in "*?[") else [text]
        )
        for item in matches:
            path = Path(item).resolve()
            if not path.is_file() or not path.name.endswith(".scenario.json") or path in seen:
                continue
            seen.add(path)
            found.append(Scenario(_scenario_id(path), path, split))
    if not found:
        raise ValueError("scenario paths did not match any .scenario.json files")
    return found


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _metrics_mapping(value: Any) -> dict[str, Any]:
    """Accept EpisodeMetrics, a mapping, or the ``(metrics, rows)`` runner result."""
    if isinstance(value, tuple) and value:
        value = value[0]
    if hasattr(value, "to_mapping"):
        value = value.to_mapping()
    if isinstance(value, Mapping):
        result = dict(value)
    else:
        result = {}
    # A few runners expose latency as *_latencies_ms lists rather than the
    # p50/p95 mapping emitted by EpisodeMetrics.
    for stem in ("policy", "detector"):
        key = f"{stem}_latencies_ms"
        if key in result and f"{stem}_latency_ms" not in result:
            values = [_number(item) for item in result[key]]
            result[f"{stem}_latency_ms"] = latency_percentiles(values)
    result["task_success"] = bool(result.get("task_success", result.get("success", False)))
    result["steps"] = int(_number(result.get("steps", 0)))
    result["path_length_m"] = _number(result.get("path_length_m", 0.0))
    safety = result.get("safety_events", {})
    result["safety_events"] = dict(safety) if isinstance(safety, Mapping) else {}
    return result


def latency_percentiles(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(_number(value) for value in values)
    if not ordered:
        return {"p50": 0.0, "p95": 0.0}

    def pick(q: float) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return ordered[index]

    return {"p50": pick(0.50), "p95": pick(0.95)}


def aggregate_metrics(reports: Iterable[Any]) -> dict[str, Any]:
    """Aggregate per-episode rollout metrics without dropping failed episodes."""
    rows = [_metrics_mapping(report) for report in reports]
    success = [bool(row["task_success"]) for row in rows]
    steps = [int(row["steps"]) for row in rows]
    paths = [_number(row["path_length_m"]) for row in rows]
    safety: dict[str, int] = {}
    policy_latencies: list[float] = []
    detector_latencies: list[float] = []
    for row in rows:
        for name, count in row["safety_events"].items():
            safety[str(name)] = safety.get(str(name), 0) + int(_number(count))
        for target, output in (
            (policy_latencies, row.get("policy_latencies_ms")),
            (detector_latencies, row.get("detector_latencies_ms")),
        ):
            if isinstance(output, Sequence) and not isinstance(output, (str, bytes, Mapping)):
                target.extend(_number(item) for item in output)
        for target, key in (
            (policy_latencies, "policy_latency_ms"),
            (detector_latencies, "detector_latency_ms"),
        ):
            output = row.get(key)
            if isinstance(output, Mapping):
                # Percentiles are retained as observations for campaign-level
                # summaries; raw lists, when supplied, take precedence above.
                target.extend(_number(output.get(name)) for name in ("p50", "p95"))
    return {
        "episode_count": len(rows),
        "success_count": sum(success),
        "success_rate": (sum(success) / len(rows)) if rows else 0.0,
        "task_success_rate": (sum(success) / len(rows)) if rows else 0.0,
        "steps_mean": statistics.fmean(steps) if steps else 0.0,
        "steps_median": statistics.median(steps) if steps else 0.0,
        "path_length_mean_m": statistics.fmean(paths) if paths else 0.0,
        "path_length_median": statistics.median(paths) if paths else 0.0,
        "safety_events": safety,
        "policy_latency_ms": latency_percentiles(policy_latencies),
        "detector_latency_ms": latency_percentiles(detector_latencies),
        "episodes": rows,
    }


def compare_baselines(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Return learned-policy deltas against each declared baseline."""
    learned = results.get("policy_v2", {})
    learned_rate = _number(learned.get("success_rate"))
    learned_safety = sum(
        int(_number(value)) for value in (learned.get("safety_events", {}) or {}).values()
    )
    comparisons: dict[str, Any] = {}
    for name, baseline in results.items():
        if name == "policy_v2":
            continue
        base_rate = _number(baseline.get("success_rate"))
        base_safety = sum(
            int(_number(value)) for value in (baseline.get("safety_events", {}) or {}).values()
        )
        comparisons[name] = {
            "success_rate_delta": learned_rate - base_rate,
            "path_length_delta_m": _number(learned.get("path_length_mean_m"))
            - _number(baseline.get("path_length_mean_m")),
            "steps_delta": _number(learned.get("steps_mean")) - _number(baseline.get("steps_mean")),
            "safety_event_delta": learned_safety - base_safety,
            "learned_beats_success": learned_rate > base_rate,
            "learned_safety_not_worse": learned_safety <= base_safety,
        }
    return comparisons


class ConstantForwardPolicy:
    """Simple fake policy used by the campaign baseline through the same runner."""

    def __init__(self, forward_m: float = 4.0) -> None:
        self.forward_m = forward_m

    def act(self, _state: Any) -> Any:
        from multidetect.autonomy.policy_v2.contracts import PolicyActionV2

        return PolicyActionV2("translate", forward_m=self.forward_m)


class LawnmowerPolicy:
    """Deterministic search-only fake policy (never uses target pose)."""

    def __init__(self, leg_steps: int = 8) -> None:
        self.leg_steps = max(1, int(leg_steps))
        self.steps = 0

    def act(self, _state: Any) -> Any:
        from multidetect.autonomy.policy_v2.contracts import PolicyActionV2

        self.steps += 1
        if self.steps % (self.leg_steps + 1) == 0:
            return PolicyActionV2("turn", yaw_delta_deg=90.0)
        return PolicyActionV2("translate", forward_m=4.0)


class ScriptedExpertPolicy:
    """Marker policy for a privileged upper-bound rollout.

    The live runner binds this marker to ``ExpertSearchPolicy`` and supplies
    the simulator truth only inside its expert-label path.  Keeping the
    marker as an object lets the campaign send expert, learned, and fake
    baselines through one ``policy`` argument without exposing truth to the
    learned state builder.
    """

    privileged = True

    def act(self, _state: Any) -> Any:
        # A runner that does not implement the privileged binding must fail
        # closed rather than accidentally executing a learned policy twice.
        from multidetect.autonomy.policy_v2.contracts import PolicyActionV2

        return PolicyActionV2("hold", seconds=0.2)


def _default_runner(**kwargs: Any) -> Any:
    """Build one collector plan and call the rollout script's live entrypoint."""
    module = importlib.import_module("scripts.rollout_policy_v2_sim")
    scenario = Path(kwargs["scenario"]).resolve()
    runtime, truth = module.collector._scenario_channels(scenario)
    output = Path(kwargs.get("output_dir") or ROOT / "artifacts" / "policy-v2-campaign")
    policy_name = str(kwargs.get("policy_name", "policy_v2"))
    plan = module.PolicyRolloutPlan(
        str(scenario),
        str(runtime),
        str(truth),
        str(Path(kwargs["model"]).resolve()) if kwargs.get("model") else str(module.DEFAULT_MODEL),
        str(module.DEFAULT_DETECTOR_MANIFEST),
        str((output / policy_name / scenario.stem).resolve()),
        episodes=1,
        max_steps=int(kwargs.get("max_steps", 200)),
        takeoff_altitude_m=kwargs.get("takeoff_altitude_m"),
    )
    if policy_name == "policy_v2":
        policy, detector, metadata = module._build_components(plan, providers=(), confidence=0.25)
    else:
        _, detector, metadata = module._build_components(plan, providers=(), confidence=0.25)
        policy = kwargs.get("policy")
        if policy is None:
            policy = (
                ScriptedExpertPolicy()
                if policy_name == "scripted_expert"
                else ConstantForwardPolicy()
            )
    return module.run_episode(
        plan,
        truth,
        0,
        policy=policy,
        detector=detector,
        metadata=metadata,
    )


def run_campaign(
    scenarios: Sequence[Scenario],
    *,
    model: Path | None = None,
    output_dir: Path | None = None,
    max_steps: int = 200,
    policies: Sequence[str] = POLICY_NAMES,
    runner: Callable[..., Any] | None = None,
    dry_run: bool = False,
    takeoff_altitude_m: float | None = None,
) -> dict[str, Any]:
    """Run each requested policy over every scenario and aggregate metrics."""
    policies = tuple(POLICY_ALIASES.get(name, name) for name in policies)
    unknown = set(policies) - set(POLICY_NAMES)
    if unknown:
        raise ValueError(f"unsupported policy baseline(s): {sorted(unknown)}")
    plan = {
        "scenario_count": len(scenarios),
        "scenarios": [item.to_mapping() for item in scenarios],
        "policies": list(policies),
        "max_steps": max_steps,
        "model": None if model is None else str(model),
    }
    if dry_run:
        return {"dry_run": True, "plan": plan}
    invoke = runner or _default_runner
    reports: dict[str, list[dict[str, Any]]] = {name: [] for name in policies}

    def invoke_runner(
        function: Callable[..., Any], scenario: Scenario, policy_name: str, baseline_policy: Any
    ) -> Any:
        kwargs = {
            "scenario": scenario.scenario_path,
            "scenario_id": scenario.scenario_id,
            "model": model,
            "output_dir": output_dir,
            "max_steps": max_steps,
            "policy_name": policy_name,
            "policy": baseline_policy,
            "takeoff_altitude_m": takeoff_altitude_m,
        }
        try:
            return function(**kwargs)
        except TypeError as first_error:
            # Keep the injection seam pleasant for tiny test doubles and old
            # wrappers that accepted only (scenario, policy_name).
            for args in (
                (scenario.scenario_path, policy_name),
                (scenario, policy_name),
                (scenario.scenario_path,),
            ):
                try:
                    return function(*args)
                except TypeError:
                    continue
            raise first_error

    for policy_name in policies:
        for scenario in scenarios:
            baseline_policy = (
                ConstantForwardPolicy()
                if policy_name == "constant_forward"
                else LawnmowerPolicy()
                if policy_name == "lawnmower"
                else ScriptedExpertPolicy()
                if policy_name == "scripted_expert"
                else None
            )
            result = invoke_runner(invoke, scenario, policy_name, baseline_policy)
            metric = _metrics_mapping(result)
            metric["scenario_id"] = scenario.scenario_id
            metric["policy"] = policy_name
            reports[policy_name].append(metric)
    aggregates = {name: aggregate_metrics(items) for name, items in reports.items()}
    return {
        "schema_version": 1,
        "contract": "multidetect.policy-v2-sim-campaign-v1",
        "plan": plan,
        "policies": aggregates,
        "comparisons": compare_baselines(aggregates),
    }


evaluate_campaign = run_campaign
aggregate_campaign = aggregate_metrics
aggregate_campaign_metrics = aggregate_metrics
compare_against_baselines = compare_baselines


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", "--scenario-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--scenarios", nargs="*", help="explicit scenario files or globs")
    parser.add_argument("--split", default="test", choices=("train", "val", "test"))
    parser.add_argument("--model", "--policy-model", type=Path)
    parser.add_argument("--output", "--out", type=Path)
    parser.add_argument("--dataset-dir", "--dataset", type=Path)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--takeoff-altitude-m", type=float)
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=(*POLICY_NAMES, *POLICY_ALIASES),
        default=list(POLICY_NAMES),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    scenarios = (
        scenarios_from_paths(args.scenarios, split=args.split)
        if args.scenarios
        else load_scenarios(args.index.resolve(), split=args.split)
    )
    result = run_campaign(
        scenarios,
        model=args.model.resolve() if args.model else None,
        output_dir=args.dataset_dir.resolve() if args.dataset_dir else None,
        max_steps=args.max_steps,
        policies=args.policies,
        dry_run=args.dry_run,
        takeoff_altitude_m=args.takeoff_altitude_m,
    )
    if args.output:
        _write_json(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
