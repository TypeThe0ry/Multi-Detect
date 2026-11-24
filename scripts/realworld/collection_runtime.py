"""Runtime options and collector evidence shared by wide collection entry points."""
import json
import math
import re

_NAME_PART = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE_REFERENCE = re.compile(
    rf"(?:[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]+)?/)?(?:{_NAME_PART}/)*{_NAME_PART}"
    r"(?::[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127})?(?:@sha256:[a-fA-F0-9]{64})?"
)


def runtime_options(spec, airframe, field):
    options = {}
    if "image" in spec:
        image = spec["image"]
        if not isinstance(image, str) or not _IMAGE_REFERENCE.fullmatch(image):
            raise ValueError(f"{field}.image must be a nonempty container image reference")
        options["image"] = image
    if "gimbal_bridge_stabilize" in spec:
        enabled = spec["gimbal_bridge_stabilize"]
        if type(enabled) is not bool:
            raise ValueError(f"{field}.gimbal_bridge_stabilize must be a JSON boolean")
        if enabled and airframe != "delta_tailsitter_gimbal_cam":
            raise ValueError(f"{field}.gimbal_bridge_stabilize requires delta_tailsitter_gimbal_cam")
        options["gimbal_bridge_stabilize"] = enabled
    if "gimbal_eval" in spec:
        enabled = spec["gimbal_eval"]
        if type(enabled) is not bool:
            raise ValueError(f"{field}.gimbal_eval must be a JSON boolean")
        if enabled and not options.get("gimbal_bridge_stabilize"):
            raise ValueError(f"{field}.gimbal_eval requires X1 bridge stabilization")
        options["gimbal_eval"] = enabled
    if "gimbal_eval_fw" in spec:
        enabled = spec["gimbal_eval_fw"]
        if type(enabled) is not bool:
            raise ValueError(f"{field}.gimbal_eval_fw must be a JSON boolean")
        if enabled and not options.get("gimbal_eval"):
            raise ValueError(f"{field}.gimbal_eval_fw requires gimbal_eval")
        options["gimbal_eval_fw"] = enabled
    if "fw_min_leg_m" in spec:
        min_leg = spec["fw_min_leg_m"]
        if type(min_leg) not in (int, float) or not math.isfinite(min_leg) or min_leg <= 0:
            raise ValueError(f"{field}.fw_min_leg_m must be a finite positive number")
        if airframe != "delta_tailsitter_gimbal_cam":
            raise ValueError(f"{field}.fw_min_leg_m requires delta_tailsitter_gimbal_cam")
        options["fw_min_leg_m"] = float(min_leg)
    return options


def read_episode_metadata(episode_paths):
    if len(episode_paths) == 1:
        try:
            episode = json.loads(episode_paths[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(episode, dict):
            return episode
    return {}


def runtime_evidence(spec, episode):
    identity = episode.get("simulator_container")
    if not isinstance(identity, dict):
        identity = {}
    return {
        "image": identity.get("requested_image") or spec.get("image"),
        "image_id": identity.get("image_id"),
        "bridge_stabilize": spec.get("gimbal_bridge_stabilize", False),
        "bridge_stabilize_check": identity.get("bridge_stabilize_check"),
    }
