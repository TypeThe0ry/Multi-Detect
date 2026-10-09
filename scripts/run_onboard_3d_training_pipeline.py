"""Run the gated 3-D OnboardAI training pipeline.

This is the single entry point for the next real training round.  It does not
create synthetic RGB frames, read Gazebo truth, or promote a model.  The
pipeline always starts with the existing hash-bound OnboardAI reference and
the repository-owned neutral 3-D mainline.  A training invocation is allowed
only when both a pixel-level RGB/Depth dataset and a recording-bound reviewed
trajectory pass the strict gate in :mod:`train_onboard_model`.

Without ``--train`` the command is a read-only preflight and writes a receipt
that can be attached to a capture run.  With ``--train`` it invokes the
existing trainer as an incremental fine-tune from the selected parent model;
the resulting directory remains a quarantined candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_model_reference import (  # noqa: E402
    OnboardModelReference,
    load_onboard_model_reference,
)
from scripts.run_onboard_3d_training_preflight import (  # noqa: E402
    DEFAULT_DETECTOR_MODEL,
    DEFAULT_EVALUATION_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_REFERENCE,
    DEFAULT_RENDERED_DATASETS,
    DEFAULT_TRAJECTORY_DATASETS,
    build_preflight,
)
from scripts.run_onboard_3d_training_preflight import (  # noqa: E402
    DEFAULT_OUTPUT as DEFAULT_PREFLIGHT_OUTPUT,
)

CONTRACT = "multidetect.onboard-3d-training-pipeline-v1"
DEFAULT_OUTPUT = ROOT / "artifacts" / "evaluation" / "onboard-3d-training-pipeline-current.json"
DEFAULT_CANDIDATE = ROOT / "artifacts" / "models" / "onboard-policy-next-3d-candidate"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def _dataset_binding(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    binding: dict[str, Any] = {
        "path": _relative(resolved),
        "exists": resolved.is_file(),
    }
    if resolved.is_file():
        binding["sha256"] = _sha256(resolved)
        try:
            document = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            binding["json_error"] = str(exc)
        else:
            if isinstance(document, dict):
                binding.update(
                    {
                        "contract": document.get("contract"),
                        "training_ready": document.get("training_ready") is True,
                        "truth_free": document.get("truth_free") is True,
                        "truth_injected": document.get("truth_injected") is True,
                        "pixel_level_rendered": document.get("pixel_level_rendered") is True,
                        "materialized_sources": document.get("materialized_sources") is True,
                    }
                )
    return binding


def _base_receipt(
    *,
    reference_path: Path,
    reference: OnboardModelReference,
    manifest_path: Path,
    detector_path: Path,
    rendered_path: Path | None,
    trajectory_path: Path | None,
    preflight_path: Path,
    candidate_path: Path,
    train_requested: bool,
    device: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "contract": CONTRACT,
        "status": "preflight_only",
        "generated_by": "scripts/run_onboard_3d_training_pipeline.py",
        "train_requested": train_requested,
        "training_device_requested": device,
        "claim_boundary": {
            "candidate_promoted": False,
            "physical_performance_claimed": False,
            "external_gazebo_runtime": "not_claimed_by_pipeline",
            "jetson_hardware_evidence": False,
            "pixhawk_hardware_evidence": False,
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
        },
        "parent_reference": {
            "path": _relative(reference_path),
            "sha256": _sha256(reference_path),
            "model_id": reference.model_id,
            "model_version": reference.model_version,
            "model_path": _relative(reference.model_path),
            "model_sha256": reference.model_sha256,
            "same_parent_for_sim_and_real": True,
        },
        "gazebo_3d_manifest": {
            "path": _relative(manifest_path),
            "sha256": _sha256(manifest_path),
        },
        "detector_model": {
            "path": _relative(detector_path),
            "sha256": _sha256(detector_path) if detector_path.is_file() else None,
            "role": "existing_detector_boundary_shared_with_jetson_adapter",
        },
        "datasets": {
            "rendered_3d": _dataset_binding(rendered_path),
            "trajectory": _dataset_binding(trajectory_path),
        },
        "preflight_report": {"path": _relative(preflight_path)},
        "candidate_directory": _relative(candidate_path),
    }


def _strict_dataset_inputs(
    rendered_path: Path | None,
    trajectory_path: Path | None,
) -> list[str]:
    failures: list[str] = []
    if rendered_path is None:
        failures.append(
            "--train requires one --rendered-dataset from actual Gazebo RGB/Depth capture"
        )
    elif not rendered_path.is_file():
        failures.append(f"rendered dataset does not exist: {rendered_path}")
    if trajectory_path is None:
        failures.append(
            "--train requires one --trajectory-dataset with reviewed continuous episodes"
        )
    elif not trajectory_path.is_file():
        failures.append(f"trajectory dataset does not exist: {trajectory_path}")
    return failures


def _trainer_command(
    *,
    reference: OnboardModelReference,
    manifest_path: Path,
    rendered_path: Path,
    trajectory_path: Path,
    candidate_path: Path,
    model_id: str,
    model_version: str,
    samples: int,
    epochs: int,
    learning_rate: float,
    navigation_geometry_weight: float,
    device: str,
) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts" / "train_onboard_model.py"),
        "--output-directory",
        str(candidate_path),
        "--parent-model",
        str(reference.model_path),
        "--parent-sha256",
        reference.model_sha256,
        "--model-id",
        model_id,
        "--model-version",
        model_version,
        "--output-name",
        "onboard_policy.onnx",
        "--samples",
        str(samples),
        "--epochs",
        str(epochs),
        "--learning-rate",
        str(learning_rate),
        "--navigation-geometry-weight",
        str(navigation_geometry_weight),
        "--no-synthetic-bootstrap",
        "--device",
        device,
        "--gazebo-3d-manifest",
        str(manifest_path),
        "--rendered-3d-dataset",
        str(rendered_path),
        "--trajectory-dataset",
        str(trajectory_path),
        "--require-real-3d-capture",
    ]


def _read_output_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"training output is missing or linked: {path.name}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"training output is not an object: {path.name}")
    return document


def _verify_onnx_output(path: Path) -> dict[str, Any]:
    """Reopen the exported policy through the shared runtime, without flight access."""

    import numpy as np
    import onnx

    from multidetect.autonomy import ONBOARD_MODEL_FEATURE_NAMES
    from multidetect.autonomy.onboard_onnx import OnboardOnnxModelRunner

    model = onnx.load(path, load_external_data=False)
    onnx.checker.check_model(model)
    if any(tensor.data_location == onnx.TensorProto.EXTERNAL for tensor in model.graph.initializer):
        raise ValueError("candidate ONNX must not depend on external weights")
    runner = OnboardOnnxModelRunner.from_path(path, providers=("CPUExecutionProvider",))
    input_shape = list(runner.session.get_inputs()[0].shape)
    output_shape = list(runner.session.get_outputs()[0].shape)
    width = len(ONBOARD_MODEL_FEATURE_NAMES)
    expected_input_shape = [1, 40]
    expected_output_shape = [1, 20]
    if input_shape != expected_input_shape or output_shape != expected_output_shape:
        raise ValueError("candidate ONNX does not preserve the shared 40/20 contract")
    result = runner.session.run(
        [runner.output_name],
        {runner.input_name: np.zeros((1, width), dtype=np.float32)},
    )
    if (
        len(result) != 1
        or tuple(result[0].shape) != tuple(expected_output_shape)
        or not np.isfinite(result[0]).all()
    ):
        raise ValueError("candidate ONNX smoke output is invalid or non-finite")
    return {
        "input_shape": input_shape,
        "output_shape": output_shape,
        "expected_contract": {"input": expected_input_shape, "output": expected_output_shape},
        "onnx_checker_passed": True,
        "shared_runner_reopened": True,
        "inference_smoke_passed": True,
        "finite_smoke_output": True,
        "performance_acceptance": False,
    }


def _verify_candidate_outputs(
    *,
    candidate_path: Path,
    reference: OnboardModelReference,
    model_id: str,
    model_version: str,
    input_hashes: dict[Path, str],
    manifest_path: Path,
    rendered_path: Path,
    trajectory_path: Path,
) -> dict[str, Any]:
    """A zero process exit is insufficient: require hash-bound, reopenable outputs."""

    if candidate_path.is_symlink() or not candidate_path.is_dir():
        raise ValueError("candidate output directory is missing or linked")
    for path, expected_hash in input_hashes.items():
        if not path.is_file() or _sha256(path) != expected_hash:
            raise ValueError(f"training input changed during the run: {path.name}")
    config_path = candidate_path / "training-config.json"
    report_path = candidate_path / "training-report.json"
    config = _read_output_json(config_path)
    report = _read_output_json(report_path)
    if config.get("schema_version") != 1 or config.get("contract") != (
        "multidetect.onboard-policy-bootstrap-v1"
    ):
        raise ValueError("candidate training config contract is invalid")
    from multidetect.autonomy import (
        ONBOARD_MODEL_ACTION_ORDER,
        ONBOARD_MODEL_FEATURE_NAMES,
        ONBOARD_MODEL_TASK_ACTION_ORDER,
    )

    for field, expected in (
        ("feature_names", list(ONBOARD_MODEL_FEATURE_NAMES)),
        ("task_action_order", [item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER]),
        ("flight_action_order", [item.value for item in ONBOARD_MODEL_ACTION_ORDER]),
    ):
        if config.get(field) != expected:
            raise ValueError(f"candidate shared contract order mismatch: {field}")
    if any(report.get(key) != value for key, value in config.items()):
        raise ValueError("candidate training report disagrees with its config")
    if report.get("training_config_sha256") != _sha256(config_path):
        raise ValueError("candidate training config hash mismatch")
    if report.get("model_id") != model_id or report.get("model_version") != model_version:
        raise ValueError("candidate model identity mismatch")
    if report.get("parent_model_sha256") != reference.model_sha256 or report.get(
        "parent_source"
    ) != "hash_verified_existing_onnx_weights":
        raise ValueError("candidate did not inherit the selected OnboardAI parent")
    for field, path in (
        ("gazebo_3d_manifest", manifest_path),
        ("rendered_3d_dataset", rendered_path),
        ("trajectory_dataset", trajectory_path),
    ):
        binding = config.get(field)
        if not isinstance(binding, dict) or binding.get("sha256") != input_hashes[path]:
            raise ValueError(f"candidate {field} hash mismatch")
        raw_path = binding.get("path")
        if not isinstance(raw_path, str) or Path(raw_path).resolve() != path:
            raise ValueError(f"candidate {field} path mismatch")
    gate = config.get("real_3d_capture_gate")
    augmentations = config.get("augmentations")
    source_breakdown = config.get("source_breakdown")
    if (
        not isinstance(gate, dict)
        or gate.get("contract") != "multidetect.onboard-real-3d-capture-gate-v1"
        or any(
            gate.get(key) is not True
            for key in ("passed", "truth_free", "external_gazebo_runtime")
        )
        or not isinstance(augmentations, dict)
        or augmentations.get("require_real_3d_capture") is not True
        or any(
            config.get(key) is not True
            for key in (
                "rendered_rows_are_reviewed",
                "trajectory_rows_are_reviewed",
                "pixel_level_rendered",
            )
        )
        or config.get("synthetic_bootstrap") is not False
        or not isinstance(source_breakdown, dict)
        or source_breakdown.get("synthetic_rows_included") is not False
        or source_breakdown.get("synthetic_structured_scene_rows_present") is not False
    ):
        raise ValueError("candidate did not complete reviewed real 3-D training")
    for key in (
        "physical_performance_claimed", "jetson_evidence", "hil_real_evidence",
        "outdoor_gnss_evidence", "final_release_allowed",
    ):
        if config.get(key) is not False:
            raise ValueError(f"candidate claims unverified acceptance: {key}")
    split = config.get("split")
    metrics = report.get("metrics")
    if not isinstance(split, dict) or not isinstance(metrics, dict):
        raise ValueError("candidate training split/metrics are missing")
    for key in ("train", "evaluation", "test"):
        count = split.get(key)
        measured = metrics.get(key)
        if (
            isinstance(count, bool) or not isinstance(count, int) or count < 1
            or not isinstance(measured, dict) or measured.get("samples") != count
        ):
            raise ValueError(f"candidate has no verified {key} samples")
        for accuracy in ("task_accuracy", "flight_accuracy"):
            value = measured.get(accuracy)
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"candidate {key} {accuracy} is invalid")
    model_path = candidate_path / "onboard_policy.onnx"
    if model_path.is_symlink() or not model_path.is_file():
        raise ValueError("candidate ONNX output is missing or linked")
    onnx_outputs = sorted(
        path
        for path in candidate_path.rglob("*")
        if path.suffix.casefold() == ".onnx" and (path.is_file() or path.is_symlink())
    )
    if onnx_outputs != [model_path]:
        raise ValueError("candidate directory contains ambiguous ONNX outputs")
    raw_model_path = report.get("model_path")
    if not isinstance(raw_model_path, str) or Path(raw_model_path).resolve() != model_path:
        raise ValueError("candidate report refers to another model")
    model_sha256 = _sha256(model_path)
    model_bytes = model_path.stat().st_size
    if (
        report.get("model_sha256") != model_sha256
        or isinstance(report.get("model_bytes"), bool)
        or not isinstance(report.get("model_bytes"), int)
        or report.get("model_bytes") != model_bytes
        or model_bytes <= 0
    ):
        raise ValueError("candidate ONNX hash/size mismatch")
    io = _verify_onnx_output(model_path)
    if (
        io.get("onnx_checker_passed") is not True
        or io.get("inference_smoke_passed") is not True
        or io.get("input_shape") != [1, 40]
        or io.get("output_shape") != [1, 20]
    ):
        raise ValueError("candidate ONNX contract checker/inference smoke did not pass")
    return {
        "directory": _relative(candidate_path),
        "report": _relative(report_path),
        "report_sha256": _sha256(report_path),
        "config": _relative(config_path),
        "config_sha256": _sha256(config_path),
        "models": [{"path": _relative(model_path), "sha256": _sha256(model_path),
                    "bytes": model_path.stat().st_size}],
        "parent_model_sha256": reference.model_sha256,
        "output_verified": True,
        "onnx_verification": io,
        "split": split,
        "promoted": False,
    }


def run_pipeline(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    reference_path = args.reference.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    detector_path = args.detector_model.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    preflight_path = args.preflight_output.expanduser().resolve()
    candidate_path = args.candidate_directory.expanduser().resolve()
    rendered_path = args.rendered_dataset.expanduser().resolve() if args.rendered_dataset else None
    trajectory_path = (
        args.trajectory_dataset.expanduser().resolve() if args.trajectory_dataset else None
    )

    reference = load_onboard_model_reference(reference_path, repository_root=ROOT)
    preflight = build_preflight(
        reference_path=reference_path,
        manifest_path=manifest_path,
        detector_model_path=detector_path,
        rendered_datasets=(
            [rendered_path] if rendered_path else list(DEFAULT_RENDERED_DATASETS)
        ),
        trajectory_datasets=(
            [trajectory_path] if trajectory_path else list(DEFAULT_TRAJECTORY_DATASETS)
        ),
        evaluation_root=args.evaluation_root.expanduser().resolve(),
    )
    _write_json(preflight_path, preflight)

    receipt = _base_receipt(
        reference_path=reference_path,
        reference=reference,
        manifest_path=manifest_path,
        detector_path=detector_path,
        rendered_path=rendered_path,
        trajectory_path=trajectory_path,
        preflight_path=preflight_path,
        candidate_path=candidate_path,
        train_requested=args.train,
        device=args.device,
    )
    receipt["preflight"] = {
        "status": preflight["status"],
        "training_ready": preflight["training_ready"],
        "capture_eligible_count": preflight["capture_inventory"]["capture_eligible_count"],
        "reasons": preflight["reasons"],
    }

    if not args.train:
        receipt["next_action"] = (
            "provide recording-bound RGB/Depth/trajectory paths after the generated "
            "capture command completes"
        )
        _write_json(output_path, receipt)
        return 0, receipt

    failures = _strict_dataset_inputs(rendered_path, trajectory_path)
    if args.candidate_directory.is_symlink() or (
        candidate_path.exists() and (
            not candidate_path.is_dir() or any(candidate_path.iterdir())
        )
    ):
        failures.append("candidate output must be a new or empty non-linked directory")
    if failures:
        receipt["status"] = "blocked"
        receipt["failures"] = failures
        _write_json(output_path, receipt)
        return 2, receipt

    input_hashes = {
        path: _sha256(path)
        for path in (reference.model_path, manifest_path, rendered_path, trajectory_path)
    }
    command = _trainer_command(
        reference=reference,
        manifest_path=manifest_path,
        rendered_path=rendered_path,
        trajectory_path=trajectory_path,
        candidate_path=candidate_path,
        model_id=args.model_id,
        model_version=args.model_version,
        samples=args.samples,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        navigation_geometry_weight=args.navigation_geometry_weight,
        device=args.device,
    )
    receipt["trainer_command"] = [
        _relative(Path(item)) if item.endswith((".py", ".json", ".onnx")) else item
        for item in command
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    receipt["trainer_exit_code"] = completed.returncode
    receipt["trainer_stdout_tail"] = completed.stdout[-4000:]
    receipt["trainer_stderr_tail"] = completed.stderr[-4000:]
    if completed.returncode != 0:
        receipt["status"] = "blocked"
        receipt["failures"] = [
            "strict OnboardAI 3-D training command failed; candidate was not accepted"
        ]
        _write_json(output_path, receipt)
        return completed.returncode or 2, receipt

    try:
        candidate = _verify_candidate_outputs(
            candidate_path=candidate_path,
            reference=reference,
            model_id=args.model_id,
            model_version=args.model_version,
            input_hashes=input_hashes,
            manifest_path=manifest_path,
            rendered_path=rendered_path,
            trajectory_path=trajectory_path,
        )
        verification = candidate.get("onnx_verification")
        if (
            candidate.get("output_verified") is not True
            or not isinstance(verification, dict)
            or verification.get("onnx_checker_passed") is not True
            or verification.get("inference_smoke_passed") is not True
        ):
            raise ValueError("candidate output gate did not return complete ONNX verification")
    except Exception as exc:
        # The candidate directory is a trust boundary.  A checker/library-specific
        # exception (for example onnx.checker.ValidationError) must be recorded as
        # an invalid candidate instead of escaping and being mistaken for success.
        receipt["status"] = "candidate_output_invalid"
        receipt["failures"] = [str(exc)]
        receipt["candidate_promoted"] = False
        _write_json(output_path, receipt)
        return 2, receipt
    receipt["status"] = "candidate_trained"
    receipt["candidate"] = candidate
    _write_json(output_path, receipt)
    return 0, receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--detector-model", type=Path, default=DEFAULT_DETECTOR_MODEL)
    parser.add_argument("--evaluation-root", type=Path, default=DEFAULT_EVALUATION_ROOT)
    parser.add_argument("--preflight-output", type=Path, default=DEFAULT_PREFLIGHT_OUTPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--candidate-directory", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--rendered-dataset", type=Path)
    parser.add_argument("--trajectory-dataset", type=Path)
    parser.add_argument(
        "--train",
        action="store_true",
        help="run strict incremental fine-tuning when the 3-D gate passes",
    )
    parser.add_argument("--model-id", default="onboard-policy-next-3d-candidate")
    parser.add_argument("--model-version", default="0.3.0-pre.8-onboard-policy-next-3d-candidate")
    parser.add_argument("--samples", type=int, default=6000)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--learning-rate", type=float, default=0.001)
    parser.add_argument("--navigation-geometry-weight", type=float, default=0.15)
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help=(
            "training device for the existing OnboardAI contract; auto selects CUDA "
            "when available and otherwise falls back to CPU"
        ),
    )
    args = parser.parse_args(argv)
    if args.samples < 300 or args.epochs < 1:
        parser.error("--samples must be >= 300 and --epochs must be positive")
    if args.learning_rate <= 0.0 or args.navigation_geometry_weight < 0.0:
        parser.error("learning rate must be positive and geometry weight must be non-negative")
    try:
        code, receipt = run_pipeline(args)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        receipt = {"schema": 1, "contract": CONTRACT, "status": "error", "error": str(exc)}
        _write_json(args.output.expanduser().resolve(), receipt)
        code = 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
