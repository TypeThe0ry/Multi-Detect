"""Append-only episode recorder and offline dataset auditor."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import Any


def _json_default(value: Any):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class EpisodeRecorder:
    def __init__(
        self,
        root: str | Path,
        episode_id: str,
        *,
        task_text: str = "",
        sensor_status: str = "unknown",
    ):
        self.episode_dir = Path(root) / "episodes" / episode_id
        self.rgb_dir = self.episode_dir / "rgb"
        self.depth_dir = self.episode_dir / "depth"
        self.rgb_dir.mkdir(parents=True, exist_ok=True)
        self.depth_dir.mkdir(exist_ok=True)
        self.steps_path = self.episode_dir / "steps.jsonl"
        self.mavlink_path = self.episode_dir / "mavlink.jsonl"
        self.episode_path = self.episode_dir / "episode.json"
        self.index = 0
        self.meta = {
            "schema_version": 2,
            "episode_id": episode_id,
            "task_text": task_text,
            "sensor_status": sensor_status,
            "created_wall_time": time.time(),
            "created_monotonic_ns": time.monotonic_ns(),
            "step_count": 0,
        }
        self.episode_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        self._mavlink_file = None
        self._mavlink_lock = threading.Lock()
        self._mavlink_count = 0
        self._mavlink_last_flush = time.monotonic()

    def begin_mavlink_stream(self) -> None:
        with self._mavlink_lock:
            if self._mavlink_file is None:
                self._mavlink_file = self.mavlink_path.open(
                    "a", encoding="utf-8", buffering=1024 * 1024
                )

    def close_mavlink_stream(self) -> None:
        with self._mavlink_lock:
            if self._mavlink_file is not None:
                self._mavlink_file.flush()
                self._mavlink_file.close()
                self._mavlink_file = None

    def mark_finished(self, *, status: str = "complete", failure: str | None = None) -> None:
        self.meta["finished_wall_time"] = time.time()
        self.meta["finished_monotonic_ns"] = time.monotonic_ns()
        self.meta["status"] = status
        if failure:
            self.meta["failure"] = failure
        self.close_mavlink_stream()
        self.episode_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def _observation(self, obs: dict[str, Any] | None, prefix: str) -> dict[str, Any] | None:
        if obs is None:
            return None
        out = {k: v for k, v in obs.items() if k not in {"rgb", "depth", "segmentation"}}
        stem = f"{self.index:06d}_{prefix}"
        rgb = obs.get("rgb")
        if rgb is not None:
            path = self.rgb_dir / f"{stem}.png"
            if isinstance(rgb, (bytes, bytearray)):
                path.write_bytes(bytes(rgb))
            elif isinstance(rgb, str):
                path.write_bytes(base64.b64decode(rgb))
            else:
                try:
                    from PIL import Image

                    Image.fromarray(rgb).save(path)
                except Exception:
                    path.write_bytes(json.dumps(rgb, default=_json_default).encode())
            out["rgb_path"] = str(path.relative_to(self.episode_dir))
        depth = obs.get("depth")
        if depth is not None:
            path = self.depth_dir / f"{stem}.npy"
            try:
                import numpy as np

                np.save(path, np.asarray(depth, dtype="float32"))
            except Exception:
                path.write_text(json.dumps(depth, default=_json_default), encoding="utf-8")
            out["depth_path"] = str(path.relative_to(self.episode_dir))
        segmentation = obs.get("segmentation")
        if segmentation is not None:
            import numpy as np
            from PIL import Image

            array = np.asarray(segmentation)
            if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3:
                raise ValueError("segmentation must be a raw RGB uint8 panoptic labels map")
            if "camera_info" in obs and array.shape[:2] != (
                int(obs["camera_info"]["height"]), int(obs["camera_info"]["width"])
            ):
                raise ValueError("segmentation geometry differs from RGB camera_info")
            directory = self.episode_dir / "seg"
            directory.mkdir(exist_ok=True)
            path = directory / f"{stem}.png"
            Image.fromarray(array).save(path, format="PNG")
            out["segmentation_path"] = str(path.relative_to(self.episode_dir)).replace("\\", "/")
        out.setdefault("receive_monotonic_ns", time.monotonic_ns())
        out.setdefault("receive_wall_time", time.time())
        return out

    def record_step(
        self,
        obs_before: dict[str, Any],
        action: Any,
        *,
        ack: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        obs_after: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        before = self._observation(obs_before, "before")
        after = self._observation(obs_after, "after") if obs_after else None
        action_obj = action.to_dict() if hasattr(action, "to_dict") else action
        row = {
            "step_index": self.index,
            "task_text": self.meta["task_text"],
            "receive_monotonic_ns": time.monotonic_ns(),
            "receive_wall_time": time.time(),
            "obs_before": before,
            "action": action_obj,
            "ack": ack or {},
            "outcome": outcome or {},
            "obs_after": after,
            "labels": labels or {},
        }
        with self.steps_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=_json_default) + "\n")
        self.index += 1
        self.meta["step_count"] = self.index
        self.episode_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def record_mavlink(self, message: Any) -> None:
        row = message if isinstance(message, dict) else {"message": str(message)}
        row = {**row, "receive_monotonic_ns": time.monotonic_ns(), "receive_wall_time": time.time()}
        with self._mavlink_lock:
            if self._mavlink_file is None:
                with self.mavlink_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, default=_json_default) + "\n")
                return
            self._mavlink_file.write(json.dumps(row, default=_json_default) + "\n")
            self._mavlink_count += 1
            now = time.monotonic()
            if self._mavlink_count % 256 == 0 or now - self._mavlink_last_flush >= 0.25:
                self._mavlink_file.flush()
                self._mavlink_last_flush = now


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class DatasetAuditError(ValueError):
    pass


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finite_pose(pose: Any) -> bool:
    if not isinstance(pose, dict):
        return False
    local = pose.get("local_ned_m", pose)
    if isinstance(local, dict):
        values = [local.get(k) for k in ("north_m", "east_m", "down_m")]
    elif isinstance(local, (list, tuple)):
        values = list(local[:3])
    else:
        return False
    if len(values) != 3 or not all(_finite_number(item) for item in values):
        return False
    # Live observations carry the global pose alongside local NED.  Require
    # finite values when those fields are present; strict live admission also
    # rejects absent fields below.
    global_values = [pose.get(key) for key in ("lat_deg", "lon_deg", "alt_m")]
    return all(_finite_number(item) for item in global_values)


def _has_gps_fix(gps: Any) -> bool:
    if not isinstance(gps, dict):
        return False
    fix = gps.get("fix_type", gps.get("fix", 0))
    return _finite_number(fix) and float(fix) >= 2


def _real_file(ep: Path, value: Any, suffix: str) -> bool:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return False
    path = ep / value
    try:
        if not path.is_file() or path.suffix.lower() != suffix or path.stat().st_size <= 0:
            return False
        # Live RGB must be an actual PNG, avoiding JSON/placeholder fallbacks.
        if suffix == ".png":
            with path.open("rb") as stream:
                return stream.read(8) == b"\x89PNG\r\n\x1a\n"
        return True
    except OSError:
        return False


def _has_action_ack(ack: Any) -> bool:
    if not isinstance(ack, dict) or not ack:
        return False
    for key in ("action_ack", "ack", "accepted", "ok"):
        if ack.get(key) is True:
            return True
    result = ack.get("result", ack.get("status"))
    if isinstance(result, str) and result.lower() in {
        "accepted",
        "success",
        "ok",
        "mav_result_accepted",
        "mav_result_in_progress",
    }:
        return True
    return False


DEPTH_MIN_FINITE_FRACTION = 0.05

# PX4's LOCAL_POSITION_NED.time_boot_ms and Gazebo's image sensor stamp are
# simulation clocks. The collector records their difference on each live
# observation. Keep the admission limit tight enough to catch a queued pipe
# frame while allowing a small amount of lockstep jitter.
DEFAULT_MAX_SIM_SKEW_MS = 250.0
DEFAULT_POSE_MOTION_THRESHOLD_M = 1.0


def _depth_finite(ep: Path, value: Any) -> bool:
    if not _real_file(ep, value, ".npy"):
        return False
    try:
        import numpy as np

        array = np.load(ep / value, allow_pickle=False)
        # Gazebo depth reports +/-inf for no-return pixels (sky, below near clip); those are
        # physical. NaN is corruption. Require a usable share of finite ranges.
        if array.size == 0 or array.dtype != np.dtype("float32") or bool(np.isnan(array).any()):
            return False
        return float(np.isfinite(array).mean()) >= DEPTH_MIN_FINITE_FRACTION
    except Exception:
        return False


def _pose_vector(pose: Any) -> tuple[float, float, float] | None:
    """Return a local NED pose as a finite 3-vector when it is available."""
    if not isinstance(pose, dict):
        return None
    local = pose.get("local_ned_m", pose)
    if isinstance(local, dict):
        values = [local.get(k) for k in ("north_m", "east_m", "down_m")]
    elif isinstance(local, (list, tuple)):
        values = list(local[:3])
    else:
        return None
    if len(values) != 3 or not all(_finite_number(item) for item in values):
        return None
    return tuple(float(item) for item in values)


def _live_sim_alignment(
    obs: dict[str, Any],
    *,
    max_sim_skew_ms: float,
    step_index: int,
    name: str,
    enforce_limit: bool = True,
) -> tuple[int, float, tuple[float, float, float]]:
    """Validate and normalize simulation clock fields on one observation."""
    frame_stamp = obs.get("frame_sensor_stamp_ns")
    if isinstance(frame_stamp, bool) or not isinstance(frame_stamp, int) or frame_stamp <= 0:
        raise DatasetAuditError(
            f"live step {step_index} {name} has invalid frame_sensor_stamp_ns"
        )
    pose_time = obs.get("pose_time_boot_ms")
    if isinstance(pose_time, bool) or not isinstance(pose_time, int) or pose_time < 0:
        raise DatasetAuditError(f"live step {step_index} {name} has invalid pose_time_boot_ms")
    sim_skew = obs.get("sim_skew_ms")
    if not _finite_number(sim_skew):
        raise DatasetAuditError(f"live step {step_index} {name} has invalid sim_skew_ms")
    expected_skew = frame_stamp / 1_000_000.0 - float(pose_time)
    if not math.isclose(float(sim_skew), expected_skew, rel_tol=0.0, abs_tol=1e-6):
        raise DatasetAuditError(
            f"live step {step_index} {name} sim_skew_ms does not match frame and pose stamps"
        )
    if enforce_limit and abs(float(sim_skew)) > max_sim_skew_ms:
        raise DatasetAuditError(
            f"live step {step_index} {name} sim_skew_ms exceeds {max_sim_skew_ms:g} ms"
        )
    pose = _pose_vector(obs.get("pose"))
    if pose is None:
        raise DatasetAuditError(f"live step {step_index} {name} has invalid pose")
    return frame_stamp, float(sim_skew), pose


def _commanded_errors(
    action: dict[str, Any], outcome: dict[str, Any]
) -> tuple[float | None, float | None]:
    achieved = outcome.get("achieved_delta_ned")
    if not isinstance(achieved, (list, tuple)) or len(achieved) < 3:
        return None, None
    if not all(_finite_number(value) for value in achieved[:3]):
        return None, None
    commanded = outcome.get("commanded_delta_ned")
    if not isinstance(commanded, (list, tuple)) or len(commanded) < 3:
        # Fallback is valid for yaw=0 recordings and remains useful as a scalar
        # diagnostic if an older executor omitted the converted target.
        commanded = [
            action.get("forward_m", 0.0),
            action.get("right_m", 0.0),
            -float(action.get("up_m", 0.0)),
        ]
    if not all(_finite_number(value) for value in commanded[:3]):
        return None, None
    position_error = math.sqrt(
        sum((float(achieved[i]) - float(commanded[i])) ** 2 for i in range(3))
    )
    yaw_error = None
    achieved_yaw = outcome.get("achieved_yaw_delta_deg")
    commanded_yaw = action.get("yaw_delta_deg", 0.0)
    if _finite_number(achieved_yaw) and _finite_number(commanded_yaw):
        yaw_error = abs(float(achieved_yaw) - float(commanded_yaw))
    return position_error, yaw_error


def audit_dataset(
    root: str | Path,
    *,
    max_sim_skew_ms: float = DEFAULT_MAX_SIM_SKEW_MS,
    pose_motion_threshold_m: float = DEFAULT_POSE_MOTION_THRESHOLD_M,
) -> dict[str, Any]:
    """Audit a dataset and write its immutable file manifest.

    ``max_sim_skew_ms`` is intentionally configurable for replay fixtures, but
    live admission defaults to 250 ms. ``pose_motion_threshold_m`` controls the
    additional stuck-frame check: a live pose movement above this distance must
    be accompanied by a strictly newer Gazebo frame stamp.
    """
    if not _finite_number(max_sim_skew_ms) or float(max_sim_skew_ms) < 0.0:
        raise ValueError("max_sim_skew_ms must be a finite non-negative number")
    if not _finite_number(pose_motion_threshold_m) or float(pose_motion_threshold_m) <= 0.0:
        raise ValueError("pose_motion_threshold_m must be a finite positive number")
    root = Path(root)
    episodes = root / "episodes"
    manifest: dict[str, str] = {}
    total_steps = 0
    successes = 0
    action_hist: dict[str, int] = {}
    skews: list[float] = []
    sim_skews: list[float] = []
    position_errors: list[float] = []
    yaw_errors: list[float] = []
    episode_reports: dict[str, dict[str, Any]] = {}
    over_limit_episodes: list[tuple[str, int]] = []
    if not episodes.is_dir():
        raise DatasetAuditError("episodes directory missing")
    for file in sorted(p for p in episodes.rglob("*") if p.is_file()):
        manifest[file.relative_to(root).as_posix()] = _sha256(file)
    # audit.json is the report being regenerated, so it is never part of its own manifest.
    manifest.pop("audit.json", None)
    previous_path = root / "audit.json"
    if previous_path.is_file():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_manifest = previous.get("files", {})
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetAuditError("existing audit.json is not valid JSON") from exc
        if not isinstance(previous_manifest, dict):
            raise DatasetAuditError("existing audit.json has no file manifest")
        if previous_manifest != manifest:
            changed = sorted(set(previous_manifest) | set(manifest), key=str)
            changed = [
                item for item in changed if previous_manifest.get(item) != manifest.get(item)
            ]
            raise DatasetAuditError(
                "dataset changed since previous audit: " + ", ".join(changed[:8])
            )
    for ep in sorted(p for p in episodes.iterdir() if p.is_dir()):
        steps_file = ep / "steps.jsonl"
        if not steps_file.exists():
            raise DatasetAuditError(f"missing steps.jsonl: {ep.name}")
        try:
            meta = json.loads((ep / "episode.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetAuditError(f"invalid episode.json: {ep.name}") from exc
        strict_live = meta.get("sensor_status") == "live"
        prev = -1
        previous_receive_ns = -1
        ep_steps = 0
        ep_successes = 0
        ep_positions: list[float] = []
        ep_yaws: list[float] = []
        ep_sim_skew_over_limit = 0
        previous_frame_stamp_ns: int | None = None
        previous_pose: tuple[float, float, float] | None = None
        for line_no, line in enumerate(steps_file.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetAuditError(f"invalid JSON {steps_file}:{line_no}") from exc
            idx = row.get("step_index")
            receive_ns = row.get("receive_monotonic_ns")
            if not isinstance(idx, int) or idx <= prev:
                raise DatasetAuditError(f"non-monotonic step index in {ep.name}")
            if isinstance(receive_ns, bool) or not isinstance(receive_ns, int):
                raise DatasetAuditError(f"invalid receive time in {ep.name}")
            if receive_ns <= previous_receive_ns:
                raise DatasetAuditError(f"non-monotonic receive time in {ep.name}")
            prev, previous_receive_ns = idx, receive_ns
            total_steps += 1
            ep_steps += 1
            action = row.get("action")
            if not isinstance(action, dict):
                raise DatasetAuditError(f"step {idx} in {ep.name} has no action")
            typ = action.get("kind", "unknown")
            action_hist[typ] = action_hist.get(typ, 0) + 1
            outcome = row.get("outcome") or {}
            if outcome.get("status") == "success":
                successes += 1
                ep_successes += 1
            before = row.get("obs_before")
            after = row.get("obs_after")
            if not isinstance(before, dict):
                raise DatasetAuditError(f"step {idx} in {ep.name} has no obs_before")
            if strict_live:
                if not isinstance(after, dict):
                    raise DatasetAuditError(f"live step {idx} in {ep.name} has no obs_after")
                for name, obs in (("obs_before", before), ("obs_after", after)):
                    if not _real_file(ep, obs.get("rgb_path"), ".png"):
                        raise DatasetAuditError(f"live step {idx} {name} has no real rgb file")
                    if not _depth_finite(ep, obs.get("depth_path")):
                        raise DatasetAuditError(f"live step {idx} {name} has no finite depth file")
                    if not _finite_pose(obs.get("pose")):
                        raise DatasetAuditError(f"live step {idx} {name} has invalid pose")
                    if not _has_gps_fix(obs.get("gps_fix")):
                        raise DatasetAuditError(f"live step {idx} {name} has no GPS fix")
                    frame_stamp, sim_skew, pose = _live_sim_alignment(
                        obs,
                        max_sim_skew_ms=float(max_sim_skew_ms),
                        step_index=idx,
                        name=name,
                        enforce_limit=False,
                    )
                    sim_skews.append(sim_skew)
                    # Count offending transitions once even when both before and
                    # after observations exceed the limit.
                    if previous_frame_stamp_ns is not None and previous_pose is not None:
                        moved = math.sqrt(
                            sum(
                                (pose[axis] - previous_pose[axis]) ** 2
                                for axis in range(3)
                            )
                        )
                        if (
                            moved > float(pose_motion_threshold_m)
                            and frame_stamp <= previous_frame_stamp_ns
                        ):
                            raise DatasetAuditError(
                                f"live episode {ep.name} frame stamps do not advance "
                                f"while pose moves ({moved:.3f} m)"
                            )
                    previous_frame_stamp_ns = frame_stamp
                    previous_pose = pose
                ack = row.get("ack")
                if not _has_action_ack(ack):
                    raise DatasetAuditError(f"live step {idx} in {ep.name} has no action ACK")
                if any(
                    abs(float(obs.get("sim_skew_ms"))) > float(max_sim_skew_ms)
                    for obs in (before, after)
                ):
                    ep_sim_skew_over_limit += 1
            else:
                for obs in (before, after or {}):
                    for key in ("rgb_path", "depth_path"):
                        if key in obs and not (ep / obs[key]).exists():
                            raise DatasetAuditError(f"missing referenced file {obs[key]}")
            if before.get("receive_monotonic_ns") and receive_ns:
                skews.append(abs(receive_ns - before["receive_monotonic_ns"]) / 1e9)
            pos_error, yaw_error = _commanded_errors(action, outcome)
            if pos_error is not None:
                position_errors.append(pos_error)
                ep_positions.append(pos_error)
            if yaw_error is not None:
                yaw_errors.append(yaw_error)
                ep_yaws.append(yaw_error)
        duration = None
        started = meta.get("created_monotonic_ns")
        finished = meta.get("finished_monotonic_ns")
        if (
            _finite_number(started)
            and _finite_number(finished)
            and float(finished) >= float(started)
        ):
            duration = (float(finished) - float(started)) / 1e9
        episode_reports[ep.name] = {
            "step_count": ep_steps,
            "success_rate": ep_successes / ep_steps if ep_steps else 0.0,
            "duration_s": duration,
            "position_error_m": {
                "count": len(ep_positions),
                "mean": sum(ep_positions) / len(ep_positions) if ep_positions else 0.0,
                "max": max(ep_positions, default=0.0),
            },
            "yaw_error_deg": {
                "count": len(ep_yaws),
                "mean": sum(ep_yaws) / len(ep_yaws) if ep_yaws else 0.0,
                "max": max(ep_yaws, default=0.0),
            },
            "sim_skew_over_limit_count": ep_sim_skew_over_limit,
            "sim_skew_over_limit_steps": ep_sim_skew_over_limit,
        }
        if ep_sim_skew_over_limit:
            over_limit_episodes.append((ep.name, ep_sim_skew_over_limit))
    audit = {
        "files": manifest,
        "step_count": total_steps,
        "success_rate": successes / total_steps if total_steps else 0.0,
        "action_histogram": action_hist,
        "frame_pose_skew_s": {
            "count": len(skews),
            "max": max(skews, default=0.0),
            "mean": sum(skews) / len(skews) if skews else 0.0,
        },
        "sim_skew_ms": {
            "count": len(sim_skews),
            "max_abs": max((abs(value) for value in sim_skews), default=0.0),
            "mean": sum(sim_skews) / len(sim_skews) if sim_skews else 0.0,
            "limit": float(max_sim_skew_ms),
            "over_limit_count": sum(
                report["sim_skew_over_limit_count"] for report in episode_reports.values()
            ),
        },
        "achieved_vs_commanded_error": {
            "position_m": {
                "count": len(position_errors),
                "mean": sum(position_errors) / len(position_errors) if position_errors else 0.0,
                "max": max(position_errors, default=0.0),
            },
            "yaw_deg": {
                "count": len(yaw_errors),
                "mean": sum(yaw_errors) / len(yaw_errors) if yaw_errors else 0.0,
                "max": max(yaw_errors, default=0.0),
            },
        },
        "episodes": episode_reports,
    }
    previous_path.write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if over_limit_episodes:
        details = ", ".join(f"{name}: {count}" for name, count in over_limit_episodes)
        raise DatasetAuditError(
            f"live episodes have sim_skew_ms exceeds {max_sim_skew_ms:g} ms "
            f"(over-limit steps by episode: {details})"
        )
    return audit


__all__ = [
    "EpisodeRecorder",
    "DatasetAuditError",
    "DEFAULT_MAX_SIM_SKEW_MS",
    "DEFAULT_POSE_MOTION_THRESHOLD_M",
    "audit_dataset",
]
