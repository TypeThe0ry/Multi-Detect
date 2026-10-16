"""Shared truth-free policy for the SIM teacher and real onboard runtime.

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
import hashlib
import json
import math
import os
import sys
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.energy_estimator import EnergyConfig  # noqa: E402
from multidetect.autonomy.ground_model import ground_projection_from_config  # noqa: E402
from multidetect.autonomy.mission_executor import (  # noqa: E402
    ExecutorConfig,
    MissionExecutor,
    plan_for_count_task,
    plan_for_find_and_inspect,
    plan_for_find_task,
    task_text_for,
)
from multidetect.autonomy.mission_package import (  # noqa: E402
    MissionPackage,
    check_plan_against_package,
    load_mission_package,
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
from multidetect.autonomy.mission_summary import summarize_mission, summary_json  # noqa: E402
from multidetect.autonomy.mission_supervisor import MissionSupervisor  # noqa: E402
from multidetect.autonomy.obstacle_source import CompositeSource, StaticMapSource  # noqa: E402
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig  # noqa: E402
from multidetect.autonomy.onboard_decision import identity_of  # noqa: E402
from multidetect.autonomy.onboard_loop import (  # noqa: E402
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
)
from multidetect.autonomy.onboard_mission_log import (  # noqa: E402
    DownlinkQueue,
    OnboardMissionLog,
)
from multidetect.onboard_runtime.health import (  # noqa: E402
    HealthConfig,
    HealthDecision,
    HealthMonitor,
)
from multidetect.sim_teacher.policies import (  # noqa: E402
    ExpertSearchPolicy,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
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


def _model_sha256(path: str | Path, *, cache_path: Path | None = None) -> str:
    """Hash a model with a size/mtime keyed cache; failures are non-fatal."""
    candidate = Path(path)
    try:
        stat = candidate.stat()
        if not candidate.is_file():
            return "unknown"
    except OSError:
        return "unknown"
    key = str(candidate.resolve())
    cached: dict[str, Any] = {}
    if cache_path is not None:
        try:
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                cached = document
            entry = cached.get(key)
            if (
                isinstance(entry, dict)
                and entry.get("size") == stat.st_size
                and entry.get("mtime_ns") == stat.st_mtime_ns
                and isinstance(entry.get("sha256"), str)
            ):
                return entry["sha256"]
        except (OSError, ValueError, TypeError):
            cached = {}
    try:
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result = digest.hexdigest()
    except (OSError, ValueError):
        return "unknown"
    if cache_path is not None:
        cached[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": result}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary.write_text(json.dumps(cached, sort_keys=True), encoding="utf-8")
            os.replace(temporary, cache_path)
        except (OSError, TypeError, ValueError):
            pass
    return result


def _planning_model_spec(value: Any, *, identity_mmproj: str) -> tuple[str, str | None] | None:
    """Normalize the optional planner model; planner prompts are text-only."""
    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        model = value.get("gguf", value.get("model", value.get("path")))
        if model is None:
            raise ValueError("planning_model requires gguf/model/path")
        # The current mission-planning prompt is text-only, so a projector is
        # unnecessary.  A caller may still provide one for a future multimodal prompt.
        mmproj = value.get("mmproj")
    else:
        model, mmproj = value, None
    model_path = Path(str(model))
    model_path = model_path if model_path.is_absolute() else ROOT / model_path
    if mmproj is not None:
        mmproj_path = Path(str(mmproj))
        mmproj = str(mmproj_path if mmproj_path.is_absolute() else ROOT / mmproj_path)
    return str(model_path), mmproj
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


def validate_sim_ground_events(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the SIM-only timeline before constructing any model resources."""
    events = config.get("sim_ground_events", [])
    if not isinstance(events, list):
        raise ValueError("sim_ground_events must be a list")
    if "sim_ground_events" in config and config.get("mission_package") is None:
        raise ValueError("sim_ground_events requires a mission_package")
    result = []
    previous = -1.0
    for entry in events:
        if not isinstance(entry, dict) or set(entry) != {"t_s", "event"}:
            raise ValueError("SIM timeline entries require exactly t_s and event")
        stamp = entry["t_s"]
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or stamp < 0
            or stamp < previous
        ):
            raise ValueError("SIM timeline t_s must be finite, nonnegative and ordered")
        if not isinstance(entry["event"], str) or entry["event"] not in {
            "link_lost",
            "link_restored",
            "abort",
            "pause",
            "resume",
        }:
            raise ValueError("unknown SIM timeline event")
        previous = stamp
        result.append(dict(entry))
    return result


