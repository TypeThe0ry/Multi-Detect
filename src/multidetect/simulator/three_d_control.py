"""Independent verifier and release binding for the deterministic 3-D SIM gate.

The 3-D control runner deliberately produces a *software/asset* acceptance
record.  This module is the second, independent boundary used by the release
gate: it re-opens each record, checks the shared manual/AI/watchdog contract,
recomputes the nested stack-plan digest, and keeps external PX4/Gazebo,
operator-visual and hardware claims fail-closed.

The binding is an ordered set of exactly one VTOL, fixed-wing and multirotor
record.  It is useful on the prerelease line as an integrity check and becomes
required by the final ``0.3.0`` release gate.  A deterministic pass never
means that an external simulator or Pixhawk was actually started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..autonomy.serialization import canonical_sha256, strict_json_loads
from ..runtime import RuntimeMode
from ..simulation import load_runtime_scenario
from .px4_image import PINNED_IMAGE

SCHEMA_VERSION = 1
EVENT = "simulator_3d_control_acceptance_finished"
VERIFIED_EVENT = "simulator_3d_control_evidence_verified"
DEFAULT_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

# The runner intentionally exposes only these repository-owned scenarios.  A
# release binding must cover all three vehicle classes exactly once.
AIRFRAME_SCENARIOS: dict[str, str] = {
    "river_rescue_vtol_001": "vtol",
    "coastal_patrol_fixed_wing_001": "fixed-wing",
    "urban_inspection_multirotor_001": "multirotor",
}
GIMBAL_PROFILE_ID = "x500_gimbal"
REQUIRED_MODES = ("fixed-wing", "multirotor", "vtol")

_REQUIRED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "event",
        "product",
        "release_label",
        "runtime_mode",
        "gate_passed",
        "deterministic_gate_passed",
        "external_runtime_gate_passed",
        "gate_scope",
        "external_runtime_required_for_real_3d",
        "scenario",
        "three_d_runtime",
        "stack_plan",
        "stack_plan_sha256",
        "stack_plan_binding",
        "stack_plan_status",
        "px4_launch",
        "launch_readiness",
        "shared_control_path",
        "interactive_qgc_path",
        "control_contract",
        "deterministic_control",
        "claims",
    }
)
_REQUIRED_SCENARIO = frozenset(
    {
        "path",
        "sha256",
        "scenario_id",
        "scenario_version",
        "manifest_canonical_sha256",
        "runtime_channel_sha256",
        "vehicle",
        "world",
    }
)
_REQUIRED_CLAIMS = (
    "manual_and_ai_share_authority_path",
    "native_qgc_interactive_path",
    "stack_plan_hash_bound",
    "px4_launch_plan_generated",
    "manual_control_evidence",
    "ai_evidence",
    "watchdog_evidence",
    "qgc_is_sole_operator_ui",
    "qgc_only_control_evidence",
)


class Simulator3DControlError(ValueError):
    """Raised when a 3-D control evidence record is malformed or unsafe."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        raw = strict_json_loads(
            path.read_text(encoding="utf-8-sig"),
            name=f"3-D control evidence {path}",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise Simulator3DControlError(f"cannot load JSON object {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise Simulator3DControlError(f"3-D control evidence must be an object: {path}")
    return raw


def _resolve_artifact_path(value: object, *, source: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Simulator3DControlError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        local = (source.parent / candidate).resolve()
        if local.is_file():
            return local
        candidate = DEFAULT_REPOSITORY_ROOT / candidate
    try:
        return candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise Simulator3DControlError(f"{label} cannot be resolved: {exc}") from exc


def _check(
    condition: bool, checks: dict[str, bool], reasons: list[str], name: str, reason: str
) -> None:
    checks[name] = bool(condition)
    if not condition:
        reasons.append(reason)


def _check_mapping(value: object, *, label: str, reasons: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        reasons.append(f"{label} must be an object")
        return None
    return value


def _verify_source_scenario(
    evidence_path: Path,
    scenario: Mapping[str, Any],
    *,
    scenario_id: str,
    scenario_version: object,
    manifest_canonical_sha256: object,
    runtime_channel_sha256: object,
    reasons: list[str],
) -> None:
    """Re-open the scenario manifest and bind its canonical runtime identity."""

    try:
        manifest_path = _resolve_artifact_path(
            scenario.get("path"), source=evidence_path, label="scenario.path"
        )
    except Simulator3DControlError as exc:
        reasons.append(str(exc))
        return
    declared_sha = scenario.get("sha256")
    if not _is_sha256(declared_sha):
        reasons.append("scenario.sha256 must be a SHA-256 digest")
    elif not manifest_path.is_file():
        reasons.append("scenario manifest does not exist")
    else:
        try:
            actual_sha = _sha256(manifest_path)
        except OSError as exc:
            reasons.append(f"scenario manifest cannot be read: {exc}")
        else:
            if actual_sha.lower() != str(declared_sha).lower():
                reasons.append("scenario manifest SHA-256 does not match evidence")
            try:
                runtime = load_runtime_scenario(manifest_path, mode=RuntimeMode.SIM)
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                reasons.append(f"scenario manifest cannot be parsed: {exc}")
            else:
                if runtime.scenario_id != scenario_id:
                    reasons.append("scenario runtime id does not match evidence")
                if runtime.scenario_version != scenario_version:
                    reasons.append("scenario runtime version does not match evidence")
                if runtime.scenario_manifest_hash != manifest_canonical_sha256:
                    reasons.append("scenario canonical hash does not match evidence")
                if runtime.canonical_hash != runtime_channel_sha256:
                    reasons.append("scenario runtime-channel hash does not match evidence")


def _verify_assets(
    evidence_path: Path,
    runtime_record: Mapping[str, Any],
    *,
    mode: str,
    reasons: list[str],
) -> None:
    # ``three_d_runtime`` is intentionally a compact record: world/model are
    # path strings and their digests live beside them, unlike the nested
    # ``stack_plan`` asset records.
    for label in ("world", "model"):
        path = _resolve_artifact_path(
            runtime_record.get(label),
            source=evidence_path,
            label=f"three_d_runtime.{label}",
        )
        declared = runtime_record.get(f"{label}_sha256")
        if not _is_sha256(declared):
            reasons.append(f"three_d_runtime.{label}_sha256 must be a SHA-256 digest")
        elif not path.is_file():
            reasons.append(f"three_d_runtime.{label} does not exist")
        else:
            try:
                actual = _sha256(path)
            except OSError as exc:
                reasons.append(f"three_d_runtime.{label} cannot be read: {exc}")
            else:
                if actual.lower() != str(declared).lower():
                    reasons.append(f"three_d_runtime.{label} SHA-256 does not match evidence")
        if label == "world" and path.is_file():
            try:
                root = ET.parse(path).getroot()
                world_element = root.find("world")
                physics = world_element.find("physics") if world_element is not None else None
                spherical = (
                    world_element.find("spherical_coordinates")
                    if world_element is not None
                    else None
                )
                if world_element is None or world_element.attrib.get("name") != "default":
                    reasons.append("Gazebo world must expose the default namespace")
                if physics is None or physics.findtext("max_step_size") != "0.004":
                    reasons.append("Gazebo world physics step is not 0.004")
                if spherical is None or spherical.findtext("surface_model") != "EARTH_WGS84":
                    reasons.append("Gazebo world must use EARTH_WGS84")
            except (OSError, ET.ParseError, ValueError) as exc:
                reasons.append(f"Gazebo world cannot be parsed: {exc}")
        if label == "model" and path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                reasons.append(f"Gazebo model cannot be read: {exc}")
            else:
                expected_tokens = {
                    "vtol": ("standard_vtol", "mono_cam", "CameraJoint", "camera_link"),
                    "fixed-wing": ("rc_cessna", "mono_cam", "CameraJoint", "camera_link"),
                    "multirotor": ("x500", "mono_cam", "CameraJoint", "camera_link"),
                    "multirotor-gimbal": (
                        "x500_gimbal", "cgo3_mount_link", "camera_link", "cgo3_vertical_arm_joint"
                    ),
                }[mode]
                if runtime_record.get("airframe_profile_id") == GIMBAL_PROFILE_ID:
                    expected_tokens = (
                        "x500_gimbal", "GimbalAttachJoint", "camera_link", "model://gimbal"
                    )
                if any(token not in text for token in expected_tokens):
                    reasons.append(f"{mode} camera model is incomplete")
                lowered = text.lower()
                if any(token in lowered for token in ("http://", "https://", "fuel://")):
                    reasons.append("Gazebo model must not pull network resources")
    if runtime_record.get("airframe_profile_id") == GIMBAL_PROFILE_ID:
        try:
            contract_path = _resolve_artifact_path(
                runtime_record.get("source_asset_contract"),
                source=evidence_path,
                label="three_d_runtime.source_asset_contract",
            )
        except Simulator3DControlError as exc:
            reasons.append(str(exc))
            return
        declared = runtime_record.get("source_asset_contract_sha256")
        if not _is_sha256(declared) or not contract_path.is_file():
            reasons.append("gimbal source asset contract path/hash is invalid")
        elif _sha256(contract_path).lower() != str(declared).lower():
            reasons.append("gimbal source asset contract SHA-256 does not match evidence")
        else:
            try:
                contract = _load_object(contract_path)
                assets = contract.get("assets")
                expected_names = {
                    "x500_gimbal_model.sdf",
                    "gimbal_model.sdf",
                    "4019_gz_x500_gimbal",
                    "4001_gz_x500",
                }
                expected_image_paths = {
                    "x500_gimbal_model.sdf": "/opt/px4-gazebo/share/gz/models/x500_gimbal/model.sdf",
                    "gimbal_model.sdf": "/opt/px4-gazebo/share/gz/models/gimbal/model.sdf",
                    "4019_gz_x500_gimbal": "/opt/px4-gazebo/etc/init.d-posix/airframes/4019_gz_x500_gimbal",
                    "4001_gz_x500": "/opt/px4-gazebo/etc/init.d-posix/airframes/4001_gz_x500",
                }
                image = contract.get("image")
                if (
                    contract.get("schema_version") != 1
                    or contract.get("contract") != "multidetect.px4-x500-gimbal-source-assets-v3"
                    or image != PINNED_IMAGE
                    or contract.get("image_revision") != image
                    or not isinstance(assets, Mapping)
                    or set(assets) != expected_names
                ):
                    reasons.append("gimbal source asset contract is incomplete or mixed-revision")
                else:
                    declared_hashes = runtime_record.get("source_asset_hashes")
                    if not isinstance(declared_hashes, Mapping):
                        declared_hashes = {}
                    for name, record in assets.items():
                        asset_path = contract_path.parent / name
                        if (
                            not isinstance(record, Mapping)
                            or record.get("image_path") != expected_image_paths[name]
                            or not asset_path.is_file()
                            or record.get("sha256") != _sha256(asset_path)
                            or declared_hashes.get(name) != record.get("sha256")
                        ):
                            reasons.append(f"gimbal source asset hash does not match evidence: {name}")
                    try:
                        x500_root = ET.parse(
                            contract_path.parent / "x500_gimbal_model.sdf"
                        ).getroot()
                        x500_model = x500_root.find("model")
                        if x500_model is None or x500_model.attrib.get("name") != "x500_gimbal":
                            reasons.append("gimbal composition source is not the x500_gimbal model")
                        component_includes = (
                            {uri.text for uri in x500_model.findall("include/uri") if uri.text}
                            if x500_model is not None
                            else set()
                        )
                        gimbal_text = (contract_path.parent / "gimbal_model.sdf").read_text(
                            encoding="utf-8"
                        )
                        gimbal_root = ET.fromstring(gimbal_text)
                        gimbal_links = {
                            link.attrib.get("name") for link in gimbal_root.findall(".//link")
                        }
                        startup_text = (contract_path.parent / "4019_gz_x500_gimbal").read_text(
                            encoding="utf-8"
                        )
                    except (OSError, UnicodeError) as exc:
                        reasons.append(f"gimbal source assets cannot be read: {exc}")
                    else:
                        if not {"model://x500", "model://gimbal"}.issubset(component_includes):
                            reasons.append("gimbal x500 composition is missing component includes")
                        if not {"camera_link", "cgo3_mount_link"}.issubset(gimbal_links):
                            reasons.append("gimbal source links are incomplete")
                        if not re.search(
                            r"(?m)^PX4_SIM_MODEL=\$\{PX4_SIM_MODEL:=x500_gimbal\}\s*$",
                            startup_text,
                        ):
                            reasons.append("gimbal PX4 startup file does not default to x500_gimbal")
                        if not re.search(
                            r"(?m)^\. \$\{R\}etc/init\.d-posix/airframes/4001_gz_x500\s*$",
                            startup_text,
                        ):
                            reasons.append("gimbal PX4 startup file does not source 4001_gz_x500")
            except (OSError, ValueError, TypeError) as exc:
                reasons.append(f"gimbal source asset contract cannot be verified: {exc}")


def verify_document(
    document: Mapping[str, Any],
    *,
    expected_release_label: str | None = None,
    evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify one runner artifact without executing the runner again."""

    if not isinstance(document, Mapping):
        raise Simulator3DControlError("3-D control evidence must be an object")
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    unknown = sorted(set(document).difference(_REQUIRED_TOP_LEVEL))
    _check(
        not unknown, checks, reasons, "top_level_fields", f"unknown top-level field(s): {unknown!r}"
    )
    for field in sorted(_REQUIRED_TOP_LEVEL):
        _check(
            field in document, checks, reasons, f"field_{field}", f"missing required field: {field}"
        )

    _check(
        document.get("schema_version") == SCHEMA_VERSION,
        checks,
        reasons,
        "schema_version",
        "schema_version must be 1",
    )
    _check(document.get("event") == EVENT, checks, reasons, "event", f"event must be {EVENT}")
    _check(
        document.get("product") == "Multi-Detect",
        checks,
        reasons,
        "product",
        "product must be Multi-Detect",
    )
    release_label = document.get("release_label")
    _check(
        isinstance(release_label, str)
        and bool(release_label.strip())
        and (expected_release_label is None or release_label == expected_release_label),
        checks,
        reasons,
        "release_label",
        "release_label is missing or does not match the product manifest",
    )
    _check(
        document.get("runtime_mode") == "sim",
        checks,
        reasons,
        "runtime_mode",
        "runtime_mode must be sim",
    )
    _check(
        document.get("gate_passed") is True,
        checks,
        reasons,
        "gate_passed",
        "gate_passed must be true",
    )
    _check(
        document.get("deterministic_gate_passed") is True,
        checks,
        reasons,
        "deterministic_gate_passed",
        "deterministic_gate_passed must be true",
    )
    _check(
        document.get("external_runtime_gate_passed") is False,
        checks,
        reasons,
        "external_runtime_gate_passed",
        "external_runtime_gate_passed must remain false for the deterministic gate",
    )
    _check(
        document.get("gate_scope") == "deterministic_protocol_asset_preflight",
        checks,
        reasons,
        "gate_scope",
        "unexpected 3-D gate scope",
    )
    _check(
        document.get("external_runtime_required_for_real_3d") is True,
        checks,
        reasons,
        "external_runtime_required",
        "external_runtime_required_for_real_3d must be true",
    )

    scenario = _check_mapping(document.get("scenario"), label="scenario", reasons=reasons)
    scenario_id: str | None = None
    scenario_version: object = None
    if scenario is not None:
        missing = sorted(_REQUIRED_SCENARIO.difference(scenario))
        _check(
            not missing,
            checks,
            reasons,
            "scenario_fields",
            f"scenario missing field(s): {missing!r}",
        )
        scenario_id_value = scenario.get("scenario_id")
        scenario_id = scenario_id_value if isinstance(scenario_id_value, str) else None
        scenario_version = scenario.get("scenario_version")
        expected_mode = AIRFRAME_SCENARIOS.get(scenario_id or "")
        _check(
            expected_mode is not None,
            checks,
            reasons,
            "scenario_identity",
            "scenario_id is not a supported three-airframe identity",
        )
        _check(
            _is_sha256(scenario.get("manifest_canonical_sha256")),
            checks,
            reasons,
            "scenario_manifest_hash",
            "scenario.manifest_canonical_sha256 must be a SHA-256 digest",
        )
        _check(
            _is_sha256(scenario.get("runtime_channel_sha256")),
            checks,
            reasons,
            "scenario_runtime_hash",
            "scenario.runtime_channel_sha256 must be a SHA-256 digest",
        )
    else:
        expected_mode = None

    runtime_record = _check_mapping(
        document.get("three_d_runtime"), label="three_d_runtime", reasons=reasons
    )
    if runtime_record is not None and expected_mode is not None:
        _check(
            runtime_record.get("mode") == expected_mode,
            checks,
            reasons,
            "runtime_mode_binding",
            "three_d_runtime.mode does not match scenario identity",
        )
        _check(
            runtime_record.get("visual_acceptance") == "not_claimed",
            checks,
            reasons,
            "runtime_visual_not_claimed",
            "three_d_runtime visual acceptance must remain not_claimed",
        )
        _check(
            runtime_record.get("hardware_acceptance") == "not_claimed",
            checks,
            reasons,
            "runtime_hardware_not_claimed",
            "three_d_runtime hardware acceptance must remain not_claimed",
        )
        scenario_vehicle = scenario.get("vehicle") if scenario is not None else None
        declared_profile = (
            scenario_vehicle.get("airframe_profile_id")
            if isinstance(scenario_vehicle, Mapping)
            else None
        )
        if (
            declared_profile == GIMBAL_PROFILE_ID
            or runtime_record.get("airframe_profile_id") == GIMBAL_PROFILE_ID
        ):
            _check(
                runtime_record.get("airframe_profile_id") == declared_profile,
                checks,
                reasons,
                "airframe_profile_binding",
                "three_d_runtime airframe profile does not match scenario vehicle",
            )
    if evidence_path is not None and scenario is not None and scenario_id is not None:
        _verify_source_scenario(
            Path(evidence_path),
            scenario,
            scenario_id=scenario_id,
            scenario_version=scenario_version,
            manifest_canonical_sha256=scenario.get("manifest_canonical_sha256"),
            runtime_channel_sha256=scenario.get("runtime_channel_sha256"),
            reasons=reasons,
        )
    if evidence_path is not None and runtime_record is not None and expected_mode is not None:
        _verify_assets(Path(evidence_path), runtime_record, mode=expected_mode, reasons=reasons)

    stack_plan = _check_mapping(document.get("stack_plan"), label="stack_plan", reasons=reasons)
    if stack_plan is not None:
        embedded = stack_plan.get("sha256")
        canonical = dict(stack_plan)
        canonical.pop("sha256", None)
        try:
            recomputed = canonical_sha256(canonical)
        except (TypeError, ValueError) as exc:
            recomputed = None
            reasons.append(f"stack_plan cannot be canonically hashed: {exc}")
        _check(
            _is_sha256(embedded),
            checks,
            reasons,
            "stack_plan_hash",
            "stack_plan.sha256 must be a SHA-256 digest",
        )
        _check(
            _is_sha256(document.get("stack_plan_sha256")),
            checks,
            reasons,
            "stack_plan_sha256",
            "stack_plan_sha256 must be a SHA-256 digest",
        )
        if recomputed is not None:
            _check(
                recomputed == embedded,
                checks,
                reasons,
                "stack_plan_digest",
                "stack_plan embedded digest does not match canonical content",
            )
            _check(
                recomputed == document.get("stack_plan_sha256"),
                checks,
                reasons,
                "stack_plan_digest_binding",
                "stack_plan_sha256 does not match canonical content",
            )
        _check(
            stack_plan.get("event") == "simulator_stack_plan",
            checks,
            reasons,
            "stack_plan_event",
            "stack_plan event is invalid",
        )
        _check(
            stack_plan.get("truth_free") is True,
            checks,
            reasons,
            "stack_plan_truth_free",
            "stack_plan truth_free must be true",
        )
        if scenario is not None:
            _check(
                stack_plan.get("scenario_id") == scenario.get("scenario_id"),
                checks,
                reasons,
                "stack_plan_scenario",
                "stack_plan scenario_id does not match evidence",
            )
            _check(
                stack_plan.get("scenario_version") == scenario.get("scenario_version"),
                checks,
                reasons,
                "stack_plan_version",
                "stack_plan scenario_version does not match evidence",
            )
            _check(
                stack_plan.get("scenario_manifest_hash")
                == scenario.get("manifest_canonical_sha256"),
                checks,
                reasons,
                "stack_plan_manifest_hash",
                "stack_plan scenario manifest hash does not match evidence",
            )
        runtime = stack_plan.get("runtime")
        validation = stack_plan.get("validation")
        ports = stack_plan.get("ports")
        _check(
            isinstance(runtime, Mapping) and runtime.get("loopback_only") is True,
            checks,
            reasons,
            "stack_plan_loopback",
            "stack_plan runtime must be loopback-only",
        )
        _check(
            isinstance(runtime, Mapping) and runtime.get("protected_udp_14550_used") is False,
            checks,
            reasons,
            "stack_plan_protected_port",
            "stack_plan must not use protected UDP 14550",
        )
        _check(
            isinstance(validation, Mapping) and validation.get("gate_passed") is True,
            checks,
            reasons,
            "stack_plan_validation",
            "stack_plan validation gate must pass",
        )
        _check(
            isinstance(validation, Mapping) and validation.get("ports_distinct") is True,
            checks,
            reasons,
            "stack_plan_ports_distinct",
            "stack_plan ports must be distinct",
        )
        _check(
            isinstance(ports, Mapping)
            and ports.get("loopback_only") is True
            and 14550 not in ports.get("all_ports", []),
            checks,
            reasons,
            "stack_plan_ports",
            "stack_plan ports must be literal loopback and exclude 14550",
        )

    binding = _check_mapping(
        document.get("stack_plan_binding"), label="stack_plan_binding", reasons=reasons
    )
    if binding is not None:
        _check(
            binding.get("gate_passed") is True,
            checks,
            reasons,
            "stack_plan_binding_gate",
            "stack_plan_binding.gate_passed must be true",
        )
        binding_checks = binding.get("checks")
        _check(
            isinstance(binding_checks, Mapping)
            and all(value is True for value in binding_checks.values()),
            checks,
            reasons,
            "stack_plan_binding_checks",
            "stack_plan_binding checks must all be true",
        )
        _check(
            binding.get("external_process_acceptance") == "not_claimed",
            checks,
            reasons,
            "stack_plan_external_not_claimed",
            "stack plan external process acceptance must remain not_claimed",
        )

    contract = _check_mapping(
        document.get("control_contract"), label="control_contract", reasons=reasons
    )
    if contract is not None:
        _check(
            contract.get("deterministic_fixture") is True,
            checks,
            reasons,
            "control_fixture",
            "control_contract must identify a deterministic fixture",
        )
        _check(
            contract.get("gate_passed") is True,
            checks,
            reasons,
            "control_gate",
            "control_contract.gate_passed must be true",
        )
        if expected_mode is not None:
            _check(
                contract.get("vehicle_mode") == expected_mode,
                checks,
                reasons,
                "control_vehicle_mode",
                "control_contract vehicle_mode does not match scenario",
            )
        manual = _check_mapping(
            contract.get("manual"), label="control_contract.manual", reasons=reasons
        )
        ai = _check_mapping(contract.get("ai"), label="control_contract.ai", reasons=reasons)
        watchdog = _check_mapping(
            contract.get("watchdog"), label="control_contract.watchdog", reasons=reasons
        )
        qgc_only = _check_mapping(
            contract.get("qgc_only"), label="control_contract.qgc_only", reasons=reasons
        )
        if manual is not None:
            for key in (
                "qgc_frame_accepted",
                "authority_takeover",
                "vehicle_advanced",
                "gate_passed",
            ):
                _check(
                    manual.get(key) is True,
                    checks,
                    reasons,
                    f"manual_{key}",
                    f"manual.{key} must be true",
                )
        if ai is not None:
            for key in (
                "track_accepted",
                "suppressed_during_manual",
                "resumed_after_release",
                "gate_passed",
            ):
                _check(ai.get(key) is True, checks, reasons, f"ai_{key}", f"ai.{key} must be true")
            _check(
                isinstance(ai.get("accepted_step_count"), int)
                and ai.get("accepted_step_count", 0) >= 1,
                checks,
                reasons,
                "ai_steps",
                "ai accepted_step_count must be positive",
            )
        if watchdog is not None:
            for key in ("failsafe", "neutral_frame", "gate_passed"):
                _check(
                    watchdog.get(key) is True,
                    checks,
                    reasons,
                    f"watchdog_{key}",
                    f"watchdog.{key} must be true",
                )
        if qgc_only is not None:
            for key in (
                "sole_operator_ui",
                "native_manual_control_path",
                "independent_simulator_window_created",
                "flight_write_commands_blocked",
                "mission_write_commands_blocked",
                "loopback_only",
                "gate_passed",
            ):
                expected = False if key == "independent_simulator_window_created" else True
                _check(
                    qgc_only.get(key) is expected,
                    checks,
                    reasons,
                    f"qgc_{key}",
                    f"qgc_only.{key} must be {expected}",
                )

    interactive = _check_mapping(
        document.get("interactive_qgc_path"), label="interactive_qgc_path", reasons=reasons
    )
    _check(
        interactive is not None and interactive.get("gate_passed") is True,
        checks,
        reasons,
        "interactive_gate",
        "interactive_qgc_path.gate_passed must be true",
    )
    launch = _check_mapping(
        document.get("launch_readiness"), label="launch_readiness", reasons=reasons
    )
    if launch is not None:
        _check(
            launch.get("external_3d_acceptance") == "not_claimed",
            checks,
            reasons,
            "launch_external_not_claimed",
            "launch external 3-D acceptance must remain not_claimed",
        )
        external = launch.get("external_runtime")
        _check(
            isinstance(external, Mapping) and external.get("acceptance") == "not_claimed",
            checks,
            reasons,
            "runtime_external_not_claimed",
            "external runtime acceptance must remain not_claimed",
        )
        _check(
            isinstance(external, Mapping)
            and external.get("process_started") is False
            and external.get("io_opened") is False
            and external.get("telemetry_observed") is False,
            checks,
            reasons,
            "runtime_no_external_io",
            "deterministic evidence must not claim an external process or IO",
        )
    px4_launch = _check_mapping(document.get("px4_launch"), label="px4_launch", reasons=reasons)
    if px4_launch is not None:
        _check(
            px4_launch.get("plan_generated") is True,
            checks,
            reasons,
            "px4_plan_generated",
            "px4 launch plan must be generated",
        )
        _check(
            px4_launch.get("preflight_gate_passed") is True,
            checks,
            reasons,
            "px4_preflight",
            "px4 launch preflight must pass",
        )
        _check(
            px4_launch.get("artifacts_verified") is True and px4_launch.get("hash_bound") is True,
            checks,
            reasons,
            "px4_artifacts",
            "px4 launch artifacts must be hash-bound and verified",
        )
        _check(
            px4_launch.get("external_process_started") is False
            and px4_launch.get("external_io_opened") is False
            and px4_launch.get("telemetry_observed") is False,
            checks,
            reasons,
            "px4_no_external_io",
            "px4 launch record must not claim external execution",
        )
        _check(
            px4_launch.get("external_3d_acceptance") == "not_claimed",
            checks,
            reasons,
            "px4_external_not_claimed",
            "px4 external 3-D acceptance must remain not_claimed",
        )

    claims = _check_mapping(document.get("claims"), label="claims", reasons=reasons)
    if claims is not None:
        for key in _REQUIRED_CLAIMS:
            _check(
                claims.get(key) is True,
                checks,
                reasons,
                f"claim_{key}",
                f"claims.{key} must be true",
            )
        if expected_mode is not None:
            _check(
                claims.get("vehicle_mode") == expected_mode,
                checks,
                reasons,
                "claim_vehicle_mode",
                "claims.vehicle_mode does not match scenario",
            )
        for key in (
            "px4_gazebo_acceptance",
            "external_3d_runtime_acceptance",
            "visual_acceptance",
            "pixhawk_hardware_acceptance",
        ):
            _check(
                claims.get(key) == "not_claimed",
                checks,
                reasons,
                f"claim_{key}_not_claimed",
                f"claims.{key} must remain not_claimed",
            )

    deterministic_control = document.get("deterministic_control")
    _check(
        deterministic_control == document.get("control_contract"),
        checks,
        reasons,
        "deterministic_control_alias",
        "deterministic_control must match control_contract",
    )

    return {
        "event": VERIFIED_EVENT,
        "schema_version": SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "mode": expected_mode,
        "release_label": release_label,
        "gate_passed": not reasons,
        "checks": checks,
        "reasons": reasons,
    }


def verify_file(
    path: str | Path,
    *,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Load and independently verify one 3-D control evidence file."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise Simulator3DControlError(f"3-D control evidence does not exist: {source}")
    return verify_document(
        _load_object(source),
        expected_release_label=expected_release_label,
        evidence_path=source,
    )


def build_binding(
    evidence_paths: Sequence[str | Path],
    *,
    manifest_directory: str | Path,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic hash binding for all three 3-D airframes."""

    if isinstance(evidence_paths, (str, bytes, bytearray)) or not isinstance(
        evidence_paths, Sequence
    ):
        raise Simulator3DControlError("simulator_3d_control evidence_paths must be a sequence")
    if len(evidence_paths) != 3:
        raise Simulator3DControlError("simulator_3d_control requires exactly three evidence files")
    output_dir = Path(manifest_directory).expanduser().resolve()
    records: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    seen_ids: set[str] = set()
    seen_modes: set[str] = set()
    for value in evidence_paths:
        source = Path(value).expanduser().resolve()
        if source in seen_paths:
            raise Simulator3DControlError(f"simulator_3d_control evidence is reused: {source}")
        seen_paths.add(source)
        verification = verify_file(source, expected_release_label=expected_release_label)
        if verification.get("gate_passed") is not True:
            detail = "; ".join(str(reason) for reason in verification.get("reasons", []) if reason)
            raise Simulator3DControlError(
                f"simulator_3d_control evidence verifier did not pass: {detail}"
            )
        scenario_id = verification.get("scenario_id")
        mode = verification.get("mode")
        if not isinstance(scenario_id, str) or scenario_id in seen_ids:
            raise Simulator3DControlError("simulator_3d_control scenario IDs must be unique")
        if not isinstance(mode, str) or mode in seen_modes:
            raise Simulator3DControlError("simulator_3d_control vehicle modes must be unique")
        seen_ids.add(scenario_id)
        seen_modes.add(mode)
        document = _load_object(source)
        records.append(
            {
                "path": _relative_path(source, output_dir),
                "sha256": _sha256(source),
                "bytes": source.stat().st_size,
                "scenario_id": scenario_id,
                "mode": mode,
                "stack_plan_sha256": document.get("stack_plan_sha256"),
                "gate_passed": document.get("gate_passed"),
                "deterministic_gate_passed": document.get("deterministic_gate_passed"),
                "external_runtime_gate_passed": document.get("external_runtime_gate_passed"),
            }
        )
    records.sort(key=lambda item: str(item["mode"]))
    checks = {
        "evidence_count": len(records) == 3,
        "all_airframes": sorted(seen_modes) == list(REQUIRED_MODES),
        "unique_scenarios": len(seen_ids) == 3,
        "unique_modes": len(seen_modes) == 3,
        "all_gate_passed": all(item["gate_passed"] is True for item in records),
        "all_deterministic_gate_passed": all(
            item["deterministic_gate_passed"] is True for item in records
        ),
        "external_runtime_not_claimed": all(
            item["external_runtime_gate_passed"] is False for item in records
        ),
        "records_hash_bound": all(
            _is_sha256(item["sha256"]) and _is_sha256(item["stack_plan_sha256"]) for item in records
        ),
    }
    if not all(checks.values()):
        failed = ", ".join(name for name, value in checks.items() if not value)
        raise Simulator3DControlError(f"simulator_3d_control binding checks failed: {failed}")
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence": records,
        "scenario_ids": sorted(seen_ids),
        "modes": sorted(seen_modes),
        "gate_passed": True,
        "deterministic_gate_passed": True,
        "external_runtime_gate_passed": False,
        "external_runtime_acceptance": "not_claimed",
        "visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "checks": checks,
    }


def _relative_path(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        import os

        return os.path.relpath(path, base).replace(os.sep, "/")


def main(argv: list[str] | None = None) -> int:
    """Verify one 3-D record or bind the three airframe records from a shell."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "evidence",
        type=Path,
        nargs="+",
        help="one evidence JSON, or three JSON files when --bind is supplied",
    )
    parser.add_argument("--expected-release-label", default=None)
    parser.add_argument("--bind", action="store_true")
    parser.add_argument("--manifest-directory", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.bind:
            if len(args.evidence) != 3:
                raise Simulator3DControlError("--bind requires exactly three evidence files")
            result = {
                "event": "simulator_3d_control_binding_verified",
                **build_binding(
                    args.evidence,
                    manifest_directory=args.manifest_directory,
                    expected_release_label=args.expected_release_label,
                ),
            }
        else:
            if len(args.evidence) != 1:
                raise Simulator3DControlError(
                    "single-file verification accepts exactly one evidence file; "
                    "use --bind for three"
                )
            source = args.evidence[0].expanduser().resolve()
            result = {
                "evidence_path": str(source),
                **verify_file(source, expected_release_label=args.expected_release_label),
            }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        result = {
            "event": (
                "simulator_3d_control_binding_failed"
                if args.bind
                else "simulator_3d_control_verification_failed"
            ),
            "schema_version": SCHEMA_VERSION,
            "gate_passed": False,
            "checks": {"readable_json": False},
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("gate_passed") is True else 1


__all__ = [
    "AIRFRAME_SCENARIOS",
    "GIMBAL_PROFILE_ID",
    "EVENT",
    "REQUIRED_MODES",
    "SCHEMA_VERSION",
    "Simulator3DControlError",
    "VERIFIED_EVENT",
    "build_binding",
    "main",
    "verify_document",
    "verify_file",
]


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())
