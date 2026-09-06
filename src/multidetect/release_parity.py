"""Deterministic shared-contract parity hashes for the E7 release gate.

The release gate needs to prove that REPLAY, SIM, HIL and REAL consume the
same five contracts.  This module computes those hashes from an explicit,
auditable list of repository-relative source files.  It intentionally does
not walk the repository: generated output, evidence and fixtures must never
silently become part of a release hash.

Hash framing (schema version 1) is deliberately boring and documented so a
second implementation can reproduce it byte-for-byte::

    ``MD-PARITY-V1\\0``
    ``u64be(len(group_utf8)) + group_utf8``
    ``u64be(number_of_files)``
    repeated for each path sorted by its POSIX relative path:
    ``u64be(len(path_utf8)) + path_utf8``
    ``u64be(len(content_after_crlf_normalisation)) + content``

Only CRLF and lone CR are changed to LF.  No whitespace, final newline or
Unicode normalization is otherwise performed.  Paths are normalized to
forward-slash UTF-8 and are resolved beneath the selected repository root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from .product_version import load_product_version

PARITY_SCHEMA_VERSION = 1
"""Schema version of the generated ``release-parity.json`` document."""

PARITY_FIELDS: tuple[str, ...] = (
    "ui_contract_hash",
    "core_algorithm_hash",
    "task_contract_hash",
    "safety_policy_hash",
    "px4_mission_path_hash",
)

CONTRACT_GROUPS: tuple[str, ...] = (
    "ui_contract",
    "core_algorithm",
    "task_contract",
    "safety_policy",
    "px4_mission_path",
)

# Keep this list explicit rather than globbing.  These are the canonical
# source surfaces shared by every runtime mode; generated ``*.qmlc`` files,
# build trees, evidence and scenario truth are intentionally absent.
DEFAULT_FILE_LIST: dict[str, tuple[str, ...]] = {
    "ui_contract": (
        "ground-control/MultiDetectGCS/custom/CMakeLists.txt",
        "ground-control/MultiDetectGCS/custom/custom.qrc",
        "ground-control/MultiDetectGCS/custom/res/Custom/MultiDetect/MultiDetectState.qml",
        "ground-control/MultiDetectGCS/custom/res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml",
        "ground-control/MultiDetectGCS/custom/res/Custom/MultiDetect/MultiDetectVideoOverlay.qml",
        "ground-control/MultiDetectGCS/custom/src/CustomPlugin.cc",
        "ground-control/MultiDetectGCS/custom/src/CustomPlugin.h",
        "ground-control/MultiDetectGCS/custom/src/FlyViewCustomLayer.qml",
        "ground-control/MultiDetectGCS/custom/src/FlyViewVideo.qml",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectConfigure.qml",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.cc",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.h",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorProtocol.cc",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorProtocol.h",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetEvidenceStore.cc",
        "ground-control/MultiDetectGCS/custom/src/MultiDetectTargetEvidenceStore.h",
        "ground-control/MultiDetectGCS/custom/src/PlanViewRightPanel.qml",
        "ground-control/MultiDetectGCS/custom/src/SelectViewDropdown.qml",
    ),
    "core_algorithm": (
        # Gazebo is a SIM adapter, but its fixed wire and ENU/FLU covariance
        # conversion feed the exact shared pose/ranging contract.  Bind both
        # producer and consumer so SIM cannot retain a stale core identity
        # after its telemetry semantics diverge from REPLAY/HIL/REAL.
        "scripts/gazebo_sensor_stream.py",
        "src/multidetect/backends/gazebo_sim.py",
        "src/multidetect/adaptive_ranging.py",
        "src/multidetect/depth_calibration.py",
        # The platform-neutral task planner is shared by REPLAY, SIM, HIL and
        # REAL; bind it to the core hash so distance/energy/readiness changes
        # cannot drift away from the common mission path.
        "src/multidetect/autonomy/planner.py",
        # Mission progress is the shared observation boundary between the
        # compiled PX4 mission and SIM/HIL/REAL telemetry.  Bind it here so
        # acceptance-radius and GPS-freshness changes cannot drift by mode.
        "src/multidetect/mission_progress.py",
        "configs/schemas/mission-progress.schema.json",
        # MAVLink GPS enum/sentinel normalisation is a shared wire-domain
        # contract.  Bind it to the core hash so SIM/HIL/REAL cannot silently
        # diverge on fix, satellite, or unknown-value semantics.
        "src/multidetect/mavlink_gps.py",
        "src/multidetect/metric_depth.py",
        # Multi-modal slant/ground ranging and its uncertainty propagation are
        # shared by SIM, REPLAY, HIL and REAL; bind the complete estimator so
        # a near-horizon or stale-distance change cannot drift by mode.  Its
        # strict camera document is part of the same runtime contract: a schema
        # semantics change must invalidate parity just like estimator code.
        "configs/schemas/camera-calibration.schema.json",
        "src/multidetect/multimodal_ranging.py",
        # Exact bilinear DEM ray intersection and GPS/terrain uncertainty are
        # shared estimator code, not simulator fixtures.  Any change must
        # invalidate one common algorithm identity across every runtime mode.
        "src/multidetect/terrain_dem.py",
        "src/multidetect/navigation_fusion.py",
        # The deterministic SIM policy is the same bounded AI decision path
        # exercised by HIL/REAL adapters; bind it to parity so a return-gate
        # freshness change cannot ship outside the shared algorithm hash.
        "src/multidetect/simulator/ai_executor.py",
        # The fallback flight model publishes the geodetic endpoint consumed
        # by SIM target evidence.  Bind it alongside the WGS-84 projector so
        # SIM cannot silently regress to a spherical/equirectangular position
        # while REAL keeps the shared ellipsoid implementation.
        "src/multidetect/simulator/flight_model.py",
        "src/multidetect/simulator/perception_bridge.py",
        # Target-event evidence carries the exact camera calibration consumed
        # by target geolocation.  Bind both the loader/builder and its strict
        # manifest so a schema drift invalidates the common algorithm identity.
        "configs/schemas/target-event-replay-manifest.schema.json",
        "src/multidetect/target_event_replay.py",
        # Detection boxes become operational target rays only after the
        # shared semantic anchor policy selects a ground-contact/image-center
        # point and its pixel uncertainty.  Bind that policy to the same core
        # hash as ranging and WGS-84 projection so SIM/HIL/REAL cannot drift
        # while retaining a stale parity identity.
        "src/multidetect/target_image_anchor.py",
        "src/multidetect/target_geolocation.py",
    ),
    "task_contract": (
        "configs/schemas/autonomy-platform-neutral-plan.schema.json",
        # A field bundle cannot claim its evidence join is complete from
        # nominal camera mount numbers alone.  Bind both the physical survey
        # contract and the seven-stratum join so every runtime mode shares the
        # same fail-closed provenance boundary.
        "configs/schemas/camera-mount-survey.schema.json",
        "configs/schemas/camera-mount-measurement-preflight.schema.json",
        "configs/schemas/camera-mount-equipment-calibration.schema.json",
        "configs/schemas/field-evidence-bundle.schema.json",
        "src/multidetect/camera_mount_survey.py",
        "src/multidetect/field_evidence_bundle.py",
        # Raw replay carries the same absolute GPS altitude used by live/HIL
        # terrain ranging.  Bind the schema and both canonical serializers so
        # REPLAY cannot silently omit a field that REAL supplies.
        "configs/schemas/raw-replay-telemetry-sidecar.schema.json",
        "src/multidetect/backends/raw_replay.py",
        "src/multidetect/domain.py",
        "src/multidetect/gnss_capture_sidecar.py",
        "src/multidetect/mission.py",
        # The Python status model, canonical wire vocabulary and status
        # builders must move together with the native QGC decoder.  Binding
        # all three prevents an internal fail-closed reason from becoming an
        # unencodable live-status exception in only one runtime mode.
        "src/multidetect/operator_link.py",
        "src/multidetect/operator_protocol.py",
        "src/multidetect/operator_status.py",
        "src/multidetect/operator_tracking.py",
        "src/multidetect/replay.py",
    ),
    "safety_policy": (
        "configs/schemas/autonomy-operator-grant.schema.json",
        # Repository-owned map/DEM resolution is an isolation gate: missing,
        # remote, malformed or world-origin-mismatched assets must fail before
        # a SIM run can claim map/terrain availability.
        "configs/schemas/local-dem-grid.schema.json",
        "src/multidetect/authorization.py",
        # Physical HIL/REAL connectivity is a safety gate, not workstation
        # setup trivia. Bind the read-only inventory, secondary-address
        # planner/applicator, wrapper, CLI shims and canonical verifier so a
        # link-readiness change invalidates stale field evidence everywhere.
        "scripts/prepare_field_link.ps1",
        "scripts/verify_field_link_address_state.py",
        "scripts/inspect_field_link_interfaces.ps1",
        "scripts/run_field_link_preflight.ps1",
        "scripts/run_field_link_preflight.py",
        "scripts/verify_field_link_preflight.py",
        "src/multidetect/field_link_preflight.py",
        "src/multidetect/field_link_address_state.py",
        "configs/schemas/field-link-address-state.schema.json",
        # A REAL alert-link report is release evidence only when the same
        # acknowledged transport and bench contract bind its selected source
        # route. Keep protocol, runner and schema in the shared safety hash.
        "src/multidetect/alerts.py",
        "src/multidetect/alert_link_topology.py",
        "src/multidetect/alert_link_bench.py",
        "configs/schemas/alert-link-topology.schema.json",
        "configs/schemas/alert-link-bench.schema.json",
        # The GR01 operator-selection bench uses the same physical-route
        # boundary: bind its connected UDP sender, strict report verifier and
        # schema so stale or loopback evidence cannot survive a parity change.
        "src/multidetect/operator_udp.py",
        "src/multidetect/gr01_bench.py",
        "src/multidetect/cli.py",
        "scripts/verify_gr01_link_bench.py",
        "configs/schemas/gr01-link-bench.schema.json",
        # A field pass is publishable only after the bench, manifest and E7
        # report are independently reopened and freshness-recomputed. Bind
        # both sides of that transaction plus its closed record contract.
        "src/multidetect/gr01_release_session.py",
        "scripts/run_gr01_release_session.ps1",
        "scripts/run_multidetect_cli.py",
        "scripts/verify_gr01_release_session.py",
        "configs/schemas/gr01-release-session-plan.schema.json",
        "configs/schemas/gr01-release-session.schema.json",
        "src/multidetect/safety.py",
        "src/multidetect/simulator/control.py",
        "src/multidetect/simulator/control_gateway.py",
        # Bind the sole MAVLink actuator adapter to the safety contract.  The
        # manual/AI watchdog and neutral wire encoding are safety semantics,
        # not merely transport implementation details.
        "src/multidetect/simulator/mavlink_control.py",
        "src/multidetect/simulator/stack.py",
        "src/multidetect/state_machine.py",
    ),
    "px4_mission_path": (
        "configs/schemas/pixhawk-odometry-covariance-observation.schema.json",
        "configs/schemas/pixhawk-covariance-field-campaign.schema.json",
        "configs/schemas/pixhawk-covariance-online-preflight.schema.json",
        "configs/schemas/pixhawk-covariance-field-workspace.schema.json",
        "configs/schemas/pixhawk-covariance-field-workspace-receipt.schema.json",
        "configs/schemas/pixhawk-covariance-field-session-record.schema.json",
        "configs/schemas/pixhawk-covariance-field-journal.schema.json",
        "configs/schemas/autonomy-executable-mission.schema.json",
        "configs/schemas/autonomy-mission-compile-runtime.schema.json",
        "configs/schemas/px4-mission-feedback.schema.json",
        "configs/schemas/px4-mission-feedback-observation.schema.json",
        "configs/schemas/px4-mission-feedback-observation-v2.schema.json",
        "configs/schemas/runtime-profile.schema.json",
        "configs/schemas/runtime-profile-document.schema.json",
        # The E7 HIL/REAL x three-airframe join is part of the Mission path:
        # changing its source binding or fail-closed release contract must
        # invalidate all previously captured feedback matrices.
        "configs/schemas/release-gate-manifest.schema.json",
        # This typed seam is the only authoring-side join between the shared
        # planner and the vehicle-specific PX4 compilers.
        "src/multidetect/mission_compile_cli.py",
        "src/multidetect/mission_planning.py",
        # Mode-3 confirmation state and the bounded SET_ATTITUDE_TARGET
        # transport are part of the physical output path.  Hash both so a
        # runtime cannot ship different live-attestation or pilot-cancel
        # semantics while retaining the same claimed PX4 Mission parity.
        "src/multidetect/approach_live.py",
        "src/multidetect/fixed_wing_aim_control.py",
        "src/multidetect/operator_mavlink.py",
        "src/multidetect/payload_hil_mission.py",
        "src/multidetect/payload_target_gate.py",
        # Bind the actual receive-only Pixhawk ingress and hash-bound Mission
        # feedback observer.  Otherwise SIM/HIL/REAL could silently disagree
        # about MISSION_CURRENT freshness or MISSION_ITEM_REACHED identity.
        # The ODOMETRY covariance observer is likewise the executable evidence
        # boundary for source-bound estimator attitude uncertainty; binding it
        # prevents a stale capture/verifier from blessing a changed parser.
        "scripts/capture_pixhawk_odometry_covariance.py",
        "src/multidetect/pixhawk.py",
        "src/multidetect/pixhawk_covariance_binding.py",
        "src/multidetect/field_covariance_campaign.py",
        "src/multidetect/online_covariance_preflight.py",
        "src/multidetect/field_covariance_workspace.py",
        "src/multidetect/field_covariance_session.py",
        "src/multidetect/field_covariance_journal.py",
        "src/multidetect/px4_mission.py",
        "src/multidetect/px4_mission_feedback.py",
        "src/multidetect/px4_mission_feedback_binding.py",
        "src/multidetect/px4_mission_feedback_cli.py",
        "src/multidetect/px4_mission_feedback_runtime.py",
        "src/multidetect/runtime.py",
        "src/multidetect/runtime_launcher.py",
        "src/multidetect/runtime_profiles.py",
    ),
}

# A mapping alias is convenient for callers and makes the intended default
# configuration obvious in introspection.  Values remain tuples internally so
# callers cannot mutate the module-level default accidentally.
DEFAULT_CONTRACT_FILES = DEFAULT_FILE_LIST

_GROUP_TO_PARITY_FIELD = dict(zip(CONTRACT_GROUPS, PARITY_FIELDS, strict=True))
_FRAME_PREFIX = b"MD-PARITY-V1\x00"
_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".cache",
        "artifact",
        "artifacts",
        "fixture",
        "fixtures",
        "build",
        "builds",
    }
)
_FORBIDDEN_NAME = re.compile(r"^(?:artifact|fixture|build)(?:$|[-_.])", re.IGNORECASE)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class ReleaseParityError(ValueError):
    """Raised when a parity file list or source tree is not valid."""


def _default_root() -> Path:
    """Find the repository root containing the canonical version manifest."""

    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        if (parent / "configs" / "product-version.json").is_file():
            return parent
    return Path.cwd().resolve()


def _resolve_root(value: str | Path) -> Path:
    try:
        root = Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ReleaseParityError(f"cannot resolve repository root: {value}") from exc
    if not root.is_dir():
        raise ReleaseParityError(f"repository root does not exist or is not a directory: {value}")
    return root


def _normalise_relative_path(value: object) -> str:
    """Validate and canonicalize a repository-relative path.

    Windows callers may use backslashes; output and hashing always use POSIX
    separators.  Drive-qualified, UNC, empty and traversal paths are rejected
    before any filesystem access occurs.
    """

    if not isinstance(value, str):
        raise ReleaseParityError("source file paths must be strings")
    raw = value.strip()
    if not raw:
        raise ReleaseParityError("source file path must not be empty")
    if "\x00" in raw:
        raise ReleaseParityError("source file path contains NUL")
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//") or _DRIVE_PREFIX.match(raw):
        raise ReleaseParityError(f"source file path must be relative: {value!r}")
    components = raw.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ReleaseParityError(f"source file path contains an invalid component: {value!r}")
    lowered = [component.casefold() for component in components]
    for component in lowered:
        if component in _FORBIDDEN_COMPONENTS or _FORBIDDEN_NAME.match(component):
            raise ReleaseParityError(
                f"generated/artifact/fixture/build paths are not parity sources: {value!r}"
            )
    return "/".join(components)


def normalize_relative_path(value: object) -> str:
    """Public alias for :func:`_normalise_relative_path`."""

    return _normalise_relative_path(value)


def normalize_source_bytes(data: bytes) -> bytes:
    """Normalize only line endings, converting CRLF and CR to LF."""

    if not isinstance(data, bytes):
        raise TypeError("source data must be bytes")
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _u64(value: int) -> bytes:
    if value < 0:
        raise ReleaseParityError("length prefix cannot be negative")
    try:
        return struct.pack(">Q", value)
    except struct.error as exc:  # pragma: no cover - impossible for normal files
        raise ReleaseParityError("source file is too large for parity framing") from exc


def _normalise_group_mapping(raw: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    keys = tuple(raw)
    non_string_keys = [key for key in keys if not isinstance(key, str)]
    if non_string_keys:
        raise ReleaseParityError("file list contract group names must be strings")
    unknown = sorted(set(keys).difference(CONTRACT_GROUPS))
    missing = sorted(set(CONTRACT_GROUPS).difference(raw))
    if unknown:
        raise ReleaseParityError(f"file list has unknown contract group(s): {unknown!r}")
    if missing:
        raise ReleaseParityError(f"file list is missing contract group(s): {missing!r}")

    normalized: dict[str, tuple[str, ...]] = {}
    for group in CONTRACT_GROUPS:
        values = raw[group]
        if not isinstance(values, (list, tuple)):
            raise ReleaseParityError(f"file list group {group!r} must be an array")
        if not values:
            raise ReleaseParityError(f"file list group {group!r} must not be empty")
        paths = tuple(sorted(_normalise_relative_path(item) for item in values))
        if len(paths) != len(set(paths)):
            raise ReleaseParityError(f"file list group {group!r} contains duplicate paths")
        normalized[group] = paths
    return normalized


def normalize_file_list(raw: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """Validate a direct or wrapped JSON file-list mapping."""

    if not isinstance(raw, Mapping):
        raise ReleaseParityError("file list JSON must contain an object")
    if "source_files" in raw:
        allowed = {"schema_version", "source_files"}
        unknown_keys = set(raw).difference(allowed)
        if any(not isinstance(key, str) for key in unknown_keys):
            raise ReleaseParityError("file list top-level field names must be strings")
        unknown = sorted(unknown_keys)
        if unknown:
            raise ReleaseParityError(f"file list has unknown top-level field(s): {unknown!r}")
        schema_version = raw.get("schema_version", PARITY_SCHEMA_VERSION)
        if schema_version != PARITY_SCHEMA_VERSION:
            raise ReleaseParityError(f"unsupported file-list schema_version: {schema_version!r}")
        source_files = raw["source_files"]
        if not isinstance(source_files, Mapping):
            raise ReleaseParityError("file list source_files must be an object")
        return _normalise_group_mapping(source_files)
    return _normalise_group_mapping(raw)


def load_file_list(path: str | Path) -> dict[str, tuple[str, ...]]:
    """Load and validate an explicit JSON file-list configuration."""

    file_list_path = Path(path).expanduser().resolve()
    try:
        document = json.loads(file_list_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseParityError(f"cannot load file-list JSON {file_list_path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ReleaseParityError(f"file-list JSON must contain an object: {file_list_path}")
    return normalize_file_list(document)


def _resolve_source(root: Path, relative_path: str) -> Path:
    try:
        root_resolved = root.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ReleaseParityError(f"cannot resolve repository root: {root}") from exc
    if not root_resolved.is_dir():
        raise ReleaseParityError(f"repository root does not exist or is not a directory: {root}")
    try:
        candidate = (root_resolved / relative_path).resolve()
    except (OSError, RuntimeError) as exc:
        raise ReleaseParityError(f"cannot resolve parity source path: {relative_path!r}") from exc
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ReleaseParityError(f"source path escapes repository root: {relative_path!r}") from exc
    if not candidate.is_file():
        raise ReleaseParityError(f"parity source file does not exist: {relative_path}")
    return candidate


def _frame_group(root: Path, group: str, paths: Iterable[str]) -> bytes:
    normalized_paths = tuple(sorted(_normalise_relative_path(path) for path in paths))
    if not normalized_paths:
        raise ReleaseParityError(f"file list group {group!r} must not be empty")
    group_bytes = group.encode("utf-8")
    payload = bytearray(_FRAME_PREFIX)
    payload.extend(_u64(len(group_bytes)))
    payload.extend(group_bytes)
    payload.extend(_u64(len(normalized_paths)))
    for relative_path in normalized_paths:
        source_path = _resolve_source(root, relative_path)
        path_bytes = relative_path.encode("utf-8")
        content = normalize_source_bytes(source_path.read_bytes())
        payload.extend(_u64(len(path_bytes)))
        payload.extend(path_bytes)
        payload.extend(_u64(len(content)))
        payload.extend(content)
    return bytes(payload)


def hash_contract(root: str | Path, group: str, paths: Iterable[str]) -> str:
    """Compute one framed SHA-256 for a contract file group."""

    if group not in CONTRACT_GROUPS:
        raise ReleaseParityError(f"unknown contract group: {group!r}")
    return hashlib.sha256(_frame_group(Path(root), group, paths)).hexdigest()


def compute_parity(
    root: str | Path,
    file_list: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Compute the five parity fields from an explicit or default file list."""

    groups = (
        _normalise_group_mapping(file_list)
        if file_list is not None
        else _normalise_group_mapping(DEFAULT_FILE_LIST)
    )
    return {
        _GROUP_TO_PARITY_FIELD[group]: hash_contract(root, group, groups[group])
        for group in CONTRACT_GROUPS
    }


