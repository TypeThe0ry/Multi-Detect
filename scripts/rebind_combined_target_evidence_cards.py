"""Reconstruct read-only target cards from one pinned historical audit.

The output is a standalone historical-observation bundle.  It is deliberately
not a combined-SIM acceptance document and never replaces source parity or
claims a new runtime execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import (  # noqa: E402
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from multidetect.operator_status import (  # noqa: E402
    MAX_RANGE_STATUS_SOURCE_AGE_S,
    MAX_TARGET_GEOLOCATION_SOURCE_AGE_S,
)
from multidetect.target_event_replay import GPS_QUALIFIED_MAX_AGE_S  # noqa: E402
from multidetect.target_evidence_card import TargetEvidenceCard  # noqa: E402

SCHEMA = "multidetect.historical-target-card-reconstruction"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_AUDIT_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 131_072
TIME_TOLERANCE_S = 1e-6
# The historical audit records primary observations and operator status at
# adjacent telemetry ticks.  The largest observed tick skew is 0.016 s; keep a
# small, explicit bound instead of pretending those timestamps are identical.
MAX_PRIMARY_TO_OPERATOR_SKEW_S = 0.02


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, *, limit: int, expected_sha256: str | None = None) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"input exceeds size limit: {path}")
    if expected_sha256 is not None and _sha256(raw) != require_sha256(
        expected_sha256, "expected input SHA-256"
    ):
        raise ValueError(f"input SHA-256 mismatch: {path}")
    return raw


def _object(raw: bytes, name: str) -> dict[str, Any]:
    value = strict_json_loads(raw, name=name)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _binding(root: Path, path: Path, raw: bytes) -> dict[str, Any]:
    return {"path": _relative(root, path), "bytes": len(raw), "sha256": _sha256(raw)}


def _write_new(path: Path, value: dict[str, Any]) -> bytes:
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
    return payload


def _same_time(left: object, right: object, name: str) -> None:
    a = require_real(left, name, minimum=0.0)
    b = require_real(right, name, minimum=0.0)
    if not math.isclose(a, b, rel_tol=0.0, abs_tol=TIME_TOLERANCE_S):
        raise ValueError(f"source time mismatch: {name}")


def _same_value(left: object, right: object, name: str) -> None:
    if require_real(left, name) != require_real(right, name):
        raise ValueError(f"source value mismatch: {name}")


def _key(details: dict[str, Any], *, operator: bool = False) -> tuple[str, str]:
    return (
        require_text(details.get("target_id"), "target_id"),
        require_text(details.get("source_frame_id" if operator else "frame_id"), "frame_id"),
    )


def _readonly(details: dict[str, Any]) -> None:
    if details.get("advisory_only") is not True:
        raise ValueError("historical source observation must be advisory-only")
    for field in ("flight_control_enabled", "physical_release_enabled", "hardware_control_enabled"):
        if field in details and details[field] is not False:
            raise ValueError(f"historical source observation enables {field}")


def _audit_events(raw: bytes, run_identity: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous_timestamp = 0.0
    for line in raw.splitlines():
        if not line.strip():
            raise ValueError("blank audit record is not allowed")
        event = _object(line, "audit event")
        strict_object(
            event,
            name="audit event",
            required=frozenset({"sequence", "timestamp_s", "event_type", "details", "session_id"}),
        )
        sequence = require_int(event["sequence"], "audit sequence", minimum=1)
        if sequence != len(events) + 1:
            raise ValueError("audit sequence must be consecutive, starting at 1")
        if require_text(event["session_id"], "audit session_id") != run_identity:
            raise ValueError("audit session_id does not match source run identity")
        timestamp = require_real(event["timestamp_s"], "audit timestamp_s", minimum=0.0)
        if timestamp < previous_timestamp:
            raise ValueError("audit timestamps must not go backwards")
        previous_timestamp = timestamp
        require_text(event["event_type"], "audit event_type")
        if not isinstance(event["details"], dict):
            raise ValueError("audit details must be an object")
        events.append(event)
        if len(events) > MAX_EVENTS:
            raise ValueError("audit exceeds event limit")
    if not events:
        raise ValueError("audit must not be empty")
    return events


def _operator_capture(
    event: dict[str, Any], primary: dict[str, Any], maximum_age_s: float
) -> float:
    details = event["details"]
    _readonly(details)
    captured = require_real(details.get("source_captured_at_s"), "source_captured_at_s", minimum=0)
    produced = require_real(details.get("produced_at_s"), "produced_at_s", minimum=0)
    age = require_real(details.get("source_age_s"), "source_age_s", minimum=0)
    if produced < captured or age > maximum_age_s:
        raise ValueError("operator source observation is stale or from the future")
    _same_time(produced - captured, age, "source_age_s")
    primary_timestamp = require_real(primary["timestamp_s"], "primary timestamp", minimum=0)
    produced_skew = produced - primary_timestamp
    if produced_skew < -TIME_TOLERANCE_S or produced_skew > MAX_PRIMARY_TO_OPERATOR_SKEW_S:
        raise ValueError("operator status is outside the primary telemetry tick")
    if event["sequence"] <= primary["sequence"] or event["timestamp_s"] < produced:
        raise ValueError("operator event must follow its primary observation")
    return captured


def _derive_cards(
    combined: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    integrated = combined["integrated_sim_qgc"]
    acceptance = integrated["target_geolocation_acceptance"]
    expected_count = require_int(
        acceptance.get("available_geolocation_count"), "available geolocation count", minimum=0
    )
    expected_primary = require_int(
        acceptance.get("primary_solution_count"), "primary solution count", minimum=0
    )
    labels: dict[str, tuple[str, int]] = {}
    primary: dict[tuple[str, str], tuple[dict[str, Any], str, int]] = {}
    range_events: dict[tuple[str, str], dict[str, Any]] = {}
    geo_events: dict[tuple[str, str], dict[str, Any]] = {}
    available_operator_keys: dict[str, set[tuple[str, str]]] = {
        "range": set(),
        "geo": set(),
    }
    primary_count = 0
    for event in events:
        details = event["details"]
        kind = event["event_type"]
        if kind == "ranging.target_solution_changed":
            target_id = require_text(details.get("target_id"), "target_id")
            labels[target_id] = (
                require_text(details.get("label"), "target label"),
                event["sequence"],
            )
            continue
        if kind == "ranging.primary_target_solution":
            primary_count += 1
            geo = details.get("target_geolocation")
            if not isinstance(geo, dict) or geo.get("available") is not True:
                continue
            if details.get("validity") not in {"valid", "degraded"}:
                raise ValueError("available geolocation requires usable primary range")
            sources = details.get("sources")
            if not isinstance(sources, list) or "camera_ground" not in sources:
                raise ValueError("available source must contain camera_ground range")
            key = _key(details)
            if key in primary:
                raise ValueError("duplicate available primary target/frame")
            causal_label = labels.get(key[0])
            if causal_label is None:
                raise ValueError("available primary has no prior causal target label")
            label, label_sequence = causal_label
            primary[key] = (event, label, label_sequence)
            continue
        if kind == "operator.remote_range_status":
            key = _key(details, operator=True)
            if details.get("validity") in {"valid", "degraded"}:
                available_operator_keys["range"].add(key)
            if key in range_events:
                raise ValueError("duplicate operator range status")
            range_events[key] = event
            continue
        if kind == "operator.remote_target_geolocation_status":
            key = _key(details, operator=True)
            if details.get("available") is True:
                available_operator_keys["geo"].add(key)
            if key in geo_events:
                raise ValueError("duplicate operator geolocation status")
            geo_events[key] = event
    if primary_count != expected_primary:
        raise ValueError("audit primary solution count does not match source")
    if len(primary) != expected_count:
        raise ValueError("audit geolocation count does not match source")
    if (
        available_operator_keys["range"] != set(primary)
        or available_operator_keys["geo"] != set(primary)
    ):
        raise ValueError("available operator status has an orphan or missing target/frame")

    cards: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for key, (primary_event, label, label_sequence) in primary.items():
        details = primary_event["details"]
        _readonly(details)
        range_event = range_events.get(key)
        geo_event = geo_events.get(key)
        if range_event is None or geo_event is None:
            raise ValueError("primary target has no matching operator status")
        if not primary_event["sequence"] < range_event["sequence"] < geo_event["sequence"]:
            raise ValueError("source event order must be primary, range, geolocation")
        range_captured = _operator_capture(
            range_event, primary_event, MAX_RANGE_STATUS_SOURCE_AGE_S
        )
        geo_captured = _operator_capture(
            geo_event, primary_event, MAX_TARGET_GEOLOCATION_SOURCE_AGE_S
        )
        _same_time(range_captured, geo_captured, "range/geolocation source capture")
        _same_time(
            range_event["details"].get("produced_at_s"),
            geo_event["details"].get("produced_at_s"),
            "range/geolocation production time",
        )

        range_details = range_event["details"]
        if range_details.get("validity") != details.get("validity"):
            raise ValueError("operator range validity mismatch")
        _same_value(range_details.get("slant_range_m"), details.get("slant_range_m"), "slant range")
        primary_freshness = require_real(
            details.get("data_freshness_s"), "data freshness", minimum=0
        )
        range_freshness = require_real(
            range_details.get("data_freshness_s"), "operator data freshness", minimum=0
        )
        primary_timestamp = require_real(
            primary_event["timestamp_s"], "primary timestamp", minimum=0
        )
        range_produced = require_real(
            range_details.get("produced_at_s"), "operator produced_at_s", minimum=0
        )
        _same_time(
            range_freshness - primary_freshness,
            range_produced - primary_timestamp,
            "data freshness telemetry tick",
        )
        freshness = primary_freshness
        if freshness > MAX_RANGE_STATUS_SOURCE_AGE_S:
            raise ValueError("primary range is stale")

        geo = details.get("target_geolocation")
        if not isinstance(geo, dict):
            raise ValueError("primary geolocation object is missing")
        published_geo = geo_event["details"]
        if published_geo.get("available") is not True:
            raise ValueError("operator geolocation is not available")
        for field in ("latitude_deg", "longitude_deg", "horizontal_sigma_m"):
            _same_value(geo.get(field), published_geo.get(field), field)
        quality = geo.get("gps_quality")
        sample = geo.get("gps_sample")
        if not isinstance(quality, dict) or not isinstance(sample, dict):
            raise ValueError("GPS quality/sample is missing")
        if quality.get("qualified") is not True or sample.get("position_healthy") is not True:
            raise ValueError("GPS source is not qualified and healthy")
        observed = require_real(sample.get("observed_at_s"), "GPS observed_at_s", minimum=0)
        gps_age = require_real(primary_event["timestamp_s"], "primary timestamp") - observed
        if gps_age < 0 or gps_age > GPS_QUALIFIED_MAX_AGE_S:
            raise ValueError("GPS sample is stale or newer than primary capture")
        _same_time(gps_age, quality.get("age_s"), "GPS age")
        card_input = dict(details, gps_quality=quality, gps_sample=sample)
        card = TargetEvidenceCard.from_live_observation(
            card_input,
            captured_at_s=range_captured,
            target_label=label,
            run_identity_sha256=integrated["run_identity_hash"],
            card_id=f"{key[0]}-{key[1]}-card",
        )
        if not card.available:
            raise ValueError("available source event produced unavailable card")
        cards.append(card.to_mapping())
        provenance.append(
            {
                "card_sha256": card.canonical_hash,
                "label_sequence": label_sequence,
                "primary_sequence": primary_event["sequence"],
                "range_sequence": range_event["sequence"],
                "geolocation_sequence": geo_event["sequence"],
                "source_captured_at_s": range_captured,
            }
        )
    return cards, provenance


def build_bundle(
    *, source: Path, audit: Path, parity: Path, root: Path,
    source_sha256: str, audit_sha256: str,
) -> dict[str, Any]:
    source_raw = _read(source, limit=MAX_JSON_BYTES, expected_sha256=source_sha256)
    audit_raw = _read(audit, limit=MAX_AUDIT_BYTES, expected_sha256=audit_sha256)
    parity_raw = _read(parity, limit=MAX_JSON_BYTES)
    combined = _object(source_raw, "combined evidence")
    parity_doc = _object(parity_raw, "current derivation parity")
    if (
        combined.get("schema") != "multidetect.px4-gazebo-qgc-combined-sim-acceptance"
        or combined.get("runtime_mode") != "sim"
    ):
        raise ValueError("historical combined SIM source required")
    integrated = combined.get("integrated_sim_qgc")
    if not isinstance(integrated, dict):
        raise ValueError("source integrated SIM section is missing")
    run_identity = require_sha256(integrated.get("run_identity_hash"), "source run identity")
    acceptance = integrated.get("target_geolocation_acceptance")
    if not isinstance(acceptance, dict) or acceptance.get("configured") is not True:
        raise ValueError("source target-geolocation acceptance must be configured")
    artifacts = combined.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("source artifact paths are missing")
    declared_audit = artifacts.get("integrated_audit")
    if not isinstance(declared_audit, str):
        raise ValueError("source integrated audit path is missing")
    declared_path = Path(declared_audit)
    if not declared_path.is_absolute():
        declared_path = root / declared_path
    if declared_path.resolve() != audit.resolve():
        raise ValueError("audit path is not the source integrated_audit artifact")
    events = _audit_events(audit_raw, run_identity)
    cards, provenance = _derive_cards(combined, events)
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "evidence_kind": "historical_observation_cards_only",
        "card_reconstruction_passed": True,
        "source": {
            "combined_evidence": _binding(root, source, source_raw),
            "integrated_audit": _binding(root, audit, audit_raw),
            "source_gate_passed": combined.get("gate_passed"),
            "source_run_identity_sha256": run_identity,
            "source_run_id": combined.get("run_id"),
            "source_generated_at_utc": combined.get("generated_at_utc"),
            "source_release_parity": combined.get("hashes", {}).get("release_parity"),
        },
        "derivation": {
            "current_parity_is_tool_context_only": True,
            "current_parity": parity_doc.get("parity"),
            "current_parity_file": _binding(root, parity, parity_raw),
            "tool_file": _binding(root, Path(__file__), Path(__file__).read_bytes()),
            "runtime_reexecuted": False,
        },
        "audit_event_count": len(events),
        "available_geolocation_count": len(cards),
        "target_evidence_card_count": len(cards),
        "target_evidence_cards_read_only": True,
        "target_evidence_cards": cards,
        "card_source_events": provenance,
        "claims": {
            "current_external_sim_acceptance": False,
            "physical_performance_claimed": False,
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
            "operator_visual_acceptance": "not_claimed",
            "release_allowed": False,
            "final_release_allowed": False,
        },
    }


def rebind(
    *,
    source: Path,
    audit: Path,
    parity: Path,
    output: Path,
    report_path: Path,
    root: Path,
    source_sha256: str, audit_sha256: str,
) -> dict[str, Any]:
    if output.exists() or report_path.exists():
        raise FileExistsError("refusing to overwrite existing artifact")
    if len({path.resolve() for path in (source, audit, parity, output, report_path)}) != 5:
        raise ValueError("source, audit, parity, output and report paths must be distinct")
    bundle = build_bundle(
        source=source, audit=audit, parity=parity, root=root,
        source_sha256=source_sha256, audit_sha256=audit_sha256,
    )
    bundle_raw = _write_new(output, bundle)
    report_doc = {
        "schema": SCHEMA + ".report",
        "schema_version": 1,
        "bundle": {
            key: value
            for key, value in bundle.items()
            if key not in {"target_evidence_cards", "card_source_events"}
        },
        "output": {
            "path": _relative(root, output),
            "bytes": len(bundle_raw),
            "sha256": _sha256(bundle_raw),
        },
        "target_evidence_card_count": bundle["target_evidence_card_count"],
        "runtime_reexecuted": False,
        "current_external_sim_acceptance": False,
        "release_allowed": False,
    }
    report_raw = _write_new(report_path, report_doc)
    return {
        "bundle": bundle,
        "report": report_doc,
        "output_raw": bundle_raw,
        "report_raw": report_raw,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--audit-sha256", required=True)
    parser.add_argument("--parity", type=Path, default=Path("configs/release-parity.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = {
        name: ((value if value.is_absolute() else ROOT / value).resolve())
        for name, value in (("source", args.source), ("audit", args.audit), ("parity", args.parity),
                            ("output", args.output), ("report", args.report))
    }
    result = rebind(
        root=ROOT,
        source_sha256=args.source_sha256,
        audit_sha256=args.audit_sha256,
        report_path=paths.pop("report"),
        **paths,
    )
    print(json.dumps({
        "output": result["report"]["output"]["path"],
        "cards": result["bundle"]["target_evidence_card_count"],
        "runtime_reexecuted": False,
        "current_external_sim_acceptance": False,
        "release_allowed": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
