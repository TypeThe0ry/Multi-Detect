"""Altair v1 LoRA training: a thin wrapper over scripts/train_slow_system_lora.py.

It reuses the audited trainer (row admission, resume, OOM retry) and only swaps
the LoRA target set and defaults. GPU work happens only when this is executed;
schedule it through the compute thread. Extra flags pass straight through, and
a later flag overrides these defaults.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LAYER = r"model\.language_model\.layers\.\d+\."
PRESETS = {
    # A reproduces the r24 recipe; B adds k/o; C (Altair v1 mainline) adds the MLP.
    "A": (re.compile(LAYER + r"self_attn\.(q_proj|v_proj)"), 16, 32),
    "B": (re.compile(LAYER + r"self_attn\.(q_proj|k_proj|v_proj|o_proj)"), 16, 32),
    "C": (
        re.compile(
            LAYER + r"(self_attn\.(q_proj|k_proj|v_proj|o_proj)|mlp\.(gate_proj|up_proj|down_proj))"
        ),
        32,
        64,
    ),
}
BASE_DEFAULTS = ["--epochs", "2", "--accumulation", "16", "--learning-rate", "1e-4",
                 "--max-side", "448", "--quantization", "bf16"]


def build_argv(argv: list[str]) -> tuple[str, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--preset", choices=sorted(PRESETS), default="C")
    known, rest = parser.parse_known_args(argv)
    _, rank, alpha = PRESETS[known.preset]
    return known.preset, [*BASE_DEFAULTS, "--rank", str(rank), "--alpha", str(alpha), *rest]


def output_dir(forwarded: list[str]) -> Path | None:
    for i in range(len(forwarded) - 1, -1, -1):
        if forwarded[i] == "--output" and i + 1 < len(forwarded):
            return Path(forwarded[i + 1])
    return None


def lora_b_mean_abs(tensors: dict) -> float | None:
    values = [float(t.abs().float().mean()) for k, t in tensors.items() if "lora_B" in k]
    return sum(values) / len(values) if values else None


def backfill_lora_stat(output: Path, load=None) -> float | None:
    """The base trainer reports lora_b_mean_abs only for completed runs; fill it in
    from the latest checkpoint when --max-steps truncated training."""
    report_path = output / "training-report.json"
    if not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("lora_b_mean_abs") is not None:
        return report["lora_b_mean_abs"]
    weights = next(
        (p for p in (output / "adapter/adapter_model.safetensors",
                     output / "checkpoint/adapter_model.safetensors") if p.is_file()),
        None,
    )
    if weights is None:
        return None
    if load is None:
        from safetensors.torch import load_file as load
    value = lora_b_mean_abs(load(str(weights)))
    report["lora_b_mean_abs"] = value
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    return value


def main(argv=None) -> int:
    preset, forwarded = build_argv(list(sys.argv[1:] if argv is None else argv))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts import train_slow_system_lora as trainer

    trainer.LANGUAGE_PROJECTION = PRESETS[preset][0]
    print(f"altair preset {preset}: {trainer.LANGUAGE_PROJECTION.pattern}", file=sys.stderr)
    code = trainer.main(forwarded)
    out = output_dir(forwarded)
    if code == 0 and out is not None:
        print(f"lora_b_mean_abs={backfill_lora_stat(out)}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