def _resolve_version_manifest(root: Path, explicit: str | Path | None) -> Path | None:
    try:
        if explicit is not None:
            candidate = Path(explicit).expanduser()
            return (candidate if candidate.is_absolute() else root / candidate).resolve()
    except (OSError, RuntimeError) as exc:
        raise ReleaseParityError("cannot resolve product version manifest") from exc
    candidate = root / "configs" / "product-version.json"
    if not candidate.is_file():
        raise ReleaseParityError(
            "product version manifest is missing; pass --version-manifest explicitly"
        )
    return candidate


def build_release_parity_document(
    root: str | Path,
    *,
    file_list: Mapping[str, object] | None = None,
    version_manifest_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> dict[str, object]:
    """Build a reproducible ``release-parity.json`` document.

    ``output_path`` is used only to prevent a caller from including the output
    document itself in a custom source list; it does not change any hash.
    """

    root_path = _resolve_root(root)
    groups = (
        _normalise_group_mapping(file_list)
        if file_list is not None
        else _normalise_group_mapping(DEFAULT_FILE_LIST)
    )
    if output_path is not None:
        output_candidate = Path(output_path).expanduser()
        output_resolved = (
            output_candidate if output_candidate.is_absolute() else root_path / output_candidate
        ).resolve()
        for paths in groups.values():
            for relative_path in paths:
                if _resolve_source(root_path, relative_path) == output_resolved:
                    raise ReleaseParityError("parity output cannot also be a source file")

    version_path = _resolve_version_manifest(root_path, version_manifest_path)
    try:
        version = load_product_version(version_path)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseParityError(f"product version manifest is invalid: {exc}") from exc
    parity = {
        _GROUP_TO_PARITY_FIELD[group]: hash_contract(root_path, group, groups[group])
        for group in CONTRACT_GROUPS
    }
    # Keep insertion order stable; callers can serialize with sort_keys=True
    # and obtain the same bytes independent of Python implementation details.
    return {
        "schema_version": PARITY_SCHEMA_VERSION,
        "product": version.product,
        "release_label": version.release_label,
        "source_files": {group: list(groups[group]) for group in CONTRACT_GROUPS},
        "parity": parity,
    }


def serialize_release_parity(document: Mapping[str, object]) -> str:
    """Serialize a parity document using stable UTF-8 JSON and LF endings."""

    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            allow_nan=False,
        )
        + "\n"
    )


