"""Run every repository-owned deterministic SIM scenario as one matrix.

The scenario runner already provides the production-shaped SIM loop for a
single manifest.  This module adds the missing *matrix* boundary used by CI
and release preparation: discover or explicitly select all scenario manifests,
run each with the same clock/AI/authority configuration, repeat the run to
prove deterministic evidence, and emit one truth-free aggregate document.

The matrix is intentionally a software gate.  It does not start PX4, Gazebo,
QGC, a serial port or a network socket and it never loads evaluator truth.
External 3-D rendering, hardware and operator visual acceptance remain
``not_claimed`` in the output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..autonomy.serialization import canonical_sha256, strict_json_loads
from ..product_version import load_product_version
from ..runtime import RuntimeMode
from ..simulation import load_runtime_scenario
from .scenario_runner import (
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
    SimulatorScenarioRunnerError,
    _canonical_repository_path,
    _repository_root_for_scenario,
)

SCENARIO_MATRIX_SCHEMA_VERSION = 1
DEFAULT_SCENARIO_DIRECTORY = Path(__file__).resolve().parents[3] / "configs" / "scenarios"


class SimulatorScenarioMatrixError(RuntimeError):
    """Raised when the scenario matrix cannot be constructed or verified."""


def discover_scenario_manifests(directory: str | Path) -> tuple[Path, ...]:
    """Return repository scenario manifests in stable lexical order."""

    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise SimulatorScenarioMatrixError(f"scenario directory not found: {root}")
    manifests = tuple(sorted(root.glob("*.scenario.json"), key=lambda item: item.name))
    if not manifests:
        raise SimulatorScenarioMatrixError(f"no *.scenario.json manifests found in {root}")
    return manifests


def _resolve_manifests(
    directory: str | Path,
    selected: Iterable[str | Path] | None,
) -> tuple[Path, ...]:
    if selected is None:
        return discover_scenario_manifests(directory)
    values = tuple(Path(item).expanduser() for item in selected)
    if not values:
        raise SimulatorScenarioMatrixError("at least one scenario manifest is required")
    manifests: list[Path] = []
    seen: set[Path] = set()
    base = Path(directory).expanduser().resolve()
    for item in values:
        if item.is_absolute():
            candidate = item
        else:
            # CLI callers commonly pass a repository-relative path such as
            # ``configs/scenarios/foo.scenario.json`` while callers that
            # selected a directory may pass only ``foo.scenario.json``.  Try
            # the process working directory first, then the selected scenario
            # directory, without silently concatenating both prefixes.
            cwd_candidate = Path.cwd() / item
            candidate = cwd_candidate if cwd_candidate.is_file() else (base / item)
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise SimulatorScenarioMatrixError(f"scenario manifest not found: {resolved}")
        if resolved in seen:
            raise SimulatorScenarioMatrixError(f"duplicate scenario manifest: {resolved}")
        seen.add(resolved)
        manifests.append(resolved)
    return tuple(manifests)


def _sha256_mapping(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _counter(mapping: object, key: str) -> int:
    """Read a non-negative integer counter from nested evidence.

    The scenario runner's evidence is intentionally JSON-shaped, but this
    small guard keeps the aggregate document fail-closed if a future runner
    changes a counter to a different type or omits it entirely.
    """

    if not isinstance(mapping, dict):
        return 0
    value = mapping.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _scenario_summary(
    result: Any,
    *,
    manifest: Path,
    repeat_hash: str | None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    mapping = result.to_mapping()
    evidence = result.evidence
    descriptor = result.runtime_descriptor
    ai_evidence = evidence.get("ai")
    control_evidence = evidence.get("control")
    summary: dict[str, Any] = {
        # The matrix is a portable, hash-bound artifact.  Keep host-specific
        # absolute paths out of its canonical content so an equivalent
        # checkout produces the same document and evidence hashes.
        "manifest": _canonical_repository_path(
            manifest,
            repository_root=repository_root,
        ),
        "scenario_id": result.scenario_id,
        "scenario_version": result.scenario_version,
        "scenario_manifest_hash": result.scenario_manifest_hash,
        "mode": result.mode,
        "seed": result.seed,
        "requested_frames": result.requested_frames,
        "completed_frames": result.completed_frames,
        "terminal_reason": result.terminal_reason,
        "active_fault_ids": list(result.active_fault_ids),
        "fault_events": list(result.fault_events),
        "fault_coverage": _fault_coverage_summary(result.fault_events),
        "closed": result.closed,
        "truth_loaded": mapping.get("truth_loaded"),
        "truth_free": mapping.get("truth_free"),
        "evidence_hash": result.evidence_hash,
        "repeat_evidence_hash": repeat_hash,
        "runtime_descriptor_hash": result.runtime_descriptor_hash,
        "runtime_descriptor": descriptor.to_mapping(),
        "world_origin": evidence.get("world_origin"),
        "clock": evidence.get("clock"),
        "flight_model_mode": (evidence.get("flight_model") or {}).get("mode"),
        # These counters come from the complete runner evidence, not the
        # bounded ``steps`` history retained for human inspection.  They make
        # the matrix claim auditable even for long (600+ frame) runs.
        "ai_attempted_steps": _counter(ai_evidence, "decisions"),
        "ai_accepted_steps": _counter(ai_evidence, "accepted"),
        "ai_rejected_steps": _counter(ai_evidence, "rejected"),
        "ai_failsafe_steps": _counter(ai_evidence, "failsafe"),
        "ai_watchdog_failsafe_steps": _counter(ai_evidence, "watchdog_failsafe"),
        "control_messages_sent": _counter(control_evidence, "messages_sent"),
    }
    return summary


def _fault_coverage_summary(fault_events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise which declared fault events reached an observation.

    ``active_frames`` is retained as the primary observable and ``fired`` is
    checked as a second independent marker.  Requiring both prevents an
    accidentally copied boolean from claiming coverage when the event was
    never applied to a sensor observation.
    """

    declared_ids: list[str] = []
    fired_ids: list[str] = []
    active_ids: list[str] = []
    invalid_events = 0
    for value in fault_events:
        if not isinstance(value, Mapping):
            invalid_events += 1
            continue
        event_id = value.get("event_id")
        active_frames = value.get("active_frames")
        if not isinstance(event_id, str) or not event_id:
            invalid_events += 1
            continue
        declared_ids.append(event_id)
        if (
            isinstance(active_frames, int)
            and not isinstance(active_frames, bool)
            and active_frames > 0
        ):
            active_ids.append(event_id)
            if value.get("fired") is True:
                fired_ids.append(event_id)
    duplicate_ids = sorted(
        event_id
        for event_id in set(declared_ids)
        if declared_ids.count(event_id) > 1
    )
    declared_unique = list(dict.fromkeys(declared_ids))
    fired_unique = list(dict.fromkeys(fired_ids))
    active_unique = list(dict.fromkeys(active_ids))
    uncovered = [event_id for event_id in declared_unique if event_id not in fired_unique]
    declared_count = len(declared_unique)
    fired_count = len(fired_unique)
    return {
        "declared_event_count": declared_count,
        "fired_event_count": fired_count,
        "active_event_count": len(active_unique),
        "fired_event_ids": fired_unique,
        "active_event_ids": active_unique,
        "uncovered_event_ids": uncovered,
        "duplicate_event_ids": duplicate_ids,
        "invalid_event_count": invalid_events,
        "coverage_ratio": 1.0 if declared_count == 0 else fired_count / declared_count,
        "all_declared_faults_exercised": (
            invalid_events == 0
            and not duplicate_ids
            and len(uncovered) == 0
        ),
    }


