"""Image source contract and runtime defaults; these are distinct identities."""

from __future__ import annotations

import os

# Asset contracts bind the original model/airframe source, not the chosen runtime.
PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:"
    "bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)

# Versioned lookup reference, not a machine-specific Docker image/config ID.
# Actual launch must verify the full rootfs/config contract and freeze its local ID.
DEFAULT_GPU_IMAGE = "multidetect/px4-sitl-gazebo:gz-rendering-830-r1"


def resolve_runtime_image(image: str | None, *, gpu_render: bool) -> str:
    """Explicit API/CLI > environment > actual GPU/CPU runtime default.

    Call after resolving GPU selection (including an explicit GPU device).
    CPU remains on the original image until separately validated.
    """
    if image is not None:
        return image
    default = DEFAULT_GPU_IMAGE if gpu_render else PINNED_IMAGE
    return os.environ.get("MULTIDETECT_SIM_IMAGE", default)


__all__ = ["PINNED_IMAGE", "DEFAULT_GPU_IMAGE", "resolve_runtime_image"]
