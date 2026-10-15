"""Truth-free onboard policy for the SIM teacher collector (``--teacher onboard``).

FAST: a fine-tuned YOLOE model with a generic open vocabulary proposes candidate
boxes at low confidence (class labels are ignored). SLOW: the quantized
Qwen3-VL slow system (llama-server) judges whether a tracked candidate is the
task's object. The host loop (``multidetect.autonomy.onboard_loop``) decides
approach, confirmation by apparent size, and the return home. The policy only
receives the task text, camera frames, the vehicle pose and the static world
map used for search legs and obstacle clearance; it never reads scenario truth.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_executor import (  # noqa: E402
    ExecutorConfig,
    MissionExecutor,
    plan_for_count_task,
    plan_for_find_and_inspect,
    plan_for_find_task,
    task_text_for,
)
from multidetect.autonomy.mission_plan import (  # noqa: E402
    MissionPlan,
    MissionStep,
    validate_mission_plan,
)
from multidetect.autonomy.mission_planner import (  # noqa: E402
    compile_mission_plan,
    plan_json_schema,
)
from multidetect.autonomy.onboard_decision import identity_of  # noqa: E402
from multidetect.autonomy.onboard_loop import (  # noqa: E402
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
)
from multidetect.sim_teacher.policies import (  # noqa: E402
    ExpertSearchPolicy,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)
from multidetect.simulator.camera_projection import pixel_ground_point  # noqa: E402
from multidetect.simulator.task_semantics import typical_object_size  # noqa: E402

DEFAULTS = {
    "yoloe_weights": ROOT / "artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt",
    "gguf": ROOT
    / "artifacts/models/slow-system-lora-v2-r2-seg-merged-gguf"
    / "slow-system-v2r2seg-merged-Q5_K_M.gguf",
    "mmproj": ROOT
    / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
    "llama_server": ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe",
}
# Generic proposal vocabulary: broad object words, not the task's answer.
VOCABULARY = (
    "car",
    "SUV",
    "pickup truck",
    "truck",
    "bus",
    "ambulance",
    "fire truck",
    "person",
    "traffic cone",
    "barrel",
    "concrete barrier",
    "fire hydrant",
    "dumpster",
    "electrical box",
    "mailbox",
    "post box",
    "stop sign",
    "cardboard box",
    "backpack",
    "sign",
)
MEMORY = "No prior confirmed target; search in progress."
MAX_PROMPT_CANDIDATES = 4


def _observation_timestamp_s(observation: Mapping[str, Any]) -> float | None:
    """Use the collector's pose/Gazebo simulation stamps, never receive time."""
    for key, units_per_s in (
        ("pose_time_boot_ms", 1_000.0),
        ("pose_sim_stamp_ns", 1_000_000_000.0),
        ("frame_sensor_stamp_ns", 1_000_000_000.0),
    ):
        value = observation.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            return value / units_per_s
    return None


def perception_task_text(step: MissionStep, original: str) -> str:
    """Use a plan query for slow perception unless the query is non-ASCII."""
    query = step.params.get("query") if step is not None else None
    if isinstance(query, str) and query.isascii():
        return task_text_for(step)
    return original


def build_mission_from_text(
    task_text: str,
    refs: Mapping[str, Any] | Sequence[str] | None,
    post: Callable[[str, dict], str],
    sorted_keys: bool = True,
) -> tuple[MissionPlan, dict[str, Any]]:
    """Compile a free task sentence, falling back to a validated return-home plan."""
    schema = plan_json_schema()
    if sorted_keys:
        from scripts.slow_system_runtime import trained_key_order_schema

        schema = trained_key_order_schema(schema)
    ref_names = tuple(refs or ())
    plan, report = compile_mission_plan(post, task_text, refs=ref_names, schema=schema)
    if plan is None:
        plan = validate_mission_plan(
            {
                "plan_version": 1,
                "goal": "return home safely",
                "steps": [
                    {
                        "id": "home",
                        "skill": "return_home",
                        "params": {},
                        "on_success": None,
                        "on_fail": None,
                    }
                ],
                "report": ["status"],
            }
        )
    return plan, report


