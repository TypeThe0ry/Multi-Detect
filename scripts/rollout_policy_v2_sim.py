#!/usr/bin/env python3
"""Run a policy-v2 ONNX controller in the PX4/Gazebo SIM.

The collector owns the simulator lifecycle and recording contract.  This
entrypoint reuses its Docker, MAVLink, frame-server, observation and teardown
helpers, replacing only the policy step with the policy-v2 closed loop.  A
``--dry-run`` performs all scenario/manifest validation and prints the exact
launch plan without contacting Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.policy_v2 import (  # noqa: E402
    COCO80_CLASS_NAMES,
    DetectorSpec,
    ONNXPolicyRunner,
    PerceptionBridge,
    TaskSpecV1,
)
from multidetect.autonomy.policy_v2 import rollout as policy_rollout  # noqa: E402
from multidetect.autonomy.policy_v2.perception_bridge import build_detector  # noqa: E402
from multidetect.autonomy.policy_v2.rollout import (  # noqa: E402
    EpisodeMetrics,
    RolloutConfig,
    _safe_action,
)
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
from scripts import collect_sim_teacher_episodes as collector  # noqa: E402

DEFAULT_STEPS = 200
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MODEL = ROOT / "models" / "policy_v2.onnx"
DEFAULT_DETECTOR_MANIFEST = ROOT / "configs" / "models" / "ultralytics_yolo26n_coco80.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_artifact(manifest_path: Path) -> tuple[Path, tuple[str, ...], str, str]:
    """Resolve a detector candidate manifest to its raw ONNX artifact."""
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("detector manifest must be a JSON object")
    export = document.get("export", {})
    artifact = export.get("artifact_path") if isinstance(export, dict) else None
    artifact_path: Path | None = None
    if artifact is None:
        # A few older manifests use ``artifact`` directly.
        artifact = document.get("artifact") or document.get("model_path")
    if artifact is None:
        filename = document.get("artifact_filename")
        if isinstance(filename, str) and filename.strip():
            # Model manifests keep the immutable artifact under
            # ``artifacts/models/<model-id>/`` rather than beside configs.
            candidates = (
                manifest_path.parent / filename,
                ROOT / "artifacts" / "models" / manifest_path.stem / filename,
                ROOT / "artifacts" / "models" / "coco-yolo26n-traditional" / filename,
            )
            artifact_path = next(
                (candidate.resolve() for candidate in candidates if candidate.is_file()),
                candidates[-1].resolve(),
            )
        else:
            artifact = None
    else:
        artifact_path = Path(artifact)
        if not artifact_path.is_absolute():
            artifact_path = (manifest_path.parent / artifact_path).resolve()
    if artifact_path is None:
        raise ValueError("detector manifest has no export.artifact_path")
    classes: list[str] = []
    raw_classes = document.get("classes", document.get("runtime_label_map", ()))
    if isinstance(raw_classes, list):
        for item in raw_classes:
            if isinstance(item, dict):
                label = item.get("canonical_label", item.get("source_name"))
            else:
                label = item
            if isinstance(label, str):
                classes.append(label)
    names = tuple(classes) or COCO80_CLASS_NAMES
    model_id = str(document.get("model_id", manifest_path.stem))
    fmt = str(export.get("format", "raw")) if isinstance(export, dict) else "raw"
    return artifact_path, names, model_id, fmt


def _truth_target(truth_path: Path) -> tuple[tuple[float, float, float], str, str, str]:
    """Read target task fields while keeping the target pose out of policy state."""
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    targets = data.get("targets", []) if isinstance(data, dict) else []
    target = targets[0] if isinstance(targets, list) and targets else {}
    if not isinstance(target, dict):
        target = {}
    position = target.get("position", {})
    if not isinstance(position, dict):
        position = {}
    if "north_m" in position or "east_m" in position:
        target_ned = (
            float(position.get("north_m", 20.0)),
            float(position.get("east_m", 0.0)),
            -float(position.get("up_m", 0.0)),
        )
    else:
        # Use the collector's world-origin conversion for GPS truth.
        target_ned, _ = collector._truth_target(truth_path)
    label = str(target.get("label", "target"))
    target_class = str(target.get("class", "")) or None
    target_color = str(target.get("color", "")) or None
    return target_ned, label, target_class or "target", target_color or "unknown"


def _task_spec(truth_path: Path) -> TaskSpecV1:
    _, label, target_class, target_color = _truth_target(truth_path)
    prompt = f"find {target_color} {target_class} and report its location ({label})"
    return TaskSpecV1(
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        target_class=target_class,
        target_color=target_color,
        resolver_id="sim-truth-task",
    )


@dataclass(frozen=True, slots=True)
class PolicyRolloutPlan:
    scenario: str
    runtime: str
    truth: str
    policy_model: str
    detector_manifest: str
    out: str
    episodes: int = 1
    max_steps: int = DEFAULT_STEPS
    seed: int = 0
    timeout_s: float = DEFAULT_TIMEOUT_S
    control_port: int = collector.DEFAULT_CONTROL_PORT
    telemetry_port: int = collector.DEFAULT_TELEMETRY_PORT
    rgb_size: tuple[int, int] = collector.DEFAULT_RGB_SIZE
    depth_size: tuple[int, int] = collector.DEFAULT_DEPTH_SIZE
    depth_encoding: str = collector.DEFAULT_DEPTH_ENCODING
    keep_container_on_failure: bool = False
    takeoff_altitude_m: float | None = None

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["collector"] = {
            "container": "DockerSimEnvironment",
            "mavlink": "single udpin socket source_system=255 source_component=190",
            "setpoint_hz": 20,
            "frame_server": "latest-wins with sim-time alignment",
            "teardown": "RTL then LAND and remove container",
        }
        try:
            value["docker_command"] = collector.docker_command(
                collector.CollectionPlan(
                    self.scenario,
                    self.runtime,
                    self.truth,
                    "expert_search",
                    self.episodes,
                    self.max_steps,
                    self.seed,
                    self.out,
                    timeout_s=self.timeout_s,
                    max_episode_s=self.timeout_s,
                    control_port=self.control_port,
                    telemetry_port=self.telemetry_port,
                    rgb_size=self.rgb_size,
                    depth_size=self.depth_size,
                    depth_encoding=self.depth_encoding,
                    keep_container_on_failure=self.keep_container_on_failure,
                    takeoff_altitude_m=self.takeoff_altitude_m,
                )
            )
        except Exception as exc:
            value["docker_command_error"] = str(exc)
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--policy-model", type=Path, required=True)
    parser.add_argument("--detector-manifest", type=Path, default=DEFAULT_DETECTOR_MANIFEST)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--control-port", type=int, default=collector.DEFAULT_CONTROL_PORT)
    parser.add_argument("--telemetry-port", type=int, default=collector.DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-container-on-failure", action="store_true")
    parser.add_argument("--takeoff-altitude-m", type=float, default=None)
    return parser


def make_plan(args: argparse.Namespace) -> tuple[PolicyRolloutPlan, Path]:
    scenario = args.scenario.resolve()
    if not scenario.is_file():
        raise ValueError(f"scenario not found: {scenario}")
    runtime, truth = collector._scenario_channels(scenario)
    collector._validate_runtime(runtime)
    if args.episodes < 1 or args.max_steps < 1:
        raise ValueError("--episodes and --max-steps must be positive")
    if not 1.0 <= args.timeout_s <= 86400.0:
        raise ValueError("--timeout-s must be between 1 and 86400 seconds")
    plan = PolicyRolloutPlan(
        str(scenario),
        str(runtime),
        str(truth),
        str(args.policy_model.resolve()),
        str(args.detector_manifest.resolve()),
        str(args.out.resolve()),
        args.episodes,
        args.max_steps,
        args.seed,
        args.timeout_s,
        args.control_port,
        args.telemetry_port,
        takeoff_altitude_m=args.takeoff_altitude_m,
    )
    return plan, truth


def _build_components(plan: PolicyRolloutPlan, *, providers: tuple[str, ...], confidence: float):
    policy_path = Path(plan.policy_model)
    detector_manifest = Path(plan.detector_manifest)
    if not policy_path.is_file():
        raise FileNotFoundError(policy_path)
    artifact, classes, detector_id, fmt = _manifest_artifact(detector_manifest)
    if not artifact.is_file():
        raise FileNotFoundError(artifact)
    spec = DetectorSpec(
        artifact,
        detector_id,
        classes,
        fmt if fmt in {"raw", "nx6"} else "raw",
        confidence,
        providers,
    )
    bridge = PerceptionBridge(((spec, build_detector(spec)),))
    policy = ONNXPolicyRunner(policy_path, providers=providers or None)
    return (
        policy,
        bridge,
        {
            "policy_model_sha256": _sha256(policy_path),
            "detector_model_sha256": _sha256(artifact),
            "detector_manifest_sha256": _sha256(detector_manifest),
            "detector_id": detector_id,
        },
    )


def _write_perception(episode_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Persist detector records beside collector steps for offline tooling."""
    path = episode_dir / "perception.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            perception = row.get("perception", {"detections": []})
            output = {"step_index": row.get("step_index"), **perception}
            stream.write(json.dumps(output, ensure_ascii=False) + "\n")


