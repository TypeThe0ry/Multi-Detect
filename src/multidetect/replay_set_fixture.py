"""Create a deterministic positive replay-set fixture for evaluator tests.

The production replay-set gate intentionally requires independently reviewed
deployment recordings.  That makes the existing Gazebo fixture a useful
truth-free transport test, but it also means the IoU evaluator cannot be
exercised against positive flame/smoke examples without hand-authoring a
temporary directory in a unit test.  This module turns an already verified raw
replay-set into a separate, clearly labelled ``synthetic_demo`` fixture with
deterministic truth and prediction streams.

The generated data is contract evidence only.  It never changes the source
recording, never claims deployment accuracy, and remains ineligible for the
REPLAY or final release gate because its ``source_kind`` is ``synthetic_demo``
and its annotations are not independently reviewed.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .backends.raw_replay import RawSensorReplayRecording
from .product_version import load_product_version
from .replay_set import (
    REPLAY_SET_POSITIVE_STRATA,
    REPLAY_SET_STRATA,
    build_replay_set_manifest,
    load_replay_set_manifest,
)
from .replay_set_evaluation import evaluate_replay_set

_FLAME_BOX = (0.10, 0.10, 0.40, 0.40)
_SMOKE_BOX = (0.55, 0.20, 0.85, 0.75)
_DEFAULT_LATENCY_BASE_MS = 4.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    payload = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    )
    path.write_text(payload, encoding="utf-8", newline="\n")


def _boxes(*, stratum: str) -> tuple[dict[str, object], ...]:
    if stratum not in REPLAY_SET_POSITIVE_STRATA:
        return ()
    # ``smoke_only`` is a content stratum, not merely a lighting condition:
    # keep it free of flame truth so per-stratum metrics can detect a model
    # that hallucinates flame when only smoke is present.
    if stratum == "smoke_only":
        return ({"label": "smoke", "bbox": list(_SMOKE_BOX)},)
    return (
        {"label": "flame", "bbox": list(_FLAME_BOX)},
        {"label": "smoke", "bbox": list(_SMOKE_BOX)},
    )


def _prediction_boxes(*, stratum: str) -> tuple[dict[str, object], ...]:
    if stratum not in REPLAY_SET_POSITIVE_STRATA:
        return ()
    if stratum == "smoke_only":
        return (
            {"label": "smoke", "confidence": 0.90, "bbox": list(_SMOKE_BOX)},
        )
    return (
        {"label": "flame", "confidence": 0.95, "bbox": list(_FLAME_BOX)},
        {"label": "smoke", "confidence": 0.90, "bbox": list(_SMOKE_BOX)},
    )


def _write_stratum_streams(
    directory: Path,
    *,
    recording: RawSensorReplayRecording,
    stratum: str,
    latency_base_ms: float,
) -> None:
    truth_records: list[dict[str, object]] = []
    prediction_records: list[dict[str, object]] = []
    for index, frame in enumerate(recording.frames):
        truth_records.append(
            {
                "frame_id": frame.frame_id,
                "objects": [dict(item) for item in _boxes(stratum=stratum)],
            }
        )
        prediction_records.append(
            {
                "captured_at_s": frame.captured_at_s,
                "detections": [
                    dict(item) for item in _prediction_boxes(stratum=stratum)
                ],
                "frame_id": frame.frame_id,
                "inference_latency_ms": latency_base_ms + (0.25 * index),
            }
        )
    _write_jsonl(directory / "ground-truth.jsonl", truth_records)
    _write_jsonl(directory / "predictions.jsonl", prediction_records)


def _write_readme(
    root: Path,
    *,
    dataset_id: str,
    dataset_version: str,
    source_dataset_id: str,
) -> None:
    (root / "README.md").write_text(
        f"""# {dataset_id} ({dataset_version})

这是从 `{source_dataset_id}` 的已校验 raw replay 记录派生的确定性正例评估 fixture。
四个火烟混合正例分层 (`day`、`night`、`haze`、`reflection`) 每帧带有固定的
`flame`/`smoke` 框；`smoke_only` 每帧只带 `smoke` 框，`no_fire` 保留为空负例。
预测流与真值逐框相同，延迟使用固定序列。

## 证据边界

- `source_kind=synthetic_demo`，真值和预测是契约测试数据，不是相机模型推理结果。
- `annotations_reviewed=false`，`deployment_domain_evidence_complete=false`，
  `accuracy_validated=false` 和 `gate_passed=false` 是预期且必须保留的状态。
- 该 fixture 只验证帧对齐、IoU、正例分层覆盖、延迟统计、严格 JSON 和哈希绑定；
  不得绑定到 REPLAY/SIM/HIL/REAL 最终发布门。
- 要关闭部署域精度门，必须替换为六层真实部署记录、独立复核标注和实际模型输出，
  再以 `source_kind=deployment_recording` 重新生成清单。

## 生成与检查

