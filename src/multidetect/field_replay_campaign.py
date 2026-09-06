"""Resumable orchestration for deployment replay evidence campaigns.

The campaign advances only deterministic machine stages. Hardware collection,
frame annotation, accuracy review and threshold review remain explicit human
gates; their absence is reported as the next action instead of being inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_string_array,
    require_text,
    strict_json_loads,
    strict_object,
)
from .deployment_recording_attestation import verify_deployment_recording_attestation
from .replay_annotation_workbench import main as replay_annotation_workbench_main
from .replay_annotation_workspace import (
    create_replay_annotation_workspace,
    finalize_replay_annotation_workspace,
    inspect_replay_annotation_workspace,
    verify_finalized_replay_annotations,
)
from .replay_prediction_run import run_replay_predictions, verify_replay_predictions
from .replay_set import REPLAY_SET_STRATA
from .replay_set_calibration import (
    calibrate_replay_set_thresholds,
    verify_replay_set_calibration_report,
)
from .replay_set_evaluation import evaluate_replay_set
from .replay_set_intake import prepare_deployment_replay_set, verify_deployment_replay_intake
from .replay_set_readiness import check_replay_set_readiness

PLAN_NAME = "field-replay-campaign.json"
SCHEMA_VERSION = 1
_OUTPUTS = {
    "intake": "intake",
    "annotations": "annotations.json",
    "reviewed": "reviewed",
    "predicted": "predicted",
    "accuracy_report": "predicted/accuracy-report.json",
    "calibration_report": "predicted/threshold-calibration.json",
    "calibration_verification": "predicted/threshold-calibration-verification.json",
}


def create_field_replay_campaign(
    workspace: str | Path,
    *,
    campaign_id: str,
    dataset_id: str,
    dataset_version: str,
    sources: Mapping[str, str | Path],
    model_path: str | Path,
    model_manifest_path: str | Path,
    class_names: Sequence[str] = ("flame", "smoke"),
    model_format: str = "auto",
    providers: Sequence[str] = (),
    confidence_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    maximum_detections: int = 300,
    warmup_iterations: int = 1,
    minimum_precision: float = 0.70,
    minimum_recall: float = 0.70,
    accuracy_iou_threshold: float = 0.50,
    maximum_false_alarm_frame_rate: float = 0.10,
    maximum_missed_detection_frame_rate: float = 0.30,
    maximum_latency_p95_ms: float = 100.0,
    calibration_thresholds: Sequence[float] = (0.25, 0.40, 0.55, 0.70, 0.85),
    calibration_beta: float = 0.5,
    calibration_stability_tolerance: float = 0.10,
) -> dict[str, Any]:
    """Create one immutable campaign plan without claiming field collection."""

    root = Path(workspace).absolute()
    if root.exists() or root.is_symlink():
        raise AutonomyContractError(f"campaign workspace already exists: {root}")
    normalized_sources = _sources(sources)
    artifact = _file_binding(model_path, "model artifact")
    manifest = _file_binding(model_manifest_path, "model manifest")
    classes = _unique_strings(class_names, "class_names")
    provider_names = _unique_strings(providers, "providers", allow_empty=True)
    thresholds = _thresholds(calibration_thresholds)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": "field_replay_campaign_planned",
        "campaign_id": require_text(campaign_id, "campaign_id"),
        "dataset_id": require_text(dataset_id, "dataset_id"),
        "dataset_version": require_text(dataset_version, "dataset_version"),
        "class_names": list(classes),
        "sources": {name: str(path) for name, path in normalized_sources.items()},
        "model": {
            "artifact": artifact,
            "manifest": manifest,
            "format": require_text(model_format, "model_format"),
            "providers": list(provider_names),
            "confidence_threshold": _ratio(confidence_threshold, "confidence_threshold"),
            "iou_threshold": _ratio(iou_threshold, "iou_threshold"),
            "maximum_detections": require_int(
                maximum_detections, "maximum_detections", minimum=1
            ),
            "warmup_iterations": require_int(
                warmup_iterations, "warmup_iterations", minimum=0
            ),
        },
        "accuracy": {
            "iou_threshold": require_real(
                accuracy_iou_threshold,
                "accuracy_iou_threshold",
                minimum=0.000001,
                maximum=1.0,
            ),
            "minimum_precision": _ratio(minimum_precision, "minimum_precision"),
            "minimum_recall": _ratio(minimum_recall, "minimum_recall"),
            "maximum_false_alarm_frame_rate": _ratio(
                maximum_false_alarm_frame_rate, "maximum_false_alarm_frame_rate"
            ),
            "maximum_missed_detection_frame_rate": _ratio(
                maximum_missed_detection_frame_rate, "maximum_missed_detection_frame_rate"
            ),
            "maximum_latency_p95_ms": require_real(
                maximum_latency_p95_ms, "maximum_latency_p95_ms", minimum=0.0
            ),
        },
        "calibration": {
            "thresholds": list(thresholds),
            "beta": require_real(calibration_beta, "calibration_beta", minimum=0.000001),
            "minimum_recall": _ratio(minimum_recall, "minimum_recall"),
            "stability_tolerance": _ratio(
                calibration_stability_tolerance, "calibration_stability_tolerance"
            ),
        },
        "outputs": dict(_OUTPUTS),
        "claims": {
            "field_data_collected": False,
            "annotations_reviewed": False,
            "runtime_inference_executed": False,
            "accuracy_validated": False,
            "calibration_promotion_ready": False,
            "release_ready": False,
        },
    }
    document["canonical_hash"] = canonical_sha256(document)
    root.mkdir(parents=True)
    _write_json(root / PLAN_NAME, document)
    return inspect_field_replay_campaign(root)


def inspect_field_replay_campaign(workspace: str | Path) -> dict[str, Any]:
    """Reopen all completed stages and return one authoritative next action."""

    root, plan = _load_plan(workspace)
    paths = {name: root / relative for name, relative in plan["outputs"].items()}
    base = _status(plan, paths)

    intake = paths["intake"]
    if not intake.exists():
        missing = [
            str(path) for path in plan["sources"].values() if not Path(path).is_dir()
        ]
        if missing:
            return _finish(
                base,
                stage="awaiting_sources",
                blocker="six explicit field recording directories are required",
                missing=missing,
            )
        attestation_errors: list[str] = []
        for stratum in REPLAY_SET_STRATA:
            try:
                verify_deployment_recording_attestation(
                    plan["sources"][stratum], expected_stratum=stratum
                )
            except (AutonomyContractError, OSError) as exc:
                attestation_errors.append(f"{stratum}: {exc}")
        if attestation_errors:
            return _finish(
                base,
                stage="awaiting_source_attestations",
                blocker="all six physical field recording attestations are required",
                missing=attestation_errors,
            )
        return _finish(base, stage="intake_ready")
    base["artifacts"]["intake"] = verify_deployment_replay_intake(intake)

    annotations = paths["annotations"]
    if not annotations.is_file():
        return _finish(base, stage="annotation_workspace_ready")
    annotation_status = inspect_replay_annotation_workspace(intake, annotations)
    base["artifacts"]["annotations"] = annotation_status
    if not (
        annotation_status["finalization_ready"] is True
        and annotation_status["workspace_schema_version"] == 2
        and annotation_status["review_content_bound"] is True
    ):
        return _finish(
            base,
            stage="annotation_required",
            blocker="human annotation and an independent reviewer are required",
        )

    reviewed = paths["reviewed"]
    if not reviewed.is_dir():
        return _finish(base, stage="annotation_finalize_ready")
    base["artifacts"]["reviewed"] = verify_finalized_replay_annotations(reviewed)

    predicted = paths["predicted"]
    if not predicted.is_dir():
        return _finish(base, stage="prediction_ready")
    prediction_status = verify_replay_predictions(predicted)
    base["artifacts"]["predicted"] = prediction_status

    accuracy_path = paths["accuracy_report"]
    if not accuracy_path.is_file():
        return _finish(
            base,
            stage="accuracy_review_required",
            blocker="independent accuracy review identity is required",
        )
    readiness = check_replay_set_readiness(
        predicted / "replay-set.manifest.json",
        accuracy_report_path=accuracy_path,
    )
    base["artifacts"]["accuracy"] = readiness
    if readiness.get("gate_passed") is not True:
        return _finish(
            base,
            stage="accuracy_failed",
            blocker="accuracy report failed fresh replay-set readiness recomputation",
        )
    _verify_review_separation(paths, require_calibration=False)

    calibration_path = paths["calibration_report"]
    if not calibration_path.is_file():
        return _finish(
            base,
            stage="calibration_review_required",
            blocker="independent threshold review identity is required",
        )
    _verify_review_separation(paths, require_calibration=True)
    verification_path = paths["calibration_verification"]
    if not verification_path.is_file():
        return _finish(base, stage="calibration_verification_ready")
    verification = verify_replay_set_calibration_report(calibration_path)
    stored = _json_object(verification_path, "calibration verification")
    if stored != verification:
        raise AutonomyContractError(
            "calibration verification artifact differs from fresh verification"
        )
    base["artifacts"]["calibration"] = verification
    if verification.get("gate_passed") is not True:
        return _finish(
            base,
            stage="calibration_verification_failed",
            blocker="calibration verification did not pass",
        )
    if not (
        verification.get("report_provenance_complete") is True
        and verification.get("recomputed_provenance_complete") is True
        and verification.get("report_promotion_ready") is True
        and verification.get("recomputed_promotion_ready") is True
    ):
        return _finish(
            base,
            stage="calibration_not_release_ready",
            blocker="calibration provenance or promotion review is incomplete",
        )
    return _finish(base, stage="complete", complete=True)


def advance_field_replay_campaign(
    workspace: str | Path,
    *,
    accuracy_reviewer: str | None = None,
    calibration_reviewer: str | None = None,
) -> dict[str, Any]:
    """Advance every safe machine stage until a human or evidence gate is reached."""

    root, plan = _load_plan(workspace)
    advanced: list[str] = []
    for _ in range(8):
        status = inspect_field_replay_campaign(root)
        stage = status["stage"]
        paths = {name: root / relative for name, relative in plan["outputs"].items()}
        if stage == "intake_ready":
            prepare_deployment_replay_set(
                plan["sources"],
                paths["intake"],
                dataset_id=plan["dataset_id"],
                dataset_version=plan["dataset_version"],
            )
        elif stage == "annotation_workspace_ready":
            create_replay_annotation_workspace(
                paths["intake"], paths["annotations"], class_names=plan["class_names"]
            )
        elif stage == "annotation_finalize_ready":
            if accuracy_reviewer is not None or calibration_reviewer is not None:
                _verify_review_separation(
                    paths,
                    accuracy_reviewer=accuracy_reviewer,
                    calibration_reviewer=calibration_reviewer,
                    require_calibration=False,
                )
            finalize_replay_annotation_workspace(
                paths["intake"], paths["annotations"], paths["reviewed"]
            )
        elif stage == "prediction_ready":
            model = plan["model"]
            run_replay_predictions(
                paths["reviewed"],
                paths["predicted"],
                model_path=model["artifact"]["path"],
                model_manifest_path=model["manifest"]["path"],
                model_format=model["format"],
                confidence_threshold=model["confidence_threshold"],
                iou_threshold=model["iou_threshold"],
                maximum_detections=model["maximum_detections"],
                providers=model["providers"],
                warmup_iterations=model["warmup_iterations"],
            )
        elif stage == "accuracy_review_required" and accuracy_reviewer:
            _verify_review_separation(
                paths,
                accuracy_reviewer=accuracy_reviewer,
                require_calibration=False,
            )
            accuracy = plan["accuracy"]
            report = evaluate_replay_set(
                paths["predicted"] / "replay-set.manifest.json",
                iou_threshold=accuracy["iou_threshold"],
                confidence_threshold=plan["model"]["confidence_threshold"],
                minimum_precision=accuracy["minimum_precision"],
                minimum_recall=accuracy["minimum_recall"],
                maximum_false_alarm_frame_rate=accuracy[
                    "maximum_false_alarm_frame_rate"
                ],
                maximum_missed_detection_frame_rate=accuracy[
                    "maximum_missed_detection_frame_rate"
                ],
                maximum_latency_p95_ms=accuracy["maximum_latency_p95_ms"],
                reviewed=True,
                reviewer=accuracy_reviewer,
                prediction_run_receipt=paths["predicted"] / "prediction-run.receipt.json",
            )
            _write_json(paths["accuracy_report"], report)
        elif stage == "calibration_review_required" and calibration_reviewer:
            threshold_identity = require_text(calibration_reviewer, "calibration reviewer")
            _verify_review_separation(
                paths,
                calibration_reviewer=threshold_identity,
                require_calibration=False,
            )
            calibration = plan["calibration"]
            report = calibrate_replay_set_thresholds(
                paths["predicted"] / "replay-set.manifest.json",
                thresholds=calibration["thresholds"],
                iou_threshold=plan["accuracy"]["iou_threshold"],
                beta=calibration["beta"],
                minimum_recall=calibration["minimum_recall"],
                stability_tolerance=calibration["stability_tolerance"],
                reviewed=True,
                reviewer=threshold_identity,
                prediction_run_receipt=paths["predicted"] / "prediction-run.receipt.json",
                accuracy_report_path=paths["accuracy_report"],
            )
            _write_json(paths["calibration_report"], report)
        elif stage == "calibration_verification_ready":
            _write_json(
                paths["calibration_verification"],
                verify_replay_set_calibration_report(paths["calibration_report"]),
            )
        else:
            status["advanced_stages"] = advanced
            return status
        advanced.append(stage)
    raise AutonomyContractError("campaign advance exceeded the deterministic stage limit")


def field_replay_annotation_workbench_argv(
    workspace: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> list[str]:
    """Resolve a workbench launch only for the current annotation gate."""

    status = inspect_field_replay_campaign(workspace)
    if status["stage"] != "annotation_required":
        raise AutonomyContractError(
            "annotation workbench requires campaign stage annotation_required; "
            f"current stage is {status['stage']}"
        )
    argv = [
        status["paths"]["intake"],
        status["paths"]["annotations"],
        "--host",
        host,
        "--port",
        str(port),
    ]
    if open_browser:
        argv.append("--open-browser")
    return argv


def _load_plan(workspace: str | Path) -> tuple[Path, Mapping[str, Any]]:
    requested = Path(workspace)
    plan_path = requested / PLAN_NAME if requested.is_dir() else requested
    if plan_path.is_symlink():
        raise AutonomyContractError("campaign plan must not be a symbolic link")
    plan_path = plan_path.resolve(strict=True)
    root = plan_path.parent
    value = strict_object(
        _json_object(plan_path, "field replay campaign plan"),
        name="FieldReplayCampaignPlan",
        required=frozenset(
            {
                "schema_version",
                "event",
                "campaign_id",
                "dataset_id",
                "dataset_version",
                "class_names",
                "sources",
                "model",
                "accuracy",
                "calibration",
                "outputs",
                "claims",
                "canonical_hash",
            }
        ),
    )
    require_schema_version(value["schema_version"], expected=SCHEMA_VERSION)
    if value["event"] != "field_replay_campaign_planned":
        raise AutonomyContractError("campaign plan event is invalid")
    require_text(value["campaign_id"], "campaign_id")
    require_text(value["dataset_id"], "dataset_id")
    require_text(value["dataset_version"], "dataset_version")
    _unique_strings(require_string_array(value["class_names"], "class_names"), "class_names")
    _sources(value["sources"])
    outputs = strict_object(
        value["outputs"], name="outputs", required=frozenset(_OUTPUTS)
    )
    if dict(outputs) != _OUTPUTS:
        raise AutonomyContractError("campaign outputs must match the canonical workspace layout")
    claims = strict_object(
        value["claims"],
        name="claims",
        required=frozenset(
            {
                "field_data_collected",
                "annotations_reviewed",
                "runtime_inference_executed",
                "accuracy_validated",
                "calibration_promotion_ready",
                "release_ready",
            }
        ),
    )
    if any(item is not False for item in claims.values()):
        raise AutonomyContractError("campaign plan must not contain positive evidence claims")
    expected_hash = require_sha256(value["canonical_hash"], "canonical_hash")
    unhashed = dict(value)
    unhashed.pop("canonical_hash")
    if canonical_sha256(unhashed) != expected_hash:
        raise AutonomyContractError("campaign plan canonical_hash does not match")
    model = strict_object(
        value["model"],
        name="model",
        required=frozenset(
            {
                "artifact",
                "manifest",
                "format",
                "providers",
                "confidence_threshold",
                "iou_threshold",
                "maximum_detections",
                "warmup_iterations",
            }
        ),
    )
    _verify_file_binding(model["artifact"], "model artifact")
    _verify_file_binding(model["manifest"], "model manifest")
    if model["format"] not in {"auto", "post_nms_nx6", "ultralytics_raw"}:
        raise AutonomyContractError("model format is unsupported")
    _unique_strings(
        require_string_array(model["providers"], "providers"),
        "providers",
        allow_empty=True,
    )
    _ratio(model["confidence_threshold"], "model.confidence_threshold")
    require_real(
        model["iou_threshold"],
        "model.iou_threshold",
        minimum=0.000001,
        maximum=1.0,
    )
    require_int(model["maximum_detections"], "model.maximum_detections", minimum=1)
    require_int(model["warmup_iterations"], "model.warmup_iterations", minimum=0)
    accuracy = strict_object(
        value["accuracy"],
        name="accuracy",
        required=frozenset(
            {
                "iou_threshold",
                "minimum_precision",
                "minimum_recall",
                "maximum_false_alarm_frame_rate",
                "maximum_missed_detection_frame_rate",
                "maximum_latency_p95_ms",
            }
        ),
    )
    require_real(
        accuracy["iou_threshold"],
        "accuracy.iou_threshold",
        minimum=0.000001,
        maximum=1.0,
    )
    for field in (
        "minimum_precision",
        "minimum_recall",
        "maximum_false_alarm_frame_rate",
        "maximum_missed_detection_frame_rate",
    ):
        _ratio(accuracy[field], f"accuracy.{field}")
    require_real(
        accuracy["maximum_latency_p95_ms"],
        "accuracy.maximum_latency_p95_ms",
        minimum=0.0,
    )
    calibration = strict_object(
        value["calibration"],
        name="calibration",
        required=frozenset({"thresholds", "beta", "minimum_recall", "stability_tolerance"}),
    )
    if not isinstance(calibration["thresholds"], Sequence) or isinstance(
        calibration["thresholds"], (str, bytes, bytearray)
    ):
        raise AutonomyContractError("calibration.thresholds must be an array")
    _thresholds(calibration["thresholds"])
    require_real(calibration["beta"], "calibration.beta", minimum=0.000001)
    _ratio(calibration["minimum_recall"], "calibration.minimum_recall")
    _ratio(calibration["stability_tolerance"], "calibration.stability_tolerance")
    return root, value


def _status(plan: Mapping[str, Any], paths: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "event": "field_replay_campaign_inspected",
        "schema_version": SCHEMA_VERSION,
        "campaign_id": plan["campaign_id"],
        "dataset_id": plan["dataset_id"],
        "dataset_version": plan["dataset_version"],
        "plan_canonical_hash": plan["canonical_hash"],
        "stage": None,
        "complete": False,
        "blocked_on": None,
        "missing": [],
        "paths": {name: str(path) for name, path in paths.items()},
        "artifacts": {},
        "next_argv": [],
        "annotation_workbench_argv": [],
        "release_ready": False,
    }


def _finish(
    status: dict[str, Any],
    *,
    stage: str,
    blocker: str | None = None,
    missing: Sequence[str] = (),
    complete: bool = False,
) -> dict[str, Any]:
    status["stage"] = stage
    status["complete"] = complete
    status["blocked_on"] = blocker
    status["missing"] = list(missing)
    status["release_ready"] = complete
    status["next_argv"] = (
        []
        if complete
        else [
            "python",
            "scripts/run_field_replay_campaign.py",
            "advance",
            str(Path(status["paths"]["intake"]).parent),
        ]
    )
    if stage == "annotation_required":
        status["annotation_workbench_argv"] = [
            "python",
            "scripts/run_field_replay_campaign.py",
            "annotation-ui",
            str(Path(status["paths"]["intake"]).parent),
            "--open-browser",
        ]
        annotation = status["artifacts"].get("annotations", {})
        pending = annotation.get("next_unannotated")
        if pending is not None:
            status["next_argv"] = [
                "python",
                "scripts/replay_annotation_workspace.py",
                "annotate",
                status["paths"]["intake"],
                status["paths"]["annotations"],
                pending["frame_id"],
                "--annotator",
                "<annotator>",
            ]
        else:
            status["next_argv"] = [
                "python",
                "scripts/replay_annotation_workspace.py",
                "review",
                status["paths"]["intake"],
                status["paths"]["annotations"],
                "--reviewer",
                "<independent-reviewer>",
            ]
    elif stage == "accuracy_review_required":
        status["next_argv"].extend(["--accuracy-reviewer", "<independent-reviewer>"])
    elif stage == "calibration_review_required":
        status["next_argv"].extend(["--calibration-reviewer", "<independent-reviewer>"])
    return status


def _sources(value: Mapping[str, str | Path]) -> dict[str, Path]:
    if not isinstance(value, Mapping) or set(value) != set(REPLAY_SET_STRATA):
        raise AutonomyContractError("sources must contain exactly the canonical six strata")
    result = {
        name: Path(require_text(str(value[name]), f"sources.{name}")).absolute()
        for name in REPLAY_SET_STRATA
    }
    if len({str(path).casefold() for path in result.values()}) != len(result):
        raise AutonomyContractError("each stratum source path must be distinct")
    return result


def _file_binding(path: str | Path, name: str) -> dict[str, Any]:
    requested = Path(path)
    if requested.is_symlink():
        raise AutonomyContractError(f"{name} must not be a symbolic link")
    resolved = requested.resolve(strict=True)
    if not resolved.is_file():
        raise AutonomyContractError(f"{name} must be a regular file")
    payload = resolved.read_bytes()
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _verify_review_separation(
    paths: Mapping[str, Path],
    *,
    accuracy_reviewer: str | None = None,
    calibration_reviewer: str | None = None,
    require_calibration: bool,
) -> None:
    annotation = (
        _json_object(
            paths["reviewed"] / "annotation-review.receipt.json",
            "annotation review receipt",
        )
        if paths["reviewed"].is_dir()
        else _json_object(paths["annotations"], "annotation workspace")
    )
    identities = [
        require_text(annotation.get("annotator"), "annotator"),
        require_text(annotation.get("reviewer"), "annotation reviewer"),
    ]
    if accuracy_reviewer is None and paths["accuracy_report"].is_file():
        accuracy = _json_object(paths["accuracy_report"], "accuracy report")
        accuracy_reviewer = require_text(accuracy.get("reviewer"), "accuracy reviewer")
    if accuracy_reviewer is not None:
        identities.append(require_text(accuracy_reviewer, "accuracy reviewer"))
    if calibration_reviewer is None and require_calibration:
        calibration = _json_object(paths["calibration_report"], "calibration report")
        calibration_reviewer = require_text(
            calibration.get("reviewer"), "calibration reviewer"
        )
    if calibration_reviewer is not None:
        identities.append(require_text(calibration_reviewer, "calibration reviewer"))
    if len({identity.casefold() for identity in identities}) != len(identities):
        raise AutonomyContractError(
            "annotator, annotation reviewer, accuracy reviewer, and calibration reviewer "
            "must be four distinct people"
        )


def _verify_file_binding(value: object, name: str) -> None:
    binding = strict_object(
        value, name=name, required=frozenset({"path", "sha256", "bytes"})
    )
    path = Path(require_text(binding["path"], f"{name}.path"))
    if path.is_symlink():
        raise AutonomyContractError(f"{name} must not be a symbolic link")
    path = path.resolve(strict=True)
    payload = path.read_bytes()
    if len(payload) != require_int(binding["bytes"], f"{name}.bytes", minimum=1):
        raise AutonomyContractError(f"{name} byte count changed")
    if hashlib.sha256(payload).hexdigest() != require_sha256(
        binding["sha256"], f"{name}.sha256"
    ):
        raise AutonomyContractError(f"{name} SHA-256 changed")


def _unique_strings(
    values: Sequence[str], name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    normalized = tuple(require_text(value, name) for value in values)
    if not normalized and not allow_empty:
        raise AutonomyContractError(f"{name} must not be empty")
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise AutonomyContractError(f"{name} must be unique")
    return normalized


def _thresholds(values: Sequence[float]) -> tuple[float, ...]:
    normalized = tuple(_ratio(value, "calibration_threshold") for value in values)
    if (
        len(normalized) < 2
        or tuple(sorted(normalized)) != normalized
        or len(set(normalized)) != len(normalized)
    ):
        raise AutonomyContractError("calibration thresholds must be unique and increasing")
    return normalized


def _ratio(value: object, name: str) -> float:
    return require_real(value, name, minimum=0.0, maximum=1.0)


def _json_object(path: str | Path, name: str) -> Mapping[str, Any]:
    requested = Path(path)
    if requested.is_symlink():
        raise AutonomyContractError(f"{name} must not be a symbolic link")
    value = strict_json_loads(requested.read_bytes(), name=name)
    if not isinstance(value, Mapping):
        raise AutonomyContractError(f"{name} root must be an object")
    return value


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise AutonomyContractError(f"refusing to overwrite artifact: {path}")
    payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parse_sources(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise AutonomyContractError("--source must use STRATUM=PATH")
        name, path = value.split("=", 1)
        name = name.strip()
        if name in result:
            raise AutonomyContractError(f"duplicate source stratum: {name}")
        result[name] = require_text(path, f"source {name}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advance a fail-closed field replay campaign")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create a non-overwriting campaign plan")
    create.add_argument("workspace", type=Path)
    create.add_argument("--campaign-id", required=True)
    create.add_argument("--dataset-id", required=True)
    create.add_argument("--dataset-version", required=True)
    create.add_argument("--source", action="append", default=[])
    create.add_argument("--model", required=True, type=Path)
    create.add_argument("--model-manifest", required=True, type=Path)
    create.add_argument("--class-names", default="flame,smoke")
    create.add_argument("--model-format", default="auto")
    create.add_argument("--provider", action="append", default=[])
    create.add_argument("--confidence-threshold", type=float, default=0.25)
    create.add_argument("--nms-iou-threshold", type=float, default=0.45)
    create.add_argument("--maximum-detections", type=int, default=300)
    create.add_argument("--warmup-iterations", type=int, default=1)
    create.add_argument("--accuracy-iou-threshold", type=float, default=0.50)
    create.add_argument("--minimum-precision", type=float, default=0.70)
    create.add_argument("--minimum-recall", type=float, default=0.70)
    create.add_argument("--maximum-false-alarm-frame-rate", type=float, default=0.10)
    create.add_argument("--maximum-missed-detection-frame-rate", type=float, default=0.30)
    create.add_argument("--maximum-latency-p95-ms", type=float, default=100.0)
    create.add_argument("--calibration-thresholds", default="0.25,0.40,0.55,0.70,0.85")
    create.add_argument("--calibration-beta", type=float, default=0.5)
    create.add_argument("--calibration-stability-tolerance", type=float, default=0.10)
    status = subparsers.add_parser("status", help="revalidate every completed campaign stage")
    status.add_argument("workspace", type=Path)
    advance = subparsers.add_parser("advance", help="run machine stages until the next real gate")
    advance.add_argument("workspace", type=Path)
    advance.add_argument("--accuracy-reviewer")
    advance.add_argument("--calibration-reviewer")
    annotation_ui = subparsers.add_parser(
        "annotation-ui", help="open the loopback workbench for this campaign"
    )
    annotation_ui.add_argument("workspace", type=Path)
    annotation_ui.add_argument("--host", default="127.0.0.1")
    annotation_ui.add_argument("--port", type=int, default=8765)
    annotation_ui.add_argument("--open-browser", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            result = create_field_replay_campaign(
                args.workspace,
                campaign_id=args.campaign_id,
                dataset_id=args.dataset_id,
                dataset_version=args.dataset_version,
                sources=_parse_sources(args.source),
                model_path=args.model,
                model_manifest_path=args.model_manifest,
                class_names=args.class_names.split(","),
                model_format=args.model_format,
                providers=args.provider,
                confidence_threshold=args.confidence_threshold,
                iou_threshold=args.nms_iou_threshold,
                maximum_detections=args.maximum_detections,
                warmup_iterations=args.warmup_iterations,
                minimum_precision=args.minimum_precision,
                minimum_recall=args.minimum_recall,
                accuracy_iou_threshold=args.accuracy_iou_threshold,
                maximum_false_alarm_frame_rate=args.maximum_false_alarm_frame_rate,
                maximum_missed_detection_frame_rate=args.maximum_missed_detection_frame_rate,
                maximum_latency_p95_ms=args.maximum_latency_p95_ms,
                calibration_thresholds=tuple(
                    float(item) for item in args.calibration_thresholds.split(",")
                ),
                calibration_beta=args.calibration_beta,
                calibration_stability_tolerance=args.calibration_stability_tolerance,
            )
        elif args.command == "status":
            result = inspect_field_replay_campaign(args.workspace)
        elif args.command == "advance":
            result = advance_field_replay_campaign(
                args.workspace,
                accuracy_reviewer=args.accuracy_reviewer,
                calibration_reviewer=args.calibration_reviewer,
            )
        else:
            workbench_argv = field_replay_annotation_workbench_argv(
                args.workspace,
                host=args.host,
                port=args.port,
                open_browser=args.open_browser,
            )
            status = inspect_field_replay_campaign(args.workspace)
            print(
                json.dumps(
                    {
                        "event": "field_replay_annotation_workbench_authorized",
                        "campaign_id": status["campaign_id"],
                        "dataset_id": status["dataset_id"],
                        "dataset_version": status["dataset_version"],
                        "plan_canonical_hash": status["plan_canonical_hash"],
                        "stage": status["stage"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return replay_annotation_workbench_main(workbench_argv)
    except (AutonomyContractError, OSError, ValueError) as exc:
        print(f"field replay campaign: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("stage") in {
        "accuracy_failed",
        "calibration_verification_failed",
        "calibration_not_release_ready",
    } else 0


__all__ = [
    "PLAN_NAME",
    "SCHEMA_VERSION",
    "advance_field_replay_campaign",
    "build_parser",
    "create_field_replay_campaign",
    "field_replay_annotation_workbench_argv",
    "inspect_field_replay_campaign",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
