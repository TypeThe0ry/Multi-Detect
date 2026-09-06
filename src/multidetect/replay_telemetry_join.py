"""Join an independent telemetry JSONL stream into a raw replay recording.

Video-to-replay conversion intentionally leaves navigation unknown.  This
module provides the explicit second step for a field workflow: a previously
verified camera recording is copied into a new directory and joined with a
separately captured, full ``RawReplayTelemetrySample`` stream.  Samples are
matched by the latest timestamp at or before each frame; no interpolation or
future sample is accepted.  Every frame must have a telemetry sample within a
bounded age, keeping stale GPS from becoming a valid target location.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .adaptive_ranging import GpsQualityAssessment, assess_gps_quality
from .autonomy.serialization import AutonomyContractError, require_text
from .backends import RawReplayTelemetrySample, RawSensorReplayRecording

DEFAULT_MAXIMUM_TELEMETRY_AGE_S = 1.0


def join_raw_replay_telemetry(
    recording_manifest: str | Path,
    telemetry_jsonl: str | Path,
    output: str | Path,
    *,
    recording_id: str | None = None,
    maximum_telemetry_age_s: float = DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
) -> dict[str, Any]:
    """Join a canonical telemetry JSONL sidecar into a new raw replay.

    The input recording and sidecar are read-only.  ``output`` must not exist;
    the new directory is staged and atomically moved into place only after the
    copied image bytes, event stream, and manifest have all been re-verified.
    A joined recording can expose qualified GPS samples to the normal replay
    pipeline, but this operation never claims deployment-domain accuracy or a
    REAL/HIL release gate.
    """

    manifest_path = Path(recording_manifest).expanduser().resolve(strict=True)
    if not manifest_path.is_file():
        raise ValueError(f"recording_manifest must be a regular file: {manifest_path}")
    telemetry_path = Path(telemetry_jsonl).expanduser().resolve(strict=True)
    if not telemetry_path.is_file():
        raise ValueError(f"telemetry_jsonl must be a regular file: {telemetry_path}")
    output_path = Path(output).expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(f"output already exists; choose a new path: {output_path}")
    maximum_age = _finite_nonnegative(maximum_telemetry_age_s, "maximum_telemetry_age_s")

    recording = RawSensorReplayRecording.from_manifest(manifest_path)
    telemetry_samples = _load_telemetry_jsonl(telemetry_path)
    telemetry_times = tuple(sample.observed_at_s for sample in telemetry_samples)
    frame_times = tuple(frame.captured_at_s for frame in recording.frames)
    if telemetry_times[0] > frame_times[0]:
        raise ValueError(
            "telemetry sidecar must contain a sample at or before the first replay frame"
        )

    frame_sample_indices: list[int] = []
    frame_ages: list[float] = []
    for frame_time in frame_times:
        index = bisect.bisect_right(telemetry_times, frame_time) - 1
        if index < 0:  # Defensive: the first-sample check above covers this path.
            raise ValueError("telemetry sidecar does not cover every replay frame")
        age = frame_time - telemetry_times[index]
        if age > maximum_age:
            raise ValueError(
                f"telemetry sidecar is stale at frame {frame_time:.6f}s: "
                f"age {age:.6f}s exceeds {maximum_age:.6f}s"
            )
        frame_sample_indices.append(index)
        frame_ages.append(age)

    normalized_recording_id = (
        f"{recording.recording_id}-telemetry-joined" if recording_id is None else recording_id
    )
    normalized_recording_id = require_text(normalized_recording_id, "recording_id").strip()
    if not normalized_recording_id:
        raise ValueError("recording_id must be non-empty text")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(
        tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=str(output_path.parent))
    )
    committed = False
    try:
        _copy_recording_images(recording, temporary_path)
        event_items: list[tuple[float, int, Mapping[str, object]]] = []
        for sample in telemetry_samples:
            event_items.append((sample.observed_at_s, 0, sample.to_mapping()))
        for frame in recording.frames:
            event_items.append((frame.captured_at_s, 1, frame.to_mapping()))
        event_items.sort(key=lambda item: (item[0], item[1]))
        event_payload = "".join(
            json.dumps(
                item[2],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for item in event_items
        ).encode("utf-8")
        events_path = temporary_path / "events.jsonl"
        events_path.write_bytes(event_payload)
        event_sha256 = hashlib.sha256(event_payload).hexdigest()
        joined = RawSensorReplayRecording.from_jsonl(
            events_path,
            recording_id=normalized_recording_id,
            airframe_profile_hash=recording.airframe_profile_hash,
            started_at_utc=recording.started_at_utc,
            expected_sha256=event_sha256,
            expected_byte_count=len(event_payload),
            # A telemetry join changes only the event stream.  Preserve the
            # source scenario identity so a SIM/HIL replay remains bound to
            # the same deterministic scenario after navigation is added.
            scenario_id=recording.scenario_id,
            scenario_manifest_hash=recording.scenario_manifest_hash,
            scenario_seed=recording.scenario_seed,
        )
        scenario_binding = None
        if joined.scenario_id is not None:
            scenario_binding = {
                "scenario_id": joined.scenario_id,
                "scenario_manifest_hash": joined.scenario_manifest_hash,
                "scenario_seed": joined.scenario_seed,
            }
        manifest_document: dict[str, object] = {
            "schema_version": 1,
            "recording_id": joined.recording_id,
            "airframe_profile_hash": joined.airframe_profile_hash,
            "started_at_utc": _format_utc(joined.started_at_utc),
            "recording_hash": joined.recording_hash,
            "events": {
                "path": "events.jsonl",
                "sha256": event_sha256,
                "bytes": len(event_payload),
            },
        }
        if scenario_binding is not None:
            manifest_document["scenario"] = scenario_binding
        manifest_payload = json.dumps(
            manifest_document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_output = temporary_path / "recording.manifest.json"
        manifest_output.write_bytes(manifest_payload)
        verified = RawSensorReplayRecording.from_manifest(manifest_output)
        if verified.recording_hash != joined.recording_hash:
            raise RuntimeError("joined replay manifest changed the recording identity")

        source_manifest_sha256, source_manifest_bytes = _sha256_file(manifest_path)
        telemetry_sha256, telemetry_bytes = _sha256_file(telemetry_path)
        gps_assessments = tuple(
            assess_gps_quality(
                telemetry_samples[index].telemetry,
                now_s=frame_time,
            )
            for frame_time, index in zip(frame_times, frame_sample_indices, strict=True)
        )
        gps_qualified_count = sum(item.qualified for item in gps_assessments)
        latest_telemetry = tuple(
            telemetry_samples[index].telemetry for index in frame_sample_indices
        )
        gps_bound = any(
            _finite(telemetry.latitude_deg) and _finite(telemetry.longitude_deg)
            for telemetry in latest_telemetry
        )
        final_manifest_path = output_path / "recording.manifest.json"
        summary = {
            "event": "raw_replay_telemetry_joined",
            "schema_version": 1,
            "source_recording_manifest": str(manifest_path),
            "source_recording_manifest_sha256": source_manifest_sha256,
            "source_recording_manifest_bytes": source_manifest_bytes,
            "source_recording_hash": recording.recording_hash,
            "source_telemetry_jsonl": str(telemetry_path),
            "source_telemetry_sha256": telemetry_sha256,
            "source_telemetry_bytes": telemetry_bytes,
            "recording_id": joined.recording_id,
            "airframe_profile_hash": joined.airframe_profile_hash,
            "started_at_utc": _format_utc(joined.started_at_utc),
            "scenario_id": joined.scenario_id,
            "scenario_manifest_hash": joined.scenario_manifest_hash,
            "scenario_seed": joined.scenario_seed,
            "scenario_binding_preserved": joined.scenario_id is not None,
            "recording_manifest": str(final_manifest_path),
            "recording_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "recording_hash": joined.recording_hash,
            "events_sha256": event_sha256,
            "events_bytes": len(event_payload),
            "frame_count": len(verified.frames),
            "telemetry_sample_count": len(verified.telemetry_samples),
            "maximum_telemetry_age_s": maximum_age,
            "maximum_observed_frame_telemetry_age_s": max(frame_ages),
            "mean_observed_frame_telemetry_age_s": sum(frame_ages) / len(frame_ages),
            "telemetry_match": "latest_at_or_before_frame_no_interpolation",
            "telemetry_mode": "joined_raw_sensor_sidecar",
            "position_healthy_for_all_frames": all(
                telemetry.position_healthy is True for telemetry in latest_telemetry
            ),
            "gps_bound": gps_bound,
            "gps_qualified_frame_count": gps_qualified_count,
            "gps_qualified_all_frames": gps_qualified_count == len(frame_times),
            "gps_quality_reasons": _quality_reason_counts(gps_assessments),
            "ground_truth_present": False,
            "predictions_present": False,
            "deployment_domain_evidence_complete": False,
            "accuracy_validated": False,
            "software_only": True,
            "gate_passed": False,
            "notes": [
                (
                    "Telemetry is joined by latest-at-or-before timestamp; no future sample "
                    "or interpolation is used."
                ),
                (
                    "GPS quality is recomputed by the canonical adaptive-ranging policy at "
                    "each frame time."
                ),
                (
                    "The joined recording still needs independent truth/review and "
                    "deployment evidence for release gates."
                ),
            ],
        }
        (temporary_path / "recording-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        shutil.move(str(temporary_path), str(output_path))
        committed = True
        return summary
    finally:
        if not committed and temporary_path.exists():
            shutil.rmtree(temporary_path, ignore_errors=True)


def _load_telemetry_jsonl(path: Path) -> tuple[RawReplayTelemetrySample, ...]:
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AutonomyContractError(f"telemetry JSONL must be UTF-8: {path}") from exc
    lines = text.splitlines()
    if not lines:
        raise AutonomyContractError("telemetry JSONL cannot be empty")
    samples: list[RawReplayTelemetrySample] = []
    previous_time = -1.0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise AutonomyContractError(f"telemetry JSONL line {line_number} cannot be blank")
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AutonomyContractError(
                f"telemetry JSONL line {line_number} is not strict JSON"
            ) from exc
        if not isinstance(raw, Mapping):
            raise AutonomyContractError(f"telemetry JSONL line {line_number} must be an object")
        sample = RawReplayTelemetrySample.from_mapping(raw)
        if sample.observed_at_s <= previous_time:
            raise AutonomyContractError(
                "telemetry JSONL observed_at_s values must be strictly increasing"
            )
        previous_time = sample.observed_at_s
        samples.append(sample)
    return tuple(samples)


def _copy_recording_images(recording: RawSensorReplayRecording, destination_root: Path) -> None:
    for frame in recording.frames:
        relative = PurePosixPath(frame.image.relative_path)
        destination = destination_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = frame.image.read_verified_bytes()
        destination.write_bytes(payload)
        observed = destination.read_bytes()
        if len(observed) != frame.image.byte_count:
            raise AutonomyContractError(
                f"copied image byte count mismatch: {frame.image.relative_path}"
            )
        if hashlib.sha256(observed).hexdigest() != frame.image.sha256:
            raise AutonomyContractError(f"copied image hash mismatch: {frame.image.relative_path}")


def _quality_reason_counts(assessments: Sequence[GpsQualityAssessment]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for assessment in assessments:
        reason = require_text(assessment.reason, "gps_quality.reason")
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite_nonnegative(value: object, name: str) -> float:
    if not _finite(value) or float(value) < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate object keys instead of silently accepting last-wins JSON."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        result[key] = value
    return result


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


__all__ = [
    "DEFAULT_MAXIMUM_TELEMETRY_AGE_S",
    "join_raw_replay_telemetry",
]
