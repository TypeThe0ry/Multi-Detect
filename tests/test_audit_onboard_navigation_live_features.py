"""Small generated CPU fixtures; no native feature/capture/model acceptance."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scripts import audit_onboard_navigation_head_diagnostics as old
from scripts import audit_onboard_navigation_live_features as api


def publish(path, value, *, raw=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if raw else api.canonical(value) + b"\n"
    path.write_bytes(data)
    return {"path": path.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def packed(values):
    records, header, payload = {}, {}, b""
    for name, value in values.items():
        array = np.asarray(value, dtype="<f4")
        raw = array.tobytes()
        records[name] = {
            "shape": list(array.shape),
            "dtype": "torch.float32",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        header[name] = {
            "shape": list(array.shape),
            "dtype": "F32",
            "data_offsets": [len(payload), len(payload) + len(raw)],
        }
        payload += raw
    raw = api.canonical(header)
    return len(raw).to_bytes(8, "little") + raw + payload, records


def cohort(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    monkeypatch.setattr(old, "ROOT", tmp_path)
    monkeypatch.setattr(api, "_api", lambda: old)
    helper = publish(tmp_path / "helper.py", b"# fixture only\n", raw=True)
    monkeypatch.setattr(api, "HELPER", ("helper.py", helper["bytes"], helper["sha256"]))
    values = {
        "weight": np.zeros((6, 2048), dtype=np.float32),
        "bias": np.asarray([1, 0, -1, 2, -2, -0.1], dtype=np.float32),
        "center": np.zeros(2048, dtype=np.float32),
        "scale": np.ones(2048, dtype=np.float32),
    }
    values["weight"][0, 0], values["weight"][3, 0] = 0.3, 0.5
    weights, tensors = packed(values)
    ids = [f"sample-{i:03d}" for i in range(208)]
    head_state = {"tensors": tensors, "normalization_train_ids": ids, "epsilon": 1e-6}
    head_weights = publish(tmp_path / "head/tensors.safetensors", weights, raw=True)
    metadata = publish(tmp_path / "head/metadata.json", head_state)
    receipt = publish(
        tmp_path / "head/receipt.json", {"metadata": metadata, "weights": head_weights}
    )
    readout = {"receipt": receipt, "metadata": metadata, "weights": head_weights}
    monkeypatch.setattr(
        api, "HEAD", {key: (row["bytes"], row["sha256"]) for key, row in readout.items()}
    )
    parameters = {"base": "b" * 64, "adapter": "c" * 64}
    model = {
        "base_weights": {"sha256": "1" * 64},
        "adapter_weights": {"sha256": "2" * 64},
        "adapter_config": {"sha256": "3" * 64},
        "expected_parameter_sha256": parameters,
    }
    plan = {
        "rule": {"model_variant": "candidate267_only", "readout_variant": "locked_final_all208"},
        "capture": {"sensor_frames": 8},
        "load_config": {"model": model, "readout": readout},
    }
    plan_binding = publish(tmp_path / "plan.json", plan)
    engine = {
        "model_sha256": "1" * 64,
        "adapter_sha256": "2" * 64,
        "adapter_config_sha256": "3" * 64,
    }
    identity = {
        "head_receipt_sha256": receipt["sha256"],
        "head_weights_sha256": head_weights["sha256"],
        "head_metadata_sha256": metadata["sha256"],
    }
    prefix = {
        "tensors": {
            name: {
                "shape": [1],
                "dtype": "torch.float32" if name == "pixel_values" else "torch.int64",
                "sha256": "4" * 64,
            }
            for name in api.FOUR
        }
    }
    observations, capture_receipts = [], []
    for generation in range(1, 9):
        feature = np.zeros((1, 2048), dtype=np.float32)
        feature[0, 0] = generation * 10
        _, output = old.replay(feature, values, np)
        feature_record = {
            "shape": [1, 2048],
            "dtype": "torch.float32",
            "sha256": hashlib.sha256(feature.tobytes()).hexdigest(),
        }
        six_record = {
            "shape": [1, 6],
            "dtype": "torch.float32",
            "sha256": hashlib.sha256(output.tobytes()).hexdigest(),
        }
        request = {
            "generation": generation,
            "actor_identity_sha256": "5" * 64,
            "source_binding_sha256": "6" * 64,
            "scope_sha256": "7" * 64,
            "source_frames": [{"frame_id": f"frame-{generation}"}],
            **engine,
        }
        prediction = {
            "raw_logits": output[0, :4].tolist(),
            "raw_distance_m": float(output[0, 4]),
            "raw_angle_magnitude_rad": float(output[0, 5]),
            "selected_class": "sequence_stop",
            "declared_bindings": {
                "prefix_sha256": api.digest(prefix["tensors"]),
                "base_weights_sha256": engine["model_sha256"],
                "adapter_weights_sha256": engine["adapter_sha256"],
                "head_weights_sha256": identity["head_weights_sha256"],
                "head_config_sha256": identity["head_metadata_sha256"],
            },
        }
        diagnostics = {
            "actual_prefix": prefix,
            "actual_feature": feature_record,
            "actual_six_output": six_record,
            "raw_prediction": prediction,
        }
        folder = tmp_path / f"captures/{generation}"
        feature_file = publish(folder / "feature.f32le", feature.tobytes(), raw=True)
        six_file = publish(
            folder / "six-output.json",
            {
                "source": "actual_same_call_head_output",
                "tensor": six_record,
                "values": output.tolist(),
            },
        )
        captured = {
            "contract": "multidetect.navigation-same-call-feature-capture-v1",
            "request": request,
            "request_sha256": api.digest(request),
            "experiment_plan_sha256": plan_binding["sha256"],
            "scope_sha256": "7" * 64,
            "actor_identity_sha256": "5" * 64,
            "source_binding_sha256": "6" * 64,
            "frame_ids": [f"frame-{generation}"],
            "backend_gate_accepted": True,
            "capture_error": None,
            "unadmitted_partial_files": [],
            "diagnostic_only": True,
            "capture_failure_requires_worker_abort": False,
            "predictor_returned": True,
            "original_head_call_attempted": True,
            "engine_identity": engine,
            "readout_identity": identity,
            "head_and_train_norm_state": head_state,
            "head_and_train_norm_state_sha256": api.digest(head_state),
            "backend_diagnostics_sha256": api.digest(diagnostics),
            "actual_prefix": prefix,
            "feature": {"file": feature_file, "tensor": feature_record},
            "six_output": {"file": six_file, "tensor": six_record, "values": output.tolist()},
            "raw_prediction": prediction,
            "raw_prediction_sha256": api.digest(prediction),
        }
        capture_binding = publish(folder / "receipt.json", captured)
        capture_receipts.append(captured)
        observations.append(
            {
                "generation": generation,
                "request_sha256": api.digest(request),
                "actor_identity_sha256": "5" * 64,
                "source_binding_sha256": "6" * 64,
                "accepted": True,
                "raw_prediction": prediction,
                "metrics": {**diagnostics, "live_feature_receipt": capture_binding},
            }
        )
    report = {
        "contract": "multidetect.onboard-navigation-gazebo-feature-shadow-r3",
        "success": True,
        "native_exit_code": 0,
        "feature_capture_complete": True,
        "feature_capture_complete_count": 8,
        "source_bindings_unchanged": True,
        "source_bindings_before": {"fixture": True},
        "source_bindings_after": {"fixture": True},
        "plan": plan_binding,
        "new_optimizer_attempts": 0,
        "new_generation_calls": 0,
        "observations": observations,
        "loader": {
            "closed_successfully": True,
            "source_bindings_unchanged": True,
            "parameter_identity_before": parameters,
            "parameter_identity_after": parameters,
            "head_state_before": head_state,
            "head_state_after": head_state,
            "runtime_origins_before": {"fixture": "entry"},
            "runtime_origins_after": {"fixture": "entry"},
        },
    }
    report_binding = publish(tmp_path / "report.json", report)
    matrix = np.zeros((208, 2048), dtype=np.float32)
    matrix[:, 0] = np.arange(208, dtype=np.float32) / 100
    weights, records = packed({"features": matrix})
    cache_weights = publish(tmp_path / "cache/tensors.safetensors", weights, raw=True)
    cache_meta = publish(
        tmp_path / "cache/metadata.json",
        {
            "partitions": ["train"] * 208,
            "sample_ids": ids,
            "tensors": records,
            "model_identity_declared": parameters,
            "observations": [
                {"feature": {"sha256": hashlib.sha256(row.tobytes()).hexdigest()}} for row in matrix
            ],
        },
    )
    cache = publish(
        tmp_path / "cache/receipt.json", {"metadata": cache_meta, "weights": cache_weights}
    )
    monkeypatch.setattr(api, "CACHE_RECEIPT", cache)
    return plan_binding, report_binding, report, capture_receipts, values


def test_complete_cpu_feature_byte_replay_and_distribution(tmp_path, monkeypatch):
    plan, report, _, _, _ = cohort(tmp_path, monkeypatch)
    result = api.audit(plan, report)
    assert result["argmax_matches"] == result["replay_count"] == 8
    assert result["replay_max_absolute_error"] == 0
    assert result["TRAIN208_matrix_read_passes_this_invocation"] == 1
    assert result["live_normalized_centroid_L2"] == pytest.approx(45)
    assert result["train_normalized_centroid_L2"] == pytest.approx(1.035)
    assert (
        not result["accuracy_or_flight_quality_proven"] and not result["causal_root_cause_proven"]
    )
    assert result["optimizer_calls"] == 0
    row = result["observations"][0]
    assert row["STOP_bias_margin"] == 1 and row["STOP_feature_weight_margin"] == 2
    assert row["margin_decomposition_absolute_error"] == 0


@pytest.mark.parametrize("change", ["native_bool", "source", "loader", "count", "optimizer"])
def test_nonterminal_or_wrong_scope_report_rejected(tmp_path, monkeypatch, change):
    plan, binding, report, _, _ = cohort(tmp_path, monkeypatch)
    if change == "native_bool":
        report["native_exit_code"] = False
    elif change == "source":
        report["source_bindings_after"]["late"] = True
    elif change == "loader":
        report["loader"]["parameter_identity_after"] = {}
    elif change == "count":
        report["feature_capture_complete_count"] = 7
    else:
        report["new_optimizer_attempts"] = 1
    binding = publish(Path(binding["path"]), report)
    with pytest.raises(ValueError):
        api.audit(plan, binding)


@pytest.mark.parametrize(
    "change", ["request", "engine", "norm", "six", "prefix", "feature", "partial"]
)
def test_captured_identity_or_byte_drift_rejected(tmp_path, monkeypatch, change):
    plan, binding, report, receipts, _ = cohort(tmp_path, monkeypatch)
    capture = receipts[0]
    if change == "request":
        capture["request"]["scope_sha256"] = "0" * 64
    elif change == "engine":
        capture["engine_identity"]["model_sha256"] = "0" * 64
    elif change == "norm":
        capture["head_and_train_norm_state_sha256"] = "0" * 64
    elif change == "six":
        capture["six_output"]["values"][0][0] += 1
    elif change == "prefix":
        capture["actual_prefix"]["tensors"]["pixel_values"]["sha256"] = "0" * 64
    elif change == "feature":
        Path(capture["feature"]["file"]["path"]).write_bytes(b"changed bytes")
    else:
        capture["unadmitted_partial_files"] = [{"path": "partial"}]
    report["observations"][0]["metrics"]["live_feature_receipt"] = publish(
        Path(report["observations"][0]["metrics"]["live_feature_receipt"]["path"]), capture
    )
    binding = publish(Path(binding["path"]), report)
    with pytest.raises(ValueError):
        api.audit(plan, binding)


def test_output_create_only_and_no_recompute_on_existing(tmp_path, monkeypatch, capsys):
    plan, report, _, _, _ = cohort(tmp_path, monkeypatch)
    output = tmp_path / "artifacts/diagnostics/new.json"
    output.parent.mkdir(parents=True)
    args = [
        "--plan",
        plan["path"],
        "--plan-sha256",
        plan["sha256"],
        "--worker-report",
        report["path"],
        "--worker-report-sha256",
        report["sha256"],
        "--output",
        str(output),
    ]
    assert api.main(args) == 0
    binding = json.loads(capsys.readouterr().out)
    assert binding["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    monkeypatch.setattr(
        api, "audit", lambda *args: pytest.fail("Existing output must not recompute")
    )
    with pytest.raises(ValueError, match="Fresh diagnostics"):
        api.main(args)


def test_hash_consistent_false_six_values_cannot_replay_feature(tmp_path, monkeypatch):
    plan, binding, report, receipts, _ = cohort(tmp_path, monkeypatch)
    captured = receipts[0]
    metrics = report["observations"][0]["metrics"]
    captured["six_output"]["values"][0][0] += 0.1
    values = captured["six_output"]["values"]
    record = captured["six_output"]["tensor"]
    record["sha256"] = hashlib.sha256(np.asarray(values, dtype="<f4").tobytes()).hexdigest()
    captured["six_output"]["file"] = publish(
        Path(captured["six_output"]["file"]["path"]),
        {"source": "actual_same_call_head_output", "values": values, "tensor": record},
    )
    captured["raw_prediction"]["raw_logits"][0] += 0.1
    # Round to exactly representable FP32 before binding the JSON values.
    actual = float(np.asarray(values, dtype="<f4")[0, 0])
    values[0][0] = captured["raw_prediction"]["raw_logits"][0] = actual
    captured["six_output"]["file"] = publish(
        Path(captured["six_output"]["file"]["path"]),
        {"source": "actual_same_call_head_output", "values": values, "tensor": record},
    )
    captured["raw_prediction_sha256"] = api.digest(captured["raw_prediction"])
    stripped = {key: value for key, value in metrics.items() if key != "live_feature_receipt"}
    captured["backend_diagnostics_sha256"] = api.digest(stripped)
    metrics["live_feature_receipt"] = publish(
        Path(metrics["live_feature_receipt"]["path"]), captured
    )
    binding = publish(Path(binding["path"]), report)
    with pytest.raises(ValueError, match="cannot replay"):
        api.audit(plan, binding)


def test_nonfinite_feature_bytes_are_rejected_even_with_matching_hashes(tmp_path, monkeypatch):
    plan, binding, report, receipts, _ = cohort(tmp_path, monkeypatch)
    captured = receipts[0]
    metrics = report["observations"][0]["metrics"]
    feature = np.zeros((1, 2048), dtype="<f4")
    feature[0, 0] = np.nan
    captured["feature"]["file"] = publish(
        Path(captured["feature"]["file"]["path"]), feature.tobytes(), raw=True
    )
    captured["feature"]["tensor"]["sha256"] = captured["feature"]["file"]["sha256"]
    captured["backend_diagnostics_sha256"] = api.digest(
        {key: value for key, value in metrics.items() if key != "live_feature_receipt"}
    )
    metrics["live_feature_receipt"] = publish(
        Path(metrics["live_feature_receipt"]["path"]), captured
    )
    binding = publish(Path(binding["path"]), report)
    with pytest.raises(ValueError, match="Finite actual feature"):
        api.audit(plan, binding)


def test_late_bound_input_drift_prevents_success(tmp_path, monkeypatch):
    plan, report, _, _, _ = cohort(tmp_path, monkeypatch)
    original, calls = old.replay, []
    metadata = tmp_path / "head/metadata.json"

    def late(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 9:
            metadata.write_bytes(b"late fixture source drift")
        return result

    monkeypatch.setattr(old, "replay", late)
    with pytest.raises(ValueError, match="changed after CPU diagnostics"):
        api.audit(plan, report)


def test_missing_worker_report_never_reads_features_or_creates_output(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    plan = publish(tmp_path / "plan.json", {})
    output = tmp_path / "artifacts/diagnostics/new.json"
    output.parent.mkdir(parents=True)
    monkeypatch.setattr(api, "audit", lambda *args: pytest.fail("Missing report must stop first"))
    with pytest.raises(FileNotFoundError):
        api.main(
            [
                "--plan",
                plan["path"],
                "--plan-sha256",
                plan["sha256"],
                "--worker-report",
                str(tmp_path / "missing.json"),
                "--worker-report-sha256",
                "0" * 64,
                "--output",
                str(output),
            ]
        )
    assert not output.exists()


def test_frozen_helper_and_import_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    publish(tmp_path / api.HELPER[0], b"raise RuntimeError('must not execute')", raw=True)
    with pytest.raises(ValueError, match="before import"):
        api._api()
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('audit',{str(Path(api.__file__))!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "assert not any(x in sys.modules for x in ('numpy','torch','PIL','transformers','peft'))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
