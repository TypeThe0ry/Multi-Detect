"""Bounded same-call CPU feature capture, not a loader or inference authority.

Inject ``recorder.head_predictor`` into the frozen learned backend. Begin before
infer and commit its actual outcome afterwards, including rejected outcomes.
Payload fsync happens inside the predictor and therefore inside the backend's
post-head freshness gate. The final receipt records that gate; it does not grant
freshness after receipt publication. No forward, training or token generation
is performed here. Import this module only after source admission; it is quiet
with respect to optional ML packages.

V2 changes only file identity checks: access time is not a content identity.
Payload/schema and inference/optimizer budgets remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.navigation-same-call-feature-capture-v2"
FALSE_CLAIMS = {
    "optimizer_performed": False,
    "text_generation_performed": False,
    "quality_proven": False,
    "source_authenticated": False,
    "transport_authority": False,
    "execution_authority": False,
    "completion_evidence_created": False,
}


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Canonical SHA256 required")
    return value


def _content_identity(stat):
    """Content/file identity only; a successful read can legitimately update atime."""
    return (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)


def _binding(path):
    before = path.stat()
    if not 0 < before.st_size <= 64 * 1024:
        raise ValueError("Bounded nonempty capture file required")
    raw = path.read_bytes()
    if _content_identity(path.stat()) != _content_identity(before):
        raise ValueError("Capture file changed during verification")
    return {"path": path.as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _write_new(path, raw):
    with path.open("xb") as stream:
        if stream.write(raw) != len(raw):
            raise OSError("Short feature capture write")
        stream.flush()
        os.fsync(stream.fileno())
    row = _binding(path)
    if row["bytes"] != len(raw) or row["sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("Published feature capture differs from actual bytes")
    return row


def _tensor_bytes(value, *, shape, torch_module):
    from scripts.onboard_navigation_stable_readout import _tensor

    if sys.byteorder != "little":
        raise ValueError("Native little-endian CPU FP32 required for f32le capture")
    _tensor(value, shape=shape)
    clone = value.detach().cpu().contiguous().clone()
    raw = clone.view(torch_module.uint8).numpy().tobytes()
    expected = 8192 if shape == (1, 2048) else 24
    if len(raw) != expected:
        raise ValueError("Exact dense FP32 feature/output byte count required")
    return (
        raw,
        clone.tolist(),
        {
            "shape": list(shape),
            "dtype": "torch.float32",
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )


class FeatureCaptureRecorder:
    """One request owner; retains bytes/JSON records, never feature tensors.

    The caller must commit even when inference rejects. A persistence failure
    must fail the enclosing worker, not be translated to successful inference.
    Existing backend pre/post adapter/head checks remain mandatory. This helper
    additionally compares the predictor's head/norm to the same saved metadata.
    """

    def __init__(
        self,
        directory,
        *,
        experiment_plan_sha256,
        engine_identity,
        readout_identity,
        readout_metadata,
        max_requests=8,
    ):
        from scripts.onboard_navigation_inference_backend import NavigationEngineIdentity
        from scripts.onboard_navigation_learned_readout_backend import (
            SavedReadoutIdentity,
            _expected_head_state,
        )

        if type(engine_identity) is not NavigationEngineIdentity or (
            type(readout_identity) is not SavedReadoutIdentity
        ):
            raise TypeError("Exact model and saved readout identities required")
        engine_identity.__post_init__()
        readout_identity.__post_init__()
        if type(max_requests) is not int or not 1 <= max_requests <= 8:
            raise ValueError("At most eight precommitted requests required")
        directory = Path(directory).resolve()
        evaluation = (ROOT / "artifacts/evaluation").resolve()
        if not directory.is_relative_to(evaluation) or not directory.parent.is_dir():
            raise ValueError("Fresh capture directory under existing evaluation output required")
        relative = directory.relative_to(evaluation)
        if len(relative.parts) < 2 or not relative.parts[0].startswith(
            "onboard-navigation-live-shadow-"
        ):
            raise ValueError("Dedicated live-shadow output subtree required")
        self.plan_sha256 = _sha(experiment_plan_sha256)
        self.engine = canonical(
            {
                key: getattr(engine_identity, key)
                for key in ("model_sha256", "adapter_sha256", "adapter_config_sha256")
            }
        )
        self.readout = canonical(readout_identity.to_mapping())
        self.expected_state = _expected_head_state(readout_metadata)
        self.expected_state_sha256 = hashlib.sha256(self.expected_state).hexdigest()
        self.maximum, self.attempted, self.active = max_requests, 0, None
        self.capture_failure_requires_worker_abort = False
        self.last_receipt = None
        directory.mkdir(exist_ok=False)
        self.directory = directory

    @property
    def capture_error(self):
        """Copied current capture error; caller snapshots it before commit clears state."""
        if self.active is None or self.active["error"] is None:
            return None
        return json.loads(canonical(self.active["error"]))

    def begin(self, request):
        from multidetect.autonomy.onboard_navigation_inference import NavigationInferenceRequest

        if self.capture_failure_requires_worker_abort:
            raise RuntimeError("Prior feature capture failure requires worker abort")
        if type(request) is not NavigationInferenceRequest:
            raise TypeError("Frozen navigation request required")
        if self.active is not None or self.attempted >= self.maximum:
            raise ValueError("Active or excess feature capture request")
        request.validate_binding()
        engine = json.loads(self.engine)
        if any(getattr(request, key) != value for key, value in engine.items()):
            raise ValueError("Request differs from admitted model files")
        if request.generation != self.attempted + 1:
            raise ValueError("No retry or generation gaps allowed")
        self.attempted += 1  # Failures consume their precommitted request; no refund.
        directory = self.directory / f"generation-{request.generation:04d}"
        directory.mkdir(exist_ok=False)
        self.active = {
            "request_sha256": request.canonical_hash,
            "request": canonical(request.to_mapping()),
            "directory": directory,
            "predictor_attempted": False,
            "predictor_returned": False,
            "original_head_call_attempted": False,
            "feature": None,
            "six_output": None,
            "error": None,
        }

    def head_predictor(self, head, normalization, feature, *, torch_module):
        from scripts.onboard_navigation_learned_readout_backend import (
            _default_head_predictor,
            _default_head_snapshot,
        )

        active = self.active
        if active is None or active["predictor_attempted"]:
            raise ValueError("Exactly one original head call per active request required")
        active["predictor_attempted"] = True
        try:
            before = canonical(
                _default_head_snapshot(head, normalization, torch_module=torch_module)
            )
            if before != self.expected_state:
                raise ValueError("Head/TRAIN norm changed before feature capture")
            raw, _, record = _tensor_bytes(feature, shape=(1, 2048), torch_module=torch_module)
            active["feature"] = {
                "file": _write_new(active["directory"] / "feature.f32le", raw),
                "tensor": record,
            }
            # The original predictor receives the original feature, once only.
            active["original_head_call_attempted"] = True
            outputs = _default_head_predictor(
                head, normalization, feature, torch_module=torch_module
            )
            active["predictor_returned"] = True
            _, values, record = _tensor_bytes(outputs, shape=(1, 6), torch_module=torch_module)
            six = {"tensor": record, "values": values, "source": "actual_same_call_head_output"}
            active["six_output"] = {
                "file": _write_new(active["directory"] / "six-output.json", canonical(six) + b"\n"),
                "tensor": record,
                "values": values,
            }
            after = canonical(
                _default_head_snapshot(head, normalization, torch_module=torch_module)
            )
            if after != before:
                raise ValueError("Head/TRAIN norm changed during capture")
            return outputs
        except BaseException as exc:
            self.capture_failure_requires_worker_abort = True
            active["error"] = {"type": type(exc).__name__, "message": str(exc)[:1024]}
            raise

    def commit_outcome(self, request, *, accepted, diagnostics, raw_prediction):
        """Seal the actual backend gate outcome; never synthesize an acceptance.

        Payloads from a rejected/failed request remain diagnostic-only. Commit
        errors retain any already-published files and consume the request. They
        must abort the worker; there is no retry, deletion or overwrite.
        """
        active = self.active
        if active is None:
            raise ValueError("No active feature capture outcome")
        try:
            request.validate_binding()
            if request.canonical_hash != active["request_sha256"] or (
                canonical(request.to_mapping()) != active["request"]
            ):
                raise ValueError("Commit request changed")
            if type(accepted) is not bool or type(diagnostics) is not dict:
                raise TypeError("Actual boolean gate and diagnostics mapping required")
            if diagnostics.get("request_sha256") != request.canonical_hash or (
                diagnostics.get("result_accepted") is not accepted
            ):
                raise ValueError("Outcome differs from the actual backend gate")
            canonical(diagnostics)
            raw_bytes = canonical(raw_prediction)
            if canonical(diagnostics.get("raw_prediction")) != raw_bytes:
                raise ValueError("Raw prediction differs from backend diagnostics")
            for key in ("feature", "six_output"):
                row = active[key]
                if row is not None and _binding(Path(row["file"]["path"])) != row["file"]:
                    raise ValueError("Captured payload changed before outcome seal")
            if (
                active["feature"] is not None
                and diagnostics.get("actual_feature") != (active["feature"]["tensor"])
            ):
                raise ValueError("Feature differs from the backend's actual feature record")
            if active["six_output"] is not None:
                if diagnostics.get("actual_six_output") is not None and (
                    diagnostics["actual_six_output"] != active["six_output"]["tensor"]
                ):
                    raise ValueError("Six raw values differ from the backend tensor record")
                if raw_prediction is not None and canonical(
                    [
                        raw_prediction["raw_logits"]
                        + [
                            raw_prediction["raw_distance_m"],
                            raw_prediction["raw_angle_magnitude_rad"],
                        ]
                    ]
                ) != canonical(active["six_output"]["values"]):
                    raise ValueError("Raw prediction differs from same-call six outputs")
            if accepted:
                if (
                    active["error"] is not None
                    or type(raw_prediction) is not dict
                    or any(active[key] is None for key in ("feature", "six_output"))
                ):
                    raise ValueError("Failed or absent payloads cannot claim accepted inference")
                if diagnostics.get("actual_six_output") != active["six_output"]["tensor"]:
                    raise ValueError("Accepted capture requires the backend six-output record")
            if accepted and set(diagnostics.get("actual_prefix", {}).get("tensors", {})) != {
                "input_ids",
                "attention_mask",
                "pixel_values",
                "image_grid_thw",
            }:
                raise ValueError("Accepted capture requires all four actual prefix records")
            expected_files = {"feature.f32le", "six-output.json"}
            if any(path.name not in expected_files for path in active["directory"].iterdir()):
                raise ValueError("Unknown or already-sealed capture output")
            partials = [
                _binding(active["directory"] / filename)
                for key, filename in (
                    ("feature", "feature.f32le"),
                    ("six_output", "six-output.json"),
                )
                if active[key] is None and (active["directory"] / filename).exists()
            ]
            receipt = {
                "contract": CONTRACT,
                "experiment_plan_sha256": self.plan_sha256,
                "request_sha256": request.canonical_hash,
                "request": json.loads(active["request"]),
                "scope_sha256": request.scope.canonical_hash,
                "actor_identity_sha256": request.actor_identity_sha256,
                "source_binding_sha256": request.source_binding_sha256,
                "frame_ids": [frame.frame_id for frame in request.source_frames],
                "actor_sequences": [frame.sequence for frame in request.actor_input.frames],
                "engine_identity": json.loads(self.engine),
                "readout_identity": json.loads(self.readout),
                "head_and_train_norm_state_sha256": self.expected_state_sha256,
                "head_and_train_norm_state": json.loads(self.expected_state),
                "feature": active["feature"],
                "six_output": active["six_output"],
                "actual_prefix": diagnostics.get("actual_prefix"),
                "backend_diagnostics_sha256": hashlib.sha256(canonical(diagnostics)).hexdigest(),
                "raw_prediction": raw_prediction,
                "raw_prediction_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "backend_gate_accepted": accepted,
                "acceptance_scope": "backend_gate_before_outcome_receipt_publication",
                "diagnostic_only": True,
                "predictor_attempted": active["predictor_attempted"],
                "predictor_returned": active["predictor_returned"],
                "original_head_call_attempted": active["original_head_call_attempted"],
                "capture_error": active["error"],
                "capture_failure_requires_worker_abort": self.capture_failure_requires_worker_abort,
                "unadmitted_partial_files": partials,
                "claims": dict(FALSE_CLAIMS),
            }
            self.last_receipt = _write_new(
                active["directory"] / "receipt.json", canonical(receipt) + b"\n"
            )
            return self.last_receipt
        except BaseException:
            self.capture_failure_requires_worker_abort = True
            raise
        finally:
            self.active = None