class YoloeProposer:
    """Class-agnostic proposals from YOLOE with a fixed generic vocabulary."""

    def __init__(self, weights: Path, *, confidence: float, imgsz: int) -> None:
        os.environ.setdefault("YOLO_OFFLINE", "true")
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        from ultralytics import YOLOE

        self.model = YOLOE(str(weights))
        prompts = list(VOCABULARY)
        # Encode prompts before the first fp16 predict fuses the model.
        self.model.set_classes(prompts, self.model.get_text_pe(prompts))
        self.confidence, self.imgsz = confidence, imgsz

    def __call__(self, bgr) -> list[Detection]:
        result = self.model.predict(
            bgr, imgsz=self.imgsz, conf=self.confidence, device=0, verbose=False
        )[0]
        boxes = result.boxes
        return [
            Detection(tuple(float(v) for v in box), float(score), self.model.names[int(cls)])
            for box, score, cls in zip(
                boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist(), strict=True
            )
        ]


class OnboardPolicy:
    def __init__(
        self,
        *,
        task_text: str,
        world_path: Path | None,
        altitude_m: float,
        camera_info: dict[str, float],
        image_size: tuple[int, int],
        work_dir: Path,
        config: dict[str, Any] | None = None,
    ) -> None:
        options = {**{k: str(v) for k, v in DEFAULTS.items()}, **(config or {})}
        for key in ("yoloe_weights", "gguf", "mmproj", "llama_server"):
            # Config paths are relative to the repository root.
            path = Path(options[key])
            options[key] = str(path if path.is_absolute() else ROOT / path)
        # An operator command from the config overrides the world's task sentence.
        self.task_text = str(options.get("task_text") or task_text)
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.image_size = image_size
        obstacles = load_sdf_obstacles(world_path) if world_path else ()
        # Size-aware search altitude: low enough that an object of the task's typical
        # size spans about min_search_px pixels at a mid-image depression angle.
        self.search_altitude_m = None
        if options.get("size_aware_search"):
            size = typical_object_size(task_text)
            depression = 0.18 + math.radians(20.0)
            needed = size * float(camera_info["fx"]) / float(options.get("min_search_px", 14.0))
            self.search_altitude_m = max(
                float(options.get("min_search_altitude_m", 8.0)),
                min(altitude_m, needed * math.sin(depression)),
            )
        # The search pattern is target-blind; a target placed far outside the
        # detection range keeps ExpertSearchPolicy in its search/scan states.
        self._search = ExpertSearchPolicy(
            target_ned=(1.0e7, 1.0e7, 0.0),
            intrinsics=camera_info,
            image_size=image_size,
            altitude_m=self.search_altitude_m or altitude_m,
            obstacles=obstacles,
        )
        camera = CameraModel(
            fx=float(camera_info["fx"]),
            fy=float(camera_info["fy"]),
            cx=float(camera_info["cx"]),
            cy=float(camera_info["cy"]),
            width=image_size[0],
            height=image_size[1],
        )
        confirm_min_side_px = float(options.get("confirm_min_side_px", 20.0))
        second_look = options.get("second_look_after_steps")
        loop_defaults: dict[str, Any] = {
            "confirm_min_side_px": confirm_min_side_px,
            "search_altitude_m": self.search_altitude_m,
        }
        if second_look is not None:
            loop_defaults["second_look_after_steps"] = int(second_look)
        if options.get("confirm_max_range_m") is not None:
            loop_defaults["confirm_max_range_m"] = float(options["confirm_max_range_m"])
        if "closest_view_identity_matches" in options:
            value = options["closest_view_identity_matches"]
            loop_defaults["closest_view_identity_matches"] = None if value is None else int(value)
        if options.get("second_look_max_confidence") is not None:
            loop_defaults["second_look_max_confidence"] = float(
                options["second_look_max_confidence"]
            )
        if "memory_enabled" in options:
            value = options["memory_enabled"]
            loop_defaults["memory_enabled"] = (
                value.strip().lower() in {"1", "true", "yes", "on"}
                if isinstance(value, str)
                else bool(value)
            )
        for key, convert in (
            ("memory_radius_m", float),
            ("memory_min_verdicts", int),
            ("memory_max_range_ratio", float),
        ):
            if key in options:
                loop_defaults[key] = convert(options[key])

        def make_loop(task_text: str, region=None, **overrides: Any) -> OnboardLoop:
            search = self._search
            if region is not None:
                # A bounded zone: the same target-blind pattern, centred on the zone.
                search = ExpertSearchPolicy(
                    target_ned=(1.0e7, 1.0e7, 0.0),
                    intrinsics=camera_info,
                    image_size=image_size,
                    altitude_m=self.search_altitude_m or altitude_m,
                    obstacles=obstacles,
                    home_ned=(float(region[0]), float(region[1]), 0.0),
                    search_radius_m=float(region[2]),
                )
            return OnboardLoop(
                camera=camera,
                search_action=lambda pose, yaw: search.next_action(pose_ned=pose, yaw_deg=yaw),
                segment_clear=lambda pose, n, e: segment_clear_of_obstacles(
                    pose, (n, e, pose[2]), max(0.0, -float(pose[2])), obstacles
                ),
                config=LoopConfig(**{**loop_defaults, **overrides}),
            )

        self.loop = make_loop(task_text)
        mission = options.get("mission")
        if mission not in {None, "find", "count", "find_and_inspect", "from_text"}:
            raise ValueError(f"unknown mission template {mission!r}")
        self._step = 0
        self.proposer = YoloeProposer(
            Path(options["yoloe_weights"]),
            confidence=float(options.get("confidence", 0.05)),
            imgsz=int(options.get("imgsz", 1280)),
        )
        from scripts.eval_slow_system import LlamaServerRunner, _http_json

        self.slow = LlamaServerRunner(
            Path(options["gguf"]),
            Path(options["mmproj"]),
            executable=Path(options["llama_server"]),
            port=int(options.get("port", 9941)),
            max_new_tokens=768 if mission == "from_text" else 128,
            crop_policy="candidate_zoom_v2",
            # Every onboard call names a focus track: only identity verdicts are allowed.
            identity_only=True,
            logprobs=True,
        )
        self.mission_plan: MissionPlan | None = None
        self.mission_plan_report: dict[str, Any] | None = None
        self._mission_plan_labeled = False
        # Optional multi-skill mode: a MissionPlan compiled from the complete task sentence.
        self.executor: MissionExecutor | None = None
        if mission:
            if mission == "find":
                plan = plan_for_find_task("target")
            elif mission == "count":
                plan = plan_for_count_task("target", region=str(options.get("region", "zone:all")))
            elif mission == "find_and_inspect":
                plan = plan_for_find_and_inspect(
                    "target",
                    views=int(options.get("inspect_views", 4)),
                    standoff_m=float(options.get("inspect_standoff_m", 12.0)),
                )
            elif mission == "from_text":
                refs = options.get("refs") or {}

                def post(prompt: str, schema: dict[str, Any]) -> str:
                    body = {
                        "model": "local",
                        "temperature": 0,
                        "max_tokens": 768,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "mission_plan",
                                "strict": True,
                                "schema": schema,
                            },
                        },
                    }
                    response = _http_json(
                        f"http://127.0.0.1:{self.slow.port}/v1/chat/completions", body
                    )
                    return response["choices"][0]["message"].get("content", "")

                labels = options.get("ref_labels") or {}
                plan, report = build_mission_from_text(
                    self.task_text,
                    tuple(f"{ref} ({labels[ref]})" if ref in labels else ref for ref in refs),
                    post,
                    sorted_keys=bool(options.get("plan_sorted_keys", True)),
                )
                self.mission_plan, self.mission_plan_report = plan, report
            else:
                raise ValueError(f"unknown mission template {mission!r}")
            self.executor = MissionExecutor(
                plan,
                loop_factory=make_loop,
                refs={str(k): tuple(v) for k, v in (options.get("refs") or {}).items()},
                task_text=(
                    (lambda step: perception_task_text(step, self.task_text))
                    if mission == "from_text"
                    else (lambda step: self.task_text)
                ),
                config=ExecutorConfig(
                    step_budget=int(options.get("step_budget", 60)),
                    alert_min_identity_matches=int(options.get("alert_min_identity_matches", 1)),
                    alert_require_majority=(
                        options.get("alert_require_majority", False)
                        if not isinstance(options.get("alert_require_majority", False), str)
                        else options["alert_require_majority"].strip().lower()
                        in {"1", "true", "yes", "on"}
                    ),
                    alert_cluster_radius_m=float(options.get("alert_cluster_radius_m", 4.0)),
                ),
            )
        self.slow_calls = 0

    def close(self) -> None:
        self.slow.close()

    def _slow_identity(
        self, frame_path: Path, current, focus, task_text: str | None = None
    ) -> dict[str, Any]:
        """Ask the slow system about ``focus``; candidates are renamed track-N as trained."""
        shown = [focus] + [t for t in current if t.track_id != focus.track_id]
        shown = shown[:MAX_PROMPT_CANDIDATES]
        width, height = self.image_size
        names = {t.track_id: f"track-{index + 1}" for index, t in enumerate(shown)}
        row = {
            "image_path": str(frame_path),
            "task_text": task_text or self.task_text,
            "memory": MEMORY,
            "candidates": [
                {
                    "track_id": names[t.track_id],
                    "box_xyxy_normalized": [
                        round(min(1.0, max(0.0, t.bbox[0] / width)), 4),
                        round(min(1.0, max(0.0, t.bbox[1] / height)), 4),
                        round(min(1.0, max(0.0, t.bbox[2] / width)), 4),
                        round(min(1.0, max(0.0, t.bbox[3] / height)), 4),
                    ],
                }
                for t in shown
            ],
            "focus_track_id": names[focus.track_id],
            "_crop_policy": "candidate_zoom_v2",
        }
        from scripts.eval_slow_system import decision_confidence, parse_model_decision

        started = time.perf_counter()
        try:
            raw, latency = self.slow(row)
        except Exception as exc:  # timeouts or server faults leave the track unverified
            return {
                "track_id": focus.track_id,
                "identity": None,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": time.perf_counter() - started,
            }
        self.slow_calls += 1
        decision, error = parse_model_decision(raw)
        identity = identity_of(decision["decision"]) if decision else None
        return {
            "track_id": focus.track_id,
            "identity": identity,
            "decision": decision["decision"] if decision else None,
            "reason": decision["reason"] if decision else None,
            "confidence": decision_confidence(self.slow.last_logprobs),
            "error": error,
            "latency_s": float(latency),
        }

    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        import numpy as np
        from PIL import Image

        step = self._step
        self._step += 1
        rgb = np.asarray(Image.open(io.BytesIO(observation["rgb"])).convert("RGB"))
        frame_path = self.work_dir / f"frame_{step:04d}.png"
        frame_path.write_bytes(observation["rgb"])
        started = time.perf_counter()
        detections = self.proposer(rgb[:, :, ::-1].copy())
        fast_s = time.perf_counter() - started
        camera_info = observation["camera_info"]

        def ground_point(u: float, v: float):
            # Full attitude + camera mount inverse of the projection model.
            return pixel_ground_point(
                u, v, pose=observation, camera_info=camera_info, image_size=self.image_size
            )

        if self.executor is not None:
            return self._act_mission(
                frame_path,
                detections,
                fast_s,
                pose_ned=pose_ned,
                yaw_deg=yaw_deg,
                ground_point=ground_point,
                timestamp_s=_observation_timestamp_s(observation),
            )
        current = self.loop.update_tracks(
            detections, pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, ground_point=ground_point
        )
        slow = []
        if self.loop.state in {"searching", "approaching"}:
            for track in self.loop.tracks_needing_verdict(current):
                verdict = self.loop.remembered_verdict(track.track_id)
                if verdict is None:
                    verdict = self._slow_identity(frame_path, current, track)
                self.loop.apply_verdict(
                    track.track_id,
                    verdict.get("decision"),
                    verdict.get("confidence"),
                    remembered=verdict.get("remembered", False),
                )
                slow.append(verdict)
        action, info = self.loop.next_action(
            current, pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, ground_point=ground_point
        )
        labels = {
            "onboard": {
                **info,
                "state": self.loop.state,
                "detections": len(detections),
                "tracks": [
                    {
                        "id": t.track_id,
                        "bbox": [round(v, 1) for v in t.bbox],
                        "verdict": t.verdict,
                        "verdict_remembered": getattr(t, "verdict_remembered", False),
                        "ground_ne": list(t.ground) if t.ground is not None else None,
                        "identity_positive_matches": getattr(t, "identity_positive_matches", 0),
                        "identity_reject_matches": getattr(t, "identity_reject_matches", 0),
                        "identity_verdict_history": list(
                            getattr(t, "identity_verdict_history", ())
                        ),
                    }
                    for t in current
                ],
                "slow_calls": slow,
                "alert_vote_clusters": [],
                "fast_latency_s": round(fast_s, 4),
                "confirmed": self.loop.confirmed,
            }
        }
        if not math.isfinite(sum(pose_ned)):
            raise RuntimeError("non-finite pose")
        return action, labels

    def _act_mission(
        self, frame_path, detections, fast_s, *, pose_ned, yaw_deg, ground_point, timestamp_s=None
    ):
        executor = self.executor
        step = executor.current_step
        perception_text = executor.current_task_text or self.task_text
        findings_before = len(executor.findings)
        needing = executor.observe(
            detections, pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, ground_point=ground_point
        )
        current = list(executor.current_tracks)
        slow = []
        for track in needing[:3]:
            loop = getattr(executor, "active_loop", None)
            # Inspection capture asks a distinct question from each deliberate
            # view, so those answers always come from the VLM.
            verdict = (
                loop.remembered_verdict(track.track_id)
                if loop is not None and step is not None and step.skill != "inspect"
                else None
            )
            if verdict is None:
                verdict = self._slow_identity(frame_path, current, track, perception_text)
            if verdict.get("remembered", False):
                executor.apply_verdict(
                    track.track_id, verdict["decision"], remembered=True
                )
            else:
                executor.apply_verdict(
                    track.track_id, verdict.get("decision"), verdict.get("confidence")
                )
            slow.append(verdict)
        alert_vote_clusters = list(getattr(executor, "alert_vote_clusters", ()))
        action, info = executor.next_action(
            pose_ned=tuple(pose_ned),
            yaw_deg=yaw_deg,
            ground_point=ground_point,
            frame_id=frame_path.name,
            timestamp_s=timestamp_s,
        )
        confirmed = None
        if len(executor.findings) > findings_before:
            finding = executor.findings[-1]
            confirmed = {
                "track_id": finding["track_id"],
                "bbox": finding["bbox"],
                "position_estimate_ne": list(finding["position_ne"]),
                "rule": finding["rule"],
            }
        report = executor.report()
        labels = {
            "onboard": {
                **{k: v for k, v in info.items() if k not in {"counted", "confirmed"}},
                "state": executor.current_step.skill if executor.current_step else "done",
                "state_before": "approaching" if confirmed else (step.skill if step else "done"),
                "detections": len(detections),
                "tracks": [
                    {
                        "id": t.track_id,
                        "bbox": [round(v, 1) for v in t.bbox],
                        "verdict": t.verdict,
                        "verdict_remembered": getattr(t, "verdict_remembered", False),
                        "ground_ne": (
                            list(t.ground) if getattr(t, "ground", None) is not None else None
                        ),
                        "identity_positive_matches": getattr(t, "identity_positive_matches", 0),
                        "identity_reject_matches": getattr(t, "identity_reject_matches", 0),
                        "identity_verdict_history": list(
                            getattr(t, "identity_verdict_history", ())
                        ),
                    }
                    for t in current
                ],
                "slow_calls": slow,
                "alert_vote_clusters": alert_vote_clusters,
                "fast_latency_s": round(fast_s, 4),
                "confirmed": confirmed,
                "mission": {
                    "history": list(executor.history),
                    "evidence": list(report.evidence_frame_ids),
                    "answers": list(report.state_answers),
                    "alerts": list(report.alerts),
                    "positions": list(report.positions),
                    "counts": dict(executor.counts),
                    "alert_vote_clusters": alert_vote_clusters,
                },
            }
        }
        if self.mission_plan_report is not None and not self._mission_plan_labeled:
            labels["onboard"]["mission"]["plan"] = self._mission_plan_label()
            self._mission_plan_labeled = True
        if not math.isfinite(sum(pose_ned)):
            raise RuntimeError("non-finite pose")
        return action, labels

    def _mission_plan_label(self) -> dict[str, Any]:
        plan = self.mission_plan
        report = self.mission_plan_report or {}
        return {
            "skills": [step.skill for step in plan.steps] if plan else [],
            "repairs": list(report.get("repairs", [])),
            "error": report.get("error"),
            "latency_s": round(sum(report.get("attempts", [])), 2),
        }


def build_onboard_policy(
    *,
    task_text: str,
    world_path: Path | None,
    altitude_m: float,
    camera_info: dict[str, float],
    image_size: tuple[int, int],
    work_dir: Path,
    config_path: Path | None = None,
) -> OnboardPolicy:
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path else None
    return OnboardPolicy(
        task_text=task_text,
        world_path=world_path,
        altitude_m=altitude_m,
        camera_info=camera_info,
        image_size=image_size,
        work_dir=work_dir,
        config=config,
    )


__all__ = [
    "OnboardPolicy",
    "VOCABULARY",
    "YoloeProposer",
    "build_mission_from_text",
    "build_onboard_policy",
    "perception_task_text",
]
