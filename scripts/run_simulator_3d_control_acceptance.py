"""Deterministic 3-D SIM gate for the shared manual/AI control contract.

This gate combines the runnable loopback control fixture with repository-owned
Gazebo assets and the scenario manifest.  It proves that both operator and AI
commands use the same authority/MAVLink path and that the 3-D runtime inputs
are pinned.  It deliberately does not claim human visual, GPU, Pixhawk, or
camera acceptance; pass that evidence separately on the desktop/hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.product_version import load_product_version
from multidetect.runtime import RuntimeMode
from multidetect.simulation import ScenarioRuntimeInput, load_runtime_scenario
from multidetect.simulator.px4_launch import Px4LaunchError, build_px4_launch_plan
from multidetect.simulator.stack import SimulatorStackPlan, stack_plan_from_scenario

try:  # pytest imports ``scripts`` as a namespace package
    from multidetect.simulator.px4_image import PINNED_IMAGE
    from scripts.run_simulator_control_acceptance import run_acceptance as run_control_acceptance
    from scripts.run_simulator_interactive_acceptance import (
        run_acceptance as run_interactive_acceptance,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from multidetect.simulator.px4_image import PINNED_IMAGE
    from run_simulator_control_acceptance import run_acceptance as run_control_acceptance
    from run_simulator_interactive_acceptance import (
        run_acceptance as run_interactive_acceptance,
    )

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
DEFAULT_LAUNCH_JOB_ROOT = ROOT / "artifacts/evaluation/simulator-3d-control-launch"


@dataclass(frozen=True, slots=True)
class _ThreeDAssetProfile:
    """Repository-owned world/model pair for one SIM scenario.

    The manifest remains the source of scenario identity.  This small registry
    only maps an already-known runtime-channel scenario to the local SDF
    assets that a real Gazebo launcher must load; it never reads the evaluator
    truth channel.
    """

    scenario_id: str
    mode: str
    world: Path
    model: Path
    required_model_tokens: tuple[str, ...]
    expected_vehicle_tokens: tuple[str, ...]
    profile_id: str = "x500_mono_cam"


ASSET_PROFILES: dict[str, _ThreeDAssetProfile] = {
    "river_rescue_vtol_001": _ThreeDAssetProfile(
        scenario_id="river_rescue_vtol_001",
        mode="vtol",
        world=ROOT / "simulation/gazebo/worlds/river_rescue_vtol_001.sdf",
        model=ROOT / "simulation/gazebo/models/standard_vtol_mono_cam/model.sdf",
        required_model_tokens=("standard_vtol", "mono_cam", "CameraJoint", "camera_link"),
        expected_vehicle_tokens=("vtol", "standard"),
    ),
    "coastal_patrol_fixed_wing_001": _ThreeDAssetProfile(
        scenario_id="coastal_patrol_fixed_wing_001",
        mode="fixed-wing",
        world=ROOT / "simulation/gazebo/worlds/coastal_patrol_fixed_wing_001.sdf",
        model=ROOT / "simulation/gazebo/models/rc_cessna_mono_cam/model.sdf",
        required_model_tokens=("rc_cessna", "mono_cam", "CameraJoint", "camera_link"),
        expected_vehicle_tokens=("fixed", "wing"),
    ),
    "urban_inspection_multirotor_001": _ThreeDAssetProfile(
        scenario_id="urban_inspection_multirotor_001",
        mode="multirotor",
        world=ROOT / "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf",
        model=ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf",
        required_model_tokens=("x500", "mono_cam", "CameraJoint", "camera_link"),
        expected_vehicle_tokens=("multirotor", "standard"),
    ),
}

GIMBAL_ASSET_PROFILE = _ThreeDAssetProfile(
    scenario_id="urban_inspection_multirotor_001",
    mode="multirotor",
    world=ROOT / "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf",
    model=ROOT / "simulation/gazebo/model-overlays/x500_gimbal_seg/model.sdf",
    required_model_tokens=("x500_gimbal", "GimbalAttachJoint", "camera_link", "model://gimbal"),
    expected_vehicle_tokens=("multirotor", "standard"),
    profile_id="x500_gimbal",
)
GIMBAL_SOURCE_ASSETS = ROOT / "simulation/gazebo/source-assets/x500_gimbal"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_3d_assets(
    profile: _ThreeDAssetProfile,
    runtime: ScenarioRuntimeInput,
) -> dict[str, Any]:
    if not isinstance(profile, _ThreeDAssetProfile):
        raise TypeError("profile must be a _ThreeDAssetProfile")
    if not isinstance(runtime, ScenarioRuntimeInput):
        raise TypeError("runtime must be a ScenarioRuntimeInput")
    if runtime.scenario_id != profile.scenario_id:
        raise ValueError(
            f"runtime scenario {runtime.scenario_id!r} does not match asset profile "
            f"{profile.scenario_id!r}"
        )
    if runtime.world.world_asset != f"builtin://gazebo/{profile.scenario_id}":
        raise ValueError("runtime world_asset is not bound to the repository-owned world")
    if not profile.world.is_file() or not profile.model.is_file():
        raise FileNotFoundError("repository-owned Gazebo world/model is missing")
    world_root = ET.parse(profile.world).getroot()
    world = world_root.find("world")
    if world is None or world.attrib.get("name") != "default":
        raise ValueError("Gazebo world must expose the default PX4 namespace")
    physics = world.find("physics")
    spherical = world.find("spherical_coordinates")
    if physics is None or spherical is None:
        raise ValueError("world must define deterministic physics and WGS84 coordinates")
    if float(physics.findtext("max_step_size", "nan")) != 0.004:
        raise ValueError("unexpected physics step size")
    if spherical.findtext("surface_model") != "EARTH_WGS84":
        raise ValueError("world must use EARTH_WGS84")
    model_text = profile.model.read_text(encoding="utf-8")
    if any(token not in model_text for token in profile.required_model_tokens):
        raise ValueError(f"{profile.mode} camera model is incomplete")
    vehicle_haystack = f"{runtime.vehicle.model} {runtime.vehicle.airframe_profile_id}".lower()
    if any(token not in vehicle_haystack for token in profile.expected_vehicle_tokens):
        raise ValueError(
            f"runtime vehicle does not match {profile.mode} asset profile: {vehicle_haystack}"
        )
    if profile.profile_id == "x500_gimbal":
        if runtime.vehicle.airframe_profile_id != "x500_gimbal":
            raise ValueError("gimbal assets require the x500_gimbal airframe profile")
        source_contract_path = GIMBAL_SOURCE_ASSETS / "extraction.json"
        if not source_contract_path.is_file():
            raise FileNotFoundError(
                "PX4 x500_gimbal source asset contract is missing; extract camera, gimbal, "
                "and 4019/4001 startup assets from one pinned image revision"
            )
        source_contract = json.loads(source_contract_path.read_text(encoding="utf-8"))
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
        assets = source_contract.get("assets")
        if (
            source_contract.get("schema_version") != 1
            or source_contract.get("contract")
            != "multidetect.px4-x500-gimbal-source-assets-v3"
            or source_contract.get("image") != PINNED_IMAGE
            or source_contract.get("image_revision") != source_contract.get("image")
            or not isinstance(assets, dict)
            or set(assets) != expected_names
        ):
            raise ValueError("invalid or mixed-revision PX4 x500_gimbal source asset contract")
        for name, record in assets.items():
            asset_path = GIMBAL_SOURCE_ASSETS / name
            if not isinstance(record, dict) or not asset_path.is_file():
                raise ValueError(f"PX4 gimbal source asset is missing: {name}")
            if record.get("image_path") != expected_image_paths[name]:
                raise ValueError(f"PX4 gimbal source path mismatch: {name}")
            if record.get("sha256") != _sha256(asset_path):
                raise ValueError(f"PX4 gimbal source asset hash mismatch: {name}")
        x500_source = ET.parse(GIMBAL_SOURCE_ASSETS / "x500_gimbal_model.sdf").getroot()
        x500_model = x500_source.find("model")
        if x500_model is None or x500_model.attrib.get("name") != "x500_gimbal":
            raise ValueError("PX4 x500_gimbal source does not declare the x500_gimbal model")
        includes = {uri.text for uri in x500_model.findall("include/uri") if uri.text}
        gimbal_source = (GIMBAL_SOURCE_ASSETS / "gimbal_model.sdf").read_text(encoding="utf-8")
        startup_source = (GIMBAL_SOURCE_ASSETS / "4019_gz_x500_gimbal").read_text(encoding="utf-8")
        if not {"model://x500", "model://gimbal"}.issubset(includes):
            raise ValueError("PX4 x500_gimbal source does not include its x500 and gimbal components")
        gimbal_root = ET.fromstring(gimbal_source)
        gimbal_links = {link.attrib.get("name") for link in gimbal_root.findall(".//link")}
        if not {"camera_link", "cgo3_mount_link"}.issubset(gimbal_links):
            raise ValueError("PX4 gimbal source does not expose camera_link and cgo3_mount_link")
        if not re.search(r"(?m)^PX4_SIM_MODEL=\$\{PX4_SIM_MODEL:=x500_gimbal\}\s*$", startup_source):
            raise ValueError("PX4 4019 startup asset does not default to x500_gimbal")
        if not re.search(
            r"(?m)^\. \$\{R\}etc/init\.d-posix/airframes/4001_gz_x500\s*$",
            startup_source,
        ):
            raise ValueError("PX4 4019 startup asset does not source 4001_gz_x500")
    # World files are self-contained: no network or Fuel resources, and model://
    # only for models this repository ships under simulation/gazebo/models.
    world_text = profile.world.read_text(encoding="utf-8")
    if any(token in world_text.lower() for token in ("http://", "https://", "fuel://")):
        raise ValueError("Gazebo world must not pull network or external model resources")
    models_root = ROOT / "simulation" / "gazebo" / "models"
    world_model_refs = {
        ref.split("</uri>", 1)[0].strip() for ref in world_text.split("<uri>model://")[1:]
    }
    missing = sorted(name for name in world_model_refs if not (models_root / name).is_dir())
    if missing:
        raise ValueError(
            f"Gazebo world must not pull network or external model resources: {missing}"
        )
    if any(token in model_text.lower() for token in ("http://", "https://", "fuel://")):
        raise ValueError("Gazebo model must not pull network resources")
    allowed_model_refs = {
        "model://standard_vtol",
        "model://rc_cessna",
        "model://x500",
        "model://mono_cam",
    }
    if profile.profile_id == "x500_gimbal":
        allowed_model_refs.add("model://gimbal")
    model_refs = {
        line.strip().split("</uri>", 1)[0].split(">", 1)[-1]
        for line in model_text.splitlines()
        if "<uri>model://" in line
    }
    if not model_refs.issubset(allowed_model_refs):
        raise ValueError(f"Gazebo model references unsupported dependencies: {sorted(model_refs)}")
    return {
        "scenario_id": profile.scenario_id,
        "mode": profile.mode,
        "airframe_profile_id": profile.profile_id,
        "world": str(profile.world),
        "world_sha256": _sha256(profile.world),
        "model": str(profile.model),
        "model_sha256": _sha256(profile.model),
        "model_dependencies": sorted(model_refs),
        "world_name": world.attrib["name"],
        "physics": {"max_step_size": 0.004, "real_time_factor": 1.0},
        "wgs84": {
            "latitude_deg": float(spherical.findtext("latitude_deg", "nan")),
            "longitude_deg": float(spherical.findtext("longitude_deg", "nan")),
        },
        "visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        **(
            {
                "source_asset_contract": str(GIMBAL_SOURCE_ASSETS / "extraction.json"),
                "source_asset_contract_sha256": _sha256(GIMBAL_SOURCE_ASSETS / "extraction.json"),
                "source_asset_hashes": {
                    name: record["sha256"]
                    for name, record in source_contract["assets"].items()
                },
            }
            if profile.profile_id == "x500_gimbal"
            else {}
        ),
    }


def _build_stack_plan_evidence(
    manifest: Path,
    runtime: ScenarioRuntimeInput,
    profile: _ThreeDAssetProfile,
    assets: dict[str, Any],
) -> tuple[SimulatorStackPlan, dict[str, object], dict[str, object]]:
    """Build and bind the hash-bound PX4/Gazebo plan used by this fixture.

    The 3-D gate used to validate the SDF files independently from the stack
    planner.  Keeping both checks is useful, but they must be tied together:
    a passing fixture now proves that the exact scenario/runtime identity and
    the exact repository world/model digests are the ones a launcher would
    receive.  Tool availability remains advisory (the normal development host
    may not have PX4/Gazebo installed), so ``runtime.dry_run`` is recorded but
    is intentionally not a gate failure.
    """

    plan = stack_plan_from_scenario(manifest, repo_root=ROOT)
    document = plan.to_mapping_with_hash()
    # Pull the nested records once and fail with a useful message if a future
    # schema change removes one of the fields this acceptance contract binds.
    scenario_record = document.get("scenario")
    vehicle_record = document.get("vehicle")
    world_record = document.get("world")
    repository_record = document.get("repository")
    ports_record = document.get("ports")
    validation_record = document.get("validation")
    runtime_record = document.get("runtime")
    if not all(
        isinstance(value, dict)
        for value in (
            scenario_record,
            vehicle_record,
            world_record,
            repository_record,
            ports_record,
            validation_record,
            runtime_record,
        )
    ):
        raise ValueError("stack plan evidence is missing required nested records")

    plan_assets = document.get("assets")
    plan_asset_sha = document.get("asset_sha256")
    if not isinstance(plan_assets, dict) or not isinstance(plan_asset_sha, dict):
        raise ValueError("stack plan evidence is missing asset provenance")

    # ``stack_plan_from_scenario`` already validates these properties.  The
    # explicit checks below make the binding visible in the evidence and turn
    # accidental future drift into a deterministic gate failure.
    checks: dict[str, object] = {
        "plan_hash_present": (
            isinstance(document.get("sha256"), str)
            and len(document["sha256"]) == 64
            and document["sha256"] == plan.evidence_hash
        ),
        "scenario_id_bound": (
            plan.scenario_id == runtime.scenario_id == profile.scenario_id
            and scenario_record.get("id") == runtime.scenario_id
        ),
        "scenario_version_bound": (
            plan.scenario_version == runtime.scenario_version
            and scenario_record.get("version") == runtime.scenario_version
        ),
        "scenario_manifest_hash_bound": (
            plan.scenario_manifest_hash == runtime.scenario_manifest_hash
            and scenario_record.get("manifest_sha256") == runtime.scenario_manifest_hash
        ),
        "seed_bound": plan.seed == runtime.seed == scenario_record.get("seed"),
        "vehicle_bound": (
            plan.vehicle_class == profile.mode
            and plan.vehicle_model == runtime.vehicle.model
            and plan.airframe_profile_id == runtime.vehicle.airframe_profile_id
            and vehicle_record.get("class") == profile.mode
        ),
        "world_uri_bound": plan.world_uri == runtime.world.world_asset,
        "world_asset_bound": plan.world_path.resolve() == profile.world.resolve(),
        "model_asset_bound": plan.model_path.resolve()
        == (
            (ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf").resolve()
            if profile.profile_id == "x500_gimbal"
            else profile.model.resolve()
        ),
        "world_digest_bound": plan.assets.world.sha256 == assets["world_sha256"],
        "model_digest_bound": plan.assets.model.sha256
        == (
            _sha256(ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf")
            if profile.profile_id == "x500_gimbal"
            else assets["model_sha256"]
        ),
        "model_config_hashed": (
            plan.assets.model_config.path.is_file()
            and isinstance(plan_asset_sha.get("model_config"), str)
            and len(plan_asset_sha["model_config"]) == 64
        ),
        "aggregate_asset_digest_present": (
            isinstance(document.get("assets_sha256"), str)
            and document.get("assets_sha256") == plan.assets_sha256
            and plan_assets.get("assets_sha256") == plan.assets_sha256
        ),
        "world_record_bound": (
            world_record.get("uri") == runtime.world.world_asset
            and world_record.get("name") == "default"
        ),
        "truth_free": (
            document.get("truth_free") is True and repository_record.get("truth_loaded") is False
        ),
        "loopback_only": (
            ports_record.get("loopback_only") is True
            and runtime_record.get("loopback_only") is True
        ),
        "ports_distinct": validation_record.get("ports_distinct") is True,
        "protected_port_untouched": (
            validation_record.get("protected_udp_14550_untouched") is True
            and 14550 not in ports_record.get("all_ports", [])
        ),
        "planner_validation_passed": validation_record.get("gate_passed") is True,
    }

    binding_passed = all(bool(value) for value in checks.values())
    raw_tool_capabilities = document.get("tool_capabilities")
    if not isinstance(raw_tool_capabilities, dict):
        raw_tool_capabilities = {}
    tool_capabilities = {
        str(name): bool(available)
        for name, available in raw_tool_capabilities.items()
        if isinstance(name, str)
    }
    # A stack plan can be structurally valid while still being a dry-run
    # plan.  Keep those concepts separate: ``gate_passed`` below proves the
    # hash/asset/isolation contract, while ``ready_to_launch`` says whether
    # both external executables were found on this host.
    tools_ready = set(tool_capabilities) >= {"px4", "gazebo"} and all(
        tool_capabilities.get(name, False) for name in ("px4", "gazebo")
    )
    binding = {
        "schema_version": 1,
        "gate_passed": binding_passed,
        "checks": checks,
        "tool_capabilities": tool_capabilities,
        "dry_run": runtime_record.get("dry_run"),
        "ready_to_launch": bool(binding_passed and tools_ready),
        "asset_validation_gate_passed": binding_passed,
        "external_tools_ready": tools_ready,
        "external_process_acceptance": "not_claimed",
    }
    if not binding_passed:
        failed = [name for name, value in checks.items() if not bool(value)]
        raise ValueError("stack plan binding checks failed: " + ", ".join(failed))
    return plan, document, binding


def _default_launch_job_dir(stack_plan: SimulatorStackPlan) -> Path:
    """Return a deterministic, ignored, job-owned PX4 preflight directory."""

    # The plan hash is part of the directory name so a changed scenario or
    # asset set cannot silently reuse another run's generated rc.mavlink.
    return DEFAULT_LAUNCH_JOB_ROOT / (f"{stack_plan.scenario_id}-{stack_plan.evidence_hash[:16]}")


def _as_nonnegative_count(value: object) -> int:
    """Coerce a fixture counter without allowing malformed evidence to pass."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _build_px4_launch_evidence(
    stack_plan: SimulatorStackPlan,
    stack_plan_document: dict[str, object],
    stack_plan_binding: dict[str, object],
    *,
    launch_job_dir: Path | None = None,
) -> tuple[dict[str, object], bool]:
    """Build a dry-run PX4 wrapper and expose honest readiness semantics.

    ``build_px4_launch_plan`` is a planner: it creates a hash-bound wrapper
    and patched startup script but never starts PX4 or opens a socket.  This
    gate records both the generated artifact and its readiness checks.  A
    missing local PX4/Gazebo installation therefore makes
    ``ready_to_launch`` false without turning the deterministic protocol
    fixture into a fake visual/flight acceptance claim.

    The boolean return value means that a valid, hash-verifiable launch plan
    was generated.  It is deliberately different from ``ready_to_launch``;
    the latter requires all external prerequisites and is not required for
    this local deterministic gate.
    """

    job_dir = (
        Path(launch_job_dir).expanduser()
        if launch_job_dir is not None
        else _default_launch_job_dir(stack_plan)
    )
    try:
        launch_plan = build_px4_launch_plan(
            stack_plan,
            job_dir=job_dir,
            # This acceptance script never starts an external process.  The
            # planner still probes executable/workdir/startup-file readiness.
            dry_run=True,
            strict=False,
            write_evidence=True,
        )
    except (OSError, Px4LaunchError, TypeError, ValueError, RuntimeError) as exc:
        # A malformed/missing planner artifact is a real evidence failure,
        # unlike merely missing external tools.  Keep a complete subrecord so
        # CI can diagnose the failure rather than dropping the launch section.
        return (
            {
                "schema_version": 1,
                "event": "simulator_3d_px4_launch_readiness",
                "plan_generated": False,
                "preflight_gate_passed": False,
                "gate_passed": False,
                "dry_run": True,
                "strict": False,
                "ready_to_launch": False,
                "artifacts_verified": False,
                "stack_plan_sha256": stack_plan.evidence_hash,
                "scenario_manifest_sha256": stack_plan.scenario_manifest_hash,
                "job_dir": str(job_dir),
                "stack_plan": {
                    "dry_run": bool(stack_plan_binding.get("dry_run")),
                    "ready_to_launch": bool(stack_plan_binding.get("ready_to_launch")),
                    "asset_validation_gate_passed": bool(
                        stack_plan_binding.get("asset_validation_gate_passed")
                    ),
                    "external_tools_ready": bool(stack_plan_binding.get("external_tools_ready")),
                    "tool_capabilities": stack_plan_document.get("tool_capabilities", {}),
                    "sha256": stack_plan.evidence_hash,
                },
                "error": str(exc),
                "external_process_started": False,
                "external_io_opened": False,
                "telemetry_observed": False,
                "dry_run_blocks_external_gate": True,
                "external_3d_acceptance": "not_claimed",
                "visual_acceptance": "not_claimed",
                "hardware_acceptance": "not_claimed",
                "evidence_semantics": (
                    "PX4 launch planning failed; no external process or socket was started."
                ),
            },
            False,
        )

    launch_document = launch_plan.to_mapping_with_hash()
    readiness = launch_document.get("readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    readiness_checks = readiness.get("checks")
    readiness_checks = readiness_checks if isinstance(readiness_checks, dict) else {}
    runtime = launch_document.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    artifacts = launch_document.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    failed_checks = sorted(str(name) for name, value in readiness_checks.items() if not bool(value))
    artifacts_verified = bool(launch_plan.verify())
    launch_hash = launch_document.get("sha256")
    hash_bound = (
        isinstance(launch_hash, str)
        and launch_hash == launch_plan.evidence_hash
        and launch_document.get("source_stack_plan_sha256") == stack_plan.evidence_hash
        and launch_document.get("scenario_manifest_sha256") == stack_plan.scenario_manifest_hash
    )
    tool_capabilities = stack_plan_document.get("tool_capabilities")
    if not isinstance(tool_capabilities, dict):
        tool_capabilities = {}

    # ``ready_to_launch`` is a preflight status, not proof that PX4/Gazebo
    # actually ran.  The planner's runtime section must stay false here.
    external_process_started = bool(runtime.get("external_process_started"))
    external_io_opened = bool(runtime.get("external_io_opened"))
    telemetry_observed = bool(readiness.get("telemetry_observed"))
    ready_to_launch = bool(launch_plan.ready_to_launch)
    preflight_gate_passed = bool(artifacts_verified and hash_bound)
    record = {
        "schema_version": 1,
        "event": "simulator_3d_px4_launch_readiness",
        "plan_generated": True,
        "preflight_gate_passed": preflight_gate_passed,
        # This field is intentionally the *external launch readiness* gate.
        # It may be false while the deterministic protocol gate passes.
        "gate_passed": ready_to_launch,
        "dry_run": bool(launch_plan.dry_run),
        "strict": bool(launch_plan.strict),
        "ready_to_launch": ready_to_launch,
        "failed_readiness_checks": failed_checks,
        "artifacts_verified": artifacts_verified,
        "hash_bound": hash_bound,
        "stack_plan_sha256": stack_plan.evidence_hash,
        "scenario_manifest_sha256": stack_plan.scenario_manifest_hash,
        "launch_plan_sha256": launch_hash,
        "job_dir": str(launch_plan.job_dir),
        "tool_capabilities": tool_capabilities,
        "stack_plan": {
            "dry_run": bool(stack_plan_binding.get("dry_run")),
            "ready_to_launch": bool(stack_plan_binding.get("ready_to_launch")),
            "asset_validation_gate_passed": bool(
                stack_plan_binding.get("asset_validation_gate_passed")
            ),
            "external_tools_ready": bool(stack_plan_binding.get("external_tools_ready")),
            "tool_capabilities": tool_capabilities,
            "sha256": stack_plan.evidence_hash,
        },
        "px4": {
            "executable_available": bool(
                isinstance(launch_document.get("px4"), dict)
                and launch_document["px4"].get("available")
            ),
            "workdir_resolved": bool(
                isinstance(launch_document.get("px4"), dict)
                and launch_document["px4"].get("workdir_resolved")
            ),
            "source_is_template": bool(artifacts.get("source_is_template")),
            "startup_patch_applied": bool(artifacts.get("startup_patch_applied")),
        },
        "readiness": {
            "gate_passed": bool(readiness.get("gate_passed")),
            "ready_to_launch": ready_to_launch,
            "checks": readiness_checks,
            "failed_checks": failed_checks,
            "process_started": bool(readiness.get("process_started")),
            "external_io_opened": bool(readiness.get("external_io_opened")),
            "telemetry_observed": telemetry_observed,
        },
        "artifacts": {
            "job_dir": str(launch_plan.job_dir),
            "generated_rc_mavlink": artifacts.get("generated_rc_mavlink"),
            "generated_rc_mavlink_sha256": artifacts.get("generated_rc_mavlink_sha256"),
            "wrapper": artifacts.get("wrapper"),
            "wrapper_sha256": artifacts.get("wrapper_sha256"),
            "source_checkout_modified": bool(artifacts.get("source_checkout_modified")),
        },
        "external_process_started": external_process_started,
        "external_io_opened": external_io_opened,
        "telemetry_observed": telemetry_observed,
        "dry_run_blocks_external_gate": bool(launch_plan.dry_run),
        "external_3d_acceptance": "not_claimed",
        "visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "evidence": launch_document,
        "evidence_semantics": (
            "dry_run is a planner/preflight record; ready_to_launch is not proof of "
            "PX4/Gazebo execution, telemetry, desktop rendering, or human flight."
        ),
    }
    return record, bool(preflight_gate_passed)


def _build_control_contract(
    interactive: dict[str, object],
    *,
    vehicle_mode: str,
) -> dict[str, object]:
    """Extract deterministic manual/AI/watchdog/QGC-only assertions."""

    selected_mode = next(
        (
            item
            for item in interactive.get("modes", [])
            if isinstance(item, dict) and item.get("mode") == vehicle_mode
        ),
        None,
    )
    if not isinstance(selected_mode, dict):
        return {
            "schema_version": 1,
            "vehicle_mode": vehicle_mode,
            "deterministic_fixture": True,
            "checks": {"selected_mode_present": False},
            "gate_passed": False,
        }
    selected_mode_gate = selected_mode.get("gate")
    selected_mode_gate = selected_mode_gate if isinstance(selected_mode_gate, dict) else {}
    router = selected_mode.get("router")
    router = router if isinstance(router, dict) else {}
    closed_loop = selected_mode.get("closed_loop")
    closed_loop = closed_loop if isinstance(closed_loop, dict) else {}
    ai_evidence = closed_loop.get("ai")
    ai_evidence = ai_evidence if isinstance(ai_evidence, dict) else {}
    adapter = router.get("adapter")
    adapter = adapter if isinstance(adapter, dict) else {}
    authority = closed_loop.get("authority")
    authority = authority if isinstance(authority, dict) else {}
    authority_events = authority.get("events")
    authority_events = authority_events if isinstance(authority_events, list) else []
    authority_states = [
        str(item.get("state"))
        for item in authority_events
        if isinstance(item, dict) and isinstance(item.get("state"), str)
    ]
    source_counts = adapter.get("source_counts")
    source_counts = source_counts if isinstance(source_counts, dict) else {}
    manual_count = _as_nonnegative_count(router.get("qgc_manual_frames_accepted"))
    manual_forwarded_count = _as_nonnegative_count(router.get("qgc_frames_sent_to_px4"))
    ai_accepted_count = _as_nonnegative_count(ai_evidence.get("accepted"))
    ai_suppressed_count = _as_nonnegative_count(ai_evidence.get("manual_suppressed"))
    watchdog_tick_count = _as_nonnegative_count(router.get("watchdog_ticks"))
    watchdog_neutral_count = _as_nonnegative_count(adapter.get("watchdog_neutral_count"))
    mode_claims = selected_mode.get("claims")
    mode_claims = mode_claims if isinstance(mode_claims, dict) else {}
    interactive_claims = interactive.get("claims")
    interactive_claims = interactive_claims if isinstance(interactive_claims, dict) else {}

    manual = {
        "qgc_frame_accepted": bool(selected_mode_gate.get("qgc_manual_accepted")),
        "accepted_frame_count": manual_count,
        "forwarded_to_px4_count": manual_forwarded_count,
        "authority_takeover": bool(selected_mode_gate.get("manual_took_over")),
        "vehicle_advanced": bool(selected_mode_gate.get("manual_advanced_vehicle")),
        "source": "native_qgc_manual_control",
    }
    ai = {
        "track_accepted": bool(selected_mode_gate.get("ai_track_accepted")),
        "accepted_step_count": ai_accepted_count,
        "adapter_source_count": _as_nonnegative_count(source_counts.get("ai")),
        "suppressed_during_manual": bool(selected_mode_gate.get("manual_suppressed_ai")),
        "suppressed_step_count": ai_suppressed_count,
        "resumed_after_release": bool(selected_mode_gate.get("ai_resumed_after_release")),
        "source": "shared_ai_executor_and_mavlink_adapter",
    }
    watchdog = {
        "failsafe": bool(selected_mode_gate.get("watchdog_failsafe")),
        "neutral_frame": bool(selected_mode_gate.get("neutral_watchdog_frame")),
        "tick_count": watchdog_tick_count,
        "neutral_frame_count": watchdog_neutral_count,
        "source": "shared_adapter_watchdog",
    }
    qgc_only = {
        "sole_operator_ui": bool(
            mode_claims.get("qgc_is_sole_operator_ui")
            and interactive_claims.get("qgc_is_sole_operator_ui")
        ),
        "native_manual_control_path": bool(
            mode_claims.get("qgc_native_manual_control_path")
            and router.get("qgc_manual_control_via_shared_adapter")
        ),
        "independent_simulator_window_created": False,
        "qgc_control_mode": router.get("qgc_control_mode"),
        "flight_write_commands_blocked": router.get("qgc_flight_commands_allowed") is False,
        "mission_write_commands_blocked": router.get("qgc_mission_writes_allowed") is False,
        "loopback_only": bool(router.get("literal_loopback_only")),
    }
    checks = {
        "manual_qgc_frame_accepted": manual["qgc_frame_accepted"] is True and manual_count == 1,
        "manual_forwarded_once": manual_forwarded_count == manual_count == 1,
        "manual_authority_takeover": manual["authority_takeover"] is True,
        "manual_vehicle_advanced": manual["vehicle_advanced"] is True,
        "ai_track_accepted": ai["track_accepted"] is True and ai_accepted_count >= 1,
        "ai_adapter_count_matches": ai["adapter_source_count"] == ai_accepted_count >= 1,
        "ai_suppressed_during_manual": ai["suppressed_during_manual"] is True
        and ai_suppressed_count >= 1,
        "ai_resumed_after_release": ai["resumed_after_release"] is True,
        "watchdog_failsafe": watchdog["failsafe"] is True,
        "watchdog_neutral_frame": watchdog["neutral_frame"] is True and watchdog_neutral_count == 1,
        "authority_transitions_cover_manual_ai_failsafe": all(
            state in authority_states for state in ("manual", "ai", "failsafe")
        ),
        "qgc_only_operator_ui": qgc_only["sole_operator_ui"] is True,
        "qgc_native_path": qgc_only["native_manual_control_path"] is True,
        "secondary_ui_absent": qgc_only["independent_simulator_window_created"] is False,
        "qgc_writes_blocked": qgc_only["flight_write_commands_blocked"] is True
        and qgc_only["mission_write_commands_blocked"] is True,
        "qgc_loopback_only": qgc_only["loopback_only"] is True,
    }
    manual["gate_passed"] = all(
        bool(checks[name])
        for name in (
            "manual_qgc_frame_accepted",
            "manual_forwarded_once",
            "manual_authority_takeover",
            "manual_vehicle_advanced",
        )
    )
    ai["gate_passed"] = all(
        bool(checks[name])
        for name in (
            "ai_track_accepted",
            "ai_adapter_count_matches",
            "ai_suppressed_during_manual",
            "ai_resumed_after_release",
        )
    )
    watchdog["gate_passed"] = all(
        bool(checks[name]) for name in ("watchdog_failsafe", "watchdog_neutral_frame")
    )
    qgc_only["gate_passed"] = all(
        bool(checks[name])
        for name in (
            "qgc_only_operator_ui",
            "qgc_native_path",
            "secondary_ui_absent",
            "qgc_writes_blocked",
            "qgc_loopback_only",
        )
    )
    return {
        "schema_version": 1,
        "vehicle_mode": vehicle_mode,
        "deterministic_fixture": True,
        "manual": manual,
        "ai": ai,
        "watchdog": watchdog,
        "qgc_only": qgc_only,
        "authority": {
            "states_observed": authority_states,
            "manual_takeover_observed": "manual" in authority_states,
            "ai_observed": "ai" in authority_states,
            "failsafe_observed": "failsafe" in authority_states,
        },
        "checks": checks,
        "gate_passed": all(bool(value) for value in checks.values()),
    }


def run_acceptance(
    manifest: Path = DEFAULT_MANIFEST,
    *,
    launch_job_dir: Path | None = None,
) -> dict[str, Any]:
    # Avoid resolve() here: the Windows checkout can be imported by the
    # controller's POSIX-flavoured Python where WindowsPath is non-stringable.
    manifest = Path(manifest)
    if not manifest.is_file():
        raise FileNotFoundError(f"scenario manifest does not exist: {manifest}")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("scenario manifest must be a JSON object")
    scenario_id = document.get("scenario_id")
    if not isinstance(scenario_id, str) or scenario_id not in ASSET_PROFILES:
        raise ValueError(f"unsupported 3-D scenario identity: {scenario_id!r}")
    profile = ASSET_PROFILES[scenario_id]
    product = load_product_version()
    runtime = load_runtime_scenario(manifest, mode=RuntimeMode.SIM)
    if runtime.scenario_id != scenario_id:
        raise ValueError("manifest/runtime scenario identity mismatch")
    if (
        scenario_id == "urban_inspection_multirotor_001"
        and runtime.vehicle.airframe_profile_id == "x500_gimbal"
    ):
        profile = GIMBAL_ASSET_PROFILE
    control = run_control_acceptance()
    interactive = run_interactive_acceptance(mode=profile.mode)
    assets = _validate_3d_assets(profile, runtime)
    stack_plan, stack_plan_document, stack_plan_binding = _build_stack_plan_evidence(
        manifest,
        runtime,
        profile,
        assets,
    )
    px4_launch, px4_launch_generated = _build_px4_launch_evidence(
        stack_plan,
        stack_plan_document,
        stack_plan_binding,
        launch_job_dir=launch_job_dir,
    )
    # Keep the launch/readiness status as a separate, explicit record.  The
    # deterministic gate below requires a valid hash-bound plan, but does not
    # pretend that a dry-run wrapper equals a running PX4/Gazebo desktop.
    runtime_readiness = {
        "schema_version": 1,
        "event": "simulator_3d_runtime_readiness",
        "deterministic_fixture": True,
        "stack_plan": px4_launch["stack_plan"],
        "px4_launch": px4_launch,
        "external_runtime": {
            "stack_plan_ready": bool(stack_plan_binding.get("ready_to_launch")),
            "px4_launch_ready": bool(px4_launch.get("ready_to_launch")),
            "ready_to_launch": bool(
                stack_plan_binding.get("ready_to_launch") and px4_launch.get("ready_to_launch")
            ),
            "dry_run": bool(stack_plan_binding.get("dry_run") or px4_launch.get("dry_run")),
            "process_started": bool(px4_launch.get("external_process_started")),
            "io_opened": bool(px4_launch.get("external_io_opened")),
            "telemetry_observed": bool(px4_launch.get("telemetry_observed")),
            "dry_run_blocks_external_gate": bool(
                stack_plan_binding.get("dry_run") or px4_launch.get("dry_run")
            ),
            "acceptance": "not_claimed",
        },
        "external_3d_acceptance": "not_claimed",
        "visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "evidence_semantics": (
            "stack_plan and px4_launch readiness are preflight records; a dry-run or "
            "ready wrapper never claims external PX4/Gazebo execution or visual flight."
        ),
    }

    # Keep a compact, mode-specific contract summary at the top level.  The
    # full router/adapter evidence remains nested under ``interactive_qgc_path``;
    # this summary makes it possible for CI and release tooling to assert the
    # critical manual -> AI -> watchdog transitions without knowing the full
    # fixture implementation details.
    control_contract = _build_control_contract(interactive, vehicle_mode=profile.mode)
    deterministic_gate_passed = bool(
        control.get("gate_passed")
        and interactive.get("gate_passed")
        and assets["world_name"] == "default"
        and stack_plan_binding["gate_passed"]
        and control_contract["gate_passed"]
        and px4_launch_generated
    )
    gate = deterministic_gate_passed
    return {
        "schema_version": 1,
        "event": "simulator_3d_control_acceptance_finished",
        "product": product.product,
        "release_label": product.release_label,
        "runtime_mode": RuntimeMode.SIM.value,
        "gate_passed": gate,
        "deterministic_gate_passed": deterministic_gate_passed,
        "external_runtime_gate_passed": False,
        "gate_scope": "deterministic_protocol_asset_preflight",
        "external_runtime_required_for_real_3d": True,
        "scenario": {
            "path": str(manifest),
            "sha256": _sha256(manifest),
            "scenario_id": scenario_id,
            "scenario_version": document.get("scenario_version"),
            "manifest_canonical_sha256": stack_plan.scenario_manifest_hash,
            "runtime_channel_sha256": runtime.canonical_hash,
            "vehicle": runtime.vehicle.to_mapping(),
            "world": runtime.world.to_mapping(),
        },
        "three_d_runtime": assets,
        "stack_plan": stack_plan_document,
        "stack_plan_sha256": stack_plan_document["sha256"],
        "stack_plan_binding": stack_plan_binding,
        "stack_plan_status": runtime_readiness["stack_plan"],
        "px4_launch": px4_launch,
        "launch_readiness": runtime_readiness,
        "shared_control_path": control,
        "interactive_qgc_path": interactive,
        "control_contract": control_contract,
        "deterministic_control": control_contract,
        "claims": {
            "manual_and_ai_share_authority_path": bool(control.get("gate_passed")),
            "native_qgc_interactive_path": bool(interactive.get("gate_passed")),
            "stack_plan_hash_bound": bool(stack_plan_binding["gate_passed"]),
            "stack_plan_ready_to_launch": bool(stack_plan_binding.get("ready_to_launch")),
            "px4_launch_plan_generated": px4_launch_generated,
            "px4_launch_ready_to_launch": bool(px4_launch.get("ready_to_launch")),
            "px4_gazebo_acceptance": "not_claimed",
            "external_3d_runtime_acceptance": "not_claimed",
            "manual_control_evidence": bool(
                control_contract["manual"]["qgc_frame_accepted"]
                and control_contract["manual"]["authority_takeover"]
            ),
            "ai_evidence": bool(
                control_contract["ai"]["track_accepted"]
                and control_contract["ai"]["resumed_after_release"]
            ),
            "watchdog_evidence": bool(
                control_contract["watchdog"]["failsafe"]
                and control_contract["watchdog"]["neutral_frame"]
            ),
            "vehicle_mode": profile.mode,
            "qgc_is_sole_operator_ui": bool(control_contract["qgc_only"]["sole_operator_ui"]),
            "qgc_only_control_evidence": bool(control_contract["qgc_only"]["gate_passed"])
            if "gate_passed" in control_contract["qgc_only"]
            else bool(
                control_contract["checks"].get("qgc_only_operator_ui")
                and control_contract["checks"].get("qgc_native_path")
            ),
            "visual_acceptance": "not_claimed",
            "pixhawk_hardware_acceptance": "not_claimed",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--launch-job-dir",
        type=Path,
        help=(
            "job-owned directory for generated PX4 dry-run wrapper evidence "
            "(defaults to artifacts/evaluation/simulator-3d-control-launch/<hash>)"
        ),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(
            args.scenario_manifest,
            launch_job_dir=args.launch_job_dir,
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(json.dumps({"event": "simulator_3d_control_acceptance_failed", "error": str(exc)}))
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