def _summary_gate(
    summary: dict[str, Any],
    *,
    require_repeat: bool,
    require_fault_coverage: bool = False,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if summary["terminal_reason"] != "frame_budget_complete":
        reasons.append(f"terminal_reason={summary['terminal_reason']!r}")
    if summary["completed_frames"] != summary["requested_frames"]:
        reasons.append("completed_frames does not match requested_frames")
    if summary["closed"] is not True:
        reasons.append("scenario runner did not close")
    if summary["truth_loaded"] is not False:
        reasons.append("truth_loaded must be false")
    if summary["truth_free"] is not True:
        reasons.append("truth_free must be true")
    if not isinstance(summary["scenario_manifest_hash"], str) or len(
        summary["scenario_manifest_hash"]
    ) != 64:
        reasons.append("scenario_manifest_hash is not a SHA-256")
    if not isinstance(summary["evidence_hash"], str) or len(summary["evidence_hash"]) != 64:
        reasons.append("evidence_hash is not a SHA-256")
    if summary["runtime_descriptor"].get("mode") != RuntimeMode.SIM.value:
        reasons.append("runtime descriptor mode is not SIM")
    if summary["runtime_descriptor"].get("deterministic") is not True:
        reasons.append("runtime descriptor is not deterministic")
    if summary["runtime_descriptor_hash"] != canonical_sha256(summary["runtime_descriptor"]):
        reasons.append("runtime descriptor hash is not self-consistent")
    if summary["flight_model_mode"] != summary["mode"]:
        reasons.append("flight model mode does not match scenario mode")
    clock = summary.get("clock")
    if not isinstance(clock, dict) or clock.get("shared_with_runtime") is not True:
        reasons.append("shared deterministic clock evidence is missing")
    if require_repeat and summary["repeat_evidence_hash"] != summary["evidence_hash"]:
        reasons.append("repeat run evidence hash differs")
    if require_fault_coverage:
        coverage = summary.get("fault_coverage")
        if not isinstance(coverage, Mapping):
            reasons.append("fault coverage summary is missing")
        elif coverage.get("all_declared_faults_exercised") is not True:
            uncovered = coverage.get("uncovered_event_ids")
            reasons.append(
                "not all declared fault events were exercised"
                + (f": {uncovered!r}" if isinstance(uncovered, list) else "")
            )
    return not reasons, reasons


_MATRIX_REQUIRED_MODES = ("fixed-wing", "multirotor", "vtol")
_MATRIX_CLAIM_KEYS = (
    "shared_truth_free_scenario_runner",
    "shared_ai_authority_watchdog_path",
    "all_selected_scenarios_passed",
)


def verify_scenario_matrix_document(
    document: Mapping[str, Any],
    *,
    expected_release_label: str | None = None,
    require_all_airframes: bool = True,
    require_fault_coverage: bool = False,
) -> dict[str, Any]:
    """Verify a matrix artifact independently of the runner that produced it.

    The release gate uses this function after re-hashing the source file.  The
    embedded canonical digest catches edits to nested summaries while the
    structural checks keep a copied ``gate_passed`` flag from being promoted.
    ``require_all_airframes`` is kept explicit so a developer can inspect a
    one-scenario diagnostic, while release bindings always require all three
    repository-owned airframes.  ``require_fault_coverage`` is a stricter
    release-channel policy: it requires the source artifact to opt into the
    coverage gate *and* to have auto-extended its frame budget through every
    declared fault event.  Prerelease diagnostics keep the historical
    optional behaviour unless their own document sets
    ``fault_coverage_required=true``.
    """

    if not isinstance(document, Mapping):
        raise SimulatorScenarioMatrixError("scenario matrix document must be an object")

    reasons: list[str] = []
    checks: dict[str, bool] = {}

    def check(name: str, condition: bool, reason: str) -> None:
        checks[name] = bool(condition)
        if not condition:
            reasons.append(reason)

    check(
        "event",
        document.get("event") == "simulator_scenario_matrix_finished",
        "event must be simulator_scenario_matrix_finished",
    )
    check(
        "schema_version",
        document.get("schema_version") == SCENARIO_MATRIX_SCHEMA_VERSION,
        f"schema_version must be {SCENARIO_MATRIX_SCHEMA_VERSION}",
    )
    check(
        "product",
        document.get("product") == "Multi-Detect",
        "product must be 'Multi-Detect'",
    )
    release_label = document.get("release_label")
    check(
        "release_label",
        isinstance(release_label, str)
        and bool(release_label.strip())
        and (
            expected_release_label is None
            or release_label == expected_release_label
        ),
        "release_label is missing or does not match the product manifest",
    )

    declared_gate = document.get("gate_passed")
    check("gate_passed", declared_gate is True, "gate_passed must be true")
    check(
        "deterministic_gate_passed",
        document.get("deterministic_gate_passed") is True,
        "deterministic_gate_passed must be true",
    )
    check(
        "truth_loaded",
        document.get("truth_loaded") is False,
        "truth_loaded must be false",
    )
    check("truth_free", document.get("truth_free") is True, "truth_free must be true")
    check("enable_ai", document.get("enable_ai") is True, "enable_ai must be true")
    check(
        "arm_on_start",
        document.get("arm_on_start") is True,
        "arm_on_start must be true",
    )
    check(
        "repeatability_checked",
        document.get("repeatability_checked") is True,
        "repeatability_checked must be true",
    )

    scenario_count = document.get("scenario_count")
    check(
        "scenario_count",
        isinstance(scenario_count, int)
        and not isinstance(scenario_count, bool)
        and scenario_count >= 1,
        "scenario_count must be a positive integer",
    )
    scenario_ids = document.get("scenario_ids")
    check(
        "scenario_ids",
        isinstance(scenario_ids, list)
        and all(isinstance(value, str) and value for value in scenario_ids)
        and len(set(scenario_ids)) == len(scenario_ids)
        and isinstance(scenario_count, int)
        and len(scenario_ids) == scenario_count,
        "scenario_ids must be unique strings matching scenario_count",
    )
    modes = document.get("modes")
    check(
        "modes",
        isinstance(modes, list)
        and all(isinstance(value, str) and value for value in modes)
        and len(set(modes)) == len(modes),
        "modes must be a list of unique strings",
    )
    required_modes = document.get("required_modes")
    check(
        "required_modes",
        required_modes == list(_MATRIX_REQUIRED_MODES),
        "required_modes must contain fixed-wing, multirotor and vtol",
    )
    if require_all_airframes:
        check(
            "all_airframes",
            isinstance(modes, list)
            and sorted(modes) == sorted(_MATRIX_REQUIRED_MODES)
            and isinstance(scenario_count, int)
            and scenario_count == len(_MATRIX_REQUIRED_MODES),
            "matrix must contain exactly one fixed-wing, multirotor and vtol scenario",
        )

    fault_coverage_required = document.get("fault_coverage_required", False)
    if "fault_coverage_required" in document:
        check(
            "fault_coverage_required",
            isinstance(fault_coverage_required, bool),
            "fault_coverage_required must be a boolean",
        )
    for optional_boolean in ("fault_coverage_auto_extended",):
        if optional_boolean in document:
            check(
                optional_boolean,
                isinstance(document[optional_boolean], bool),
                f"{optional_boolean} must be a boolean",
            )
    if require_fault_coverage:
        check(
            "fault_coverage_required_for_release",
            fault_coverage_required is True,
            "final release requires fault_coverage_required=true",
        )
        check(
            "fault_coverage_auto_extended_for_release",
            document.get("fault_coverage_auto_extended") is True,
            "final release requires fault_coverage_auto_extended=true",
        )

    claims = document.get("claims")
    check("claims", isinstance(claims, Mapping), "claims must be an object")
    if isinstance(claims, Mapping):
        for key in _MATRIX_CLAIM_KEYS:
            check(
                f"claim_{key}",
                claims.get(key) is True,
                f"claims.{key} must be true",
            )
        check(
            "claim_external_3d_runtime",
            claims.get("external_3d_runtime") == "not_claimed",
            "claims.external_3d_runtime must remain not_claimed",
        )
        check(
            "claim_qgc_visual_acceptance",
            claims.get("qgc_visual_acceptance") == "not_claimed",
            "claims.qgc_visual_acceptance must remain not_claimed",
        )
        check(
            "claim_hardware_acceptance",
            claims.get("hardware_acceptance") == "not_claimed",
            "claims.hardware_acceptance must remain not_claimed",
        )
        check(
            "claim_real_flight_acceptance",
            claims.get("real_flight_acceptance") == "not_claimed",
            "claims.real_flight_acceptance must remain not_claimed",
        )

    summaries = document.get("scenarios")
    check(
        "scenarios",
        isinstance(summaries, list)
        and isinstance(scenario_count, int)
        and len(summaries) == scenario_count,
        "scenarios must be a list matching scenario_count",
    )
    seen_ids: set[str] = set()
    seen_modes: set[str] = set()
    if isinstance(summaries, list):
        for index, summary_value in enumerate(summaries):
            if not isinstance(summary_value, Mapping):
                reasons.append(f"scenarios[{index}] must be an object")
                continue
            summary = dict(summary_value)
            summary_id = summary.get("scenario_id")
            summary_mode = summary.get("mode")
            if not isinstance(summary_id, str) or not summary_id:
                reasons.append(f"scenarios[{index}].scenario_id must be a non-empty string")
            else:
                if summary_id in seen_ids:
                    reasons.append(f"duplicate scenario_id: {summary_id}")
                seen_ids.add(summary_id)
            if not isinstance(summary_mode, str) or not summary_mode:
                reasons.append(f"scenarios[{index}].mode must be a non-empty string")
            else:
                if summary_mode in seen_modes:
                    reasons.append(f"duplicate scenario mode: {summary_mode}")
                seen_modes.add(summary_mode)
            required_summary_keys = {
                "scenario_id",
                "mode",
                "terminal_reason",
                "completed_frames",
                "requested_frames",
                "closed",
                "truth_loaded",
                "truth_free",
                "scenario_manifest_hash",
                "evidence_hash",
                "runtime_descriptor",
                "runtime_descriptor_hash",
                "flight_model_mode",
                "clock",
                "repeat_evidence_hash",
            }
            missing = sorted(required_summary_keys.difference(summary))
            if missing:
                reasons.append(f"scenarios[{index}] missing field(s): {missing!r}")
                continue
            try:
                passed, summary_reasons = _summary_gate(
                    summary,
                    require_repeat=document.get("repeatability_checked") is True,
                    require_fault_coverage=(
                        fault_coverage_required is True or require_fault_coverage
                    ),
                )
            except (KeyError, TypeError, AttributeError) as exc:
                reasons.append(f"scenarios[{index}] cannot be verified: {exc}")
                continue
            if not passed:
                reasons.extend(f"scenarios[{index}]: {reason}" for reason in summary_reasons)
            if summary.get("gate_passed") is not True:
                reasons.append(f"scenarios[{index}].gate_passed must be true")
            if isinstance(scenario_ids, list) and summary_id not in scenario_ids:
                reasons.append(f"scenarios[{index}].scenario_id is not listed in scenario_ids")
            if isinstance(modes, list) and summary_mode not in modes:
                reasons.append(f"scenarios[{index}].mode is not listed in modes")

    if isinstance(scenario_ids, list) and seen_ids != set(scenario_ids):
        reasons.append("scenario_ids do not match scenario summaries")
    if isinstance(modes, list) and seen_modes != set(modes):
        reasons.append("modes do not match scenario summaries")

    embedded_sha = document.get("sha256")
    canonical_document = dict(document)
    canonical_document.pop("sha256", None)
    recomputed_sha = _sha256_mapping(canonical_document)
    check(
        "embedded_sha256",
        _is_sha256(embedded_sha) and str(embedded_sha).lower() == recomputed_sha,
        "embedded sha256 does not match canonical matrix content",
    )

    return {
        "event": "simulator_scenario_matrix_verified",
        "schema_version": SCENARIO_MATRIX_SCHEMA_VERSION,
        "release_label": release_label,
        "gate_passed": not reasons,
        "checks": checks,
        "recomputed_sha256": recomputed_sha,
        "reasons": reasons,
    }


def verify_file(
    path: str | Path,
    *,
    expected_release_label: str | None = None,
    require_all_airframes: bool = True,
    require_fault_coverage: bool = False,
) -> dict[str, Any]:
    """Load and verify a UTF-8 JSON scenario-matrix artifact.

    ``require_fault_coverage`` is used by the final release gate.  It is
    intentionally separate from the artifact's self-declared option so a
    caller cannot turn a required release check off by omitting a JSON field.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise SimulatorScenarioMatrixError(f"scenario matrix evidence does not exist: {source}")
    try:
        raw = strict_json_loads(
            source.read_text(encoding="utf-8-sig"),
            name="scenario matrix evidence",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SimulatorScenarioMatrixError(
            f"scenario matrix evidence is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise SimulatorScenarioMatrixError("scenario matrix evidence must contain an object")
    return verify_scenario_matrix_document(
        raw,
        expected_release_label=expected_release_label,
        require_all_airframes=require_all_airframes,
        require_fault_coverage=require_fault_coverage,
    )


def run_scenario_matrix(
    directory: str | Path = DEFAULT_SCENARIO_DIRECTORY,
    *,
    scenarios: Iterable[str | Path] | None = None,
    frames: int = 120,
    dt_s: float = 0.1,
    enable_ai: bool = True,
    arm_on_start: bool = True,
    repeat: bool = True,
    require_fault_coverage: bool = False,
    cover_faults: bool = False,
    fault_tail_s: float = 1.0,
) -> dict[str, Any]:
    """Run and verify a deterministic matrix of scenario manifests."""

    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 1_000_000:
        raise SimulatorScenarioMatrixError("frames must be an integer in [1, 1000000]")
    if (
        isinstance(dt_s, bool)
        or not isinstance(dt_s, (int, float))
        or not math.isfinite(float(dt_s))
        or float(dt_s) <= 0.0
    ):
        raise SimulatorScenarioMatrixError("dt_s must be a finite number greater than zero")
    if (
        not isinstance(enable_ai, bool)
        or not isinstance(arm_on_start, bool)
        or not isinstance(repeat, bool)
        or not isinstance(require_fault_coverage, bool)
        or not isinstance(cover_faults, bool)
    ):
        raise SimulatorScenarioMatrixError(
            "enable_ai, arm_on_start, repeat, require_fault_coverage and cover_faults must be bool"
        )
    if (
        isinstance(fault_tail_s, bool)
        or not isinstance(fault_tail_s, (int, float))
        or not math.isfinite(float(fault_tail_s))
        or float(fault_tail_s) < 0.0
    ):
        raise SimulatorScenarioMatrixError(
            "fault_tail_s must be a finite number greater than or equal to zero"
        )

    manifests = _resolve_manifests(directory, scenarios)
    requested_frames = frames
    effective_frames = frames
    fault_coverage_requested = require_fault_coverage or cover_faults
    if cover_faults:
        # Extend the common frame budget through the latest declared event so
        # every selected scenario is observed under the same deterministic
        # clock.  Indefinite events get a short tail; their transform remains
        # active while the run completes, but the matrix stays finite.
        latest_fault_time_s = 0.0
        for manifest in manifests:
            try:
                runtime_input = load_runtime_scenario(manifest, mode=RuntimeMode.SIM)
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                raise SimulatorScenarioMatrixError(
                    f"{manifest.name}: cannot inspect runtime faults: {exc}"
                ) from exc
            for event in runtime_input.faults:
                if event.duration_s == 0.0:
                    continue
                duration = fault_tail_s if event.duration_s is None else float(event.duration_s)
                latest_fault_time_s = max(latest_fault_time_s, float(event.at_s) + duration)
        if latest_fault_time_s > 0.0:
            # The runner samples at t=0, dt, ...; one extra sample guarantees
            # a finite event ending exactly on a sample boundary is included.
            effective_frames = max(
                frames,
                int(math.ceil(latest_fault_time_s / float(dt_s))) + 1,
            )
    summaries: list[dict[str, Any]] = []
    scenario_ids: set[str] = set()
    modes: set[str] = set()
    for manifest in manifests:
        config = SimulatorScenarioRunnerConfig(
            frames=effective_frames,
            dt_s=float(dt_s),
            enable_ai=enable_ai,
            arm_on_start=arm_on_start,
            history_limit=min(frames, 8),
        )
        try:
            first = SimulatorScenarioRunner(manifest, config=config).run()
            second_hash: str | None = None
            if repeat:
                second = SimulatorScenarioRunner(manifest, config=config).run()
                second_hash = second.evidence_hash
        except (OSError, SimulatorScenarioRunnerError, ValueError, TypeError, RuntimeError) as exc:
            raise SimulatorScenarioMatrixError(f"{manifest.name}: {exc}") from exc
        summary = _scenario_summary(
            first,
            manifest=manifest,
            repeat_hash=second_hash,
            repository_root=_repository_root_for_scenario(manifest),
        )
        if summary["scenario_id"] in scenario_ids:
            raise SimulatorScenarioMatrixError(
                f"duplicate scenario_id across manifests: {summary['scenario_id']}"
            )
        scenario_ids.add(summary["scenario_id"])
        modes.add(summary["mode"])
        passed, reasons = _summary_gate(
            summary,
            require_repeat=repeat,
            require_fault_coverage=fault_coverage_requested,
        )
        summary["gate_passed"] = passed
        summary["failure_reasons"] = reasons
        summaries.append(summary)

    product = load_product_version()
    gate_passed = bool(summaries) and all(item["gate_passed"] for item in summaries)
    # The structural path exists in every runner, but a control-path claim is
    # only truthful when AI was requested and every selected scenario actually
    # accepted at least one AI command through the shared authority/adapter.
    ai_path_exercised = bool(
        gate_passed
        and enable_ai
        and summaries
        and all(item["ai_accepted_steps"] >= 1 for item in summaries)
    )
    matrix_root = _repository_root_for_scenario(manifests[0])
    document: dict[str, Any] = {
        "schema_version": SCENARIO_MATRIX_SCHEMA_VERSION,
        "event": "simulator_scenario_matrix_finished",
        "product": product.product,
        "release_label": product.release_label,
        "python_version": product.python,
        "scenario_directory": _canonical_repository_path(
            directory,
            repository_root=matrix_root,
        ),
        "scenario_count": len(summaries),
        "scenario_ids": sorted(scenario_ids),
        "modes": sorted(modes),
        "requested_frames": effective_frames,
        "requested_frames_before_fault_coverage": requested_frames,
        "dt_s": float(dt_s),
        "enable_ai": enable_ai,
        "arm_on_start": arm_on_start,
        "repeatability_checked": repeat,
        "fault_coverage_required": fault_coverage_requested,
        "fault_coverage_auto_extended": cover_faults,
        "fault_coverage_tail_s": float(fault_tail_s),
        "required_modes": ["fixed-wing", "multirotor", "vtol"],
        "scenarios": summaries,
        "gate_passed": gate_passed,
        "deterministic_gate_passed": gate_passed,
        "claims": {
            "shared_truth_free_scenario_runner": gate_passed,
            "shared_ai_authority_watchdog_path": ai_path_exercised,
            "all_selected_scenarios_passed": gate_passed,
            "external_3d_runtime": "not_claimed",
            "qgc_visual_acceptance": "not_claimed",
            "hardware_acceptance": "not_claimed",
            "real_flight_acceptance": "not_claimed",
        },
        "external_runtime_required_for_real_3d": True,
        "truth_loaded": False,
        "truth_free": True,
    }
    document["sha256"] = _sha256_mapping(document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario-directory",
        "--directory",
        type=Path,
        default=DEFAULT_SCENARIO_DIRECTORY,
        help="directory containing *.scenario.json manifests",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        type=Path,
        help="explicit manifest (repeat for multiple scenarios; default discovers all)",
    )
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--dt", type=float, default=0.1)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--no-arm", action="store_true")
    parser.add_argument(
        "--no-repeat",
        action="store_true",
        help="skip the second deterministic run (not recommended for CI)",
    )
    parser.add_argument(
        "--require-fault-coverage",
        action="store_true",
        help="fail unless every declared fault reaches at least one observation",
    )
    parser.add_argument(
        "--cover-faults",
        action="store_true",
        help="extend the frame budget through the latest declared fault and require coverage",
    )
    parser.add_argument(
        "--fault-tail",
        type=float,
        default=1.0,
        help="seconds to run after an indefinite fault starts when --cover-faults is set",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_scenario_matrix(
            args.scenario_directory,
            scenarios=args.scenarios,
            frames=args.frames,
            dt_s=args.dt,
            enable_ai=not args.no_ai,
            arm_on_start=not args.no_arm,
            repeat=not args.no_repeat,
            require_fault_coverage=args.require_fault_coverage,
            cover_faults=args.cover_faults,
            fault_tail_s=args.fault_tail,
        )
    except (OSError, SimulatorScenarioMatrixError, ValueError, TypeError, RuntimeError) as exc:
        print(json.dumps({"event": "simulator_scenario_matrix_failed", "error": str(exc)}))
        return 2

    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["gate_passed"] else 1


__all__ = [
    "DEFAULT_SCENARIO_DIRECTORY",
    "SCENARIO_MATRIX_SCHEMA_VERSION",
    "SimulatorScenarioMatrixError",
    "discover_scenario_manifests",
    "main",
    "run_scenario_matrix",
    "verify_file",
    "verify_scenario_matrix_document",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
