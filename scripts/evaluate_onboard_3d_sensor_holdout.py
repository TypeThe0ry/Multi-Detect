# ruff: noqa: E402, I001
"""Evaluate the shared OnboardAI model on an independent 3-D sensor contract.

The holdout is generated from a different seed than training and uses the
repository-owned three-airframe Gazebo manifest.  It evaluates the same 40
structured features consumed by the ONNX model; it does not open Gazebo or
read target truth from a world file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.train_onboard_model import _gazebo_3d_transition_samples
from multidetect.autonomy import ONBOARD_MODEL_ACTION_ORDER, ONBOARD_MODEL_TASK_ACTION_ORDER
from multidetect.autonomy.onboard_onnx import _TASK_FLIGHT_COMPATIBILITY

DEFAULT_MANIFEST = ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(model_path: Path, manifest_path: Path, *, count: int, seed: int) -> dict[str, object]:
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - dependency-gated runtime path
        raise RuntimeError("onnxruntime is required for the 3-D sensor holdout") from exc

    rows = _gazebo_3d_transition_samples(count, seed, manifest_path)
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1:
        raise RuntimeError("OnboardAI ONNX model must have one input and one output")
    matrix = np.asarray([row.features for row in rows], dtype=np.float32)
    raw = np.asarray(
        [
            session.run([outputs[0].name], {inputs[0].name: row[None, :]})[0][0]
            for row in matrix
        ],
        dtype=np.float32,
    )
    if raw.ndim != 2 or raw.shape[1] < len(ONBOARD_MODEL_TASK_ACTION_ORDER) + len(
        ONBOARD_MODEL_ACTION_ORDER
    ):
        raise RuntimeError("OnboardAI output does not contain task and flight heads")
    task_end = len(ONBOARD_MODEL_TASK_ACTION_ORDER)
    flight_end = task_end + len(ONBOARD_MODEL_ACTION_ORDER)
    task_indices = raw[:, :task_end].argmax(axis=1)
    flight_indices = raw[:, task_end:flight_end].argmax(axis=1)
    expected_tasks = np.asarray([row.task_label for row in rows])
    expected_flights = np.asarray([row.flight_label for row in rows])
    compatible = np.asarray(
        [
            ONBOARD_MODEL_ACTION_ORDER[flight_index]
            in _TASK_FLIGHT_COMPATIBILITY[ONBOARD_MODEL_TASK_ACTION_ORDER[task_index]]
            for task_index, flight_index in zip(task_indices, flight_indices, strict=True)
        ],
        dtype=bool,
    )
    scenario_families = sorted({row.family.split("_", 4)[3] for row in rows})
    return {
        "schema_version": 1,
        "contract": "multidetect.onboard-policy-3d-sensor-holdout",
        "model_path": str(model_path),
        "model_sha256": _sha256(model_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "seed": seed,
        "case_count": len(rows),
        "scenario_families": scenario_families,
        "task_accuracy": float((task_indices == expected_tasks).mean()),
        "flight_accuracy": float((flight_indices == expected_flights).mean()),
        "compatible_output_rate": float(compatible.mean()),
        "training_used": False,
        "structured_sensor_contract": True,
        "truth_free": True,
        "pixel_level_rendered": False,
        "external_gazebo_runtime": False,
        "jetson_hardware_evidence": False,
        "pixhawk_hardware_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=900)
    parser.add_argument("--seed", type=int, default=20261009)
    args = parser.parse_args(argv)
    if args.count < 30:
        raise ValueError("count must be at least 30")
    report = evaluate(
        args.model.resolve(),
        args.manifest.resolve(),
        count=args.count,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
