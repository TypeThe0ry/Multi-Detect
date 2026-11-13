"""Verify a portable content contract, then launch by the observed immutable ID.

A Docker image ID may change across storage backends. Ordered uncompressed
rootfs layer digests plus the entire runtime Config and platform bind content
without trusting a tag, a locally generated receipt, or a caller-supplied ID.
No pull, build, tag, container creation or fallback is performed here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from multidetect.simulator.px4_image import DEFAULT_GPU_IMAGE

CONTRACT = Path(__file__).resolve().parents[3] / "simulation/gz-transport/runtime-image-contract.json"


def load_contract() -> dict[str, Any]:
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1 or doc.get("default_reference") != DEFAULT_GPU_IMAGE:
        raise ValueError("GPU runtime content contract/reference mismatch")
    return doc


def verify_gpu_runtime_image(
    reference: str = DEFAULT_GPU_IMAGE, *, docker: str = "docker",
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Read-only verification; explicit invocation also verifies an imported ID.

    RootFS/Config validation covers all files and runtime settings, rather than
    accepting two correct binaries inside an otherwise modified image.
    """
    contract = load_contract()
    run = subprocess.run if runner is None else runner
    try:
        result = run([docker, "image", "inspect", reference], capture_output=True,
                     text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("GPU runtime inspect failed; prepare/import the pinned runtime before launch") from exc
    if result.returncode != 0:
        raise ValueError("GPU runtime unavailable; build/import and verify the pinned runtime; no fallback or automatic pull")
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("Expected one image inspect record")
        image = rows[0]
        actual = image.get("Id")
        if not isinstance(actual, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", actual) is None:
            raise ValueError("Image inspect did not provide a complete immutable ID")
        if {k: image.get(k) for k in contract["platform"]} != contract["platform"]:
            raise ValueError("GPU runtime platform mismatch")
        if image.get("RootFS") != contract["rootfs"]:
            raise ValueError("GPU runtime rootfs/layer content mismatch")
        if image.get("Config") != contract["config"]:
            raise ValueError("GPU runtime configuration mismatch")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Invalid GPU image inspect response") from exc
    return {"status": "content_verified", "requested_image": reference,
            "image_id": actual, "identity_source": "docker_image_inspect",
            "contract_sha256": hashlib.sha256(CONTRACT.read_bytes()).hexdigest(),
            "components": contract["components"],
            "flight_acceptance": "separate; content verification does not establish flight acceptance"}
