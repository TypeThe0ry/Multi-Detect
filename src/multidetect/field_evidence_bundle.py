"""Join and verify the six strata of a field evidence session.

The field workflow produces several deliberately independent artifacts: a
read-only link preflight, a strict GNSS observer report, a DET/TRK/LCK/TGT
session capture, a physical camera-mount/lever-arm survey, a target-event media
bundle, a raw replay recording with its telemetry sidecar, and an independent
WGS-84 accuracy report.  Each artifact
has a useful standalone verifier, but checking them one at a time leaves a
hole where a frame, GPS sample, target event and truth row can come from
different sessions.  This module is the narrow, read-only join boundary.

No file is modified, no network socket is opened, and no flight-control value
is emitted.  The result is a deterministic, hash-bound diagnostic document.
``gate_passed`` means that the seven evidence strata and their cross-bindings
are internally complete; it never changes the REAL/HIL release status.  The
CLI therefore keeps ``release_eligible`` false even for a complete bundle.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .adaptive_ranging import assess_gps_quality
from .autonomy.serialization import AutonomyContractError, canonical_sha256
from .backends import RawReplayTelemetrySample, RawSensorReplayRecording
from .camera_mount_survey import verify_camera_mount_survey
from .field_link_preflight import verify_field_link_report
from .field_session_capture import verify_field_session_capture
from .geolocation_accuracy import verify_geolocation_accuracy_report
from .gnss_capture import verify_gnss_capture_report
from .mavlink_gps import GNSS_CANONICAL_MAX_GPS_AGE_S
from .product_version import PRODUCT_VERSION
from .target_event_replay import load_target_event_replay_manifest

FIELD_EVIDENCE_SCHEMA_VERSION = 1
FIELD_EVIDENCE_EVIDENCE_TYPE = "multidetect.field-evidence-bundle.v1"
FIELD_EVIDENCE_STRATA = (
    "field_link",
    "gnss",
    "execution",
    "camera_mount",
    "target_event",
    "media_timeline",
    "accuracy",
)
DEFAULT_MAXIMUM_TELEMETRY_AGE_S = 1.0
_SHA256_LENGTH = 64
_FIELD_EVIDENCE_REPORT_KEYS = frozenset(
    {
        "event",
        "schema_version",
        "evidence_type",
        "release_label",
        "bundle_id",
        "policy",
        "inputs",
        "strata",
        "checks",
        "bindings",
        "blocking_reasons",
        "next_actions",
        "gate_passed",
        "release_eligible",
        "hardware_acceptance",
        "real_flight_acceptance",
        "software_only",
        "read_only_evidence",
        "advisory_only",
        "ground_truth_isolated",
        "contains_flight_control",
        "contains_truth",
    }
)
_FIELD_EVIDENCE_REQUIRED_KEYS = frozenset(
    {
        "event",
        "schema_version",
        "evidence_type",
        "release_label",
        "bundle_id",
        "inputs",
        "strata",
        "checks",
        "bindings",
        "blocking_reasons",
        "next_actions",
        "gate_passed",
        "release_eligible",
        "hardware_acceptance",
        "real_flight_acceptance",
        "software_only",
        "read_only_evidence",
        "advisory_only",
        "ground_truth_isolated",
        "contains_flight_control",
        "contains_truth",
    }
)

_INPUT_NAMES = (
    "field_session_report",
    "gnss_capture_report",
    "target_event_manifest",
    "recording_manifest",
    "telemetry_sidecar",
    "link_preflight",
    "camera_mount_survey",
    "accuracy_report",
)
_STRATUM_INPUTS: dict[str, tuple[str, ...]] = {
    "field_link": ("link_preflight",),
    "gnss": ("gnss_capture_report",),
    "execution": ("field_session_report", "link_preflight"),
    "camera_mount": ("camera_mount_survey",),
    "target_event": ("target_event_manifest",),
    "media_timeline": (
        "recording_manifest",
        "telemetry_sidecar",
        "target_event_manifest",
    ),
    "accuracy": ("accuracy_report", "target_event_manifest"),
}
_NEXT_ACTIONS = {
    "field_link": "Run the explicit read-only field-link preflight and bind its JSON report.",
    "gnss": (
        "Capture GPS_RAW_INT/GPS2_RAW with the receive-only GNSS observer and "
        "verify its quality gate."
    ),
    "execution": "Capture a strict DET→TRK→LCK→TGT session with cancel and reacquire evidence.",
    "camera_mount": (
        "Measure the installed camera boresight and GNSS-to-camera lever arm, "
        "retain the raw notes, and obtain independent review."
    ),
    "target_event": (
        "Create a hash-bound target-event replay containing qualified GPS, RANGE_STATUS and media."
    ),
    "media_timeline": (
        "Join the raw recording and telemetry sidecar, then bind target event "
        "frame IDs and timestamps."
    ),
    "accuracy": (
        "Survey every target event independently and generate a replay-derived "
        "WGS-84 accuracy report."
    ),
}


def check_field_evidence_bundle(
    *,
    field_session_report: str | Path | None = None,
    gnss_capture_report: str | Path | None = None,
    target_event_manifest: str | Path | None = None,
    recording_manifest: str | Path | None = None,
    telemetry_sidecar: str | Path | None = None,
    link_preflight: str | Path | None = None,
    camera_mount_survey: str | Path | None = None,
    accuracy_report: str | Path | None = None,
    maximum_telemetry_age_s: float = DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
    require_real_domain: bool = True,
) -> dict[str, Any]:
    """Verify and cross-bind one field evidence set without writing files.

    Every argument is optional so a report can be generated during bring-up
    before the aircraft is connected.  Missing inputs close only the affected
    strata and produce concrete ``blocking_reasons``/``next_actions``.  A
    complete set still remains an advisory evidence check, not a release or
    hardware acceptance claim.
    """

    if (
        isinstance(maximum_telemetry_age_s, bool)
        or not isinstance(maximum_telemetry_age_s, (int, float))
        or not math.isfinite(float(maximum_telemetry_age_s))
        or float(maximum_telemetry_age_s) < 0.0
        or float(maximum_telemetry_age_s) > GNSS_CANONICAL_MAX_GPS_AGE_S
    ):
        raise ValueError(
            "maximum_telemetry_age_s must be finite, non-negative, and at most "
            f"{GNSS_CANONICAL_MAX_GPS_AGE_S:g}"
        )
    if type(require_real_domain) is not bool:
        raise ValueError("require_real_domain must be a boolean")
    maximum_age = float(maximum_telemetry_age_s)

    values: dict[str, str | Path | None] = {
        "field_session_report": field_session_report,
        "gnss_capture_report": gnss_capture_report,
        "target_event_manifest": target_event_manifest,
        "recording_manifest": recording_manifest,
        "telemetry_sidecar": telemetry_sidecar,
        "link_preflight": link_preflight,
        "camera_mount_survey": camera_mount_survey,
        "accuracy_report": accuracy_report,
    }
    inputs = {name: _describe_input(values[name], name=name) for name in _INPUT_NAMES}
    blocking_reasons: list[str] = []

    def add_blocking(reason: str) -> None:
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)

    strata: dict[str, dict[str, Any]] = {
        name: _new_stratum(name, _STRATUM_INPUTS[name]) for name in FIELD_EVIDENCE_STRATA
    }

    def fail(stratum: str, reason: str) -> None:
        _add_reason(strata[stratum], reason)
        add_blocking(f"{stratum}: {reason}")

    def require_inputs(stratum: str) -> bool:
        ready = True
        for input_name in _STRATUM_INPUTS[stratum]:
            reference = inputs[input_name]
            if reference["present"] is not True:
                ready = False
                detail = reference.get("error")
                reason = f"{input_name} artifact is unavailable"
                if isinstance(detail, str) and detail:
                    reason += f": {detail}"
                fail(stratum, reason)
        strata[stratum]["present"] = ready
        return ready

    context: dict[str, Any] = {}

    # 1. Explicit link preflight.  This remains a passive verifier and does
    # not retry a socket probe.
    if require_inputs("field_link"):
        try:
            link_verification = verify_field_link_report(
                inputs["link_preflight"]["path"],  # type: ignore[arg-type]
                require_ready=True,
            )
            strata["field_link"]["verification"] = link_verification
            if link_verification.get("gate_passed") is not True:
                _extend_reasons(strata["field_link"], link_verification.get("reasons"))
            if link_verification.get("hardware_link_ready") is not True:
                fail("field_link", "hardware_link_ready is not true")
            if link_verification.get("gate_passed") is not True:
                fail("field_link", "field-link verifier gate_passed is not true")
        except Exception as exc:  # noqa: BLE001 - malformed evidence is a report result
            fail("field_link", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["field_link"]["reasons"]:
                strata["field_link"]["gate_passed"] = True
    _finalize_stratum(strata["field_link"])

    # 2. Receive-only raw GNSS report.
    if require_inputs("gnss"):
        try:
            gnss_document = _load_json_object(inputs["gnss_capture_report"]["path"])
            gnss_verification = verify_gnss_capture_report(
                gnss_document,
                require_gate_passed=True,
            )
            strata["gnss"]["verification"] = gnss_verification
            if gnss_verification.get("passed") is not True:
                _extend_reasons(strata["gnss"], gnss_verification.get("reasons"))
            records = gnss_document.get("records")
            summary = gnss_document.get("summary")
            raw_records = (
                [
                    item
                    for item in records
                    if isinstance(item, Mapping) and item.get("kind") == "gps_raw"
                ]
                if isinstance(records, list)
                else []
            )
            qualified_count = sum(item.get("qualified") is True for item in raw_records)
            if not raw_records:
                fail("gnss", "GNSS capture contains no GPS_RAW_INT/GPS2_RAW records")
            if qualified_count < 1:
                fail("gnss", "GNSS capture contains no qualified raw GPS sample")
            if not isinstance(summary, Mapping):
                fail("gnss", "GNSS capture summary is missing")
            elif summary.get("gps_raw_qualified_samples") != qualified_count:
                fail("gnss", "GNSS summary qualified raw sample count does not match records")
            if gnss_verification.get("gate_passed") is not True:
                fail("gnss", "GNSS capture verifier gate_passed is not true")
            strata["gnss"]["qualified_raw_gps_samples"] = qualified_count
            strata["gnss"]["raw_gps_samples"] = len(raw_records)
            context["gnss_document"] = gnss_document
        except Exception as exc:  # noqa: BLE001
            fail("gnss", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["gnss"]["reasons"]:
                strata["gnss"]["gate_passed"] = True
    _finalize_stratum(strata["gnss"])

    # 3. Strict operator execution/state evidence.  The capture report itself
    # is checked again, and its embedded link hash must agree with the
    # explicit link artifact above.
    if require_inputs("execution"):
        try:
            session_path = inputs["field_session_report"]["path"]
            session_document = _load_json_object(session_path)  # type: ignore[arg-type]
            session_verification = verify_field_session_capture(session_path)  # type: ignore[arg-type]
            strata["execution"]["verification"] = session_verification
            if session_verification.get("gate_passed") is not True:
                _extend_reasons(strata["execution"], session_verification.get("reasons"))
            capture = session_document.get("capture")
            validation = session_document.get("validation")
            if not isinstance(capture, Mapping):
                fail("execution", "field-session capture object is missing")
                capture = {}
            if not isinstance(validation, Mapping):
                fail("execution", "field-session validation object is missing")
                validation = {}
            if capture.get("require_target_state_evidence") is not True:
                fail("execution", "capture require_target_state_evidence must be true")
            target_state = validation.get("target_state_evidence")
            if not isinstance(target_state, Mapping) or target_state.get("valid") is not True:
                fail("execution", "strict TGT target_state_evidence.valid is not true")
            if validation.get("cancel_seen") is not True:
                fail("execution", "cancel_seen is not true")
            if validation.get("reacquired_after_cancel") is not True:
                fail("execution", "reacquired_after_cancel is not true")
            if session_verification.get("gate_passed") is not True:
                fail("execution", "field-session verifier gate_passed is not true")

            embedded_link = session_document.get("link_preflight")
            if not isinstance(embedded_link, Mapping):
                fail("execution", "field-session report is not bound to link_preflight")
            else:
                _check_embedded_link_binding(
                    embedded_link,
                    report_directory=Path(session_path).parent,  # type: ignore[arg-type]
                    explicit_link=inputs["link_preflight"],
                    fail=lambda reason: fail("execution", reason),
                )
            capture_output = _resolve_report_path(
                capture.get("output_path"),
                report_directory=Path(session_path).parent,  # type: ignore[arg-type]
            )
            if capture_output is not None and capture_output.is_file():
                captured_records = _load_jsonl(capture_output)
                session_target_ids = sorted(_collect_target_ids(captured_records))
                strata["execution"]["captured_records"] = len(captured_records)
                strata["execution"]["target_ids"] = session_target_ids
                context["session_target_ids"] = tuple(session_target_ids)
            else:
                fail("execution", "field-session captured output JSONL is unavailable")
            session_id = capture.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                strata["execution"]["session_id"] = session_id.strip()
            context["session_document"] = session_document
        except Exception as exc:  # noqa: BLE001
            fail("execution", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["execution"]["reasons"]:
                strata["execution"]["gate_passed"] = True
    _finalize_stratum(strata["execution"])

    # 4. Independently reviewed physical camera-mount and GNSS-to-camera
    # lever-arm survey.  Nominal or simulated values cannot satisfy this
    # stratum, even when the numeric calibration happens to match.
    if require_inputs("camera_mount"):
        try:
            mount_document = _load_json_object(inputs["camera_mount_survey"]["path"])
            mount_verification = verify_camera_mount_survey(
                mount_document,
                expected_release_label=PRODUCT_VERSION.release_label,
                require_gate_passed=True,
            )
            strata["camera_mount"]["verification"] = mount_verification
            if mount_verification.get("passed") is not True:
                _extend_reasons(strata["camera_mount"], mount_verification.get("reasons"))
            if mount_verification.get("gate_passed") is not True:
                fail("camera_mount", "camera-mount survey verifier gate_passed is not true")
            if mount_document.get("mount_survey_acceptance") != "passed":
                fail("camera_mount", "mount_survey_acceptance is not passed")
            if mount_document.get("simulated_values_used") is not False:
                fail("camera_mount", "simulated_values_used must be false")
            if mount_document.get("hardware_acceptance") != "not_claimed":
                fail("camera_mount", "camera-mount survey claims hardware acceptance")
            if mount_document.get("release_eligible") is not False:
                fail("camera_mount", "camera-mount survey claims release eligibility")
            estimate = mount_document.get("estimate")
            if not isinstance(estimate, Mapping):
                fail("camera_mount", "camera-mount survey estimate is missing")
            else:
                strata["camera_mount"].update(
                    {
                        "survey_id": mount_document.get("survey_id"),
                        "aircraft_id": mount_document.get("aircraft_id"),
                        "camera_id": mount_document.get("camera_id"),
                        "gnss_antenna_id": mount_document.get("gnss_antenna_id"),
                        "calibration_id": estimate.get("calibration_id"),
                    }
                )
            context["camera_mount_document"] = mount_document
        except Exception as exc:  # noqa: BLE001
            fail("camera_mount", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["camera_mount"]["reasons"]:
                strata["camera_mount"]["gate_passed"] = True
    _finalize_stratum(strata["camera_mount"])

    # 5. Hash-bound target-event replay with strict GPS and RANGE_STATUS.
    if require_inputs("target_event"):
        try:
            target_bundle = load_target_event_replay_manifest(
                inputs["target_event_manifest"]["path"]  # type: ignore[arg-type]
            )
            context["target_bundle"] = target_bundle
            if target_bundle.camera_calibration_sha256 is None:
                fail("target_event", "target-event replay camera calibration is missing")
            event_ids = [event.event_id for event in target_bundle.events]
            target_ids = sorted({event.target_id for event in target_bundle.events})
            if not event_ids:
                fail("target_event", "target-event replay contains no events")
            for event in target_bundle.events:
                if not event.gps.qualified:
                    fail("target_event", f"event {event.event_id} GPS is not qualified")
                if event.gps_freshness != "fresh":
                    fail("target_event", f"event {event.event_id} GPS freshness is not fresh")
                age = event.gps_age_s
                if age is None or age > GNSS_CANONICAL_MAX_GPS_AGE_S:
                    fail("target_event", f"event {event.event_id} GPS age exceeds canonical limit")
                if event.ranging.validity not in {"VALID", "DEGRADED"}:
                    fail("target_event", f"event {event.event_id} RANGE_STATUS is unavailable")
                if event.ranging.range_m is None or event.ranging.range_m <= 0.0:
                    fail("target_event", f"event {event.event_id} range_m is not positive")
                if event.video is None and event.depth is None:
                    fail("target_event", f"event {event.event_id} has no media reference")
            strata["target_event"].update(
                {
                    "bundle_id": target_bundle.bundle_id,
                    "session_id": target_bundle.session_id,
                    "event_count": target_bundle.event_count,
                    "event_ids": event_ids,
                    "target_ids": target_ids,
                    "media_count": target_bundle.media_count,
                    "camera_calibration_id": target_bundle.camera_calibration_id,
                    "camera_calibration_sha256": target_bundle.camera_calibration_sha256,
                }
            )
        except Exception as exc:  # noqa: BLE001
            fail("target_event", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["target_event"]["reasons"]:
                strata["target_event"]["gate_passed"] = True
    _finalize_stratum(strata["target_event"])

    # 6. Raw frame/telemetry timeline and target-event frame binding.
    if require_inputs("media_timeline"):
        try:
            recording = RawSensorReplayRecording.from_manifest(
                inputs["recording_manifest"]["path"]  # type: ignore[arg-type]
            )
            sidecar_samples, sidecar_mappings = _load_telemetry_sidecar(
                inputs["telemetry_sidecar"]["path"]  # type: ignore[arg-type]
            )
            context["recording"] = recording
            context["sidecar_samples"] = sidecar_samples
            if len(sidecar_samples) != len(recording.telemetry_samples):
                fail(
                    "media_timeline",
                    "telemetry sidecar sample count does not match recording telemetry",
                )
            else:
                for index, (sample, expected) in enumerate(
                    zip(sidecar_mappings, recording.telemetry_samples, strict=True)
                ):
                    if canonical_sha256(sample) != canonical_sha256(expected.to_mapping()):
                        fail(
                            "media_timeline",
                            f"telemetry sidecar sample {index} differs from recording",
                        )
                        break

            telemetry_times = tuple(sample.observed_at_s for sample in sidecar_samples)
            frame_ages: list[float] = []
            frame_assessments = []
            frame_sample_indices: list[int] = []
            for frame in recording.frames:
                index = bisect.bisect_right(telemetry_times, frame.captured_at_s) - 1
                if index < 0:
                    fail(
                        "media_timeline",
                        f"frame {frame.frame_id} has no telemetry at or before its timestamp",
                    )
                    continue
                age = frame.captured_at_s - telemetry_times[index]
                frame_ages.append(age)
                frame_sample_indices.append(index)
                if age > maximum_age:
                    fail(
                        "media_timeline",
                        f"frame {frame.frame_id} telemetry age {age:.6f}s exceeds "
                        f"{maximum_age:.6f}s",
                    )
                assessment = assess_gps_quality(
                    sidecar_samples[index].telemetry,
                    now_s=frame.captured_at_s,
                )
                frame_assessments.append(assessment)
                if assessment.qualified is not True:
                    fail(
                        "media_timeline",
                        f"frame {frame.frame_id} GPS quality is {assessment.reason}",
                    )
            if not recording.frames:
                fail("media_timeline", "raw recording contains no frames")
            if not sidecar_samples:
                fail("media_timeline", "telemetry sidecar contains no samples")
            elif not frame_assessments:
                fail("media_timeline", "no frame-to-telemetry bindings were produced")

            frame_by_id = {frame.frame_id: frame for frame in recording.frames}
            target_bundle = context.get("target_bundle")
            target_bindings: list[dict[str, Any]] = []
            if target_bundle is None:
                fail("media_timeline", "target-event replay is unavailable for frame binding")
            else:
                for event in target_bundle.events:
                    candidates = [event.frame_id]
                    if event.source_frame_id is not None:
                        candidates.append(event.source_frame_id)
                    matched = [frame_by_id[value] for value in candidates if value in frame_by_id]
                    if not matched:
                        fail(
                            "media_timeline",
                            f"target event {event.event_id} does not reference a recording frame",
                        )
                        continue
                    if len({frame.frame_id for frame in matched}) != 1:
                        fail(
                            "media_timeline",
                            f"target event {event.event_id} frame_id/source_frame_id disagree",
                        )
                        continue
                    frame = matched[0]
                    if not math.isclose(
                        event.captured_at_s,
                        frame.captured_at_s,
                        rel_tol=0.0,
                        abs_tol=1.0e-6,
                    ):
                        fail(
                            "media_timeline",
                            f"target event {event.event_id} timestamp does not match "
                            f"frame {frame.frame_id}",
                        )
                    index = bisect.bisect_right(telemetry_times, frame.captured_at_s) - 1
                    qualified = (
                        index >= 0
                        and assess_gps_quality(
                            sidecar_samples[index].telemetry,
                            now_s=frame.captured_at_s,
                        ).qualified
                    )
                    if event.gps.qualified and not qualified:
                        fail(
                            "media_timeline",
                            f"qualified target event {event.event_id} is backed by "
                            "unqualified frame telemetry",
                        )
                    target_bindings.append(
                        {
                            "event_id": event.event_id,
                            "frame_id": frame.frame_id,
                            "captured_at_s": event.captured_at_s,
                            "telemetry_index": index,
                            "gps_qualified": qualified,
                        }
                    )
            scenario_bound = recording.scenario_id is not None
            if scenario_bound and require_real_domain:
                fail(
                    "media_timeline",
                    "recording is scenario-bound; use --allow-sim-domain only for "
                    "diagnostic SIM checks",
                )
            strata["media_timeline"].update(
                {
                    "recording_id": recording.recording_id,
                    "recording_hash": recording.recording_hash,
                    "frame_count": len(recording.frames),
                    "telemetry_sample_count": len(recording.telemetry_samples),
                    "scenario_id": recording.scenario_id,
                    "scenario_bound": scenario_bound,
                    "maximum_observed_frame_telemetry_age_s": max(frame_ages)
                    if frame_ages
                    else None,
                    "gps_qualified_frame_count": sum(
                        assessment.qualified for assessment in frame_assessments
                    ),
                    "target_bindings": target_bindings,
                }
            )
        except Exception as exc:  # noqa: BLE001
            fail("media_timeline", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["media_timeline"]["reasons"]:
                strata["media_timeline"]["gate_passed"] = True
    _finalize_stratum(strata["media_timeline"])

    # 7. Independent WGS-84 accuracy report must name this exact target-event
    # manifest and conserve every selected event/truth row.
    if require_inputs("accuracy"):
        try:
            accuracy_document = _load_json_object(inputs["accuracy_report"]["path"])
            accuracy_verification = verify_geolocation_accuracy_report(
                accuracy_document,
                require_gate_passed=True,
            )
            strata["accuracy"]["verification"] = accuracy_verification
            if accuracy_verification.get("passed") is not True:
                _extend_reasons(strata["accuracy"], accuracy_verification.get("reasons"))
            selection = accuracy_document.get("selection")
            measurement = accuracy_document.get("measurement")
            target_bundle = context.get("target_bundle")
            if not isinstance(selection, Mapping):
                fail("accuracy", "accuracy report selection provenance is missing")
            else:
                target_hash = inputs["target_event_manifest"].get("sha256")
                if selection.get("source_manifest_sha256") != target_hash:
                    fail(
                        "accuracy",
                        "accuracy selection source manifest hash does not match "
                        "target-event manifest",
                    )
                if selection.get("source_manifest_bytes") != inputs["target_event_manifest"].get(
                    "bytes"
                ):
                    fail(
                        "accuracy",
                        "accuracy selection source manifest byte count does not match "
                        "target-event manifest",
                    )
                if selection.get("allow_unmatched_events") is not False:
                    fail("accuracy", "accuracy selection allow_unmatched_events must be false")
                if selection.get("allow_extra_truth") is not False:
                    fail("accuracy", "accuracy selection allow_extra_truth must be false")
                if selection.get("unmatched_event_ids") != []:
                    fail("accuracy", "accuracy selection contains unmatched event IDs")
                if selection.get("unused_truth_ids") != []:
                    fail("accuracy", "accuracy selection contains unused truth IDs")
                if not isinstance(measurement, Mapping):
                    fail("accuracy", "accuracy report measurement is missing")
                else:
                    if selection.get("matched_event_count") != measurement.get("sample_count"):
                        fail(
                            "accuracy",
                            "accuracy matched_event_count does not match measurement sample_count",
                        )
                if target_bundle is not None:
                    event_ids = {event.event_id for event in target_bundle.events}
                    samples = accuracy_document.get("samples")
                    sample_ids = (
                        {item.get("sample_id") for item in samples if isinstance(item, Mapping)}
                        if isinstance(samples, list)
                        else set()
                    )
                    if sample_ids != event_ids:
                        fail(
                            "accuracy",
                            "accuracy sample IDs do not cover exactly the target-event IDs",
                        )
                    target_ids = {event.target_id for event in target_bundle.events}
                    selected_target_id = selection.get("target_id")
                    if (
                        len(target_ids) == 1
                        and selected_target_id is not None
                        and selected_target_id not in target_ids
                    ):
                        fail(
                            "accuracy", "accuracy selection target_id does not match target events"
                        )
                    if len(target_ids) > 1 and selected_target_id is None:
                        fail(
                            "accuracy",
                            "accuracy selection target_id is required for multiple target IDs",
                        )
            if accuracy_verification.get("gate_passed") is not True:
                fail("accuracy", "accuracy verifier gate_passed is not true")
            if isinstance(measurement, Mapping):
                strata["accuracy"]["sample_count"] = measurement.get("sample_count")
            context["accuracy_document"] = accuracy_document
        except Exception as exc:  # noqa: BLE001
            fail("accuracy", f"verification error: {type(exc).__name__}: {exc}")
        else:
            if not strata["accuracy"]["reasons"]:
                strata["accuracy"]["gate_passed"] = True
    _finalize_stratum(strata["accuracy"])

    # Cross-bind the independent session and target bundle IDs when the
    # session report explicitly chose one.  A capture with session_id=null is
    # intentionally allowed to select all observed sessions.
    bindings: dict[str, Any] = {
        "input_sha256": {name: inputs[name].get("sha256") for name in _INPUT_NAMES},
        "input_bytes": {name: inputs[name].get("bytes", 0) for name in _INPUT_NAMES},
        "maximum_telemetry_age_s": maximum_age,
        "require_real_domain": require_real_domain,
    }
    cross_binding_reasons: list[str] = []
    session_document = context.get("session_document")
    target_bundle = context.get("target_bundle")
    if isinstance(session_document, Mapping) and target_bundle is not None:
        capture = session_document.get("capture")
        if isinstance(capture, Mapping):
            session_id = capture.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                if session_id.strip() != target_bundle.session_id:
                    cross_binding_reasons.append(
                        "explicit field-session session_id does not match target-event session_id"
                    )
    session_target_ids = context.get("session_target_ids")
    if session_target_ids and target_bundle is not None:
        event_target_ids = {event.target_id for event in target_bundle.events}
        if set(session_target_ids).isdisjoint(event_target_ids):
            cross_binding_reasons.append(
                "field-session target IDs do not match target-event target IDs"
            )
    if target_bundle is not None:
        bindings.update(
            {
                "target_event_bundle_id": target_bundle.bundle_id,
                "target_event_session_id": target_bundle.session_id,
                "target_event_ids": [event.event_id for event in target_bundle.events],
                "target_ids": sorted({event.target_id for event in target_bundle.events}),
                "target_event_camera_calibration_id": target_bundle.camera_calibration_id,
                "target_event_camera_calibration_sha256": (target_bundle.camera_calibration_sha256),
            }
        )
    recording = context.get("recording")
    if recording is not None:
        bindings.update(
            {
                "recording_id": recording.recording_id,
                "recording_hash": recording.recording_hash,
                "recording_manifest_sha256": inputs["recording_manifest"].get("sha256"),
            }
        )
    mount_document = context.get("camera_mount_document")
    if isinstance(mount_document, Mapping):
        mount_inputs = mount_document.get("inputs")
        calibration_reference = (
            mount_inputs.get("camera_calibration") if isinstance(mount_inputs, Mapping) else None
        )
        bindings.update(
            {
                "camera_mount_survey_id": mount_document.get("survey_id"),
                "camera_mount_aircraft_id": mount_document.get("aircraft_id"),
                "camera_mount_camera_id": mount_document.get("camera_id"),
                "camera_mount_gnss_antenna_id": mount_document.get("gnss_antenna_id"),
                "camera_calibration_sha256": (
                    calibration_reference.get("sha256")
                    if isinstance(calibration_reference, Mapping)
                    else None
                ),
            }
        )
        if target_bundle is not None:
            mount_calibration_hash = (
                calibration_reference.get("sha256")
                if isinstance(calibration_reference, Mapping)
                else None
            )
            mount_estimate = mount_document.get("estimate")
            mount_calibration_id = (
                mount_estimate.get("calibration_id")
                if isinstance(mount_estimate, Mapping)
                else None
            )
            if target_bundle.camera_calibration_sha256 != mount_calibration_hash:
                cross_binding_reasons.append(
                    "target-event camera calibration hash does not match mount survey"
                )
            if target_bundle.camera_calibration_id != mount_calibration_id:
                cross_binding_reasons.append(
                    "target-event camera calibration ID does not match mount survey"
                )
    if cross_binding_reasons:
        for reason in cross_binding_reasons:
            add_blocking(f"cross_binding: {reason}")

    # Verifier-specific reasons are kept at stratum level for diagnosis, but
    # the top-level gate must expose every one of them as a blocking reason as
    # well.  This prevents a generic ``gate_passed=false`` summary from hiding
    # the exact stale/hash/schema defect that needs to be fixed.
    for stratum_name in FIELD_EVIDENCE_STRATA:
        for reason in strata[stratum_name].get("reasons", []):
            add_blocking(f"{stratum_name}: {reason}")

    all_inputs_present = all(inputs[name]["present"] is True for name in _INPUT_NAMES)
    all_strata_passed = all(
        strata[name].get("gate_passed") is True for name in FIELD_EVIDENCE_STRATA
    )
    cross_bindings_passed = not cross_binding_reasons
    checks = {
        "all_inputs_present": all_inputs_present,
        "all_strata_passed": all_strata_passed,
        "cross_bindings_passed": cross_bindings_passed,
        "hashes_complete": all(
            isinstance(inputs[name].get("sha256"), str) and _is_sha256(inputs[name].get("sha256"))
            for name in _INPUT_NAMES
        ),
        "read_only_inputs": True,
        "flight_control_output_present": False,
        "real_domain_required": require_real_domain,
    }
    software_only = bool(recording is not None and recording.scenario_id is not None) or any(
        isinstance(context.get(name), Mapping) and context[name].get("software_only") is True
        for name in ("gnss_document", "accuracy_document")
    )
    bundle_identity = {
        "schema_version": FIELD_EVIDENCE_SCHEMA_VERSION,
        "release_label": PRODUCT_VERSION.release_label,
        "inputs": {name: inputs[name].get("sha256") for name in _INPUT_NAMES},
        "policy": {
            "maximum_telemetry_age_s": maximum_age,
            "require_real_domain": require_real_domain,
        },
    }
    bundle_id = f"field-evidence-{canonical_sha256(bundle_identity)[:24]}"
    next_actions = [
        _NEXT_ACTIONS[name]
        for name in FIELD_EVIDENCE_STRATA
        if strata[name].get("gate_passed") is not True
    ]
    gate_passed = all_inputs_present and all_strata_passed and cross_bindings_passed
    return {
        "event": "field_evidence_bundle_checked",
        "schema_version": FIELD_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": FIELD_EVIDENCE_EVIDENCE_TYPE,
        "release_label": PRODUCT_VERSION.release_label,
        "bundle_id": bundle_id,
        "policy": {
            "maximum_telemetry_age_s": maximum_age,
            "require_real_domain": require_real_domain,
        },
        "inputs": inputs,
        "strata": strata,
        "checks": checks,
        "bindings": bindings,
        "blocking_reasons": blocking_reasons,
        "next_actions": next_actions,
        "gate_passed": gate_passed,
        "release_eligible": False,
        "hardware_acceptance": "not_claimed",
        "real_flight_acceptance": "not_claimed",
        "software_only": software_only,
        "read_only_evidence": True,
        "advisory_only": True,
        "ground_truth_isolated": True,
        "contains_flight_control": False,
        "contains_truth": True,
    }


def verify_field_evidence_bundle_report(
    report_path: str | Path,
    *,
    require_gate_passed: bool = False,
    require_real_domain: bool | None = None,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Re-open and independently re-evaluate a serialized field bundle.

    A release manifest binds the bundle JSON by SHA-256, but the JSON also
    contains a compact summary that could be edited without changing the
    referenced source artifacts.  This verifier therefore reads every input
    path recorded in the bundle and runs :func:`check_field_evidence_bundle`
    again.  The serialized summary is compared with the recomputed result so
    a copied ``gate_passed`` bit, stale hash map or edited blocker cannot pass
    the release boundary.

    ``passed`` describes the integrity of the report and its re-evaluation;
    ``gate_passed`` remains the actual six-strata field gate.  Consequently a
    valid diagnostic report with missing field artifacts can have
    ``passed=True`` and ``gate_passed=False``.  ``require_gate_passed`` turns
    that diagnostic into a failed verification for callers that need a
    release-ready field bundle.
    """

    result: dict[str, Any] = {
        "event": "field_evidence_bundle_report_verified",
        "schema_version": FIELD_EVIDENCE_SCHEMA_VERSION,
        "report_path": None,
        "report_sha256": None,
        "report_bytes": None,
        "gate_passed": False,
        "passed": False,
        "reasons": [],
        "report": None,
        "verification": None,
    }
    reasons: list[str] = result["reasons"]

    requested = Path(report_path).expanduser()
    if requested.is_symlink():
        reasons.append("field evidence report must not be a symbolic link")
    try:
        source = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        reasons.append(f"field evidence report path cannot be resolved: {exc}")
        source = None
    if source is not None and not source.is_file():
        reasons.append("field evidence report must be a regular file")
        source = None

    document: Mapping[str, object] | None = None
    if source is not None:
        try:
            payload = source.read_bytes()
            actual_sha = hashlib.sha256(payload).hexdigest()
            result.update(
                {
                    "report_path": str(source),
                    "report_sha256": actual_sha,
                    "report_bytes": len(payload),
                }
            )
        except OSError as exc:
            reasons.append(f"field evidence report cannot be read: {exc}")
        try:
            document = _load_json_object(source)
        except (AutonomyContractError, OSError, UnicodeError, ValueError, TypeError) as exc:
            reasons.append(f"field evidence report JSON is invalid: {exc}")

    if document is None:
        if require_gate_passed:
            reasons.append("field evidence report gate_passed cannot be verified")
        return result

    result["report"] = dict(document)
    unknown = sorted(set(document).difference(_FIELD_EVIDENCE_REPORT_KEYS))
    missing = sorted(_FIELD_EVIDENCE_REQUIRED_KEYS.difference(document))
    if unknown:
        reasons.append(f"unknown field evidence report field(s): {unknown!r}")
    if missing:
        reasons.append(f"missing field evidence report field(s): {missing!r}")

    expected_contract: dict[str, object] = {
        "event": "field_evidence_bundle_checked",
        "schema_version": FIELD_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": FIELD_EVIDENCE_EVIDENCE_TYPE,
        "release_eligible": False,
        "hardware_acceptance": "not_claimed",
        "real_flight_acceptance": "not_claimed",
        "read_only_evidence": True,
        "advisory_only": True,
        "ground_truth_isolated": True,
        "contains_flight_control": False,
        "contains_truth": True,
    }
    for field, expected in expected_contract.items():
        if document.get(field) != expected:
            reasons.append(f"field evidence report {field} does not match verifier contract")
    report_release_label = document.get("release_label")
    if not isinstance(report_release_label, str) or not report_release_label.strip():
        reasons.append("field evidence report release_label is missing or empty")
    elif expected_release_label is not None and report_release_label != expected_release_label:
        reasons.append("field evidence report release_label does not match the product manifest")

    report_gate = document.get("gate_passed")
    if type(report_gate) is not bool:
        reasons.append("field evidence report gate_passed must be a boolean")
    result["gate_passed"] = report_gate if type(report_gate) is bool else False

    policy = document.get("policy")
    checks = document.get("checks")
    if policy is not None and not isinstance(policy, Mapping):
        reasons.append("field evidence report policy must be an object")
        policy = None
    if not isinstance(checks, Mapping):
        reasons.append("field evidence report checks must be an object")
        checks = None

    maximum_age: object = DEFAULT_MAXIMUM_TELEMETRY_AGE_S
    policy_real_domain: object = None
    if isinstance(policy, Mapping):
        maximum_age = policy.get("maximum_telemetry_age_s", DEFAULT_MAXIMUM_TELEMETRY_AGE_S)
        policy_real_domain = policy.get("require_real_domain")
        if type(policy_real_domain) is not bool:
            reasons.append("field evidence report policy.require_real_domain must be a boolean")
    elif isinstance(checks, Mapping):
        policy_real_domain = checks.get("real_domain_required")
    if type(policy_real_domain) is not bool:
        reasons.append("field evidence report real-domain policy is missing or invalid")
        policy_real_domain = True
    if require_real_domain is not None:
        if type(require_real_domain) is not bool:
            reasons.append("require_real_domain must be a boolean when supplied")
        elif policy_real_domain is not require_real_domain:
            reasons.append(
                "field evidence report real-domain policy does not match the requested policy"
            )
    effective_real_domain = bool(policy_real_domain)

    raw_inputs = document.get("inputs")
    input_paths: dict[str, str | Path | None] = {}
    if not isinstance(raw_inputs, Mapping):
        reasons.append("field evidence report inputs must be an object")
        raw_inputs = {}
    for input_name in _INPUT_NAMES:
        entry = raw_inputs.get(input_name)
        if not isinstance(entry, Mapping):
            reasons.append(f"field evidence report input {input_name} must be an object")
            input_paths[input_name] = None
            continue
        if entry.get("name") != input_name:
            reasons.append(f"field evidence report input {input_name} name does not match")
        present = entry.get("present")
        if type(present) is not bool:
            reasons.append(f"field evidence report input {input_name}.present must be a boolean")
            present = False
        value = entry.get("path")
        if value is None:
            input_paths[input_name] = None
            if present:
                reasons.append(f"field evidence report input {input_name}.path is missing")
            continue
        if not isinstance(value, str) or not value.strip():
            input_paths[input_name] = None
            reasons.append(f"field evidence report input {input_name}.path must be text or null")
            continue

        # Preserve a serialized path even when the original report marked the
        # input as absent.  Bring-up reports commonly record the expected
        # future artifact path before the aircraft is connected; passing that
        # path back through ``check_field_evidence_bundle`` reproduces the
        # original ``path is not a regular file`` descriptor exactly.  If the
        # file appears later, re-evaluation flips ``present`` to true and the
        # comparison fails closed until a new report is written.
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            reasons.append(f"field evidence report input {input_name}.path must be absolute")
        input_paths[input_name] = candidate

    try:
        recomputed = check_field_evidence_bundle(
            field_session_report=input_paths["field_session_report"],
            gnss_capture_report=input_paths["gnss_capture_report"],
            target_event_manifest=input_paths["target_event_manifest"],
            recording_manifest=input_paths["recording_manifest"],
            telemetry_sidecar=input_paths["telemetry_sidecar"],
            link_preflight=input_paths["link_preflight"],
            camera_mount_survey=input_paths["camera_mount_survey"],
            accuracy_report=input_paths["accuracy_report"],
            maximum_telemetry_age_s=maximum_age,  # type: ignore[arg-type]
            require_real_domain=effective_real_domain,
        )
    except Exception as exc:  # noqa: BLE001 - malformed evidence is a verification result
        reasons.append(f"field evidence bundle re-evaluation failed: {type(exc).__name__}: {exc}")
    else:
        result["verification"] = recomputed
        result["gate_passed"] = recomputed.get("gate_passed") is True

        # Compare every deterministic output section.  The optional policy
        # key is skipped for historical reports written before policy was
        # promoted to a first-class field.
        comparable_keys = (
            "event",
            "schema_version",
            "evidence_type",
            "release_label",
            "bundle_id",
            "inputs",
            "strata",
            "checks",
            "bindings",
            "blocking_reasons",
            "next_actions",
            "gate_passed",
            "release_eligible",
            "hardware_acceptance",
            "real_flight_acceptance",
            "software_only",
            "read_only_evidence",
            "advisory_only",
            "ground_truth_isolated",
            "contains_flight_control",
            "contains_truth",
        )
        for key in comparable_keys:
            if document.get(key) != recomputed.get(key):
                reasons.append(f"field evidence report {key} does not match re-evaluation")
        if "policy" in document and document.get("policy") != recomputed.get("policy"):
            reasons.append("field evidence report policy does not match re-evaluation")

    if require_gate_passed and result["gate_passed"] is not True:
        reasons.append("field evidence bundle gate_passed is not true")
    result["passed"] = not reasons
    return result