def write_release_parity(
    destination: str | Path,
    document: Mapping[str, object],
    *,
    overwrite: bool = True,
) -> Path:
    """Atomically write a deterministic parity document."""

    output = Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise ReleaseParityError(f"parity output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_release_parity(document)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
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
            "Generate deterministic v1 shared-contract parity hashes for Multi-Detect. "
            "Only explicit source files are read; artifact/fixture/build paths are rejected."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (default: this checkout)",
    )
    parser.add_argument(
        "--out",
        "--output",
        type=Path,
        default=None,
        help="output JSON path (default: <root>/configs/release-parity.json)",
    )
    parser.add_argument(
        "--version-manifest",
        type=Path,
        default=None,
        help="canonical product-version.json (default: <root>/configs/product-version.json)",
    )
    parser.add_argument(
        "--file-list-json",
        type=Path,
        default=None,
        help="explicit JSON file list; relative paths are resolved from --root",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="refuse to replace an existing output file",
    )
    return parser


def _resolve_cli_path(value: Path | None, *, root: Path) -> Path | None:
    if value is None:
        return None
    return value if value.is_absolute() else root / value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = _resolve_root(args.root) if args.root is not None else _default_root()
    output = _resolve_cli_path(args.out, root=root)
    if output is None:
        output = root / "configs" / "release-parity.json"
    file_list_path = _resolve_cli_path(args.file_list_json, root=root)
    version_path = _resolve_cli_path(args.version_manifest, root=root)
    try:
        file_list = load_file_list(file_list_path) if file_list_path is not None else None
        document = build_release_parity_document(
            root,
            file_list=file_list,
            version_manifest_path=version_path,
            output_path=output,
        )
        destination = write_release_parity(
            output,
            document,
            overwrite=not args.no_overwrite,
        )
    except (OSError, ReleaseParityError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(serialize_release_parity(document), end="")
    print(f"release parity written: {destination}", file=sys.stderr)
    return 0


__all__ = [
    "CONTRACT_GROUPS",
    "DEFAULT_CONTRACT_FILES",
    "DEFAULT_FILE_LIST",
    "PARITY_FIELDS",
    "PARITY_SCHEMA_VERSION",
    "ReleaseParityError",
    "build_parser",
    "build_release_parity_document",
    "compute_parity",
    "hash_contract",
    "load_file_list",
    "main",
    "normalize_file_list",
    "normalize_relative_path",
    "normalize_source_bytes",
    "serialize_release_parity",
    "write_release_parity",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
