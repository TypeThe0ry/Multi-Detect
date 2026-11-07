from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_onboard_3d_training_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("require_current_onboard_model")


def _preflight_stub(**_kwargs: object) -> dict[str, object]:
    return {
        "status": "blocked",
        "training_ready": False,
        "capture_inventory": {"capture_eligible_count": 0},
        "reasons": ["Docker daemon is not reachable"],
    }


def _args(tmp_path: Path, *, train: bool) -> Namespace:
    return Namespace(
        reference=ROOT / "configs/training/onboard-policy-current-reference-v1.json",
        manifest=ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
        detector_model=ROOT / "artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.onnx",
        evaluation_root=tmp_path,
        preflight_output=tmp_path / "preflight.json",
        output=tmp_path / "pipeline.json",
        candidate_directory=tmp_path / "candidate",
        rendered_dataset=None,
        trajectory_dataset=None,
        train=train,
        model_id="test-candidate",
        model_version="0.3.0-pre.8-test-candidate",
        samples=300,
        epochs=1,
        learning_rate=0.001,
        navigation_geometry_weight=0.15,
        device="auto",
    )


def test_pipeline_preflight_only_keeps_current_parent_and_claim_boundary(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(pipeline, "build_preflight", _preflight_stub)
    code, receipt = pipeline.run_pipeline(_args(tmp_path, train=False))

    assert code == 0
    assert receipt["status"] == "preflight_only"
    assert receipt["train_requested"] is False
    assert receipt["training_device_requested"] == "auto"
    assert receipt["parent_reference"]["model_id"] == "onboard-policy-r33-balanced-bearing-3d-dev"
    assert receipt["parent_reference"]["same_parent_for_sim_and_real"] is True
    assert receipt["claim_boundary"]["candidate_promoted"] is False
    assert json.loads((tmp_path / "pipeline.json").read_text(encoding="utf-8"))["status"] == (
        "preflight_only"
    )


def test_pipeline_train_fails_closed_without_pixel_and_trajectory_inputs(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(pipeline, "build_preflight", _preflight_stub)
    code, receipt = pipeline.run_pipeline(_args(tmp_path, train=True))

    assert code == 2
    assert receipt["status"] == "blocked"
    assert any("rendered-dataset" in item for item in receipt["failures"])
    assert any("trajectory-dataset" in item for item in receipt["failures"])
    assert not (tmp_path / "candidate").exists()


def test_trainer_command_forwards_explicit_cuda_device() -> None:
    reference = pipeline.load_onboard_model_reference(
        ROOT / "configs/training/onboard-policy-current-reference-v1.json",
        repository_root=ROOT,
    )
    command = pipeline._trainer_command(
        reference=reference,
        manifest_path=ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
        rendered_path=ROOT / "rendered.json",
        trajectory_path=ROOT / "trajectory.json",
        candidate_path=ROOT / "candidate",
        model_id="test-candidate",
        model_version="0.3.0-pre.8-test-candidate",
        samples=300,
        epochs=1,
        learning_rate=0.001,
        navigation_geometry_weight=0.15,
        device="cuda",
    )

    assert command[command.index("--device") + 1] == "cuda"
    assert "--require-real-3d-capture" in command
    assert "--no-synthetic-bootstrap" in command
    assert command[command.index("--output-name") + 1] == "onboard_policy.onnx"


def test_pipeline_train_rejects_nonempty_candidate_directory_before_subprocess(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(pipeline, "build_preflight", _preflight_stub)
    args = _args(tmp_path, train=True)
    args.rendered_dataset = tmp_path / "rendered.json"
    args.trajectory_dataset = tmp_path / "trajectory.json"
    args.rendered_dataset.write_text("{}", encoding="utf-8")
    args.trajectory_dataset.write_text("{}", encoding="utf-8")
    args.candidate_directory.mkdir()
    (args.candidate_directory / "stale.onnx").write_bytes(b"stale")

    code, receipt = pipeline.run_pipeline(args)

    assert code == 2
    assert receipt["status"] == "blocked"
    assert any("new or empty" in item for item in receipt["failures"])


def test_zero_trainer_exit_without_outputs_is_not_training_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pipeline, "build_preflight", _preflight_stub)
    args = _args(tmp_path, train=True)
    args.rendered_dataset = tmp_path / "rendered.json"
    args.trajectory_dataset = tmp_path / "trajectory.json"
    args.rendered_dataset.write_text("{}", encoding="utf-8")
    args.trajectory_dataset.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    code, receipt = pipeline.run_pipeline(args)

    assert code == 2
    assert receipt["status"] == "candidate_output_invalid"
    assert receipt["candidate_promoted"] is False
    assert "missing or linked" in receipt["failures"][0]


def _write_candidate_fixture(tmp_path: Path) -> tuple[Path, dict[Path, str], object]:
    """Write the smallest hash-bound candidate accepted by the output gate."""

    reference = pipeline.load_onboard_model_reference(
        ROOT / "configs/training/onboard-policy-current-reference-v1.json",
        repository_root=ROOT,
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    manifest = tmp_path / "manifest.json"
    rendered = tmp_path / "rendered.json"
    trajectory = tmp_path / "trajectory.json"
    for path in (manifest, rendered, trajectory):
        path.write_text("{}\n", encoding="utf-8")

    model = candidate / "onboard_policy.onnx"
    model.write_bytes(b"candidate-model")
    from multidetect.autonomy import (
        ONBOARD_MODEL_ACTION_ORDER,
        ONBOARD_MODEL_FEATURE_NAMES,
        ONBOARD_MODEL_TASK_ACTION_ORDER,
    )

    model_id = "test-candidate"
    model_version = "0.3.0-pre.8-test-candidate"
    input_hashes = {
        path: pipeline._sha256(path)
        for path in (reference.model_path, manifest, rendered, trajectory)
    }
    config = {
        "schema_version": 1,
        "contract": "multidetect.onboard-policy-bootstrap-v1",
        "feature_names": list(ONBOARD_MODEL_FEATURE_NAMES),
        "task_action_order": [item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER],
        "flight_action_order": [item.value for item in ONBOARD_MODEL_ACTION_ORDER],
        "model_id": model_id,
        "model_version": model_version,
        "parent_model_sha256": reference.model_sha256,
        "parent_source": "hash_verified_existing_onnx_weights",
        "gazebo_3d_manifest": {
            "path": pipeline._relative(manifest),
            "sha256": input_hashes[manifest],
        },
        "rendered_3d_dataset": {
            "path": pipeline._relative(rendered),
            "sha256": input_hashes[rendered],
        },
        "trajectory_dataset": {
            "path": pipeline._relative(trajectory),
            "sha256": input_hashes[trajectory],
        },
        "real_3d_capture_gate": {
            "contract": "multidetect.onboard-real-3d-capture-gate-v1",
            "passed": True,
            "truth_free": True,
            "external_gazebo_runtime": True,
        },
        "augmentations": {"require_real_3d_capture": True},
        "rendered_rows_are_reviewed": True,
        "trajectory_rows_are_reviewed": True,
        "pixel_level_rendered": True,
        "synthetic_bootstrap": False,
        "source_breakdown": {
            "synthetic_rows_included": False,
            "synthetic_structured_scene_rows_present": False,
        },
        "physical_performance_claimed": False,
        "jetson_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
        "split": {"train": 1, "evaluation": 1, "test": 1},
        "metrics": {
            key: {"samples": 1, "task_accuracy": 1.0, "flight_accuracy": 1.0}
            for key in ("train", "evaluation", "test")
        },
    }
    config_path = candidate / "training-config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    report = {
        **config,
        "model_path": str(model),
        "model_sha256": pipeline._sha256(model),
        "model_bytes": model.stat().st_size,
        "training_config_sha256": pipeline._sha256(config_path),
    }
    (candidate / "training-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return candidate, input_hashes, reference


def test_candidate_output_gate_rejects_an_extra_case_insensitive_onnx(
    monkeypatch, tmp_path
) -> None:
    candidate, input_hashes, reference = _write_candidate_fixture(tmp_path)
    (candidate / "unexpected.ONNX").write_bytes(b"another-model")
    monkeypatch.setattr(
        pipeline,
        "_verify_onnx_output",
        lambda _path: {
            "input_shape": [1, 40],
            "output_shape": [1, 20],
            "onnx_checker_passed": True,
            "inference_smoke_passed": True,
        },
    )

    with pytest.raises(ValueError, match="ambiguous ONNX"):
        pipeline._verify_candidate_outputs(
            candidate_path=candidate,
            reference=reference,
            model_id="test-candidate",
            model_version="0.3.0-pre.8-test-candidate",
            input_hashes=input_hashes,
            manifest_path=tmp_path / "manifest.json",
            rendered_path=tmp_path / "rendered.json",
            trajectory_path=tmp_path / "trajectory.json",
        )


def test_candidate_output_gate_rejects_report_model_bytes_or_hash_mismatch(
    monkeypatch, tmp_path
) -> None:
    candidate, input_hashes, reference = _write_candidate_fixture(tmp_path)
    report_path = candidate / "training-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["model_bytes"] += 1
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        pipeline,
        "_verify_onnx_output",
        lambda _path: {
            "input_shape": [1, 40],
            "output_shape": [1, 20],
            "onnx_checker_passed": True,
            "inference_smoke_passed": True,
        },
    )

    with pytest.raises(ValueError, match="ONNX hash/size mismatch"):
        pipeline._verify_candidate_outputs(
            candidate_path=candidate,
            reference=reference,
            model_id="test-candidate",
            model_version="0.3.0-pre.8-test-candidate",
            input_hashes=input_hashes,
            manifest_path=tmp_path / "manifest.json",
            rendered_path=tmp_path / "rendered.json",
            trajectory_path=tmp_path / "trajectory.json",
        )


def test_candidate_output_gate_requires_checker_and_inference_smoke_flags(
    monkeypatch, tmp_path
) -> None:
    candidate, input_hashes, reference = _write_candidate_fixture(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_verify_onnx_output",
        lambda _path: {"input_shape": [1, 40], "output_shape": [1, 20]},
    )

    with pytest.raises(ValueError, match="checker/inference smoke"):
        pipeline._verify_candidate_outputs(
            candidate_path=candidate,
            reference=reference,
            model_id="test-candidate",
            model_version="0.3.0-pre.8-test-candidate",
            input_hashes=input_hashes,
            manifest_path=tmp_path / "manifest.json",
            rendered_path=tmp_path / "rendered.json",
            trajectory_path=tmp_path / "trajectory.json",
        )
