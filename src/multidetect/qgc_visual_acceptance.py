"""Create and independently verify a human QGC visual-acceptance record.

The machine capture proves process, window, transport and file identity.  It
does not prove that a person inspected the rendered controls.  This module
keeps that boundary explicit: a reviewer supplies a complete checklist, and
the resulting record binds the exact machine report, screenshot, executable,
current UI contract and a fail-closed geolocation negative control.

``review_sha256`` is an integrity digest, not an electronic identity
signature.  The reviewer's name and attestation remain a human declaration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "multidetect.qgc-operator-visual-acceptance"
SCHEMA_VERSION = 1
VERIFICATION_SCHEMA = "multidetect.qgc-operator-visual-acceptance-verification"
ATTESTATION = (
    "I personally reviewed the bound screenshot and confirm that this checklist "
    "accurately records what was visibly rendered."
)
REQUIRED_CHECKS = (
    "single_native_surface",
    "target_marker_visible",
    "uncertainty_circle_visible",
    "latitude_longitude_visible",
    "target_class_visible",
    "distance_visible",
    "bearing_visible",
    "freshness_visible",
    "copy_coordinate_control_visible",
    "snapshot_control_visible",
    "record_control_visible",
    "read_only_notice_visible",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "event",
        "runtime_mode",
        "review",
        "checklist",
        "positive_visual_evidence",
        "negative_geolocation_control",
        "claim_boundary",
        "gate_passed",
        "reasons",
        "review_sha256",
    }
)
_REVIEW_KEYS = frozenset({"reviewer", "reviewed_at_utc", "decision", "attestation", "notes"})
_CLAIM_BOUNDARY = {
    "human_visual_review_claimed": True,
    "cryptographic_identity_signature_claimed": False,
    "hardware_acceptance": "not_claimed",
    "hil_acceptance": "not_claimed",
    "real_acceptance": "not_claimed",
    "flight_acceptance": "not_claimed",
}


class QgcVisualAcceptanceError(ValueError):
    """Raised when a visual review cannot be bound safely."""


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_identity(path: Path) -> tuple[bytes, dict[str, object]]:
    if not path.is_file():
        raise QgcVisualAcceptanceError(f"required file does not exist: {path}")
    raw = path.read_bytes()
    return raw, {"bytes": len(raw), "sha256": _sha256_bytes(raw)}


def _load_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw, _ = _file_identity(path)
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QgcVisualAcceptanceError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise QgcVisualAcceptanceError(f"{label} must contain a JSON object")
    return document, raw


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QgcVisualAcceptanceError(f"{label} must be an object")
    return value


def _relative(path: Path, root: Path, *, label: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise QgcVisualAcceptanceError(f"{label} must be inside repository root {root}") from exc


def _parse_utc(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise QgcVisualAcceptanceError("reviewed_at_utc must not be empty")
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QgcVisualAcceptanceError("reviewed_at_utc must be ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise QgcVisualAcceptanceError("reviewed_at_utc must include a timezone")
    return (
        timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _is_canonical_utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return _parse_utc(value) == value
    except QgcVisualAcceptanceError:
        return False


def checklist_template() -> dict[str, bool]:
    """Return the complete fail-closed visual checklist."""

    return {name: False for name in REQUIRED_CHECKS}


def _normalise_checklist(value: Mapping[str, object]) -> dict[str, bool]:
    unknown = sorted(set(value).difference(REQUIRED_CHECKS))
    missing = sorted(set(REQUIRED_CHECKS).difference(value))
    if unknown or missing:
        raise QgcVisualAcceptanceError(
            f"checklist keys must match exactly; missing={missing!r}, unknown={unknown!r}"
        )
    result: dict[str, bool] = {}
    for name in REQUIRED_CHECKS:
        observed = value[name]
        if type(observed) is not bool:
            raise QgcVisualAcceptanceError(f"checklist.{name} must be a boolean")
        result[name] = observed
    return result


def _machine_runtime_mode(machine: Mapping[str, Any], qgc: Mapping[str, Any]) -> str | None:
    value = machine.get("runtime_mode")
    if not isinstance(value, str):
        value = qgc.get("runtime_mode")
    if not isinstance(value, str) and qgc.get("isolated_hil_cli") is True:
        value = "hil"
    return value.strip().lower() if isinstance(value, str) else None


def _legacy_hil_machine_parts(
    machine: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    qgc = _mapping(machine.get("qgc"), label="machine.qgc")
    capture = _mapping(
        qgc.get("native_visual_machine_capture"),
        label="machine.qgc.native_visual_machine_capture",
    )
    assertions = _mapping(machine.get("assertions"), label="machine.assertions")
    target = _mapping(
        _mapping(machine.get("jetson_metadata_hil"), label="machine.jetson_metadata_hil").get(
            "target_geolocation_evidence"
        ),
        label="machine target geolocation evidence",
    )
    expected_parity = _mapping(
        target.get("expected_release_parity"), label="machine expected release parity"
    )
    return (
        qgc,
        capture,
        assertions,
        _mapping(expected_parity.get("parity"), label="machine embedded release parity"),
    )


def _native_capture_machine_parts(
    machine: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    executable = _mapping(machine.get("executable_identity"), label="machine executable identity")
    parity = _mapping(machine.get("release_parity"), label="machine release parity")
    return executable, parity, _mapping(parity.get("parity"), label="machine embedded parity")


def _machine_binding(
    machine_path: Path,
    screenshot_path: Path,
    executable_path: Path,
    parity_path: Path,
    root: Path,
    expected_runtime_mode: str,
) -> tuple[dict[str, object], dict[str, bool]]:
    machine, machine_raw = _load_object(machine_path, label="machine capture report")
    screenshot_raw, screenshot_identity = _file_identity(screenshot_path)
    executable_raw, executable_identity = _file_identity(executable_path)
    parity, parity_raw = _load_object(parity_path, label="release parity")

    current_parity = _mapping(parity.get("parity", parity), label="current release parity")
    source_schema = machine.get("schema")
    native_capture = source_schema == "multidetect.qgc-native-style-capture"
    combined_sim = source_schema == "multidetect.px4-gazebo-qgc-combined-sim-acceptance"
    identity_bound_source = native_capture or combined_sim
    if combined_sim:
        qgc = _mapping(machine.get("qgc"), label="machine.qgc")
        toolbar = _mapping(qgc.get("native_toolbar_indicator"), label="machine QGC toolbar")
        qgc_process = _mapping(toolbar.get("qgc_process"), label="machine QGC process")
        qgc_client_rect = _mapping(
            qgc_process.get("client_screen_rect"), label="machine QGC client rect"
        )
        toolbar_checks = _mapping(toolbar.get("checks"), label="machine QGC toolbar checks")
        capture_file = _mapping(toolbar.get("open_capture"), label="machine capture file")
        hashes = _mapping(machine.get("hashes"), label="machine hashes")
        executable = _mapping(hashes.get("qgc_exe"), label="machine executable identity")
        parity_binding = _mapping(hashes.get("release_parity"), label="machine release parity")
        embedded_parity = _mapping(
            parity_binding.get("parity"), label="machine embedded release parity"
        )
        integrated = _mapping(machine.get("integrated_sim_qgc"), label="integrated SIM result")
        synchronization = _mapping(
            integrated.get("synchronization"), label="integrated SIM synchronization"
        )
        machine_mode = _machine_runtime_mode(machine, qgc)
        machine_event_valid = (
            machine.get("schema_version") == 2
            and machine_mode == "sim"
            and isinstance(machine.get("run_id"), str)
            and _RUN_ID_RE.fullmatch(machine["run_id"]) is not None
            and qgc.get("expected_runtime_mode") == "SIM"
            and integrated.get("runtime_mode") == "sim"
            and integrated.get("qgc_transport_acceptance_gate")
            == "passed_rtsp_frame_ack_and_runtime_status_post_adoption_ack"
        )
        machine_gate = machine.get("gate_passed") is True
        capture_bound_to_pid = (
            type(qgc_process.get("pid")) is int
            and qgc_process["pid"] > 0
            and toolbar.get("gate_passed") is True
            and bool(toolbar_checks)
            and all(value is True for value in toolbar_checks.values())
            and capture_file.get("width") == qgc_client_rect.get("width")
            and capture_file.get("height") == qgc_client_rect.get("height")
            and synchronization.get("complete") is True
            and type(synchronization.get("metadata_acceptance_ack_count")) is int
            and synchronization["metadata_acceptance_ack_count"] > 0
            and qgc.get("runtime_status_run_hash_bound_to_integrated_identity") is True
        )
        qgc_path_value = executable.get("path")
        qgc_sha_value = executable.get("sha256")
        parity_path_value = parity_binding.get("path")
        parity_identity_matches = (
            isinstance(parity_path_value, str)
            and Path(parity_path_value).resolve() == parity_path
            and parity_binding.get("bytes") == len(parity_raw)
            and str(parity_binding.get("sha256", "")).lower() == _sha256_bytes(parity_raw)
        )
        capture = {
            "qgc_pid": qgc_process.get("pid"),
            "exact_client_rect_screen": qgc_process.get("client_screen_rect"),
            "single_native_surface_machine_check": _mapping(
                toolbar.get("single_surface"), label="machine QGC single surface"
            ).get("unchanged_and_only_main_window")
            is True,
            "operator_visual_acceptance": toolbar.get("operator_visual_acceptance"),
            "error": toolbar.get("geometry_error") or toolbar.get("automation_error"),
            "external_occluders_after": [],
            "capture": capture_file,
        }
    elif native_capture:
        executable, parity_binding, embedded_parity = _native_capture_machine_parts(machine)
        qgc: Mapping[str, Any] = {}
        capture = machine
        assertions: Mapping[str, Any] = {}
        machine_mode = _machine_runtime_mode(machine, qgc)
        machine_event_valid = (
            machine.get("schema_version") == 2
            and machine.get("event") == "qgc_native_style_capture"
            and isinstance(machine.get("run_id"), str)
            and _RUN_ID_RE.fullmatch(machine["run_id"]) is not None
        )
        machine_gate = machine.get("capture_gate_passed") is True
        attached = machine.get("attached_existing_process") is True
        requested_pid = machine.get("requested_existing_process_id")
        qgc_pid = machine.get("qgc_pid")
        capture_bound_to_pid = (
            type(qgc_pid) is int and qgc_pid > 0 and (not attached or requested_pid == qgc_pid)
        )
        qgc_path_value = executable.get("path")
        qgc_sha_value = executable.get("sha256")
        parity_path_value = parity_binding.get("path")
        parity_identity_matches = (
            isinstance(parity_path_value, str)
            and Path(parity_path_value).resolve() == parity_path
            and parity_binding.get("bytes") == len(parity_raw)
            and str(parity_binding.get("sha256", "")).lower() == _sha256_bytes(parity_raw)
        )
    else:
        qgc, capture, assertions, embedded_parity = _legacy_hil_machine_parts(machine)
        machine_mode = _machine_runtime_mode(machine, qgc)
        machine_event_valid = (
            machine.get("event") == "px4_sitl_qgc_operator_acceptance"
            and source_schema is None
            and expected_runtime_mode == "hil"
        )
        machine_gate = assertions.get("all_passed") is True
        capture_bound_to_pid = (
            assertions.get("native_visual_capture_bound_to_acceptance_pid") is True
        )
        qgc_path_value = qgc.get("executable")
        qgc_sha_value = qgc.get("executable_sha256")
        parity_identity_matches = True

    capture_file = _mapping(capture.get("capture"), label="machine capture file")
    capture_path_value = capture_file.get("path")
    rect = capture.get("exact_client_rect_screen")
    valid_rect = (
        isinstance(rect, Mapping)
        and type(rect.get("width")) is int
        and rect["width"] > 0
        and type(rect.get("height")) is int
        and rect["height"] > 0
    )
    checks = {
        "machine_event": machine_event_valid,
        "runtime_mode_matches": machine_mode == expected_runtime_mode,
        "machine_gate": machine_gate,
        "capture_bound_to_pid": capture_bound_to_pid,
        "valid_exact_client_rect": valid_rect,
        "no_external_occluders": not identity_bound_source
        or capture.get("external_occluders_after") == [],
        "single_native_surface": capture.get("single_native_surface_machine_check") is True,
        "machine_did_not_claim_human_review": capture.get("operator_visual_acceptance")
        == "not_claimed",
        "capture_error_free": capture.get("error") is None,
        "capture_path_matches": isinstance(capture_path_value, str)
        and Path(capture_path_value).resolve() == screenshot_path,
        "capture_bytes_match": capture_file.get("bytes") == len(screenshot_raw),
        "capture_sha256_matches": str(capture_file.get("sha256", "")).lower()
        == screenshot_identity["sha256"],
        "capture_is_png": screenshot_raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "executable_path_matches": isinstance(qgc_path_value, str)
        and Path(qgc_path_value).resolve() == executable_path,
        "executable_sha256_matches": str(qgc_sha_value or "").lower()
        == executable_identity["sha256"],
        "executable_bytes_match": not identity_bound_source
        or executable.get("bytes") == len(executable_raw),
        "release_parity_identity_matches": parity_identity_matches,
        "release_parity_values_match_current": not identity_bound_source
        or dict(embedded_parity) == dict(current_parity),
        "ui_contract_matches_current": embedded_parity.get("ui_contract_hash")
        == current_parity.get("ui_contract_hash"),
    }
    machine_report_binding = {
        "path": _relative(machine_path, root, label="machine report"),
        "bytes": len(machine_raw),
        "sha256": _sha256_bytes(machine_raw),
        "event": machine.get("event") or source_schema,
        "runtime_mode": machine_mode,
        "run_id": machine.get("run_id"),
        "qgc_pid": capture.get("qgc_pid"),
        "qgc_hwnd_exact_client": capture.get("exact_client_rect_screen"),
    }
    if source_schema is not None:
        machine_report_binding["schema"] = source_schema
    binding = {
        "machine_report": machine_report_binding,
        "screenshot": {
            "path": _relative(screenshot_path, root, label="screenshot"),
            **screenshot_identity,
        },
        "qgc_executable": {
            "path": _relative(executable_path, root, label="QGC executable"),
            **executable_identity,
        },
        "ui_contract": {
            "path": _relative(parity_path, root, label="release parity"),
            "bytes": len(parity_raw),
            "sha256": _sha256_bytes(parity_raw),
            "ui_contract_hash": current_parity.get("ui_contract_hash"),
        },
        "machine_checks": checks,
    }
    return binding, checks


def _negative_control_binding(path: Path, root: Path) -> tuple[dict[str, object], dict[str, bool]]:
    document, raw = _load_object(path, label="geolocation negative control")
    frames = document.get("frames")
    rows = frames if isinstance(frames, list) else []
    detection_frames = sum(
        1 for row in rows if isinstance(row, Mapping) and bool(row.get("detections"))
    )
    false_points = sum(
        1 for row in rows if isinstance(row, Mapping) and row.get("predicted_target") is not None
    )
    invalid_frames = sum(
        1
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("range_solution"), Mapping)
        and row["range_solution"].get("validity") == "invalid"
    )
    expected_reason_frames = sum(
        1
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("range_solution"), Mapping)
        and "target_ray_does_not_intersect_ground_safely"
        in row["range_solution"].get("reasons", [])
    )
    checks = {
        "schema": document.get("schema") == "multidetect.gazebo-target-geolocation-acceptance",
        "schema_version": document.get("schema_version") == 2,
        "negative_gate_remains_closed": document.get("gate_passed") is False,
        "minimum_frames": len(rows) >= 5,
        "detections_present": detection_frames == len(rows) and len(rows) >= 5,
        "zero_false_geolocation_points": false_points == 0,
        "all_ranges_invalid": invalid_frames == len(rows) and len(rows) >= 5,
        "expected_fail_closed_reason": expected_reason_frames == len(rows) and len(rows) >= 5,
    }
    return (
        {
            "path": _relative(path, root, label="negative control"),
            "bytes": len(raw),
            "sha256": _sha256_bytes(raw),
            "frame_count": len(rows),
            "detection_frame_count": detection_frames,
            "false_geolocation_point_count": false_points,
            "checks": checks,
        },
        checks,
    )


def create_record(
    *,
    repository_root: str | Path,
    machine_report: str | Path,
    screenshot: str | Path,
    qgc_executable: str | Path,
    release_parity: str | Path,
    negative_control: str | Path,
    reviewer: str,
    reviewed_at_utc: str,
    checklist: Mapping[str, object],
    decision: str,
    runtime_mode: str,
    notes: str = "",
) -> dict[str, Any]:
    """Build a source-bound human visual-review record without writing it."""

    root = Path(repository_root).expanduser().resolve()
    if not root.is_dir():
        raise QgcVisualAcceptanceError(f"repository root does not exist: {root}")
    reviewer_name = reviewer.strip()
    if not reviewer_name:
        raise QgcVisualAcceptanceError("reviewer must not be empty")
    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"accepted", "rejected"}:
        raise QgcVisualAcceptanceError("decision must be accepted or rejected")
    normalized_mode = runtime_mode.strip().lower()
    if normalized_mode not in {"replay", "sim", "hil", "real"}:
        raise QgcVisualAcceptanceError("runtime_mode must be replay, sim, hil or real")
    normalized_checklist = _normalise_checklist(checklist)
    machine_binding, machine_checks = _machine_binding(
        Path(machine_report).expanduser().resolve(),
        Path(screenshot).expanduser().resolve(),
        Path(qgc_executable).expanduser().resolve(),
        Path(release_parity).expanduser().resolve(),
        root,
        normalized_mode,
    )
    negative_binding, negative_checks = _negative_control_binding(
        Path(negative_control).expanduser().resolve(), root
    )
    source_checks_passed = all(machine_checks.values()) and all(negative_checks.values())
    checklist_passed = all(normalized_checklist.values())
    gate_passed = normalized_decision == "accepted" and checklist_passed and source_checks_passed
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": "qgc_operator_visual_acceptance",
        "runtime_mode": normalized_mode,
        "review": {
            "reviewer": reviewer_name,
            "reviewed_at_utc": _parse_utc(reviewed_at_utc),
            "decision": normalized_decision,
            "attestation": ATTESTATION,
            "notes": notes,
        },
        "checklist": normalized_checklist,
        "positive_visual_evidence": machine_binding,
        "negative_geolocation_control": negative_binding,
        "claim_boundary": dict(_CLAIM_BOUNDARY),
        "gate_passed": gate_passed,
        "reasons": [
            name
            for name, passed in {
                "decision_accepted": normalized_decision == "accepted",
                "checklist_complete": checklist_passed,
                "bound_sources_valid": source_checks_passed,
            }.items()
            if not passed
        ],
    }
    document["review_sha256"] = _sha256_bytes(_canonical_bytes(document))
    return document


def _bound_file_matches(root: Path, record: Mapping[str, Any]) -> bool:
    path_value = record.get("path")
    expected_bytes = record.get("bytes")
    expected_sha = record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected_bytes, int):
        return False
    if not isinstance(expected_sha, str) or _SHA256_RE.fullmatch(expected_sha) is None:
        return False
    path = (root / path_value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return False
    if not path.is_file():
        return False
    raw = path.read_bytes()
    return len(raw) == expected_bytes and _sha256_bytes(raw) == expected_sha


def _discover_repository_root(evidence_path: Path, document: Mapping[str, Any]) -> Path:
    positive = _mapping(document.get("positive_visual_evidence"), label="positive evidence")
    parity = _mapping(positive.get("ui_contract"), label="UI contract binding")
    relative_value = parity.get("path")
    if not isinstance(relative_value, str) or not relative_value.strip():
        raise QgcVisualAcceptanceError("UI contract binding path is unavailable")
    for candidate in (evidence_path.parent, *evidence_path.parents):
        resolved = candidate.resolve()
        if (resolved / relative_value).is_file():
            return resolved
    raise QgcVisualAcceptanceError(
        "repository root cannot be discovered from the visual acceptance record"
    )


def verify_record(
    path: str | Path, *, repository_root: str | Path | None = None
) -> dict[str, object]:
    """Reopen every bound source and reproduce the review record."""

    evidence_path = Path(path).expanduser().resolve()
    document, _ = _load_object(evidence_path, label="visual acceptance record")
    root = (
        Path(repository_root).expanduser().resolve()
        if repository_root is not None
        else _discover_repository_root(evidence_path, document)
    )
    review = _mapping(document.get("review"), label="review")
    checklist = _normalise_checklist(_mapping(document.get("checklist"), label="checklist"))
    positive = _mapping(document.get("positive_visual_evidence"), label="positive evidence")
    negative = _mapping(document.get("negative_geolocation_control"), label="negative control")
    claim_boundary = _mapping(document.get("claim_boundary"), label="claim boundary")
    machine_record = _mapping(positive.get("machine_report"), label="machine report binding")
    screenshot_record = _mapping(positive.get("screenshot"), label="screenshot binding")
    executable_record = _mapping(positive.get("qgc_executable"), label="executable binding")
    parity_record = _mapping(positive.get("ui_contract"), label="UI contract binding")

    unsigned = dict(document)
    actual_review_sha = unsigned.pop("review_sha256", None)
    expected_review_sha = _sha256_bytes(_canonical_bytes(unsigned))
    decision = review.get("decision")
    claimed_gate = document.get("gate_passed")
    checks = {
        "top_level_fields": set(document) == _TOP_LEVEL_KEYS,
        "schema": document.get("schema") == SCHEMA,
        "schema_version": document.get("schema_version") == SCHEMA_VERSION,
        "event": document.get("event") == "qgc_operator_visual_acceptance",
        "review_sha256": isinstance(actual_review_sha, str)
        and _SHA256_RE.fullmatch(actual_review_sha) is not None
        and actual_review_sha == expected_review_sha,
        "review_fields": set(review) == _REVIEW_KEYS,
        "reviewer_present": isinstance(review.get("reviewer"), str)
        and bool(review["reviewer"].strip()),
        "reviewed_at_utc": _is_canonical_utc(review.get("reviewed_at_utc")),
        "attestation": review.get("attestation") == ATTESTATION,
        "decision_accepted": decision == "accepted",
        "checklist_complete": all(checklist.values()),
        "claim_boundary": dict(claim_boundary) == _CLAIM_BOUNDARY,
        "reasons_recomputed": document.get("reasons") == [],
        "machine_report_file": _bound_file_matches(root, machine_record),
        "screenshot_file": _bound_file_matches(root, screenshot_record),
        "qgc_executable_file": _bound_file_matches(root, executable_record),
        "release_parity_file": _bound_file_matches(root, parity_record),
        "negative_control_file": _bound_file_matches(root, negative),
        "claimed_gate_passed": claimed_gate is True,
    }

    try:
        machine_binding, machine_checks = _machine_binding(
            (root / str(machine_record.get("path", ""))).resolve(),
            (root / str(screenshot_record.get("path", ""))).resolve(),
            (root / str(executable_record.get("path", ""))).resolve(),
            (root / str(parity_record.get("path", ""))).resolve(),
            root,
            str(document.get("runtime_mode", "")),
        )
        negative_binding, negative_checks = _negative_control_binding(
            (root / str(negative.get("path", ""))).resolve(), root
        )
        checks["machine_binding_reproduced"] = machine_binding == positive
        checks["machine_checks_passed"] = all(machine_checks.values())
        checks["negative_binding_reproduced"] = negative_binding == negative
        checks["negative_checks_passed"] = all(negative_checks.values())
    except (OSError, QgcVisualAcceptanceError, ValueError):
        checks["machine_binding_reproduced"] = False
        checks["machine_checks_passed"] = False
        checks["negative_binding_reproduced"] = False
        checks["negative_checks_passed"] = False

    passed = all(checks.values())
    return {
        "schema": VERIFICATION_SCHEMA,
        "schema_version": 1,
        "evidence_path": str(evidence_path),
        "gate_passed": passed,
        "review_sha256": actual_review_sha,
        "checks": checks,
        "reasons": [name for name, value in checks.items() if not value],
    }


def _load_checklist(path: Path) -> Mapping[str, object]:
    document, _ = _load_object(path.expanduser().resolve(), label="checklist")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    template = subparsers.add_parser("template", help="write a fail-closed checklist template")
    template.add_argument("--out", required=True, type=Path)

    create = subparsers.add_parser("create", help="create a source-bound human review")
    create.add_argument("--repository-root", type=Path, default=Path.cwd())
    create.add_argument("--machine-report", required=True, type=Path)
    create.add_argument("--screenshot", required=True, type=Path)
    create.add_argument("--qgc-executable", required=True, type=Path)
    create.add_argument("--release-parity", required=True, type=Path)
    create.add_argument("--negative-control", required=True, type=Path)
    create.add_argument("--reviewer", required=True)
    create.add_argument("--reviewed-at-utc", required=True)
    create.add_argument("--checklist", required=True, type=Path)
    create.add_argument("--decision", required=True, choices=("accepted", "rejected"))
    create.add_argument("--runtime-mode", required=True, choices=("replay", "sim", "hil", "real"))
    create.add_argument("--notes", default="")
    create.add_argument("--out", required=True, type=Path)

    verify = subparsers.add_parser("verify", help="reopen and verify a human review")
    verify.add_argument("evidence", type=Path)
    verify.add_argument("--repository-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "template":
            document: dict[str, Any] = checklist_template()
            output = args.out.expanduser().resolve()
            exit_code = 0
        elif args.command == "create":
            document = create_record(
                repository_root=args.repository_root,
                machine_report=args.machine_report,
                screenshot=args.screenshot,
                qgc_executable=args.qgc_executable,
                release_parity=args.release_parity,
                negative_control=args.negative_control,
                reviewer=args.reviewer,
                reviewed_at_utc=args.reviewed_at_utc,
                checklist=_load_checklist(args.checklist),
                decision=args.decision,
                runtime_mode=args.runtime_mode,
                notes=args.notes,
            )
            output = args.out.expanduser().resolve()
            exit_code = 0 if document["gate_passed"] else 1
        else:
            document = verify_record(args.evidence, repository_root=args.repository_root)
            output = None
            exit_code = 0 if document["gate_passed"] else 1
    except (OSError, QgcVisualAcceptanceError, TypeError, ValueError) as exc:
        document = {
            "schema": VERIFICATION_SCHEMA,
            "schema_version": 1,
            "gate_passed": False,
            "checks": {"input_valid": False},
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
        output = None
        exit_code = 1
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ATTESTATION",
    "QgcVisualAcceptanceError",
    "REQUIRED_CHECKS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "checklist_template",
    "create_record",
    "verify_record",
]
