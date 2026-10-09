"""Tiny CPU arithmetic/byte gates, not real feature extraction or model evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scripts import audit_onboard_navigation_head_diagnostics as api


def encoded(value):
    payload = np.asarray(value, dtype="<f4").tobytes()
    shape = list(np.asarray(value).shape)
    header = json.dumps(
        {"features": {"dtype": "F32", "shape": shape, "data_offsets": [0, len(payload)]}}
    ).encode()
    raw = len(header).to_bytes(8, "little") + header + payload
    record = {
        "dtype": "torch.float32",
        "shape": shape,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    return raw, {"features": record}


def test_tensor_payload_exact_float32_and_readonly_view():
    raw, records = encoded([[1, 2], [3, 4]])
    values = api.bundle(raw, records, np)["features"]
    assert values.tolist() == [[1, 2], [3, 4]]
    assert values.dtype == np.dtype("<f4") and not values.flags.writeable


@pytest.mark.parametrize("mutation", ["hash", "nan", "trailing", "bool_offset"])
def test_tensor_negative_bytes_not_admitted(mutation):
    raw, records = encoded([[1.0, 2.0]])
    if mutation == "hash":
        records["features"]["sha256"] = "0" * 64
    elif mutation == "nan":
        raw, records = encoded([[float("nan"), 1.0]])
    elif mutation == "trailing":
        raw += b"extra"
    else:
        size = int.from_bytes(raw[:8], "little")
        doc = json.loads(raw[8 : 8 + size])
        doc["features"]["data_offsets"][0] = False
        header = json.dumps(doc).encode()
        raw = len(header).to_bytes(8, "little") + header + raw[8 + size :]
    with pytest.raises(ValueError):
        api.bundle(raw, records, np)


def head():
    return {
        "weight": np.zeros((6, 2), dtype=np.float32),
        "bias": np.zeros(6, dtype=np.float32),
        "center": np.asarray([3, 4], dtype=np.float32),
        "scale": np.asarray([2, 4], dtype=np.float32),
    }


def test_bias_and_feature_contributions_are_distinguished_without_training():
    tensors = head()
    tensors["bias"][3] = 0.25
    tensors["weight"][0, 0] = 2.0
    features = np.asarray([[3, 4], [5, 4]], dtype=np.float32)
    normalized, output = api.replay(features, tensors, np)
    assert normalized.tolist() == [[0, 0], [1, 0]]
    assert output[:, :4].argmax(axis=1).tolist() == [3, 0]
    report = api.summarize(output, np.asarray([3, 0]), tensors, np)
    assert report["raw_classifier_correct"] == 2
    assert report["STOP_winner_bias_margin"]["mean"] == 0.25
    assert report["STOP_winner_feature_margin"]["mean"] == 0
    assert report["predicted"]["sequence_stop"] == report["predicted"]["forward"] == 1


@pytest.mark.parametrize("change", ["scale_zero", "shape", "nonfinite"])
def test_replay_rejects_invalid_norm_and_head(change):
    tensors = head()
    if change == "scale_zero":
        tensors["scale"][0] = 0
    elif change == "shape":
        tensors["weight"] = tensors["weight"][:5]
    else:
        tensors["weight"][0, 0] = np.nan
    with pytest.raises(ValueError):
        api.replay(np.zeros((1, 2), dtype=np.float32), tensors, np)


def test_single_bound_read_and_late_drift_reject(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    path = tmp_path / "features.bin"
    path.write_bytes(b"tiny feature bytes")
    raw = path.read_bytes()
    binding = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    original = Path.read_bytes
    reads = []

    def read_once(value):
        reads.append(value)
        return original(value)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    reader = api.BoundInputs()
    assert reader.read(binding) == reader.read(binding) == raw and len(reads) == 1
    reader.close()
    path.write_bytes(b"changed fixture bytes")
    with pytest.raises(ValueError, match="changed after"):
        reader.close()


@pytest.mark.parametrize("value", [0, None, True, float("nan"), 4.0])
def test_unknown_zero_or_invalid_yaw_never_becomes_a_class(value):
    with pytest.raises(ValueError):
        api.gold_class({"planned_action": {"kind": "turn_yaw", "yaw_delta_rad": value}})


def test_vertical_not_collapsed_into_forward():
    with pytest.raises(ValueError):
        api.gold_class({"planned_action": {"kind": "translate", "translation_m": [0, 0, 3]}})


def test_import_is_stdlib_only_no_numpy_or_ml():
    path = Path(api.__file__).resolve()
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('cpu_audit',{str(path)!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "assert not any(x in sys.modules for x in "
        "('numpy','torch','PIL','transformers','peft'));print('stdlib_only')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and result.stdout.strip() == "stdlib_only"


def cli_inputs(tmp_path):
    inputs = tmp_path / "bound.json"
    inputs.write_bytes(b"{}")
    digest = hashlib.sha256(b"{}").hexdigest()
    return [
        "--plan",
        str(inputs),
        "--plan-sha256",
        digest,
        "--live-report",
        str(inputs),
        "--live-report-sha256",
        digest,
    ]


def test_create_only_output_keeps_python_json_bytes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    target = tmp_path / "artifacts" / "diagnostics" / "new.json"
    target.parent.mkdir(parents=True)
    report = {"uint64": 2**64 - 1, "optimizer_calls": 0}
    monkeypatch.setattr(api, "audit", lambda *args: report)
    assert api.main(cli_inputs(tmp_path) + ["--output", str(target)]) == 0
    binding = json.loads(capsys.readouterr().out)
    raw = target.read_bytes()
    assert json.loads(raw) == report
    assert binding == {
        "path": "artifacts/diagnostics/new.json",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize("kind", ["existing", "outside"])
def test_output_rejection_happens_before_cpu_recompute(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    target = tmp_path / "artifacts" / "diagnostics" / "new.json"
    target.parent.mkdir(parents=True)
    if kind == "existing":
        target.write_bytes(b"original")
    else:
        target = tmp_path / "outside.json"

    def forbidden(*args):
        pytest.fail("Rejected output must not reread the feature matrix")

    monkeypatch.setattr(api, "audit", forbidden)
    with pytest.raises(ValueError, match="Fresh diagnostics"):
        api.main(cli_inputs(tmp_path) + ["--output", str(target)])
    if kind == "existing":
        assert target.read_bytes() == b"original"