def write_field_evidence_bundle(
    destination: str | Path,
    document: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write one bundle report without replacing it by default."""

    requested = Path(destination).expanduser()
    if requested.is_symlink():
        raise FileExistsError(f"field evidence output must not be a symbolic link: {requested}")
    output = requested.resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"field evidence output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check the read-only field-link, GNSS, execution, target-event, "
            "camera-mount, media timeline and WGS-84 accuracy evidence strata."
        )
    )
    parser.add_argument("--field-session-report", required=True, type=Path)
    parser.add_argument("--gnss-capture-report", required=True, type=Path)
    parser.add_argument("--target-event-manifest", required=True, type=Path)
    parser.add_argument("--recording-manifest", required=True, type=Path)
    parser.add_argument("--telemetry-sidecar", required=True, type=Path)
    parser.add_argument("--link-preflight", required=True, type=Path)
    parser.add_argument("--camera-mount-survey", required=True, type=Path)
    parser.add_argument("--accuracy-report", required=True, type=Path)
    parser.add_argument(
        "--maximum-telemetry-age-s",
        type=float,
        default=DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
        help="maximum frame-to-telemetry age (default: 1.0; canonical maximum: 1.5)",
    )
    parser.add_argument(
        "--allow-sim-domain",
        action="store_true",
        help="allow a scenario-bound recording for diagnostic SIM checks",
    )
    parser.add_argument(
        "--out", type=Path, help="write a new JSON report; existing files are never replaced"
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero while strata are pending; the report still records every blocker",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = check_field_evidence_bundle(
            field_session_report=args.field_session_report,
            gnss_capture_report=args.gnss_capture_report,
            target_event_manifest=args.target_event_manifest,
            recording_manifest=args.recording_manifest,
            telemetry_sidecar=args.telemetry_sidecar,
            link_preflight=args.link_preflight,
            camera_mount_survey=args.camera_mount_survey,
            accuracy_report=args.accuracy_report,
            maximum_telemetry_age_s=args.maximum_telemetry_age_s,
            require_real_domain=not args.allow_sim_domain,
        )
        if args.out is not None:
            write_field_evidence_bundle(args.out, report)
    except (AutonomyContractError, FileExistsError, OSError, TypeError, ValueError) as exc:
        print(f"field evidence bundle check failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
    if args.out is not None:
        print(f"report_written={Path(args.out).expanduser().resolve()}", file=sys.stderr)
    return 0 if report["gate_passed"] or args.report_only else 2


def _new_stratum(name: str, input_names: Sequence[str]) -> dict[str, Any]:
    return {
        "name": name,
        "input_names": list(input_names),
        "present": False,
        "gate_passed": False,
        "reasons": [],
    }


def _finalize_stratum(stratum: dict[str, Any]) -> None:
    # ``present`` is set by ``require_inputs`` from the actual file references;
    # do not infer it from the non-empty list of input names.
    stratum["present"] = bool(stratum.get("present"))
    stratum["reasons"] = _unique_texts(stratum.get("reasons"))
    if stratum["reasons"]:
        stratum["gate_passed"] = False


def _add_reason(stratum: dict[str, Any], reason: str) -> None:
    reasons = stratum.setdefault("reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _extend_reasons(stratum: dict[str, Any], reasons: object) -> None:
    if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes, bytearray)):
        for reason in reasons:
            if isinstance(reason, str) and reason.strip():
                _add_reason(stratum, reason.strip())
    elif isinstance(reasons, str) and reasons.strip():
        _add_reason(stratum, reasons.strip())


def _describe_input(value: str | Path | None, *, name: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "present": False,
        "path": None,
        "sha256": None,
        "bytes": 0,
    }
    if value is None:
        result["error"] = "path was not supplied"
        return result
    if not isinstance(value, (str, Path)):
        result["error"] = "path must be text or pathlib.Path"
        return result
    try:
        requested = Path(value).expanduser()
        if requested.is_symlink():
            result["path"] = str(requested.resolve(strict=False))
            result["error"] = "path must not be a symbolic link"
            return result
        candidate = requested.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result["error"] = f"path cannot be resolved: {exc}"
        return result
    result["path"] = str(candidate)
    if not candidate.exists() or not candidate.is_file():
        result["error"] = "path is not a regular file"
        return result
    try:
        digest = hashlib.sha256()
        byte_count = 0
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as exc:
        result["error"] = f"file cannot be read: {exc}"
        result["bytes"] = byte_count
        return result
    result.update({"present": True, "sha256": digest.hexdigest(), "bytes": byte_count})
    return result


def _load_json_object(path: str | Path) -> Mapping[str, object]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        payload = source.read_bytes()
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AutonomyContractError(f"{source} is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AutonomyContractError(f"{source} must contain a JSON object")
    return value


def _load_jsonl(path: Path) -> list[Mapping[str, object]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise AutonomyContractError(f"JSONL cannot be read: {path}: {exc}") from exc
    records: list[Mapping[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise AutonomyContractError(f"JSONL line {line_number} cannot be blank")
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise AutonomyContractError(f"JSONL line {line_number} is invalid strict JSON") from exc
        if not isinstance(value, Mapping):
            raise AutonomyContractError(f"JSONL line {line_number} must be an object")
        records.append(value)
    return records


def _load_telemetry_sidecar(
    path: str | Path,
) -> tuple[tuple[RawReplayTelemetrySample, ...], tuple[Mapping[str, object], ...]]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise AutonomyContractError(f"telemetry sidecar cannot be read: {source}: {exc}") from exc
    samples: list[RawReplayTelemetrySample] = []
    mappings: list[Mapping[str, object]] = []
    previous_time = -1.0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise AutonomyContractError(f"telemetry sidecar line {line_number} cannot be blank")
        try:
            raw = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise AutonomyContractError(
                f"telemetry sidecar line {line_number} is invalid strict JSON"
            ) from exc
        if not isinstance(raw, Mapping):
            raise AutonomyContractError(f"telemetry sidecar line {line_number} must be an object")
        sample = RawReplayTelemetrySample.from_mapping(raw)
        if sample.observed_at_s <= previous_time:
            raise AutonomyContractError("telemetry sidecar timestamps must be strictly increasing")
        previous_time = sample.observed_at_s
        samples.append(sample)
        mappings.append(raw)
    if not samples:
        raise AutonomyContractError("telemetry sidecar cannot be empty")
    return tuple(samples), tuple(mappings)


def _resolve_report_path(value: object, *, report_directory: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = report_directory / path
    return path.resolve(strict=False)


def _check_embedded_link_binding(
    embedded: Mapping[str, object],
    *,
    report_directory: Path,
    explicit_link: Mapping[str, Any],
    fail: Any,
) -> None:
    if embedded.get("required") is not True:
        fail("embedded link_preflight.required must be true")
    embedded_path = _resolve_report_path(embedded.get("path"), report_directory=report_directory)
    explicit_path = explicit_link.get("path")
    if embedded_path is None:
        fail("embedded link_preflight.path is missing")
    elif not isinstance(explicit_path, str) or Path(explicit_path).resolve() != embedded_path:
        fail("embedded link_preflight.path does not match explicit link_preflight")
    if embedded.get("report_sha256") != explicit_link.get("sha256"):
        fail("embedded link_preflight SHA-256 does not match explicit link_preflight")
    if embedded.get("report_bytes") != explicit_link.get("bytes"):
        fail("embedded link_preflight byte count does not match explicit link_preflight")
    if embedded.get("hardware_link_ready") is not True:
        fail("embedded link_preflight.hardware_link_ready must be true")
    if embedded.get("verified") is not True:
        fail("embedded link_preflight.verified must be true")


def _collect_target_ids(records: Sequence[Mapping[str, object]]) -> set[str]:
    found: set[str] = set()
    names = {"target_id", "source_target_id", "primary_target_id", "selected_target_id"}

    def visit(value: object, depth: int = 0) -> None:
        if depth > 8 or not isinstance(value, Mapping):
            return
        for key, item in value.items():
            if str(key).casefold() in names and isinstance(item, str) and item.strip():
                found.add(item.strip())
            if isinstance(item, Mapping):
                visit(item, depth + 1)

    for record in records:
        visit(record)
    return found


def _unique_texts(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item not in result:
            result.append(item)
    return result


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        result[key] = value
    return result


__all__ = [
    "DEFAULT_MAXIMUM_TELEMETRY_AGE_S",
    "FIELD_EVIDENCE_EVIDENCE_TYPE",
    "FIELD_EVIDENCE_SCHEMA_VERSION",
    "FIELD_EVIDENCE_STRATA",
    "build_parser",
    "check_field_evidence_bundle",
    "main",
    "verify_field_evidence_bundle_report",
    "write_field_evidence_bundle",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
