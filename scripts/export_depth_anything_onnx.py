"""Export the repository's Depth Anything metric checkpoint to ONNX.

The runtime consumes ONNX/TensorRT artifacts, while the checked-in outdoor
checkpoint is a Transformers ``safetensors`` directory.  This tool performs a
local, reproducible conversion and writes a provenance document next to the
export; it never changes the source checkpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_checkpoint(
    source: Path,
    output: Path,
    *,
    input_size: int,
    opset: int,
) -> dict[str, object]:
    if input_size <= 0 or input_size % 14:
        raise ValueError("input size must be positive and divisible by 14")
    if opset < 17:
        raise ValueError("opset must be at least 17")
    source = source.resolve(strict=True)
    if not (source / "config.json").is_file() or not (source / "model.safetensors").is_file():
        raise FileNotFoundError("source must contain config.json and model.safetensors")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    import torch
    from transformers import DepthAnythingForDepthEstimation

    model = DepthAnythingForDepthEstimation.from_pretrained(
        source,
        local_files_only=True,
    ).eval()
    example = torch.zeros((1, 3, input_size, input_size), dtype=torch.float32)
    with torch.no_grad():
        torch.onnx.export(
            model,
            (example,),
            output,
            input_names=["pixel_values"],
            output_names=["predicted_depth"],
            opset_version=opset,
            do_constant_folding=True,
            dynamo=False,
        )
    provenance = {
        "schema": "multidetect.depth-anything-onnx-export/v1",
        "source_directory": str(source),
        "source_config_sha256": _sha256(source / "config.json"),
        "source_checkpoint_sha256": _sha256(source / "model.safetensors"),
        "output_path": str(output),
        "output_sha256": _sha256(output),
        "output_bytes": output.stat().st_size,
        "input_shape": [1, 3, input_size, input_size],
        "opset": opset,
        "framework": "transformers.DepthAnythingForDepthEstimation",
        "model_max_depth_m": float(model.config.max_depth),
        "source_checkpoint_unchanged": True,
        "hardware_acceptance": "not_claimed",
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    provenance_path = output.with_suffix(output.suffix + ".provenance.json")
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--input-size", type=int, default=518)
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()
    print(
        json.dumps(
            export_checkpoint(
                args.source,
                args.output,
                input_size=args.input_size,
                opset=args.opset,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
