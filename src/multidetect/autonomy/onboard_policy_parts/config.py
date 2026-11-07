"""Onboard policy configuration defaults."""

from pathlib import Path

from multidetect.host_paths import executable_name

ROOT = Path(__file__).resolve().parents[4]


def llama_server_path(root: Path = ROOT, *, platform: str | None = None) -> Path:
    return root / "artifacts/cache/llama-cpp/b11552" / executable_name(
        "llama-server", platform=platform
    )

DEFAULTS = {
    "airframe": "x500_mono_cam",
    "yoloe_weights": ROOT / "artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt",
    "gguf": ROOT
    / "artifacts/models/slow-system-lora-v2-r18-multi-merged-gguf"
    / "slow-system-v2r18multi-merged-Q5_K_M.gguf",
    "mmproj": ROOT
    / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
    "llama_server": llama_server_path(),
    # Leave batch sizing to the selected llama.cpp build unless a measured
    # hardware profile explicitly configures these optional limits.
    "llama_batch_size": None,
    "llama_ubatch_size": None,
    "near_depth": False,
    "near_depth_model_path": ROOT
    / "artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx",
    "near_depth_backend": "onnx-cpu",
    "near_depth_max_range_m": 10.0,
    "near_depth_min_interval_s": 0.2,
    "near_depth_max_age_s": 1.0,
    "near_depth_sigma_scale": 1.0,
    # Initial, uncalibrated identity-view independence gates.
    "confirm_min_view_bearing_deg": 25.0,
    "confirm_min_view_pitch_deg": 10.0,
    "confirm_min_view_altitude_m": 3.0,
}

# Target positioning defaults, kept typed (DEFAULTS above is stringified).
# Step triangulation is on by default since 2026-10-18: in closed-loop SIM with
# the ground believed 2 m high it cut the median position error 3.8 -> 2.0 m
# (8/8 successes, no false confirms either way) and matched the ground hit on
# flat ground. ground_height_sigma_m=5 is the honest prior without a DEM; set
# it lower only when the mission supplies a trusted ground model.
# docs/onboard-step-triangulation.md
POSITION_DEFAULTS = {
    "step_triangulation": True,
    "ground_height_sigma_m": 5.0,
}