class SimGroundTimeline:
    """Drive supervisor time from SIM frames, never inference or startup latency.

    A nonempty timeline models a healthy link until link_lost stops heartbeats.
    An empty timeline emits no ground events at all. Loss becomes observable
    after heartbeat_timeout_s; the policy silence timer uses the last heartbeat.
    Events crossed between frames are delivered once, in order, at their time.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.now = 0.0
        self.origin_ns: int | None = None
        self.index = 0
        self.link_up = bool(events)

    def advance(self, observation: Mapping[str, Any], supervisor: MissionSupervisor) -> None:
        stamp = observation.get("frame_sensor_stamp_ns")
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0:
            raise ValueError("SIM timeline requires frame_sensor_stamp_ns")
        if self.origin_ns is None:
            self.origin_ns = stamp
        target = (stamp - self.origin_ns) / 1_000_000_000
        if target < self.now:
            raise ValueError("SIM frame clock moved backwards")

        def heartbeat():
            supervisor.ground_event(
                {"mission_id": supervisor.package.mission_id, "type": "heartbeat"}
            )

        while self.index < len(self.events) and self.events[self.index]["t_s"] <= target:
            entry = self.events[self.index]
            self.now = float(entry["t_s"])
            if self.link_up:
                heartbeat()
            # Latch an expired silence/pause before a later resume/reconnect.
            supervisor.evaluate()
            kind = entry["event"]
            supervisor.log.append("sim_ground_event", self.now, event=kind, t_s=self.now)
            if kind == "link_lost":
                self.link_up = False
            elif kind == "link_restored":
                self.link_up = True
                heartbeat()
            else:
                supervisor.ground_event({"mission_id": supervisor.package.mission_id, "type": kind})
            self.index += 1
        self.now = target
        if self.link_up:
            heartbeat()


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


def _enabled(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


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
    *,
    plan_checker: Callable[[MissionPlan], Sequence[Mapping[str, Any]]] | None = None,
    attempts: int = 2,
    capabilities: frozenset[str] = frozenset(),
) -> tuple[MissionPlan, dict[str, Any]]:
    """Compile a free task sentence, falling back to a validated return-home plan."""
    schema = plan_json_schema(capabilities=capabilities)
    if sorted_keys:
        from scripts.slow_system_runtime import trained_key_order_schema

        schema = trained_key_order_schema(schema)
    ref_names = tuple(refs or ())
    plan, report = compile_mission_plan(
        post,
        task_text,
        refs=ref_names,
        schema=schema,
        attempts=attempts,
        plan_checker=plan_checker,
        capabilities=capabilities,
    )
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
    """Class-agnostic proposals with optional full-frame plus 2x2 tiled inference.

    ``proposer_tiled`` defaults off in OnboardPolicy. Tiles overlap by 20% of
    their width/height; all five passes use the same imgsz and confidence.
    ``proposer_tile_overlap`` and ``proposer_merge_iou`` control crop overlap
    and the class-agnostic merge. The loop's box filter runs after merging.
    """

    def __init__(
        self,
        weights: Path,
        *,
        confidence: float,
        imgsz: int,
        device: str | int = 0,
        tiled: bool = False,
        tile_overlap: float = 0.2,
        merge_iou: float = 0.5,
    ) -> None:
        if not isinstance(tiled, bool):
            raise ValueError("proposer_tiled must be a boolean")
        if not 0 <= tile_overlap < 1 or not 0 < merge_iou <= 1:
            raise ValueError("tile overlap must be in [0, 1); merge IoU must be in (0, 1]")
        os.environ.setdefault("YOLO_OFFLINE", "true")
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        from ultralytics import YOLOE

        self.model = YOLOE(str(weights))
        self.model.to(device)
        prompts = list(VOCABULARY)
        # Encode prompts before the first fp16 predict fuses the model.
        self.model.set_classes(prompts, self.model.get_text_pe(prompts))
        self.confidence, self.imgsz = confidence, imgsz
        self.device, self.tiled = device, tiled
        self.tile_overlap, self.merge_iou = tile_overlap, merge_iou

    def _predict(self, bgr) -> list[Detection]:
        result = self.model.predict(
            bgr, imgsz=self.imgsz, conf=self.confidence, device=self.device, verbose=False
        )[0]
        boxes = result.boxes
        return [
            Detection(tuple(float(v) for v in box), float(score), self.model.names[int(cls)])
            for box, score, cls in zip(
                boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist(), strict=True
            )
        ]

    def tile_proposals(self, bgr) -> list[Detection]:
        """Four overlapping crops, resized by YOLOE to imgsz, in full-frame pixels."""
        height, width = bgr.shape[:2]
        tile_w = min(width, math.ceil(width / (2 - self.tile_overlap)))
        tile_h = min(height, math.ceil(height / (2 - self.tile_overlap)))
        detections = []
        for y in dict.fromkeys((0, height - tile_h)):
            for x in dict.fromkeys((0, width - tile_w)):
                crop = bgr[y : y + tile_h, x : x + tile_w].copy()
                for detection in self._predict(crop):
                    x1, y1, x2, y2 = detection.bbox
                    box = (
                        max(0.0, min(float(width), x1 + x)),
                        max(0.0, min(float(height), y1 + y)),
                        max(0.0, min(float(width), x2 + x)),
                        max(0.0, min(float(height), y2 + y)),
                    )
                    if box[2] > box[0] and box[3] > box[1]:
                        detections.append(Detection(box, detection.score, detection.label))
        return detections

    def merge(self, detections: Sequence[Detection]) -> list[Detection]:
        """Class-agnostic greedy NMS; downstream loop retains its normal limits."""
        kept: list[Detection] = []
        for detection in sorted(detections, key=lambda d: d.score, reverse=True):
            a = detection.bbox
            for prior in kept:
                b = prior.bbox
                intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
                    0.0, min(a[3], b[3]) - max(a[1], b[1])
                )
                union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
                if union > 0 and intersection / union > self.merge_iou:
                    break
            else:
                kept.append(detection)
        return kept

    def __call__(self, bgr) -> list[Detection]:
        detections = self._predict(bgr)
        if self.tiled:
            # Keep full-frame context and objects that cross tile boundaries.
            return self.merge([*detections, *self.tile_proposals(bgr)])
        return detections


class OnboardPolicy:
    # Optional subsystems default to "absent" (tests build bare instances).
    supervisor: MissionSupervisor | None = None
    downlink: DownlinkQueue | None = None
    sim_timeline: SimGroundTimeline | None = None
    mission_plan: MissionPlan | None = None
    ground_projection = ground_projection_from_config(None, base_dir=ROOT)
    frame_format = "png"

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
        proposer: Callable | None = None,
        slow_runner: Any = None,
        ground_projector: Callable | None = None,
        home_ned: tuple[float, float, float] | None = None,
        runtime_mode: str | None = None,
        frame_format: str = "png",
        frame_quality: int = 90,
        mission_package: MissionPackage | None = None,
    ) -> None:
        options = {**{k: str(v) for k, v in DEFAULTS.items()}, **(config or {})}
        effective_runtime_mode = runtime_mode or options.get("runtime_mode")
        sim_events = validate_sim_ground_events(options)
        self.sim_timeline = (
            SimGroundTimeline(sim_events) if "sim_ground_events" in options else None
        )
        package_raw = options.get("mission_package")
        if (
            effective_runtime_mode
            and str(effective_runtime_mode).lower() in {"real", "flight", "hil", "live"}
            and package_raw is None
        ):
            raise ValueError("real/flight runtime requires a signed mission_package")
        security = options.get("security") if isinstance(options.get("security"), dict) else {}
        mission_key_setting = options.get("mission_package_verify_key") or security.get(
            "mission_package_verify_key"
        )
        legacy_key_setting = (
            options.get("operator_key_path")
            or options.get("mission_key_path")
            or options.get("mission_signing_key_path")
            or security.get("key_path")
        )
        key_setting = mission_key_setting or legacy_key_setting
        if legacy_key_setting is not None and mission_key_setting is None:
            warnings.warn(
                "operator_key_path is deprecated; use mission_package_verify_key",
                DeprecationWarning,
                stacklevel=2,
            )
        ground_key_setting = (
            options.get("ground_command_verify_key")
            or security.get("ground_command_verify_key")
            or legacy_key_setting
        )
        if isinstance(key_setting, str):
            key_path = Path(key_setting)
            key_setting = key_path if key_path.is_absolute() else ROOT / key_path
        if isinstance(package_raw, str):
            package_path = Path(package_raw)
            package_raw = package_path if package_path.is_absolute() else ROOT / package_path
        # ``mission_package`` is supplied by the admission layer when the
        # comms-free entry is used.  Reusing that exact object prevents a
        # second load from observing a different path/file after admission.
        self.mission_package = mission_package or (
            load_mission_package(
                package_raw,
                key_path=key_setting,
                require_signature=key_setting is not None,
                mode=effective_runtime_mode,
            )
            if package_raw is not None
            else None
        )
        if self.mission_package is not None:
            package = self.mission_package
            options["refs"] = package.refs
            options["ref_labels"] = package.document.get("ref_labels", {})
            options["task_text"] = package.document.get("task_text") or package.plan.goal
            options["mission"] = "package" if package.plan is not None else "from_text"
            area = package.document["operating_area"]
            if not area["min_altitude_m"] <= altitude_m <= area["max_altitude_m"]:
                raise ValueError("launch altitude outside mission package limits")
        package_capabilities = (
            frozenset(self.mission_package.document.get("capabilities", ()))
            if self.mission_package is not None
            else frozenset()
        )
        self.ground_projection = ground_projection_from_config(
            options.get("ground_model"), base_dir=ROOT
        )
        # One final arbiter is shared by SIM and onboard callers.  Package
        # geometry is authoritative for the home and operating rectangle;
        # explicit safety_envelope settings may tighten it further.
        envelope_source = dict(options)
        if self.mission_package is not None:
            area = self.mission_package.document["operating_area"]
            package_polygon = (
                (float(area["north_min_m"]), float(area["east_min_m"])),
                (float(area["north_max_m"]), float(area["east_min_m"])),
                (float(area["north_max_m"]), float(area["east_max_m"])),
                (float(area["north_min_m"]), float(area["east_max_m"])),
            )
            envelope_source.setdefault("operating_polygon", package_polygon)
            envelope_source.setdefault("min_altitude_m", float(area["min_altitude_m"]))
            envelope_source.setdefault("max_altitude_m", float(area["max_altitude_m"]))
        package_home = None
        if self.mission_package is not None:
            raw_home = tuple(float(v) for v in self.mission_package.document["home"])
            package_home = raw_home if len(raw_home) == 3 else (*raw_home, 0.0)
        self.safety_envelope = SafetyEnvelope(
            SafetyEnvelopeConfig.from_mapping(envelope_source),
            home_ned=(
                tuple(float(v) for v in home_ned)
                if home_ned is not None
                else package_home
            ),
            ground_model=getattr(self.ground_projection, "model", None),
        )
        for key in ("yoloe_weights", "gguf", "mmproj", "llama_server"):
            # Config paths are relative to the repository root.
            path = Path(options[key])
            options[key] = str(path if path.is_absolute() else ROOT / path)
        # An operator command from the config overrides the world's task sentence.
        self.task_text = str(options.get("task_text") or task_text)
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.image_size = image_size
        if frame_format not in {"png", "jpeg"}:
            raise ValueError("frame_format must be png or jpeg")
        self.frame_format = frame_format
        self.frame_quality = int(frame_quality)
        self.ground_projector = ground_projector
        mount_pitch = float(options.get("camera_pitch_down_rad", 0.18))
        search_pattern = str(options.get("search_pattern", "current"))
        if search_pattern not in {"current", "lawnmower", "spiral", "two_altitude"}:
            raise ValueError(
                "search_pattern must be one of current, lawnmower, spiral, two_altitude"
            )
        low_search_altitude = None
        obstacles = load_sdf_obstacles(world_path) if world_path else ()
        # Keep the final arbiter on the same typed clearance source as the
        # executor/loop.  Optional depth sources can be composed by callers
        # before runtime construction; an empty map remains byte-compatible.
        self.safety_envelope.obstacle_source = CompositeSource((StaticMapSource(obstacles),))
        # Size-aware search altitude: low enough that an object of the task's typical
        # size spans about min_search_px pixels at a mid-image depression angle.
        self.search_altitude_m = None
        if options.get("size_aware_search"):
            size = typical_object_size(self.task_text)
            depression = mount_pitch + math.radians(20.0)
            needed = size * float(camera_info["fx"]) / float(options.get("min_search_px", 14.0))
            self.search_altitude_m = max(
                float(options.get("min_search_altitude_m", 8.0)),
                min(altitude_m, needed * math.sin(depression)),
            )
        if search_pattern == "two_altitude":
            low_search_altitude = float(options.get("low_search_altitude_m", 12.0))
            if not math.isfinite(low_search_altitude) or low_search_altitude <= 0:
                raise ValueError("low_search_altitude_m must be finite and positive")
            low_search_altitude = min(
                low_search_altitude, self.search_altitude_m or float(altitude_m)
            )
        # The search pattern is target-blind; a target placed far outside the
        # detection range keeps ExpertSearchPolicy in its search/scan states.
        self._search = ExpertSearchPolicy(
            target_ned=(1.0e7, 1.0e7, 0.0),
            intrinsics=camera_info,
            image_size=image_size,
            altitude_m=self.search_altitude_m or altitude_m,
            obstacles=obstacles,
            home_ned=home_ned or (0.0, 0.0, 0.0),
            camera_pitch_rad=mount_pitch,
            search_pattern=search_pattern,
            **({"low_altitude_m": low_search_altitude} if low_search_altitude is not None else {}),
        )
        camera = CameraModel(
            fx=float(camera_info["fx"]),
            fy=float(camera_info["fy"]),
            cx=float(camera_info["cx"]),
            cy=float(camera_info["cy"]),
            width=image_size[0],
            height=image_size[1],
            pitch_down_rad=mount_pitch,
        )
        confirm_min_side_px = float(options.get("confirm_min_side_px", 20.0))
        second_look = options.get("second_look_after_steps")
        loop_defaults: dict[str, Any] = {
            "confirm_min_side_px": confirm_min_side_px,
            "search_altitude_m": self.search_altitude_m,
        }
        if "proposal_min_side_px" in options:
            min_side = float(options["proposal_min_side_px"])
            if not math.isfinite(min_side) or min_side <= 0:
                raise ValueError("proposal_min_side_px must be finite and positive")
            loop_defaults["min_box_side_px"] = min_side
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
            ("ground_reid_radius_m", float),
            ("appearance_size_ratio", float),
            ("appearance_aspect_ratio", float),
            ("appearance_color_distance", float),
            ("memory_radius_m", float),
            ("memory_footprint_factor", float),
            ("memory_min_verdicts", int),
            ("memory_max_range_ratio", float),
        ):
            if key in options:
                loop_defaults[key] = convert(options[key])
        if "ground_reid_max_steps" in options:
            value = options["ground_reid_max_steps"]
            loop_defaults["ground_reid_max_steps"] = None if value is None else int(value)

        def make_loop(task_text: str, region=None, **overrides: Any) -> OnboardLoop:
            search = self._search
            obstacle_source = StaticMapSource(obstacles)
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
                    camera_pitch_rad=mount_pitch,
                    search_pattern=search_pattern,
                    **(
                        {"low_altitude_m": low_search_altitude}
                        if low_search_altitude is not None
                        else {}
                    ),
                )
            loop = OnboardLoop(
                camera=camera,
                search_action=lambda pose, yaw: search.next_action(pose_ned=pose, yaw_deg=yaw),
                segment_clear=lambda pose, n, e: segment_clear_of_obstacles(
                    pose, (n, e, pose[2]), max(0.0, -float(pose[2])), obstacles
                ),
                obstacle_source=obstacle_source,
                config=LoopConfig(**{**loop_defaults, **overrides}),
            )
            if home_ned is not None:
                loop.home = home_ned[:2]
                loop.cruise_altitude_m = altitude_m
            return loop

        self.loop = make_loop(task_text)
        mission = options.get("mission")
        if mission not in {None, "find", "count", "find_and_inspect", "from_text", "package"}:
            raise ValueError(f"unknown mission template {mission!r}")
        self._step = 0
        self.proposer = (
            proposer
            if proposer is not None
            else YoloeProposer(
                Path(options["yoloe_weights"]),
                confidence=float(options.get("confidence", 0.05)),
                imgsz=int(options.get("imgsz", 1280)),
                tiled=options.get("proposer_tiled", False),
                tile_overlap=float(options.get("proposer_tile_overlap", 0.2)),
                merge_iou=float(options.get("proposer_merge_iou", 0.5)),
            )
        )
        from scripts.eval_slow_system import LlamaServerRunner

        identity_port = int(options.get("port", 9941))
        planning_spec = _planning_model_spec(
            options.get("planning_model"), identity_mmproj=options["mmproj"]
        )
        split_planning = mission == "from_text" and planning_spec is not None
        self._model_split = bool(split_planning)
        model_hash_cache = self.work_dir / ".model-hash-cache.json"
        self._model_runtime: dict[str, Any] = {
            "enabled": bool(split_planning),
            "planning_model_path": planning_spec[0] if planning_spec else None,
            "planning_model_sha256": (
                _model_sha256(planning_spec[0], cache_path=model_hash_cache)
                if split_planning
                else None
            ),
            "identity_model_path": options["gguf"],
            "identity_model_sha256": (
                _model_sha256(options["gguf"], cache_path=model_hash_cache)
                if split_planning
                else None
            ),
            "planning_fallback_to_identity": False,
            "swap_duration_s": None,
            "swap_completed_monotonic_s": None,
        }

        def make_identity_runner():
            return LlamaServerRunner(
                Path(options["gguf"]),
                Path(options["mmproj"]),
                executable=Path(options["llama_server"]),
                port=identity_port,
                max_new_tokens=128 if split_planning else (768 if mission == "from_text" else 128),
                crop_policy="candidate_zoom_v2",
                identity_only=True,
                logprobs=True,
                fast_identity=str(options.get("fast_identity", False)).lower()
                in {"1", "true", "yes"},
            )

        def start_identity_runner():
            if slow_runner is not None:
                return slow_runner
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    return make_identity_runner()
                except Exception as exc:
                    address_busy = "address already in use" in str(exc).lower() or "address in use" in str(exc).lower()
                    if not address_busy or time.monotonic() >= deadline:
                        health = getattr(self, "health", None)
                        if health is not None:
                            health.record_planning_failure()
                        raise
                    time.sleep(0.1)

        planner_start_error: Exception | None = None
        if split_planning:
            try:
                self.slow = LlamaServerRunner(
                    Path(planning_spec[0]),
                    Path(planning_spec[1]) if planning_spec[1] is not None else None,
                    executable=Path(options["llama_server"]),
                    port=identity_port,
                    max_new_tokens=768,
                    crop_policy="none",
                    identity_only=False,
                    logprobs=False,
                    wait_port_free=True,
                )
            except Exception as exc:
                planner_start_error = exc
                self._model_runtime["planning_fallback_to_identity"] = True
                self.slow = start_identity_runner()
        elif slow_runner is None:
            self.slow = make_identity_runner()
        else:
            self.slow = slow_runner
        health_options = options.get("health") if isinstance(options.get("health"), dict) else {}
        self.slow_call_timeout_s = float(health_options.get("slow_timeout_s", 180.0))
        self.health = HealthMonitor(
            config=HealthConfig(
                **{
                    k: v
                    for k, v in health_options.items()
                    if k in HealthConfig.__dataclass_fields__
                }
            ),
            runner=self.slow,
            disk_path=self.work_dir,
        )
        self._slow_failed_current = False
        self._slow_skipped_current = False
        self._health_recovery_executor = None
        self.mission_plan: MissionPlan | None = None
        self.mission_plan_report: dict[str, Any] | None = None
        self._mission_plan_labeled = False
        # Optional multi-skill mode: a MissionPlan compiled from the complete task sentence.
        self.executor: MissionExecutor | MissionSupervisor | None = None
        self.supervisor: MissionSupervisor | None = None
        self.downlink: DownlinkQueue | None = None
        executor_config = ExecutorConfig(
            step_budget=int(options.get("step_budget", 60)),
            alert_min_identity_matches=int(options.get("alert_min_identity_matches", 1)),
            alert_require_majority=(
                options.get("alert_require_majority", False)
                if not isinstance(options.get("alert_require_majority", False), str)
                else options["alert_require_majority"].strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            alert_cluster_radius_m=float(options.get("alert_cluster_radius_m", 4.0)),
            deliver_min_altitude_m=float(options.get("deliver_min_altitude_m", 1.0)),
            deliver_max_altitude_m=float(options.get("deliver_max_altitude_m", 5.0)),
            deliver_altitude_m=float(options.get("deliver_altitude_m", 2.0)),
            deliver_position_tolerance_m=float(options.get("deliver_position_tolerance_m", 1.0)),
            deliver_hold_s=float(options.get("deliver_hold_s", 2.0)),
            deliver_ack_timeout_s=float(options.get("deliver_ack_timeout_s", 10.0)),
        )
        capabilities = frozenset(package_capabilities) | (
            frozenset({"payload"}) if _enabled(options.get("payload_capability")) else frozenset()
        )
        if mission:
            if mission == "package":
                if self.mission_package is None:
                    raise ValueError("mission=package requires mission_package")
                plan = self.mission_package.plan
                self.mission_plan = plan
            elif mission == "find":
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
                from scripts.eval_slow_system import _http_json

                refs = options.get("refs") or {}

                labels = options.get("ref_labels") or {}
                retry_setting = options.get(
                    "retry_on_package_violation",
                    options.get(
                        "retry_on_package_violations",
                        options.get("mission_package_retry", False),
                    ),
                )
                package_retry = (
                    retry_setting.strip().lower() in {"1", "true", "yes", "on"}
                    if isinstance(retry_setting, str)
                    else bool(retry_setting)
                )
                plan_checker = (
                    (
                        lambda candidate: check_plan_against_package(
                            candidate,
                            self.mission_package,
                            capabilities=capabilities,
                            executor_config=executor_config,
                        )
                    )
                    if package_retry and self.mission_package is not None
                    else None
                )
                def compile_once(runner):
                    post_attempted = False

                    def post(prompt: str, schema: dict[str, Any]) -> str:
                        nonlocal post_attempted
                        post_attempted = True
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
                        started = time.perf_counter()
                        try:
                            response = _http_json(
                                f"http://127.0.0.1:{runner.port}/v1/chat/completions",
                                body,
                                timeout=self.slow_call_timeout_s,
                            )
                        except Exception:
                            self.health.record_slow_call(time.perf_counter() - started, ok=False)
                            raise
                        self.health.record_slow_call(time.perf_counter() - started, ok=True)
                        return response["choices"][0]["message"].get("content", "")

                    try:
                        candidate, candidate_report = build_mission_from_text(
                            self.task_text,
                            tuple(f"{ref} ({labels[ref]})" if ref in labels else ref for ref in refs),
                            post,
                            sorted_keys=bool(options.get("plan_sorted_keys", True)),
                            plan_checker=plan_checker,
                            capabilities=capabilities,
                        )
                    except Exception:
                        # ``post`` records HTTP outcomes; do not count an
                        # exception a second time as a slow fault.
                        self.health.record_planning_failure()
                        if not post_attempted:
                            self.health.record_slow_call(0.0, ok=False)
                        raise
                    return candidate, candidate_report

                if split_planning:
                    planner_error = planner_start_error
                    swap_close_started = None
                    if planner_error is not None:
                        self._model_runtime["planning_fallback_to_identity"] = True
                        self.health.record_planning_failure()
                        self.health.record_slow_call(0.0, ok=False)
                        self.health.runner = self.slow
                        plan, report = compile_once(self.slow)
                    else:
                        try:
                            plan, report = compile_once(self.slow)
                            if report.get("error"):
                                self.health.record_planning_failure()
                                raise ValueError(
                                    f"mission plan compilation failed: {report['error']}"
                                )
                        except Exception:
                            self._model_runtime["planning_fallback_to_identity"] = True
                            swap_close_started = time.monotonic()
                            close = getattr(self.slow, "close", None)
                            if close is not None:
                                close()
                            self.slow = start_identity_runner()
                            self.health.runner = self.slow
                            try:
                                plan, report = compile_once(self.slow)
                            except Exception:
                                close = getattr(self.slow, "close", None)
                                if close is not None:
                                    close()
                                raise
                        else:
                            swap_close_started = time.monotonic()
                            close = getattr(self.slow, "close", None)
                            if close is not None:
                                close()
                            self.slow = start_identity_runner()
                            self.health.runner = self.slow
                    swap_completed = time.monotonic()
                    self._model_runtime["swap_duration_s"] = (
                        swap_completed - swap_close_started
                        if swap_close_started is not None
                        else None
                    )
                    self._model_runtime["swap_completed_monotonic_s"] = swap_completed
                else:
                    try:
                        plan, report = compile_once(self.slow)
                    except Exception:
                        raise
                self.mission_plan, self.mission_plan_report = plan, report
                if report.get("error"):
                    if split_planning:
                        self.health.record_planning_failure()
                    if self.mission_package is not None:
                        package_violations = report.get("package_violations", [])
                        if package_violations:
                            self._refuse_package_plan(package_violations)
                        self.slow.close()
                        raise ValueError(
                            f"mission package plan compilation failed: {report['error']}"
                        )
                    raise ValueError(f"mission plan compilation failed: {report['error']}")
            else:
                raise ValueError(f"unknown mission template {mission!r}")
            if self.mission_package is not None:
                violations = check_plan_against_package(
                    plan,
                    self.mission_package,
                    capabilities=capabilities,
                    executor_config=executor_config,
                )
                if violations:
                    self._refuse_package_plan(violations)
                try:
                    self.mission_package.validate_plan(plan)
                except ValueError:
                    self.slow.close()
                    raise
            self.executor = MissionExecutor(
                plan,
                loop_factory=make_loop,
                refs={str(k): tuple(v) for k, v in (options.get("refs") or {}).items()},
                capabilities=capabilities,
                mission_package=self.mission_package,
                no_fly_zones=(
                    self.mission_package.no_fly_zones
                    if self.mission_package is not None
                    else ()
                ),
                release_authorized=_enabled(options.get("release_authorized")),
                task_text=(
                    (lambda step: perception_task_text(step, self.task_text))
                    if mission in {"from_text", "package"}
                    else (lambda step: self.task_text)
                ),
                config=executor_config,
            )
            # When no package is loaded, use the executor's configured home if
            # it provides one; never infer home from the first observed pose.
            if self.safety_envelope.home_ned is None and self.executor.home is not None:
                self.safety_envelope.set_home((*self.executor.home, 0.0))
            # Set home on the executor itself, before any supervisor wraps it.
            if home_ned is not None:
                self.executor.home = home_ned[:2]
                self.executor.cruise_altitude_m = altitude_m
            if self.mission_package is not None:
                self.executor.cruise_altitude_m = altitude_m
                log = OnboardMissionLog(
                    self.work_dir / "mission_log", self.mission_package.mission_id
                )
                self.downlink = DownlinkQueue(log, sender=None)
                self.supervisor = MissionSupervisor(
                    self.executor,
                    self.mission_package,
                    log=log,
                    downlink=self.downlink,
                    **({"clock": lambda: self.sim_timeline.now} if self.sim_timeline else {}),
                    security_key_path=(
                        str(ground_key_setting) if ground_key_setting is not None else None
                    ),
                    timestamp_window_s=float(
                        options.get(
                            "command_timestamp_window_s",
                            security.get("timestamp_window_s", 30.0),
                        )
                    ),
                    clock_skew_s=float(
                        options.get("command_clock_skew_s", security.get("clock_skew_s", 5.0))
                    ),
                    rate_limit=int(
                        options.get("command_rate_limit", security.get("rate_limit", 20))
                    ),
                    rate_window_s=float(
                        options.get("command_rate_window_s", security.get("rate_window_s", 10.0))
                    ),
                    energy_config=EnergyConfig.from_budget(
                        self.mission_package.document["budget"], options.get("energy_model")
                    ),
                    runtime_mode=effective_runtime_mode,
                )
                if self._model_split:
                    self.downlink.log.append(
                        "model_swap",
                        self.sim_timeline.now if self.sim_timeline is not None else time.monotonic(),
                        model_runtime=dict(self._model_runtime),
                    )
                # Telemetry readers stamp receipts with host monotonic time,
                # while SIM supervisors run on the simulation clock. Keep a
                # fixed affine offset so both values are compared in one domain.
                self._energy_receipt_clock_offset = self.supervisor._now - time.monotonic()
                self.executor = self.supervisor
                for event in options.get("ground_events", []):
                    self.supervisor.ground_event(event)
        self.slow_calls = 0

    def _refuse_package_plan(self, violations: Sequence[Mapping[str, Any]]) -> None:
        """Persist a fail-closed admission decision before raising to the caller."""
        if self.mission_package is None:
            raise ValueError("mission plan violates package policy")
        log = None
        try:
            log = OnboardMissionLog(self.work_dir / "mission_log", self.mission_package.mission_id)
            now = self.sim_timeline.now if self.sim_timeline is not None else time.monotonic()
            log.append(
                "mission_refused",
                now,
                reason="package_policy_violation",
                violations=list(violations),
            )
            log.finish(
                now,
                {
                    "status": "refused",
                    "reason": "package_policy_violation",
                    "violations": list(violations),
                },
            )
        finally:
            close = getattr(self.slow, "close", None)
            if close is not None:
                close()
        details = "; ".join(
            f"{item.get('code')}: {item.get('message_zh')} / {item.get('message_en')}"
            for item in violations
        )
        raise ValueError(f"mission plan refused by package policy: {details}")

    def acknowledge_payload_release(self, request_id: str, *, released: bool = True) -> bool:
        """Forward a host receipt; this policy has no payload hardware driver."""
        return self.executor is not None and self.executor.acknowledge_payload_release(
            request_id, released=released
        )

    def close(self) -> None:
        try:
            if self.supervisor is not None:
                self.supervisor.finish()
            if self.downlink is not None:
                self.downlink.close()
        finally:
            try:
                close = getattr(self.slow, "close", None)
                if close is not None:  # injected runners may not own a process
                    close()
            finally:
                if self.executor is not None:
                    try:
                        self._write_mission_summary()
                    except OSError as exc:
                        warnings.warn(f"Could not write mission summary: {exc}", stacklevel=2)

    def ground_event(self, event: dict[str, Any]) -> bool:
        """Optional transport adapter: timestamps are local receipt times."""
        return self.supervisor.ground_event(event) if self.supervisor is not None else False

    def set_downlink_sender(self, sender: Callable[[dict[str, Any]], object] | None) -> None:
        """Attach an optional acknowledged transport; delivery runs in the queue worker."""
        if self.downlink is not None:
            self.downlink.set_sender(sender)

    def _write_mission_summary(self) -> None:
        """Persist at teardown, beside the collector's steps.jsonl labels."""
        executor = self.executor
        report = executor.report()
        report_input = {
                "history": list(executor.history),
                "findings": list(executor.findings),
                "counts": dict(executor.counts),
                "coverage": dict(executor.coverage),
                "evidence": list(report.evidence_frame_ids),
                "answers": list(report.state_answers),
                "alerts": list(report.alerts),
            }
        if self._model_split:
            report_input["model_runtime"] = dict(self._model_runtime)
        summary, chinese, english = summarize_mission(
            report_input,
            executor.plan,
            self.task_text,
        )
        # The collector uses <out>/<episode-id>-onboard-frames as work_dir.
        suffix = "-onboard-frames"
        directory = (
            self.work_dir.parent / "episodes" / self.work_dir.name.removesuffix(suffix)
            if self.work_dir.name.endswith(suffix)
            else self.work_dir
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mission_summary.json").write_text(summary_json(summary), encoding="utf-8")
        (directory / "mission_summary.txt").write_text(
            chinese + "\n\n" + english + "\n", encoding="utf-8"
        )

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

        if getattr(self, "health", None) is not None and self.health.state.low_disk:
            self._slow_skipped_current = True
            return {
                "track_id": focus.track_id,
                "identity": None,
                "error": "slow_call_skipped_low_disk",
                "latency_s": 0.0,
            }

        started = time.perf_counter()
        try:
            raw, latency = self.slow(row)
        except Exception as exc:  # timeouts or server faults leave the track unverified
            self._slow_failed_current = True
            health = getattr(self, "health", None)
            if health is not None:
                health.record_slow_call(time.perf_counter() - started, ok=False)
            return {
                "track_id": focus.track_id,
                "identity": None,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": time.perf_counter() - started,
            }
        self.slow_calls += 1
        decision, error = parse_model_decision(raw)
        health = getattr(self, "health", None)
        if health is not None:
            health.record_slow_call(float(latency), ok=decision is not None)
        if decision is None:
            self._slow_failed_current = True
        identity = identity_of(decision["decision"]) if decision else None
        return {
            "track_id": focus.track_id,
            "identity": identity,
            "decision": decision["decision"] if decision else None,
            "reason": decision["reason"] if decision else None,
            "confidence": decision_confidence(getattr(self.slow, "last_logprobs", None)),
            "error": error,
            "latency_s": float(latency),
        }

    def _act_impl(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        self._slow_failed_current = False
        self._slow_skipped_current = False
        # Process external safety inputs first. A latched supervisor decision and
        # one-shot ground event must never be hidden by a health hold.
        if self.supervisor is not None:
            if self.sim_timeline is not None:
                self.sim_timeline.advance(observation, self.supervisor)
            for sample in observation.get("energy_telemetry", []):
                received_at = float(sample["received_monotonic_s"])
                received_at += getattr(self, "_energy_receipt_clock_offset", 0.0)
                received_at = min(received_at, self.supervisor._now)
                self.supervisor.ingest_energy_telemetry(
                    sample["message"], received_at_s=received_at
                )
            for event in observation.get("ground_events", []):
                self.supervisor.ground_event(event)
            update_estimates = getattr(self.supervisor, "update_estimates", None)
            if update_estimates is not None:
                update_estimates(**observation.get("mission_estimates", {}))
            evaluate = getattr(self.supervisor, "evaluate", None)
            if evaluate is not None and evaluate(pose_ned=pose_ned) != "continue":
                action, info = self.supervisor.next_action(
                    pose_ned=tuple(pose_ned),
                    yaw_deg=yaw_deg,
                    landed=observation.get("landed"),
                    timestamp_s=_observation_timestamp_s(observation),
                )
                supervised_labels = {
                    **info,
                    "state": "supervised",
                    "detections": 0,
                    "tracks": [],
                    "slow_calls": [],
                    "confirmed": None,
                    "alert_vote_clusters": [],
                    "fast_latency_s": 0.0,
                    "mission": {"history": list(self.supervisor.history)},
                }
                if getattr(getattr(self, "health", None), "config", None) is not None and getattr(
                    self.health.config, "enabled", False
                ):
                    supervised_labels["health"] = {}
                return action, {"onboard": supervised_labels}
        health_monitor = getattr(self, "health", None)
        health_decision = (
            health_monitor.on_frame(mission_requires_images=bool(self.executor is not None))
            if health_monitor is not None
            else HealthDecision()
        )
        # A completed recovery must not be reused by a later degradation
        # cycle.  Likewise, once health is back to ``ok`` and the main
        # mission resumes, discard any stale recovery executor so the next
        # escalation gets a fresh plan and state.
        if (
            health_decision.decision == "ok"
            and getattr(self, "_health_recovery_executor", None) is not None
        ):
            self._health_recovery_executor = None
        if health_decision.decision in {"return_home", "land"}:
            if self.supervisor is not None:
                self.supervisor._escalate(
                    "land_now" if health_decision.decision == "land" else "return_now",
                    health_decision.reason or "health degradation",
                )
                action, info = self.supervisor.next_action(
                    pose_ned=tuple(pose_ned), yaw_deg=yaw_deg
                )
                return action, {"onboard": {**info, "health": health_decision.__dict__}}
            if self.executor is not None:
                action, info = self._health_recovery_action(
                    pose_ned=pose_ned,
                    yaw_deg=yaw_deg,
                    land=health_decision.decision == "land",
                    timestamp_s=_observation_timestamp_s(observation),
                )
                return action, {"onboard": {**info, "health": health_decision.__dict__}}
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": health_decision.decision, "health": health_decision.__dict__}
            }
        if health_decision.decision == "hold":
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": "health_hold", "health": health_decision.__dict__}
            }
        if health_decision.detector_skip:
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": "health_hold", "health": health_decision.__dict__}
            }
        import numpy as np
        from PIL import Image

        step = self._step
        self._step += 1
        rgb = np.asarray(Image.open(io.BytesIO(observation["rgb"])).convert("RGB"))
        suffix = "jpg" if self.frame_format == "jpeg" else "png"
        frame_path = self.work_dir / f"frame_{step:04d}.{suffix}"
        if health_decision.stop_frame_writes:
            # Low disk: write no frame; slow calls are skipped by _slow_identity.
            pass
        elif self.frame_format == "jpeg":
            Image.fromarray(rgb, mode="RGB").save(
                frame_path, format="JPEG", quality=self.frame_quality, optimize=True
            )
        else:
            frame_path.write_bytes(observation["rgb"])
        started = time.perf_counter()
        detections = self.proposer(rgb[:, :, ::-1].copy())
        fast_s = time.perf_counter() - started
        camera_info = observation["camera_info"]
        ground_model = self.ground_projection.for_observation(observation)

        def ground_point(u: float, v: float):
            projector = getattr(self, "ground_projector", None)
            if projector is not None:
                return projector(u, v, observation)
            # Full attitude + camera mount inverse of the projection model.
            return pixel_ground_point(
                u,
                v,
                pose=observation,
                camera_info=camera_info,
                image_size=self.image_size,
                ground_model=ground_model,
            )

        if self.executor is not None:
            result = self._act_mission(
                frame_path,
                detections,
                fast_s,
                frame_rgb=rgb,
                pose_ned=pose_ned,
                yaw_deg=yaw_deg,
                ground_point=ground_point,
                landed=observation.get("landed"),
                timestamp_s=_observation_timestamp_s(observation),
            )
            if self._slow_failed_current:
                labels = {"state": "health_hold"}
                if self.health.config.enabled:
                    labels["health"] = {"decision": "hold", "reason": "slow_call_failed"}
                return ActionPrimitive.hold(1.0), {"onboard": labels}
            if self._slow_skipped_current:
                labels = {"state": "health_hold"}
                if self.health.config.enabled:
                    labels["health"] = {"reason": "low_disk"}
                return ActionPrimitive.hold(1.0), {"onboard": labels}
            return result
        current = self.loop.update_tracks(
            detections,
            pose_ned=tuple(pose_ned),
            yaw_deg=yaw_deg,
            ground_point=ground_point,
            frame_rgb=rgb,
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
        if self._slow_failed_current:
            labels = {"state": "health_hold"}
            if self.health.config.enabled:
                labels["health"] = {"decision": "hold", "reason": "slow_call_failed"}
            return ActionPrimitive.hold(1.0), {"onboard": labels}
        if self._slow_skipped_current:
            labels = {"state": "health_hold"}
            if self.health.config.enabled:
                labels["health"] = {"reason": "low_disk"}
            return ActionPrimitive.hold(1.0), {"onboard": labels}
        return action, labels

    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        """Single return point: every policy primitive crosses the envelope."""
        action, labels = self._act_impl(observation, pose_ned=pose_ned, yaw_deg=yaw_deg)
        # Some lightweight test/integration adapters use ``None`` to indicate
        # that no motion was requested this frame; there is no outgoing
        # primitive to arbitrate in that case.
        if action is None:
            return action, labels
        envelope = getattr(self, "safety_envelope", None)
        if envelope is None:
            return action, labels
        executor = getattr(self, "executor", None)
        step = getattr(executor, "current_step", None)
        phase = None
        if isinstance(labels, dict):
            onboard = labels.get("onboard", {})
            phase = onboard.get("phase") if isinstance(onboard, dict) else None
            if isinstance(onboard, dict) and onboard.get("supervisor_action") == "land_now":
                phase = "landing"
                # A terminal supervisor landing is an explicit landing skill;
                # never let the still-active mission step disable its bounded
                # low-altitude exception.
                step = type("_LandingStep", (), {"skill": "land"})()
        agl = observation.get("ground_agl", observation.get("agl_m"))
        if isinstance(agl, Mapping):
            agl = agl.get("height_m", agl.get("agl_m"))
        decision = envelope.apply(
            action,
            pose_ned=tuple(float(v) for v in pose_ned),
            yaw_deg=float(yaw_deg),
            current_agl_m=agl,
            phase=phase,
            skill=getattr(step, "skill", None),
        )
        if decision.rejected and self.supervisor is not None:
            self.supervisor.report_envelope_violation(
                ", ".join(decision.reasons) or "rejected primitive"
            )
        labels = {**labels, **decision.info()}
        return decision.action, labels

    def _health_recovery_action(self, *, pose_ned, yaw_deg: float, land: bool, timestamp_s=None):
        skill = "land" if land else "return_home"
        if (
            self._health_recovery_executor is None
            or self._health_recovery_executor.plan.steps[0].skill != skill
        ):
            plan = validate_mission_plan(
                {
                    "plan_version": 1,
                    "goal": "health recovery",
                    "report": ["status"],
                    "steps": [
                        {
                            "id": "recovery",
                            "skill": skill,
                            "params": {},
                            "on_success": None,
                            "on_fail": None,
                        }
                    ],
                }
            )
            recovery = MissionExecutor(plan, loop_factory=self.executor.loop_factory)
            recovery.home = getattr(self.executor, "home", None)
            recovery.cruise_altitude_m = getattr(self.executor, "cruise_altitude_m", None)
            self._health_recovery_executor = recovery
        action, info = self._health_recovery_executor.next_action(
            pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, timestamp_s=timestamp_s
        )
        if self._health_recovery_executor.done:
            self._health_recovery_executor = None
        return action, info

    def _act_mission(
        self,
        frame_path,
        detections,
        fast_s,
        *,
        pose_ned,
        yaw_deg,
        ground_point,
        frame_rgb=None,
        landed=None,
        timestamp_s=None,
    ):
        executor = self.executor
        step = executor.current_step
        perception_text = executor.current_task_text or self.task_text
        findings_before = len(executor.findings)
        needing = executor.observe(
            detections,
            pose_ned=tuple(pose_ned),
            yaw_deg=yaw_deg,
            ground_point=ground_point,
            frame_rgb=frame_rgb,
            landed=landed,
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
                executor.apply_verdict(track.track_id, verdict["decision"], remembered=True)
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
            landed=landed,
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
        if self.mission_plan is not None and not self._mission_plan_labeled:
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
    frame_format: str = "png",
    frame_quality: int = 90,
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
        frame_format=frame_format,
        frame_quality=frame_quality,
    )


__all__ = [
    "OnboardPolicy",
    "VOCABULARY",
    "YoloeProposer",
    "build_mission_from_text",
    "build_onboard_policy",
    "perception_task_text",
]
