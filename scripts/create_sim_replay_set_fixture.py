#!/usr/bin/env python3
"""Create a six-stratum SIM replay-set from a persisted Gazebo capture.

This wrapper turns an already-gated ``gazebo_camera_capture.py`` directory into
the same hash-bound replay contract used by the production evaluator.  The
capture is copied into each of the six condition strata and receives an
explicit, empty baseline truth/prediction stream.  Empty labels are deliberate:
Gazebo's camera capture does not contain independently reviewed detector truth,
so this fixture exercises transport, frame alignment, hashing and replay
plumbing without inventing an accuracy claim.

The resulting manifest is ``source_kind=synthetic_demo`` and therefore remains
closed for deployment-domain and final release gates.  It is useful for SIM
regression and for proving that a model/evaluator can consume the exact raw
recording layout before field strata are collected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from create_gazebo_raw_replay_fixture import create_fixture

from multidetect.backends import RawSensorReplayRecording
from multidetect.product_version import load_product_version
from multidetect.replay_set import (
    REPLAY_SET_STRATA,
    build_replay_set_manifest,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
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


def _write_readme(
    root: Path,
    *,
    dataset_id: str,
    dataset_version: str,
    source_capture: Path,
) -> None:
    text = f"""# {dataset_id} ({dataset_version})

这是由 Gazebo/Harmonic 已通过门禁的相机捕获生成的 SIM replay-set。六个分层目录
(`day`、`night`、`haze`、`reflection`、`smoke_only`、`no_fire`) 使用同一组原始帧，
用于验证 raw-sensor recording、哈希绑定、帧对齐、评估器和发布门接线。

## 证据边界

- `source_kind` 为 `synthetic_demo`；该数据集是软件/SIM 证据，不是现场部署域数据。
- `ground-truth.jsonl` 和 `predictions.jsonl` 是显式空基线，未从 Gazebo 图像推断火焰或烟雾，
  也没有伪造人工标注。
- `deployment_domain_evidence_complete=false`、`accuracy_validated=false`、
  `gate_passed=false` 是预期结果；不能据此宣称室外 GNSS、真实相机精度或最终发布通过。
- 原始捕获：`{source_capture}`。

## 目录

每个分层目录都包含：

```text
recording.manifest.json  # 哈希绑定的 events.jsonl
events.jsonl             # telemetry + frame 事件
frame-*.png              # 经校验的原始图像
ground-truth.jsonl       # 空基线真值
predictions.jsonl        # 空基线预测
```

运行 `scripts/evaluate_replay_set.py replay-set.manifest.json --report-only` 可生成结构化
评估报告。待现场采集六个分层并完成独立标注复核后，替换对应目录，再以
`source_kind=deployment_recording` 重新生成清单。
"""
    (root / "README.md").write_text(text, encoding="utf-8", newline="\n")


def create_sim_replay_set(
    capture_directory: Path,
    output: Path,
    *,
    dataset_id: str,
    dataset_version: str,
    frame_limit: int | None = None,
    recording_id: str = "gazebo-harmonic-sim-replay",
) -> dict[str, Any]:
    capture_directory = capture_directory.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists; choose a new path: {output}")
    if frame_limit is not None and frame_limit < 3:
        raise ValueError("frame_limit must be at least 3")

    output.parent.mkdir(parents=True, exist_ok=True)
    source_capture = capture_directory / "capture.json"
    if not source_capture.is_file():
        raise FileNotFoundError(f"Gazebo capture.json not found: {source_capture}")

    with tempfile.TemporaryDirectory(prefix="multidetect-sim-replay-") as temporary_name:
        staging = Path(temporary_name) / "raw-fixture"
        fixture_summary = create_fixture(
            capture_directory,
            staging,
            recording_id=recording_id,
            frame_limit=frame_limit,
        )
        recording_source = staging / "recording"
        source_manifest = recording_source / "recording.manifest.json"
        source_recording = RawSensorReplayRecording.from_manifest(source_manifest)
        frame_ids = tuple(frame.frame_id for frame in source_recording.frames)
        captured_at = {frame.frame_id: frame.captured_at_s for frame in source_recording.frames}

        output.mkdir()
        for stratum in REPLAY_SET_STRATA:
            destination = output / stratum
            shutil.copytree(recording_source, destination)
            _write_jsonl(
                destination / "ground-truth.jsonl",
                [{"frame_id": frame_id, "objects": []} for frame_id in frame_ids],
            )
            _write_jsonl(
                destination / "predictions.jsonl",
                [
                    {
                        "frame_id": frame_id,
                        "captured_at_s": captured_at[frame_id],
                        "inference_latency_ms": 0.0,
                        "detections": [],
                    }
                    for frame_id in frame_ids
                ],
            )

    manifest_path = output / "replay-set.manifest.json"
    manifest = build_replay_set_manifest(
        output,
        manifest_path,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        class_names=("flame", "smoke"),
        required_strata=REPLAY_SET_STRATA,
        source_kind="synthetic_demo",
        annotations_reviewed=False,
    )
    product = load_product_version()
    source_payload = json.loads(source_capture.read_text(encoding="utf-8"))
    fixture_evidence = {
        key: fixture_summary[key]
        for key in (
            "fixture_type",
            "source_capture_sha256",
            "recording_hash",
            "events_sha256",
            "frame_count",
            "width",
            "height",
            "camera_topic",
            "navsat_topic",
            "model_instance",
            "telemetry_fallback_count",
            "invalid_navsat_count",
            "ground_truth_present",
            "hardware_outputs_present",
        )
        if key in fixture_summary
    }
    summary: dict[str, Any] = {
        "event": "sim_replay_set_fixture_created",
        "schema_version": 1,
        "product": product.product,
        "release_label": product.release_label,
        "python_version": product.python,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "source_capture": str(source_capture),
        "source_capture_sha256": _sha256(source_capture),
        "source_capture_schema": source_payload.get("schema"),
        "source_capture_gate_passed": source_payload.get("gate_passed") is True,
        "fixture_evidence": fixture_evidence,
        "strata": list(REPLAY_SET_STRATA),
        "frame_count_per_stratum": len(frame_ids),
        "frame_ids": list(frame_ids),
        "source_kind": "synthetic_demo",
        "annotations_reviewed": False,
        "empty_baseline_truth_and_predictions": True,
        "software_only": True,
        "deployment_domain_evidence_complete": manifest.is_complete,
        "accuracy_validated": manifest.claims["accuracy_validated"],
        "gate_passed": manifest.is_complete,
        "replay_set_manifest": str(manifest_path),
        "replay_set_manifest_sha256": _sha256(manifest_path),
        "replay_set_canonical_sha256": manifest.canonical_hash,
        "claims": dict(manifest.claims),
        "coverage": manifest.coverage(),
        "failure_reasons": [
            "synthetic_demo source is not deployment-domain evidence",
            "independent annotations are not reviewed",
            "empty baseline truth/predictions do not validate detector accuracy",
        ],
    }
    summary_path = output / "fixture-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_readme(
        output,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        source_capture=source_capture,
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_directory", type=Path, help="persisted Gazebo capture directory")
    parser.add_argument("output", type=Path, help="new replay-set root to create")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--frame-limit", type=int, default=None)
    parser.add_argument("--recording-id", default="gazebo-harmonic-sim-replay")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = create_sim_replay_set(
        args.capture_directory,
        args.output,
        dataset_id=args.dataset_id,
        dataset_version=args.dataset_version,
        frame_limit=args.frame_limit,
        recording_id=args.recording_id,
    )
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