def run_policy_rollout(*args: Any, **kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Public test/integration seam around the policy-v2 per-step loop."""
    return policy_rollout.rollout_episode(*args, **kwargs)


def run_episode(
    plan: PolicyRolloutPlan,
    truth_path: Path,
    episode_index: int,
    *,
    policy: Any,
    detector: Any,
    metadata: Mapping[str, Any],
) -> Path:
    """Run one live episode using collector-owned resources.

    Docker/MAVLink setup is intentionally delegated to the collector.  The
    observation source and teardown callbacks are accepted by
    :func:`rollout_episode`, making this function straightforward to exercise
    with fakes without a simulator.
    """
    # The collector already has a battle-tested lifecycle.  Keep a single
    # source of truth by constructing a collector plan and invoking its setup
    # through a small adapter.  A deployment that needs live SIM calls
    # ``run_live_episode`` below; tests can call ``rollout_episode`` directly.
    return _run_live_episode(plan, truth_path, episode_index, policy, detector, metadata)


def _run_live_episode(
    plan: PolicyRolloutPlan,
    truth_path: Path,
    episode_index: int,
    policy: Any,
    detector: Any,
    metadata: Mapping[str, Any],
) -> Path:
    """Run collector-owned Docker/PX4 lifecycle with a policy-v2 adapter.

    The collector remains untouched. Its existing ``run_episode`` supplies
    container startup, one 255/190 MAVLink socket, normal ARM/OFFBOARD,
    20-Hz setpoint streaming, latest-wins frame alignment and RTL/LAND
    cleanup. We replace only its private policy factory and step recorder
    callback for this process.
    """
    target_ned, target_label, _target_class, _target_color = _truth_target(truth_path)
    adapter = _LivePolicyAdapter(
        policy,
        detector,
        _task_spec(truth_path),
        target_ned,
        max_steps=plan.max_steps,
        altitude_m=collector.resolve_takeoff_altitude(plan.runtime, plan.takeoff_altitude_m)[0],
        obstacles=collector.load_sdf_obstacles(
            collector.scenario_world_path(plan.runtime) or ""
        ),
    )
    old_factory = collector._policy_for
    old_png = collector._png_observation
    old_record = collector._record_policy_step
    perception_rows: list[dict[str, Any]] = []

    def policy_factory(_plan: Any, _truth: Path, _seed: int) -> tuple[Any, str]:
        # The second value is collector task text metadata; retain the actual
        # scenario target label while the episode teacher is set explicitly to
        # ``policy_v2`` below.
        return adapter, target_label

    def png_observation(observation: dict[str, Any]) -> dict[str, Any]:
        adapter.set_observation(observation)
        return old_png(observation)

    def record_step(recorder: Any, before: dict[str, Any], action: Any, **kwargs: Any) -> bool:
        recorder.meta.update({"teacher": "policy_v2", **dict(metadata)})
        after = kwargs.get("obs_after")
        pose = after.get("pose", {}).get("local_ned_m") if isinstance(after, Mapping) else None
        if isinstance(pose, (list, tuple)) and len(pose) >= 2:
            try:
                horizontal = math.hypot(
                    float(pose[0]) - target_ned[0], float(pose[1]) - target_ned[1]
                )
                if getattr(action, "kind", "") in {"stop", "hold"} and horizontal <= 18.0:
                    adapter.metrics.task_success = True
            except (TypeError, ValueError):
                pass
        perception_rows.append(
            {"step_index": recorder.index, **(adapter.last_perception or {"detections": []})}
        )
        return old_record(recorder, before, action, **kwargs)

    collector._policy_for = policy_factory
    collector._png_observation = png_observation
    collector._record_policy_step = record_step
    try:
        collector_plan = collector.CollectionPlan(
            plan.scenario,
            plan.runtime,
            plan.truth,
            "expert_search",
            plan.episodes,
            plan.max_steps,
            plan.seed,
            plan.out,
            timeout_s=plan.timeout_s,
            max_episode_s=plan.timeout_s,
            control_port=plan.control_port,
            telemetry_port=plan.telemetry_port,
            rgb_size=plan.rgb_size,
            depth_size=plan.depth_size,
            depth_encoding=plan.depth_encoding,
            takeoff_altitude_m=plan.takeoff_altitude_m,
        )
        episode_dir = collector.run_episode(collector_plan, truth_path, episode_index)
        _write_perception(episode_dir, perception_rows)
        episode_path = episode_dir / "episode.json"
        meta = json.loads(episode_path.read_text(encoding="utf-8"))
        meta.update({"teacher": "policy_v2", "task_spec_sha256": adapter.task.canonical_hash})
        meta["metrics"] = adapter.metrics.to_mapping()
        episode_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        return episode_dir
    finally:
        collector._policy_for = old_factory
        collector._png_observation = old_png
        collector._record_policy_step = old_record


class _LivePolicyAdapter:
    """Collector policy facade that performs the learned closed-loop tick."""

    def __init__(
        self,
        policy: Any,
        detector: Any,
        task: TaskSpecV1,
        target_ned: tuple[float, float, float],
        *,
        max_steps: int,
        altitude_m: float = collector.DEFAULT_TAKEOFF_ALTITUDE_M,
        obstacles: tuple[Any, ...] = (),
    ) -> None:
        from multidetect.autonomy.policy_v2.state_builder import StructuredSceneStateBuilder

        self.policy = policy
        self.detector = detector
        self.task = task
        self.target_ned = target_ned
        self.builder = StructuredSceneStateBuilder()
        self.metrics = EpisodeMetrics()
        self.config = RolloutConfig(max_steps=max_steps)
        self.expert = collector.ExpertSearchPolicy(
            target_ned=target_ned,
            intrinsics=collector.DEFAULT_CAMERA_INFO,
            altitude_m=altitude_m,
            obstacles=obstacles,
        )
        self.observation: dict[str, Any] | None = None
        self.last_perception: dict[str, Any] | None = None
        self.last_pose: tuple[float, float, float] | None = None
        self.last_action: ActionPrimitive | None = None
        self.last_expert_action: ActionPrimitive | None = None

    def set_observation(self, observation: dict[str, Any]) -> None:
        self.observation = dict(observation)

    def next_action(
        self, *, pose_ned: tuple[float, float, float], yaw_deg: float = 0.0
    ) -> ActionPrimitive:
        self.last_expert_action = self.expert.next_action(pose_ned=pose_ned, yaw_deg=yaw_deg)
        if getattr(self.policy, "privileged", False):
            self.last_action = self.last_expert_action
            return self.last_action
        if self.observation is None:
            return ActionPrimitive.hold(0.2)
        observation = self.observation
        detector_start = time.perf_counter()
        try:
            self.last_perception = dict(
                self.detector.annotate_observation(
                    observation, observation.get("rgb"), observation.get("depth")
                )
            )
        except Exception:
            self.last_perception = {"detections": [], "error": "detector_fault"}
            self.metrics.event("detector_fault")
        self.metrics.detector_latencies_ms.append((time.perf_counter() - detector_start) * 1000.0)
        try:
            state = self.builder.from_sim_step_record(
                {"obs_before": observation, "perception": self.last_perception}, self.task
            )
            policy_start = time.perf_counter()
            action = self.policy.act(state)
            self.metrics.policy_latencies_ms.append((time.perf_counter() - policy_start) * 1000.0)
            safe = _safe_action(action, self.config.action_mask, self.metrics)
            self.last_action = safe.to_primitive()
            return self.last_action
        except Exception:
            self.metrics.event("inference_fault")
            self.metrics.policy_latencies_ms.append(0.0)
            return ActionPrimitive.hold(0.2)

    def labels(
        self, *, pose_ned: tuple[float, float, float], yaw_deg: float = 0.0
    ) -> dict[str, Any]:
        labels = dict(self.expert.labels(pose_ned=pose_ned, yaw_deg=yaw_deg))
        labels["expert_action"] = (self.last_expert_action or ActionPrimitive.hold(0.2)).to_dict()
        if labels.get("target_in_fov") and self.metrics.steps_to_acquire is None:
            self.metrics.steps_to_acquire = self.metrics.steps + 1
        current = tuple(float(value) for value in pose_ned[:3])
        if not (-100.0 <= current[0] <= 100.0 and -100.0 <= current[1] <= 100.0):
            self.metrics.event("out_of_search_area")
        if self.last_pose is not None:
            self.metrics.path_length_m += (
                (current[0] - self.last_pose[0]) ** 2 + (current[1] - self.last_pose[1]) ** 2
            ) ** 0.5
        self.last_pose = current
        if self.last_action is not None and self.last_action.kind in {"stop", "hold"}:
            distance = (
                (current[0] - self.target_ned[0]) ** 2 + (current[1] - self.target_ned[1]) ** 2
            ) ** 0.5
            self.metrics.task_success = distance <= self.config.standoff_success_m
        self.metrics.steps += 1
        return labels


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    plan, truth_path = make_plan(args)
    if args.dry_run:
        print(json.dumps(plan.to_mapping(), ensure_ascii=False, indent=2))
        return 0
    if not collector._docker_available():
        raise SystemExit("Docker daemon is not reachable; use --dry-run or start Docker")
    policy, detector, metadata = _build_components(
        plan, providers=tuple(args.provider), confidence=args.confidence
    )
    args.out.mkdir(parents=True, exist_ok=True)
    completed: list[str] = []
    for index in range(plan.episodes):
        completed.append(
            str(
                run_episode(
                    plan,
                    truth_path,
                    index,
                    policy=policy,
                    detector=detector,
                    metadata=metadata,
                )
            )
        )
    print(json.dumps({"episodes": completed, "out": plan.out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
