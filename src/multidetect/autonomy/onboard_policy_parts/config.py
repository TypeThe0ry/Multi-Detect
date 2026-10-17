"""Onboard policy configuration defaults."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

DEFAULTS = {
    "airframe": "x500_mono_cam",
    "yoloe_weights": ROOT / "artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt",
    "gguf": ROOT
    / "artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf"
    / "slow-system-v2r12multi-merged-Q5_K_M.gguf",
    "mmproj": ROOT
    / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
    "llama_server": ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe",
    "near_depth": False,
    "near_depth_model_path": ROOT / "artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx",
    "near_depth_backend": "onnx-cpu",
    "near_depth_max_range_m": 10.0,
    "near_depth_min_interval_s": 0.2,
    "near_depth_max_age_s": 1.0,
    "near_depth_sigma_scale": 1.0,
}
