"""Onboard policy configuration defaults."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

DEFAULTS = {
    "yoloe_weights": ROOT / "artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt",
    "gguf": ROOT
    / "artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf"
    / "slow-system-v2r12multi-merged-Q5_K_M.gguf",
    "mmproj": ROOT
    / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
    "llama_server": ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe",
}
