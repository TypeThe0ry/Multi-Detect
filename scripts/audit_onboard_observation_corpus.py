"""Audit unlabelled Gazebo observations; never create labels or train a policy.

Source integrity and suitability of the existing 40-feature representation are
different results. Host capture time, Gazebo sensor time and derived UTC stay
separate. This audit does not authenticate a past simulator run by re-running it.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _import_root in (ROOT / "src", ROOT / "scripts"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

import train_onboard_model as trainer  # noqa: E402
from build_onboard_rendered_3d_dataset import _verify_gazebo_provenance  # noqa: E402
from build_onboard_trajectory_staging import _reject_simulator_truth  # noqa: E402

from multidetect.autonomy.contracts import ContextSnapshot  # noqa: E402
from multidetect.autonomy.onboard_agent import (  # noqa: E402
    ONBOARD_MODEL_FEATURE_NAMES,
    OnboardModelInput,
)
from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_json,
    canonical_sha256,
    require_real,
    strict_json_loads,
)
from multidetect.backends.gazebo_sim import validate_frame_set_clock  # noqa: E402
from multidetect.backends.raw_replay import (  # noqa: E402
    RawSensorReplayRecording,
    _decode_image_bgr,
    _validate_decoded_image,
)

CONTRACT = "multidetect.onboard-observation-corpus-audit-v1"
CHANGE_THRESHOLD = 1e-6  # Reporting heuristic in normalized feature units, not an ML gate.


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_bytes(), name=str(path))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    canonical_json(value)  # Reject numeric overflow in unused metadata too.
    return value


def _file(root: Path, parent: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("source path is missing")
    path = Path(raw)
    path = (path if path.is_absolute() else parent / path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"source file escapes the audit root or is not a file: {path}")
    return path


def _seal(path: Path, expected: object, sealed: dict[Path, str]) -> dict[str, Any]:
    observed = _sha256(path)
    if observed != expected:
        raise ValueError(f"source SHA-256 mismatch: {path}")
    sealed[path] = observed
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": observed}


def _tool_sources() -> list[dict[str, Any]]:
    objects = (
        ContextSnapshot,
        OnboardModelInput,
        RawSensorReplayRecording,
        trainer._verify_materialized_frame_binding,
        _verify_gazebo_provenance,
        _reject_simulator_truth,
        validate_frame_set_clock,
        canonical_json,
    )
    paths = [Path(__file__), *(Path(inspect.getfile(obj)) for obj in objects)]
    tests_path = ROOT / "tests" / "test_audit_onboard_observation_corpus.py"
    if tests_path.is_file():
        paths.append(tests_path)
    return [
        {"path": path.resolve().as_posix(), "sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in dict.fromkeys(paths)
    ]


def _unique(values: Any) -> int:
    return len({json.dumps(value, sort_keys=True, allow_nan=False) for value in values})


def _event(raw: dict[str, Any]):
    # The legacy training parser omits this field. Preserve the persisted
    # coordinate domain without changing that policy-training admission path.
    return replace(
        trainer._onboard_event_from_mapping(raw),
        target_geometry_frame=raw.get("target_geometry_frame", "ned"),
    )


def _times(values: list[float]) -> dict[str, Any]:
    gaps = [right - left for left, right in zip(values, values[1:], strict=False)]
    if any(gap <= 0 for gap in gaps):
        raise ValueError("timestamps must be strictly increasing within their own clock domain")
    return {
        "first": values[0],
        "last": values[-1],
        "duration_s": values[-1] - values[0],
        "dt_min_s": min(gaps) if gaps else None,
        "dt_max_s": max(gaps) if gaps else None,
        "dt_median_s": statistics.median(gaps) if gaps else None,
    }


def _feature_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = list(zip(*(row["features"] for row in rows), strict=True))
    varying = {
        name: {"unique": _unique(column), "min": min(column), "max": max(column)}
        for name, column in zip(ONBOARD_MODEL_FEATURE_NAMES, columns, strict=True)
        if _unique(column) > 1
    }
    return {
        "feature_count": 40,
        "distinct_vectors": _unique(row["features"] for row in rows),
        "constant_count": 40 - len(varying),
        "constant_fraction": (40 - len(varying)) / 40,
        "varying": varying,
        "range_above_reporting_threshold": sum(
            item["max"] - item["min"] > CHANGE_THRESHOLD for item in varying.values()
        ),
        "reporting_threshold_normalized_units": CHANGE_THRESHOLD,
    }


def _source(
    reference: dict[str, Any], root: Path, staging: Path, sealed: dict[Path, str]
) -> tuple[dict[str, Any], tuple, RawSensorReplayRecording]:
    rgb_path = _file(root, staging.parent, reference.get("path"))
    depth_ref = reference["depth_manifest"]
    depth_path = _file(root, staging.parent, depth_ref["path"])
    raw_ref = reference["recording_manifest"]
    raw_path = _file(root, staging.parent, raw_ref["path"])
    summary = {
        "rgb_manifest": _seal(rgb_path, reference["sha256"], sealed),
        "depth_manifest": _seal(depth_path, depth_ref["sha256"], sealed),
        "recording_manifest": _seal(raw_path, raw_ref["sha256"], sealed),
    }
    rgb, depth = _json(rgb_path), _json(depth_path)
    _reject_simulator_truth(rgb)
    _reject_simulator_truth(depth)
    _verify_gazebo_provenance(rgb, rgb_path)
    validate_frame_set_clock(rgb)
    if depth.get("schema") != "multidetect.gazebo-depth-companion-set":
        raise ValueError("unsupported Depth manifest")
    if depth.get("source_attribution", {}).get("truth_loaded_by_runtime") is not False:
        raise ValueError("Depth source does not explicitly exclude runtime truth")
    if reference.get("scenario_manifest_sha256") != rgb.get("scenario_manifest_hash"):
        raise ValueError("RGB source scenario mismatch")
    if depth.get("scenario_manifest_hash") != rgb.get("scenario_manifest_hash"):
        raise ValueError("Depth source scenario mismatch")
    recording = RawSensorReplayRecording.from_manifest(
        raw_path, expected_manifest_sha256=raw_ref["sha256"]
    )
    if (
        recording.recording_hash != raw_ref["recording_hash"]
        or recording.recording_id != raw_ref["recording_id"]
        or len(recording.frames) != raw_ref["frame_count"]
    ):
        raise ValueError("raw recording content identity/count mismatch")
    events = _json(raw_path)["events"]
    events_path = _file(root, raw_path.parent, events["path"])
    summary["events"] = _seal(events_path, events["sha256"], sealed)
    if summary["events"]["bytes"] != events["bytes"]:
        raise ValueError("raw event byte count mismatch")
    summary.update(
        recording_id=recording.recording_id,
        recording_hash=recording.recording_hash,
        frame_count=len(recording.frames),
        telemetry_sample_count=len(recording.telemetry_samples),
        raw_event_kind_counts={
            "frame": len(recording.frames),
            "telemetry": len(recording.telemetry_samples),
        },
        raw_home_position_event_count=0,
        source_attribution=rgb.get("source_attribution"),
        mainline_asset_binding=rgb.get("mainline_asset_binding"),
    )
    return summary, (rgb_path, rgb, depth_path, depth), recording


def _episode(
    episode: dict[str, Any],
    binding: tuple,
    recording: RawSensorReplayRecording,
    sealed: dict[Path, str],
    declared_missing: list[str],
) -> dict[str, Any]:
    rgb_path, rgb, depth_path, depth = binding
    rows = episode["transitions"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("episode has no transitions")
    if episode["split"] not in {"train", "evaluation", "test"}:
        raise ValueError("unsupported observation split")
    if (
        episode["recording_id"] != recording.recording_id
        or episode["recording_hash"] != recording.recording_hash
        or episode["airframe_profile_sha256"] != recording.airframe_profile_hash
        or episode["scenario_manifest_sha256"] != recording.scenario_manifest_hash
        or episode["scenario_seed"] != recording.scenario_seed
    ):
        raise ValueError("episode raw recording/profile/scenario identity mismatch")
    group_hash = hashlib.sha256(
        "|".join(
            (
                recording.recording_hash,
                episode["scenario_manifest_sha256"],
                recording.airframe_profile_hash,
                str(episode["seed"]),
                episode["task_contract_sha256"],
            )
        ).encode()
    ).hexdigest()
    if episode["episode_group_sha256"] != group_hash:
        raise ValueError("episode group hash mismatch")
    if len(rows) != len(recording.frames) or len(rows) != len(rgb["frames"]):
        raise ValueError("episode/source frame count mismatch")
    depth_by_id = {frame["frame_id"]: frame for frame in depth["frames"]}
    if len(depth_by_id) != len(rows):
        raise ValueError("Depth frame identity/count mismatch")
    contexts, sensor_times, clock_labels, clock_ids = [], [], [], []
    frame_bindings, raw_telemetry = [], []
    # Preserve the builder's report separately: absent ground_speed_mps may
    # legitimately be derived from observed N/E velocity, unlike airspeed.
    missing = {
        "geofence_valid" if key == "geofence_healthy" else key
        for key in declared_missing
        if key != "ground_speed_mps"
    }
    home_hashes, profiles = set(), set()
    telemetry_by_time = {sample.observed_at_s: sample for sample in recording.telemetry_samples}
    context_fields = {
        "battery_remaining_pct": "battery_remaining_pct",
        "estimated_flight_time_s": "estimated_flight_time_s",
        "airspeed_mps": "airspeed_mps",
        "wind_north_mps": "wind_north_mps",
        "wind_east_mps": "wind_east_mps",
        "geofence_valid": "geofence_healthy",
        "map_cache_available": "map_cache_available",
    }
    for index, (row, raw_frame, rgb_frame) in enumerate(
        zip(rows, recording.frames, rgb["frames"], strict=True)
    ):
        if row["sequence"] != index or isinstance(row["sequence"], bool):
            raise ValueError("transition sequence is not contiguous from zero")
        if (
            row["frame_id"] != raw_frame.frame_id
            or row["frame_id"] != rgb_frame["frame_id"]
            or rgb_frame["sequence"] != index
        ):
            raise ValueError("exact recording/frame/sequence binding mismatch")
        expected_next = rows[index + 1]["frame_id"] if index + 1 < len(rows) else None
        if row["next_frame_id"] != expected_next:
            raise ValueError("next_frame_id is not the actual following observation")
        if row.get("truth_free") is not True or row.get("policy_applied_to_actuator") is not False:
            raise ValueError("transition is not truth-free observation-only data")
        if row["observation"]["event"].get("truth_free") is not True:
            raise ValueError("runtime event does not exclude truth")
        context = ContextSnapshot.from_mapping(row["observation"]["context"])
        event = _event(row["observation"]["event"])
        if row["event_kind"] != event.kind.value or row["task_phase"] != (
            None if event.task_phase is None else event.task_phase.value
        ):
            raise ValueError("event/task phase summary does not match canonical event")
        timestamp = require_real(row["observed_at_s"], "observed_at_s", minimum=0)
        if not (
            timestamp == raw_frame.captured_at_s == context.captured_at_s == event.observed_at_s
        ):
            raise ValueError("observation/context/event time mismatch or future event")
        if (
            context.airframe_profile_id != episode["airframe_profile_id"]
            or context.airframe_profile_hash != recording.airframe_profile_hash
        ):
            raise ValueError("context airframe profile changed within episode")
        source = row["source"]
        expected_source = {
            "recording_id": recording.recording_id,
            "recording_hash": recording.recording_hash,
            "frame_id": raw_frame.frame_id,
            "captured_at_s": timestamp,
            "scenario_manifest_sha256": episode["scenario_manifest_sha256"],
            "episode_group_sha256": group_hash,
            "context_sha256": context.canonical_hash,
            "event_sha256": event.canonical_hash,
        }
        if any(source.get(key) != value for key, value in expected_source.items()):
            raise ValueError("transition source/context/event canonical hash binding mismatch")
        expected = OnboardModelInput(context, event).feature_vector()
        if len(row["features"]) != 40 or any(
            not math.isclose(require_real(a, "feature"), b, rel_tol=0, abs_tol=1e-12)
            for a, b in zip(row["features"], expected, strict=True)
        ):
            raise ValueError("canonical 40-feature recomputation mismatch")
        trainer._verify_materialized_frame_binding(
            source, binding=binding, episode_id=episode["episode_id"], sequence=index
        )
        rgb_payload = _file(rgb_path.parent, rgb_path.parent, rgb_frame["file"])
        depth_frame = depth_by_id[raw_frame.frame_id]
        depth_payload = _file(depth_path.parent, depth_path.parent, depth_frame["file"])
        for payload_path, frame in ((rgb_payload, rgb_frame), (depth_payload, depth_frame)):
            sealed[payload_path] = frame["sha256"]
            if payload_path.stat().st_size != frame["bytes"]:
                raise ValueError("RGB/Depth payload byte count mismatch")
        image = _decode_image_bgr(raw_frame.image.read_verified_bytes(), raw_frame.image)
        _validate_decoded_image(image, raw_frame.image)
        if hashlib.sha256(image.tobytes()).hexdigest() != rgb_frame["sha256"]:
            raise ValueError("reopened PNG pixels do not equal exact RGB wire payload")
        sealed[raw_frame.image.resolved_path] = raw_frame.image.sha256
        provenance = rgb_frame["provenance"]
        for key in (
            "camera_info_stamp_ns",
            "navsat_stamp_ns",
            "odometry_stamp_ns",
            "depth_source_stamp_ns",
        ):
            if provenance.get(key) is not None and provenance[key] > provenance["sensor_stamp_ns"]:
                raise ValueError(f"future sensor-domain source timestamp: {key}")
        telemetry = provenance["frame_telemetry"]
        raw_telemetry.append(telemetry)
        sample = telemetry_by_time.get(timestamp)
        if sample is None:
            raise ValueError("no exact frame-bound raw telemetry sample (nearest join forbidden)")
        raw_values = sample.to_mapping()["telemetry"]
        for values in (telemetry, raw_values):
            for key, value in values.items():
                if key.endswith("observed_at_s") and value is not None and value > timestamp:
                    raise ValueError(f"future telemetry sample: {key}")
        for key, value in telemetry.items():
            if key in raw_values and raw_values[key] != value:
                raise ValueError(f"raw/frame telemetry value mismatch: {key}")
        if (
            context.aircraft_position.latitude_deg != telemetry.get("latitude_deg")
            or context.aircraft_position.longitude_deg != telemetry.get("longitude_deg")
            or context.aircraft_position.altitude_m != max(0, telemetry.get("altitude_agl_m", -1))
            or context.gps_fix_type != telemetry.get("gps_fix_type")
            or context.gps_horizontal_accuracy_m != telemetry.get("gps_horizontal_accuracy_m")
            or context.ground_speed_mps != raw_values.get("ground_speed_mps")
        ):
            raise ValueError(
                "context position/GNSS/groundspeed does not match real source telemetry"
            )
        for context_field, telemetry_field in context_fields.items():
            if telemetry.get(telemetry_field) is None:
                missing.add(context_field)
            elif getattr(context, context_field) != telemetry[telemetry_field]:
                raise ValueError(f"context does not equal observed telemetry: {context_field}")
        contexts.append(context)
        home_hashes.add(context.home_position.canonical_hash)
        profiles.add((context.airframe_profile_id, context.airframe_profile_hash))
        sensor_times.append(
            require_real(provenance["sensor_stamp_ns"], "sensor_stamp_ns", minimum=1) / 1e9
        )
        clock_labels.append(provenance["captured_at_clock"])
        clock_ids.append(provenance.get("host_clock_identity"))
        frame_bindings.append(
            {
                "recording_hash": recording.recording_hash,
                "frame_id": raw_frame.frame_id,
                "rgb_payload_sha256": source["rgb_payload_sha256"],
                "depth_payload_sha256": source["depth_payload_sha256"],
            }
        )
    if len(home_hashes) != 1 or len(profiles) != 1:
        raise ValueError(
            "home/profile changed within one observation episode; explicit boundary required"
        )
    host = _times([context.captured_at_s for context in contexts])
    sensor = _times(sensor_times)
    utc_values = [
        datetime.fromisoformat(c.captured_at_utc.replace("Z", "+00:00")) for c in contexts
    ]
    _times([value.timestamp() for value in utc_values])
    for context, utc in zip(contexts, utc_values, strict=True):
        expected_utc = recording.started_at_utc + timedelta(
            seconds=context.captured_at_s - host["first"]
        )
        if abs((utc - expected_utc).total_seconds()) > 1e-6:
            raise ValueError("UTC does not match recording origin plus host capture offset")
    features = _feature_summary(rows)
    reasons = []
    if missing:
        reasons.append("canonical_40_contains_missing_defaulted_or_proxy_context_fields")
    if any(identity is None for identity in clock_ids):
        reasons.append("legacy_host_clock_has_no_bound_session_epoch_identity")
    if len(rows) < 8:
        reasons.append("fewer_than_8_real_observations")
    if features["range_above_reporting_threshold"] == 0:
        reasons.append("no_feature_range_above_explicit_reporting_threshold")
    if not math.isclose(host["duration_s"], sensor["duration_s"], abs_tol=1e-6):
        reasons.append("host_and_sensor_elapsed_time_differ_no_clock_mapping_claimed")
    first_home = contexts[0].home_position
    first_aircraft = contexts[0].aircraft_position
    return {
        "episode_id": episode["episode_id"],
        "recording_id": recording.recording_id,
        "recording_hash": recording.recording_hash,
        "scene_id": episode["scene_id"],
        "scenario_manifest_sha256": recording.scenario_manifest_hash,
        "scenario_seed": recording.scenario_seed,
        "split": episode["split"],
        "airframe_profile_id": episode["airframe_profile_id"],
        "airframe_profile_sha256": recording.airframe_profile_hash,
        "episode_group_sha256": group_hash,
        "frame_count": len(rows),
        "frame_identity_fields": ["recording_hash", "frame_id"],
        "ordered_frame_bindings_sha256": canonical_sha256({"frames": frame_bindings}),
        "frame_bindings": frame_bindings,
        "time_domains": {
            "host_capture": {**host, "clock": clock_labels[0], "identity": clock_ids[0]},
            "gazebo_sensor": {**sensor, "clock": "gazebo_sensor_stamp_ns", "epoch_identity": None},
            "utc": {
                "first": contexts[0].captured_at_utc,
                "last": contexts[-1].captured_at_utc,
                "source": "recording_started_at_utc_plus_host_capture_offset_not_sensor_clock",
            },
            "domains_joined_or_relabelled": False,
        },
        "home": {
            "distinct_within_episode": len(home_hashes),
            "sha256": first_home.canonical_hash,
            "position": first_home.to_mapping(),
            "observed_return_home_source_present": False,
            "equals_first_captured_gps": first_home.latitude_deg == first_aircraft.latitude_deg
            and first_home.longitude_deg == first_aircraft.longitude_deg,
            "equals_current_aircraft_gps_rows": sum(
                c.home_position.latitude_deg == c.aircraft_position.latitude_deg
                and c.home_position.longitude_deg == c.aircraft_position.longitude_deg
                for c in contexts
            ),
        },
        "defaulted_context_fields": sorted(missing),
        "declared_missing_context_fields": declared_missing,
        "session_boundary": {
            "conservative_boundary": "recording_hash",
            "prompt_task_session_identity_recorded": False,
            "clock_epoch_invented": False,
            "same_epoch_across_recordings_claimed": False,
        },
        "availability_flags_are_capture_builder_assertions_not_vehicle_telemetry": True,
        "features": features,
        "context_constant_values": {
            key: contexts[0].to_mapping()[key]
            for key in contexts[0].to_mapping()
            if all(c.to_mapping().get(key) == contexts[0].to_mapping()[key] for c in contexts)
            and key not in {"home_position", "schema_version"}
        },
        "telemetry": {
            "varying_ranges": {
                key: [min(t[key] for t in raw_telemetry), max(t[key] for t in raw_telemetry)]
                for key in raw_telemetry[0]
                if isinstance(raw_telemetry[0][key], (int, float))
                and not isinstance(raw_telemetry[0][key], bool)
                and all(isinstance(t.get(key), (int, float)) for t in raw_telemetry)
                and _unique(t[key] for t in raw_telemetry) > 1
            },
            "groundspeed_was_missing_from_frame_telemetry": "ground_speed_mps" in declared_missing,
            "airspeed_equals_groundspeed_all_frames": all(
                c.airspeed_mps == c.ground_speed_mps for c in contexts
            ),
        },
        "event_kind_counts": dict(Counter(row["event_kind"] for row in rows)),
        "target_class_counts": dict(
            Counter(str(row["observation"]["event"].get("target_class")) for row in rows)
        ),
        "task_phase_counts": dict(Counter(row["task_phase"] for row in rows)),
        "existing_proposed_label_nonnull_rows": sum(
            any(
                row.get(key) is not None
                for key in ("proposed_task_label", "proposed_flight_label", "proposed_regression")
            )
            for row in rows
        ),
        "observation_pretraining_eligible": not reasons,
        "ineligibility_reasons": reasons,
        "training_ready": False,
    }


def audit_corpus(staging_paths: list[Path], *, source_root: Path = ROOT) -> dict[str, Any]:
    """Reopen sources and report failures without admitting them as training data."""
    root = source_root.resolve(strict=True)
    tool_sources = _tool_sources()
    records = []
    for input_path in staging_paths:
        record: dict[str, Any] = {
            "path": str(input_path),
            "source_verified": False,
            "observation_pretraining_eligible": False,
            "errors": [],
            "episodes": [],
        }
        try:
            path = _file(root, root, str(input_path))
            record.update(path=path.as_posix(), bytes=path.stat().st_size, sha256=_sha256(path))
            sealed = {path: record["sha256"]}
            document = _json(path)
            _reject_simulator_truth(document)
            if (
                document.get("contract") != "multidetect.onboard-trajectory-staging-v1"
                or document.get("schema") != "multidetect.onboard-trajectory-staging"
                or document.get("schema_version") != 1
                or document.get("feature_names") != list(ONBOARD_MODEL_FEATURE_NAMES)
            ):
                raise ValueError("unsupported staging/40-feature contract")
            if (
                document.get("truth_free") is not True
                or document.get("truth_injected") is not False
                or document.get("policy_applied_to_actuator") is not False
                or document.get("training_ready") is not False
            ):
                raise ValueError(
                    "staging must remain truth-free, training_ready=false, non-actuating"
                )
            sources = {}
            source_summaries = []
            for reference in document["source_manifests"]:
                summary, binding, recording = _source(reference, root, path, sealed)
                if recording.recording_hash in sources:
                    raise ValueError("duplicate raw source within staging")
                sources[recording.recording_hash] = (binding, recording)
                source_summaries.append(summary)
            record["sources"] = source_summaries
            for episode in document["episodes"]:
                binding, recording = sources[episode["recording_hash"]]
                record["episodes"].append(
                    _episode(
                        episode,
                        binding,
                        recording,
                        sealed,
                        document.get("missing_context_fields", []),
                    )
                )
            if not record["episodes"]:
                raise ValueError("staging has no episodes")
            for sealed_path, expected_hash in sealed.items():
                if _sha256(sealed_path) != expected_hash:
                    raise ValueError(f"source changed during audit: {sealed_path}")
            record["source_verified"] = True
            record["observation_pretraining_eligible"] = all(
                episode["observation_pretraining_eligible"] for episode in record["episodes"]
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            record["errors"].append(str(exc))
            for episode in record["episodes"]:
                episode["observation_pretraining_eligible"] = False
        records.append(record)
    episodes = [episode for record in records for episode in record["episodes"]]
    owners: dict[tuple, list[tuple[dict, dict]]] = defaultdict(list)
    for record in records:
        for episode in record["episodes"]:
            for key in (
                ("recording", episode["recording_hash"]),
                ("scenario_seed", episode["scenario_manifest_sha256"], episode["scenario_seed"]),
            ):
                owners[key].append((record, episode))
            for frame in episode["frame_bindings"]:
                for field in ("rgb_payload_sha256", "depth_payload_sha256"):
                    owners[(field, frame[field])].append((record, episode))
    contamination = []
    for key, references in owners.items():
        splits = sorted({episode["split"] for _, episode in references})
        if len(splits) > 1:
            contamination.append(
                {"identity_kind": key[0], "identity": list(key[1:]), "splits": splits}
            )
            for record, episode in references:
                record["observation_pretraining_eligible"] = False
                episode["observation_pretraining_eligible"] = False
                if "cross_split_source_contamination" not in episode["ineligibility_reasons"]:
                    episode["ineligibility_reasons"].append("cross_split_source_contamination")
    frames = [frame for episode in episodes for frame in episode["frame_bindings"]]
    distinct_sources = {episode["recording_hash"] for episode in episodes}
    duplicate_bindings = len(frames) - len({(f["recording_hash"], f["frame_id"]) for f in frames})
    split_counts = dict(Counter(episode["split"] for episode in episodes))
    corpus_reasons = []
    if any(not record["source_verified"] for record in records):
        corpus_reasons.append("unverified_or_invalid_source")
    if not all(record["observation_pretraining_eligible"] for record in records):
        corpus_reasons.append("one_or_more_episode_admission_checks_failed")
    if contamination:
        corpus_reasons.append("cross_split_source_contamination")
    if duplicate_bindings:
        corpus_reasons.append("duplicate_recording_frame_observations_not_independent")
    if not ({"evaluation", "test"} & split_counts.keys()):
        corpus_reasons.append("no_independent_held_out_observation_partition")
    if not records:
        corpus_reasons.append("empty_corpus")
    if any(_sha256(Path(source["path"])) != source["sha256"] for source in tool_sources):
        corpus_reasons.append("audit_tool_sources_changed_during_validation")
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "audit_only": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool_sources": tool_sources,
        "staging": records,
        "summary": {
            "staging_count": len(records),
            "source_verified_staging_count": sum(r["source_verified"] for r in records),
            "episodes": len(episodes),
            "distinct_bare_episode_ids": len({e["episode_id"] for e in episodes}),
            "independent_recording_sources": len(distinct_sources),
            "scenes": len({e["scenario_manifest_sha256"] for e in episodes}),
            "airframe_profiles": len({e["airframe_profile_sha256"] for e in episodes}),
            "scenario_seed_groups": len(
                {(e["scenario_manifest_sha256"], e["scenario_seed"]) for e in episodes}
            ),
            "split_episode_counts": split_counts,
            "frames": len(frames),
            "unique_recording_frame_identities": len(frames) - duplicate_bindings,
            "unique_bare_frame_ids": len({f["frame_id"] for f in frames}),
            "unique_rgb_payload_hashes": len({f["rgb_payload_sha256"] for f in frames}),
            "unique_depth_payload_hashes": len({f["depth_payload_sha256"] for f in frames}),
            "duplicate_recording_frame_observations": duplicate_bindings,
        },
        "split_contamination": contamination,
        "observation_pretraining_eligible": not corpus_reasons,
        "ineligibility_reasons": corpus_reasons,
        "claim_boundary": {
            "source_authentication": (
                "persisted_hash_bound_capture_attribution_not_a_new_simulator_run"
            ),
            "action_labels_created": False,
            "training_performed": False,
            "training_ready": False,
            "candidate_promoted": False,
            "final_release_allowed": False,
            "policy_applied_to_actuator": False,
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", action="append", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New report; never overwrite an existing artifact",
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("--output must be a new file")
    report = audit_corpus(args.staging)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "output": args.output.resolve().as_posix(),
                "sha256": _sha256(args.output),
                "summary": report["summary"],
                "eligible": report["observation_pretraining_eligible"],
            }
        )
    )
    # 0 = completed evidence audit, not training admission. Invalid sources/leakage return 2.
    return (
        2
        if report["split_contamination"]
        or "audit_tool_sources_changed_during_validation" in report["ineligibility_reasons"]
        or any(not r["source_verified"] for r in report["staging"])
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
