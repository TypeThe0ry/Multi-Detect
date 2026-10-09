"""Verify an untrained history scaffold against the actual selected parent ONNX.

Random contract vectors and typed software observations are diagnostics, not
training data, flight demonstrations, or performance evidence.  Only the zero
residual migration is eligible for this check; a trained model needs independent
episode evaluation and history ablations instead of equality with its parent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.contracts import ContextSnapshot, GeoPoint  # noqa: E402
from multidetect.autonomy.onboard_agent import (  # noqa: E402
    OnboardEventKind,
    OnboardModelInput,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from multidetect.autonomy.onboard_history import (  # noqa: E402
    OnboardHistoryError,
    OnboardInferenceScope,
)
from multidetect.autonomy.onboard_history_onnx import (  # noqa: E402
    OnboardHistoryOnnxModelRunner,
)
from multidetect.autonomy.onboard_model_reference import (  # noqa: E402
    load_onboard_model_reference,
)
from multidetect.autonomy.onboard_onnx import OnboardOnnxModelRunner  # noqa: E402
from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402


def identity(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.resolve().relative_to(ROOT).as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def validate_scaffold_metadata(
    metadata: dict[str, str], training: dict, parent_sha256: str
) -> None:
    """Bind the actual graph provenance to the report, not merely hash syntax."""
    if (
        metadata.get("parent_model_sha256") != parent_sha256
        or metadata.get("parent_model_sha256") != training.get("parent_model_sha256")
        or metadata.get("temporal_training_performed") != "false"
        or metadata.get("candidate_promoted") != "false"
        or metadata.get("history_steps") != str(training.get("history_steps"))
    ):
        raise ValueError("Scaffold ONNX metadata does not match its untrained migration report")


def verify_scaffold(candidate_directory: Path, parent_reference: Path) -> dict:
    import numpy as np
    import onnxruntime as ort

    candidate = candidate_directory.resolve()
    model = candidate / "onboard_history_policy.onnx"
    report = candidate / "training-report.json"
    before = [identity(path) for path in (model, report, parent_reference)]
    training = strict_json_loads(report.read_bytes(), name="history scaffold report")
    if (
        training.get("method") != "parent_weight_migration_only"
        or training.get("temporal_training_performed") is not False
        or training.get("epochs") != 0
        or training.get("candidate_promoted") is not False
    ):
        raise ValueError("Migration equality verification requires an untrained scaffold")
    parent = load_onboard_model_reference(parent_reference, repository_root=ROOT)
    if (
        training.get("parent_model_sha256") != parent.model_sha256
        or training.get("model_sha256") != before[0]["sha256"]
    ):
        raise ValueError("Scaffold model or parent identity mismatch")
    parent_before = identity(parent.model_path)
    original = ort.InferenceSession(str(parent.model_path), providers=["CPUExecutionProvider"])
    extended = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    validate_scaffold_metadata(
        extended.get_modelmeta().custom_metadata_map, training, parent.model_sha256
    )
    runner = OnboardHistoryOnnxModelRunner(extended)
    legacy = OnboardOnnxModelRunner(original)
    rng = np.random.default_rng(20261017)
    largest_error = 0.0
    probes = 0
    for count in range(1, 9):
        for _ in range(16):
            arrays = {
                "features": rng.uniform(-1, 1, (1, 8, 40)).astype(np.float32),
                "valid_mask": np.asarray([[0] * (8 - count) + [1] * count], dtype=np.float32),
                "dt_s": rng.uniform(0.01, 5, (1, 8)).astype(np.float32),
                "reset_mask": np.zeros((1, 8), dtype=np.float32),
            }
            arrays["dt_s"][0, 8 - count] = 0
            arrays["reset_mask"][0, 8 - count] = 1
            expected = original.run(["policy_output"], {"features": arrays["features"][:, -1]})[0]
            actual = extended.run(["policy_output"], arrays)[0]
            error = float(np.max(np.abs(expected - actual)))
            largest_error = max(largest_error, error)
            if not np.isfinite(actual).all() or not np.allclose(
                actual, expected, atol=1e-5, rtol=1e-4
            ):
                raise ValueError("History scaffold no longer preserves raw parent logits")
            probes += 1

    home = GeoPoint(1.3, 103.8)
    context = ContextSnapshot(
        "history-diagnostic-context",
        100.0,
        "2026-10-07T00:00:00Z",
        home,
        home,
        "diagnostic-profile-not-flight",
        90.0,
        1800.0,
        0.0,
        0.0,
        0.0,
        0.0,
        3,
        1.0,
        True,
        True,
        True,
        True,
        True,
        airframe_profile_hash="b" * 64,
    )
    scope = OnboardInferenceScope(
        "44444444-4444-4444-8444-444444444444",
        "a" * 64,
        "diagnostic-aircraft",
        "diagnostic-task",
        context.airframe_profile_id,
        "b" * 64,
        "multirotor",
        home.canonical_hash,
        "diagnostic-clock-epoch",
    )
    current = None
    for index in range(12):
        at_s = 100.0 + index * 0.2
        sample_context = replace(context, snapshot_id=f"diagnostic-{index}", captured_at_s=at_s)
        event = OnboardRuntimeEvent(
            f"diagnostic-event-{index}",
            OnboardEventKind.TARGET_LOST,
            at_s,
            task_phase=OnboardTaskAction.SEARCH,
        )
        current = OnboardModelInput(sample_context, event)
        actual = runner.infer_scoped(current, scope)
        expected = legacy.infer(current)
        if (actual.response, actual.task_action, actual.reason, actual.flight_plan.action) != (
            expected.response,
            expected.task_action,
            expected.reason,
            expected.flight_plan.action,
        ) or not np.allclose(
            [
                actual.confidence,
                actual.flight_plan.north_offset_m,
                actual.flight_plan.east_offset_m,
                actual.flight_plan.altitude_m,
                actual.flight_plan.speed_mps,
            ],
            [
                expected.confidence,
                expected.flight_plan.north_offset_m,
                expected.flight_plan.east_offset_m,
                expected.flight_plan.altitude_m,
                expected.flight_plan.speed_mps,
            ],
            rtol=1e-5,
            atol=1e-3,
        ):
            raise ValueError("History runner changed the shared decoded parent proposal")
    before_retry = len(runner.history.entries)
    runner.infer_scoped(current, scope)
    if len(runner.history.entries) != before_retry or before_retry != 8:
        raise ValueError("History retry advanced the bounded window")
    rejected = {}
    for name, bad_scope in (
        ("prompt", replace(scope, prompt_sha256="c" * 64)),
        ("task", replace(scope, selected_task_id="another-task")),
        ("aircraft", replace(scope, aircraft_id="another-aircraft")),
        ("clock_epoch", replace(scope, clock_epoch="another-epoch")),
    ):
        try:
            runner.infer_scoped(current, bad_scope)
        except OnboardHistoryError:
            rejected[name] = True
        else:
            raise ValueError(f"History scope change was not rejected: {name}")
    runner.reset_history(reason="diagnostic_explicit_new_task", scope=scope)
    runner.infer_scoped(current, scope)
    if len(runner.history.entries) != 1 or not runner.history.entries[0].reset:
        raise ValueError("Explicit reset did not discard previous history")
    if before != [identity(path) for path in (model, report, parent_reference)]:
        raise ValueError("Scaffold inputs changed during verification")
    if parent_before != identity(parent.model_path):
        raise ValueError("Parent ONNX changed during verification")
    return {
        "schema_version": 1,
        "contract": "multidetect.onboard-history-migration-verification-v1",
        "evidence_type": "actual_cpu_onnx_runtime_contract_diagnostics_not_flight_or_learning",
        "observed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inputs": before + [parent_before],
        "onnx_metadata_matches_untrained_report_and_actual_parent": True,
        "raw_parent_parity_probes": probes,
        "maximum_absolute_logit_error": largest_error,
        "typed_scoped_inference_observations": 12,
        "maximum_retained_history_entries": 8,
        "idempotent_retry_did_not_advance_history": True,
        "scope_changes_rejected": rejected,
        "explicit_reset_discarded_history": True,
        "shared_decoded_parent_output_preserved": True,
        "decoded_numeric_tolerance": {"rtol": 1e-5, "atol": 1e-3},
        "onnxruntime_version": ort.__version__,
        "execution_provider": "CPUExecutionProvider",
        "passed": True,
        "temporal_training_performed": False,
        "history_learning_verified": False,
        "flight_action_labels_created": False,
        "current_model_reference_changed": False,
        "candidate_promoted": False,
        "gazebo_runtime_reexecuted": False,
        "autonomous_flight_verified": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "jetson_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-directory", type=Path, required=True)
    parser.add_argument("--parent-reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite verification evidence")
    result = verify_scaffold(args.candidate_directory, args.parent_reference)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