```powershell
Set-Location D:/Projects/Multi-Detect
$env:PYTHONPATH = 'src;.'
# choose a new output directory; do not point it at the source replay-set
$output = 'artifacts/replay-sets/positive-fixture-output'
python scripts/create_replay_set_positive_fixture.py `
  artifacts/evaluation/terminal-continuation-v34/replay-set-sim-gazebo-2026 `
  $output `
  --dataset-id multidetect-synthetic-positive-evaluator `
  --dataset-version {dataset_version}

python scripts/evaluate_replay_set.py `
  "$output/replay-set.manifest.json" `
  --report-only
```
""",
        encoding="utf-8",
        newline="\n",
    )


def create_positive_replay_set(
    source: str | Path,
    output: str | Path,
    *,
    dataset_id: str,
    dataset_version: str,
    latency_base_ms: float = _DEFAULT_LATENCY_BASE_MS,
) -> dict[str, Any]:
    """Create a positive synthetic replay-set from an existing raw replay-set.

    ``source`` must contain all six canonical strata and a valid
    ``replay-set.manifest.json``.  The source tree is copied into a temporary
    sibling directory, so a failed generation cannot leave a half-written
    destination.  The destination is moved into place only after its manifest,
    evaluator report and summary have been written.
    """

    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("dataset_id must be a non-empty string")
    if not isinstance(dataset_version, str) or not dataset_version.strip():
        raise ValueError("dataset_version must be a non-empty string")
    if (
        isinstance(latency_base_ms, bool)
        or not isinstance(latency_base_ms, (int, float))
        or not math.isfinite(float(latency_base_ms))
        or float(latency_base_ms) < 0.0
    ):
        raise ValueError("latency_base_ms must be finite and non-negative")

    source_root = Path(source).expanduser().resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError(f"source replay-set root is not a directory: {source_root}")
    source_manifest_path = source_root / "replay-set.manifest.json"
    source_manifest = load_replay_set_manifest(source_manifest_path)
    if set(source_manifest.required_strata) != set(REPLAY_SET_STRATA):
        raise ValueError("source replay-set must declare all six canonical strata")

    output_root = Path(output).expanduser().resolve()
    if output_root == source_root:
        raise ValueError("output replay-set must differ from source replay-set")
    try:
        output_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("output replay-set cannot be inside source replay-set")
    try:
        source_root.relative_to(output_root)
    except ValueError:
        pass
    else:
        raise ValueError("source replay-set cannot be inside output replay-set")
    if output_root.exists():
        raise FileExistsError(f"output replay-set already exists: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)

    product = load_product_version()
    with tempfile.TemporaryDirectory(
        prefix=".multidetect-positive-replay-",
        dir=str(output_root.parent),
    ) as temporary_name:
        staging = Path(temporary_name) / output_root.name
        staging.mkdir()
        for stratum in REPLAY_SET_STRATA:
            source_stratum = source_root / stratum
            if not source_stratum.is_dir():
                raise ValueError(f"source replay-set is missing stratum: {stratum}")
            destination_stratum = staging / stratum
            shutil.copytree(source_stratum, destination_stratum)
            recording = RawSensorReplayRecording.from_manifest(
                destination_stratum / "recording.manifest.json"
            )
            _write_stratum_streams(
                destination_stratum,
                recording=recording,
                stratum=stratum,
                latency_base_ms=float(latency_base_ms),
            )

        manifest_path = staging / "replay-set.manifest.json"
        manifest = build_replay_set_manifest(
            staging,
            manifest_path,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            class_names=("flame", "smoke"),
            required_strata=REPLAY_SET_STRATA,
            source_kind="synthetic_demo",
            annotations_reviewed=False,
        )
        accuracy = evaluate_replay_set(manifest_path)
        # The general evaluator reports the absolute manifest path for
        # operator diagnostics.  A fixture is copied into arbitrary temporary
        # or checkout directories, so keep its serialized evidence portable
        # and byte-for-byte deterministic by storing the root-relative name.
        accuracy["manifest_path"] = "replay-set.manifest.json"
        accuracy_path = staging / "accuracy-report.json"
        _write_json(accuracy_path, accuracy)
        _write_readme(
            staging,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            source_dataset_id=source_manifest.dataset_id,
        )
        summary: dict[str, Any] = {
            "event": "synthetic_positive_replay_fixture_created",
            "schema_version": 1,
            "product": product.product,
            "release_label": product.release_label,
            "python_version": product.python,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "source_dataset_id": source_manifest.dataset_id,
            "source_manifest_sha256": _sha256(source_manifest_path),
            "source_manifest_canonical_sha256": source_manifest.canonical_hash,
            "source_kind": "synthetic_demo",
            "fixture_type": "positive_iou_evaluator_contract",
            "software_only": True,
            "annotations_reviewed": False,
            "deployment_domain_evidence_complete": False,
            "accuracy_validated": False,
            "gate_passed": False,
            "required_strata": list(REPLAY_SET_STRATA),
            "positive_strata": list(REPLAY_SET_POSITIVE_STRATA),
            "frame_count_by_stratum": manifest.coverage(),
            "positive_stratum_coverage": accuracy["positive_stratum_coverage"],
            "positive_strata_coverage_passed": accuracy[
                "positive_strata_coverage_passed"
            ],
            "metrics_evaluated": accuracy["metrics_evaluated"],
            "metrics_gate_passed": accuracy["metrics_gate_passed"],
            "accuracy_report": {
                "path": "accuracy-report.json",
                "sha256": _sha256(accuracy_path),
                "bytes": accuracy_path.stat().st_size,
                "gate_passed": accuracy["gate_passed"],
            },
            "replay_set_manifest": {
                "path": "replay-set.manifest.json",
                "sha256": _sha256(manifest_path),
                "canonical_sha256": manifest.canonical_hash,
                "bytes": manifest_path.stat().st_size,
            },
            "failure_reasons": [
                "synthetic_demo source is not deployment-domain evidence",
                "annotations are deterministic fixture data and are not independently reviewed",
                "the fixture validates evaluator plumbing, not detector accuracy",
            ],
        }
        _write_json(staging / "fixture-summary.json", summary)
        shutil.move(str(staging), str(output_root))

    return summary


__all__ = ["create_positive_replay_set"]
