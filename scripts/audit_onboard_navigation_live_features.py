"""SHA-bound R3 feature-byte CPU replay; no model, optimizer or accuracy claims.

Reads the eight same-call captures and locked head/TRAIN norm, plus one pass of
the sealed TRAIN208 feature matrix (not labels/images). No R2 pseudo-features,
optional ML imports or source-inventory/model-weight scans are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.navigation-live-feature-byte-cpu-audit-v1"
HELPER = (
    "scripts/audit_onboard_navigation_head_diagnostics.py",
    16658,
    "050db0abb7f6df40d406b7bd696e1c09bfa4dc3f76991fd8e6ff2fd8c2fe39ca",
)
CACHE_RECEIPT = {
    "path": (
        "artifacts/training/onboard-navigation-candidate208-features-20261008-r1/cache/receipt.json"
    ),
    "bytes": 4140,
    "sha256": "2775f0ac14abe02b0cd2997a452212dfb5123b14b49fafbd2f47186f0683c5c6",
}
HEAD = {
    "receipt": (28481, "0d31701b9bf50de1b2570df280f5e7ad3672cc1db52aa2525dea07bd4767ff2b"),
    "metadata": (44465, "94ed0c59cae551facf2c0731e68fae41c8c5bb02e5c7520c083582df313f3473"),
    "weights": (65832, "c73603f452b4073cf1f90d4b90c55127e569ba06fcc72d2fd2ba9e7e869b415d"),
}
FOUR = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _api():
    path = ROOT / HELPER[0]
    raw = path.read_bytes()
    if len(raw) != HELPER[1] or hashlib.sha256(raw).hexdigest() != HELPER[2]:
        raise ValueError("Frozen CPU diagnostic helper changed before import")
    spec = importlib.util.spec_from_file_location("_frozen_head_diagnostic", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(record, shape, dtype="torch.float32"):
    if (
        type(record) is not dict
        or set(record) != {"shape", "dtype", "sha256"}
        or record["dtype"] != dtype
        or record["shape"] != list(shape)
        or any(type(x) is not int or x < 1 for x in record["shape"])
        or type(record["sha256"]) is not str
        or len(record["sha256"]) != 64
        or any(x not in "0123456789abcdef" for x in record["sha256"])
    ):
        raise ValueError("Exact canonical tensor record required")


def _closed(plan, report, plan_binding):
    if (
        report.get("contract") != "multidetect.onboard-navigation-gazebo-feature-shadow-r3"
        or report.get("success") is not True
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 0
        or report.get("feature_capture_complete") is not True
        or type(report.get("feature_capture_complete_count")) is not int
        or report["feature_capture_complete_count"] != 8
        or report.get("source_bindings_unchanged") is not True
        or canonical(report.get("source_bindings_before"))
        != canonical(report.get("source_bindings_after"))
        or report.get("source_bindings_before") is None
        or report["plan"]["sha256"] != plan_binding["sha256"]
        or report["plan"]["bytes"] != plan_binding["bytes"]
    ):
        raise ValueError("Closed successful eight-feature R3 report required")
    if (
        plan["rule"]["model_variant"] != "candidate267_only"
        or plan["rule"]["readout_variant"] != "locked_final_all208"
        or type(plan["capture"]["sensor_frames"]) is not int
        or plan["capture"]["sensor_frames"] != 8
        or report.get("new_optimizer_attempts") != 0
        or type(report.get("new_optimizer_attempts")) is not int
        or report.get("new_generation_calls") != 0
        or type(report.get("new_generation_calls")) is not int
    ):
        raise ValueError("Fixed candidate/locked head and zero-training/generation required")
    loader = report["loader"]
    expected = plan["load_config"]["model"]["expected_parameter_sha256"]
    if (
        loader.get("closed_successfully") is not True
        or loader.get("source_bindings_unchanged") is not True
        or loader.get("parameter_identity_before") != expected
        or loader.get("parameter_identity_after") != expected
        or canonical(loader.get("head_state_before")) != canonical(loader.get("head_state_after"))
        or canonical(loader.get("runtime_origins_before"))
        != canonical(loader.get("runtime_origins_after"))
        or loader.get("runtime_origins_before") is None
    ):
        raise ValueError("Recorded loader model/head/runtime closure differs")
    observations = report["observations"]
    if type(observations) is not list or len(observations) != 8:
        raise ValueError("Exactly eight observations required")
    return observations


def _request_binding(receipt, row, plan_binding):
    request = receipt["request"]
    if (
        receipt["contract"] != "multidetect.navigation-same-call-feature-capture-v1"
        or receipt["experiment_plan_sha256"] != plan_binding["sha256"]
        or digest(request) != receipt["request_sha256"]
        or receipt["request_sha256"] != row["request_sha256"]
        or type(row["generation"]) is not int
        or request["generation"] != row["generation"]
        or receipt["actor_identity_sha256"] != request["actor_identity_sha256"]
        or receipt["actor_identity_sha256"] != row["actor_identity_sha256"]
        or receipt["source_binding_sha256"] != request["source_binding_sha256"]
        or receipt["source_binding_sha256"] != row["source_binding_sha256"]
        or receipt["scope_sha256"] != request["scope_sha256"]
        or receipt["frame_ids"] != [frame["frame_id"] for frame in request["source_frames"]]
        or type(row["accepted"]) is not bool
        or receipt["backend_gate_accepted"] is not row["accepted"]
        or receipt["capture_error"] is not None
        or receipt["unadmitted_partial_files"]
        or receipt.get("capture_failure_requires_worker_abort") is not False
        or receipt["predictor_returned"] is not True
        or receipt["original_head_call_attempted"] is not True
        or receipt["diagnostic_only"] is not True
    ):
        raise ValueError("Request/outcome capture identities differ or are incomplete")
    return request


def _one(inputs, receipt, row, plan, plan_binding, head_state, np):
    request = _request_binding(receipt, row, plan_binding)
    model, readout = plan["load_config"]["model"], plan["load_config"]["readout"]
    engine = {
        key: model[source]["sha256"]
        for key, source in (
            ("model_sha256", "base_weights"),
            ("adapter_sha256", "adapter_weights"),
            ("adapter_config_sha256", "adapter_config"),
        )
    }
    identity = {
        "head_receipt_sha256": readout["receipt"]["sha256"],
        "head_weights_sha256": readout["weights"]["sha256"],
        "head_metadata_sha256": readout["metadata"]["sha256"],
    }
    if (
        receipt["engine_identity"] != engine
        or receipt["readout_identity"] != identity
        or any(request[key] != value for key, value in engine.items())
        or canonical(receipt["head_and_train_norm_state"]) != canonical(head_state)
        or receipt["head_and_train_norm_state_sha256"] != digest(head_state)
    ):
        raise ValueError("Captured model/head/TRAIN normalization differs from locked bundle")
    metrics = row["metrics"]
    stripped = {key: value for key, value in metrics.items() if key != "live_feature_receipt"}
    if (
        digest(stripped) != receipt["backend_diagnostics_sha256"]
        or canonical(receipt["actual_prefix"]) != canonical(metrics["actual_prefix"])
        or set(receipt["actual_prefix"]["tensors"]) != FOUR
    ):
        raise ValueError("Backend diagnostic/prefix binding differs")
    for name, record in receipt["actual_prefix"]["tensors"].items():
        _record(
            record, record["shape"], "torch.float32" if name == "pixel_values" else "torch.int64"
        )
    feature, six = receipt["feature"], receipt["six_output"]
    _record(feature["tensor"], (1, 2048))
    _record(six["tensor"], (1, 6))
    raw = inputs.read(feature["file"])
    if (
        len(raw) != 8192
        or hashlib.sha256(raw).hexdigest() != feature["tensor"]["sha256"]
        or canonical(feature["tensor"]) != canonical(metrics["actual_feature"])
        or canonical(six["tensor"]) != canonical(metrics["actual_six_output"])
    ):
        raise ValueError("Actual feature/six tensor hashes differ")
    feature_array = np.frombuffer(raw, dtype="<f4").reshape(1, 2048)
    if not np.isfinite(feature_array).all():
        raise ValueError("Finite actual feature bytes required")
    six_file = inputs.json(six["file"])
    if (
        six_file["source"] != "actual_same_call_head_output"
        or canonical(six_file["values"]) != canonical(six["values"])
        or canonical(six_file["tensor"]) != canonical(six["tensor"])
    ):
        raise ValueError("Same-call six JSON differs from receipt")
    values = six["values"]
    if (
        type(values) is not list
        or len(values) != 1
        or type(values[0]) is not list
        or len(values[0]) != 6
        or any(type(x) not in (int, float) or not math.isfinite(x) for x in values[0])
    ):
        raise ValueError("Six actual finite numbers required")
    if hashlib.sha256(struct.pack("<6f", *values[0])).hexdigest() != six["tensor"]["sha256"]:
        raise ValueError("Same-call six JSON cannot reconstruct actual FP32 hash")
    prediction = receipt["raw_prediction"]
    if (
        canonical(prediction) != canonical(row["raw_prediction"])
        or digest(prediction) != receipt["raw_prediction_sha256"]
        or canonical(
            [
                prediction["raw_logits"]
                + [prediction["raw_distance_m"], prediction["raw_angle_magnitude_rad"]]
            ]
        )
        != canonical(values)
    ):
        raise ValueError("Durable raw differs from actual same-call six values")
    expected_decl = {
        "prefix_sha256": digest(receipt["actual_prefix"]["tensors"]),
        "base_weights_sha256": engine["model_sha256"],
        "adapter_weights_sha256": engine["adapter_sha256"],
        "head_weights_sha256": identity["head_weights_sha256"],
        "head_config_sha256": identity["head_metadata_sha256"],
    }
    if prediction["declared_bindings"] != expected_decl:
        raise ValueError("Raw source declarations differ")
    return feature_array, np.asarray(values, dtype=np.float32)


def audit(plan_binding, report_binding):
    api = _api()
    inputs = api.BoundInputs()
    inputs.read({"path": HELPER[0], "bytes": HELPER[1], "sha256": HELPER[2]})
    plan, report = inputs.json(plan_binding), inputs.json(report_binding)
    observations = _closed(plan, report, plan_binding)
    readout = plan["load_config"]["readout"]
    for key, (size, sha) in HEAD.items():
        if readout[key]["bytes"] != size or readout[key]["sha256"] != sha:
            raise ValueError("Only sealed final-all208 head is allowed")
    head_receipt, metadata = inputs.json(readout["receipt"]), inputs.json(readout["metadata"])
    if (
        head_receipt["metadata"] != readout["metadata"]
        or head_receipt["weights"] != readout["weights"]
    ):
        raise ValueError("Saved bundle receipt bindings differ")
    head_state = {
        "tensors": metadata["tensors"],
        "normalization_train_ids": metadata["normalization_train_ids"],
        "epsilon": metadata["epsilon"],
    }
    if canonical(report["loader"]["head_state_before"]) != canonical(head_state):
        raise ValueError("Recorded loaded head/TRAIN norm differs from saved bundle")
    import numpy as np

    if any(name in sys.modules for name in ("torch", "PIL", "transformers", "peft")):
        raise ValueError("CPU audit cannot import model or processor packages")
    tensors = api.bundle(inputs.read(readout["weights"]), metadata["tensors"], np)
    rows, normalized_rows = [], []
    largest = 0.0
    for generation, row in enumerate(observations, 1):
        if row["generation"] != generation:
            raise ValueError("Exactly eight ordered no-retry generations required")
        receipt = inputs.json(row["metrics"]["live_feature_receipt"])
        feature, actual = _one(inputs, receipt, row, plan, plan_binding, head_state, np)
        normalized, replayed = api.replay(feature, tensors, np)
        error = float(np.abs(replayed.astype(np.float64) - actual.astype(np.float64)).max())
        largest = max(largest, error)
        selected, observed = int(replayed[0, :4].argmax()), int(actual[0, :4].argmax())
        if (
            not np.allclose(replayed, actual, rtol=1e-5, atol=2e-5)
            or selected != observed
            or (row["raw_prediction"]["selected_class"] != api.CLASSES[observed])
        ):
            raise ValueError("Actual live feature cannot replay recorded same-call outputs")
        runner = int(replayed[0, :3].argmax())
        bias = float(tensors["bias"][3] - tensors["bias"][runner])
        feature_logits = normalized @ tensors["weight"].T
        contribution = float(feature_logits[0, 3] - feature_logits[0, runner])
        margin = float(replayed[0, 3] - replayed[0, runner])
        rows.append(
            {
                "generation": generation,
                "request_sha256": row["request_sha256"],
                "feature_file": receipt["feature"]["file"],
                "selected_class": api.CLASSES[selected],
                "backend_gate_accepted": row["accepted"],
                "replay_max_absolute_error": error,
                "feature_L2": float(np.linalg.norm(feature)),
                "normalized_feature_L2": float(np.linalg.norm(normalized)),
                "STOP_best_other_class": api.CLASSES[runner],
                "STOP_margin": margin,
                "STOP_bias_margin": bias,
                "STOP_feature_weight_margin": contribution,
                "margin_decomposition_absolute_error": abs(margin - bias - contribution),
            }
        )
        normalized_rows.append(normalized)
    cache_receipt = inputs.json(CACHE_RECEIPT)
    cache_meta = inputs.json(cache_receipt["metadata"])
    matrix = api.bundle(inputs.read(cache_receipt["weights"]), cache_meta["tensors"], np)[
        "features"
    ]
    if (
        matrix.shape != (208, 2048)
        or cache_meta["partitions"] != ["train"] * 208
        or cache_meta["sample_ids"] != metadata["normalization_train_ids"]
        or cache_meta["model_identity_declared"]
        != plan["load_config"]["model"]["expected_parameter_sha256"]
    ):
        raise ValueError("Sealed all-TRAIN208 matrix differs from saved normalization cohort")
    for feature, observation in zip(matrix, cache_meta["observations"], strict=True):
        if hashlib.sha256(feature.tobytes()).hexdigest() != observation["feature"]["sha256"]:
            raise ValueError("TRAIN208 row feature SHA differs")
    train_normalized, _ = api.replay(matrix, tensors, np)
    live_normalized = np.concatenate(normalized_rows)
    inputs.close()
    return {
        "contract": CONTRACT,
        "plan": plan_binding,
        "worker_report": report_binding,
        "bound_inputs": list(inputs.rows.values()),
        "inputs_final_stat_unchanged": True,
        "observations": rows,
        "replay_count": 8,
        "argmax_matches": 8,
        "replay_max_absolute_error": largest,
        "live_normalized_row_L2": api.describe(np.linalg.norm(live_normalized, axis=1), np),
        "train_normalized_row_L2": api.describe(np.linalg.norm(train_normalized, axis=1), np),
        "live_normalized_centroid_L2": float(np.linalg.norm(live_normalized.mean(axis=0))),
        "train_normalized_centroid_L2": float(np.linalg.norm(train_normalized.mean(axis=0))),
        "live_to_train_centroid_L2": float(
            np.linalg.norm(live_normalized.mean(axis=0) - train_normalized.mean(axis=0))
        ),
        "TRAIN208_matrix_read_passes_this_invocation": 1,
        "current_model_weights_or_full_source_inventory_rehashed": False,
        "source_runtime_parameter_closure_scope": "SHA-bound recorded worker evidence, not rerun",
        "native_status_scope": "worker_report_field_not_independent_process_observation",
        "numeric_runtime": {"numpy_version": np.__version__, "numpy_origin": np.__file__},
        "optimizer_calls": 0,
        "model_or_processor_loaded": False,
        "causal_root_cause_proven": False,
        "accuracy_or_flight_quality_proven": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "plan-sha256", "worker-report", "worker-report-sha256", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    output = (output if output.is_absolute() else ROOT / output).resolve()
    if (
        output.exists()
        or not output.parent.is_dir()
        or output.suffix != ".json"
        or not output.is_relative_to((ROOT / "artifacts/diagnostics").resolve())
    ):
        raise ValueError("Fresh diagnostics JSON with existing parent required")
    bindings = []
    for path, sha in (
        (args.plan, args.plan_sha256),
        (args.worker_report, args.worker_report_sha256),
    ):
        path = Path(path)
        path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
        bindings.append({"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": sha})
    result = audit(*bindings)
    raw = canonical(result) + b"\n"
    with output.open("xb") as stream:
        if stream.write(raw) != len(raw):
            raise OSError("Short live feature audit report write")
        stream.flush()
        os.fsync(stream.fileno())
    print(
        json.dumps(
            {
                "path": output.as_posix(),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
