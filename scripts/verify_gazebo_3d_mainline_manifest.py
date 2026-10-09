"""Verify the repository-owned Gazebo 3-D mainline asset manifest.

This is an offline asset audit.  It parses the checked-in SDF/XML/JSON files,
checks their hashes and cross-file identities, and asks the existing simulator
stack planner to re-resolve the same world/model composition.  It deliberately
does not start Gazebo, PX4, Docker, QGC, or any network connection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "configs" / "scenarios" / "gazebo-3d-mainline-v1.manifest.json"
NETWORK_URI = re.compile(r"(?:https?|ftp|fuel)://", re.IGNORECASE)
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


class ManifestError(ValueError):
    """Raised when a 3-D mainline manifest does not match repository assets."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path(root: Path, value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ManifestError(f"{name} must be a repository-relative path")
    path = (root / value).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ManifestError(f"{name} escapes the repository root") from exc
    if not path.is_file():
        raise ManifestError(f"{name} is not a file: {value}")
    return path


def _check_asset(root: Path, raw: object, name: str) -> Path:
    if not isinstance(raw, dict):
        raise ManifestError(f"{name} must be an object")
    path = _path(root, raw.get("path"), f"{name}.path")
    expected = raw.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ManifestError(f"{name}.sha256 is invalid")
    actual = _sha256(path)
    if actual != expected:
        raise ManifestError(f"{name} hash mismatch: expected {expected}, got {actual}")
    size = raw.get("size_bytes")
    if not isinstance(size, int) or size < 1 or path.stat().st_size != size:
        raise ManifestError(f"{name}.size_bytes does not match {path}")
    return path


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{name} must be a non-empty string")
    return value


def _verify_sdf(
    world_path: Path,
    model_path: Path,
    *,
    model_name: str,
    deps: set[str],
    repository_root: Path,
) -> dict[str, Any]:
    world_text = world_path.read_text(encoding="utf-8")
    model_text = model_path.read_text(encoding="utf-8")
    if NETWORK_URI.search(world_text) or NETWORK_URI.search(model_text):
        raise ManifestError(f"network asset URI found in {world_path} or {model_path}")
    for path, text in ((world_path, world_text), (model_path, model_text)):
        lowered = text.lower()
        if "billboard" in lowered:
            raise ManifestError(f"billboard fixture is not allowed in primary 3-D asset: {path}")
        for token in ("ground_truth", "expected_answer", "false_geolocations"):
            if token in lowered:
                raise ManifestError(f"evaluator metadata {token!r} found in {path}")

    world_root = ET.fromstring(world_text)
    world = world_root.find("world")
    if world_root.tag != "sdf" or world is None or world.attrib.get("name") != "default":
        raise ManifestError(f"{world_path} is not a default SDF world")
    physics = world.find("physics")
    if physics is None or physics.attrib.get("type") != "ode":
        raise ManifestError(f"{world_path} must declare ODE physics")
    coordinates = world.find("spherical_coordinates")
    if coordinates is None:
        raise ManifestError(f"{world_path} has no WGS-84 origin")
    if coordinates.findtext("surface_model") != "EARTH_WGS84":
        raise ManifestError(f"{world_path} must use EARTH_WGS84")
    if coordinates.findtext("world_frame_orientation") != "ENU":
        raise ManifestError(f"{world_path} must use ENU")

    model_root = ET.fromstring(model_text)
    model = model_root.find("model")
    if model_root.tag != "sdf" or model is None or model.attrib.get("name") != model_name:
        raise ManifestError(f"{model_path} model identity is invalid")
    includes = {
        (item.findtext("uri") or "").strip()
        for item in model.findall("include")
        if item.findtext("uri")
    }
    if includes != deps:
        raise ManifestError(f"{model_path} dependencies differ from manifest: {sorted(includes)}")
    if "model://mono_cam" not in includes:
        raise ManifestError(f"{model_path} has no shared mono camera composition")
    return {
        "world": str(world_path.relative_to(repository_root)).replace("\\", "/"),
        "model": str(model_path.relative_to(repository_root)).replace("\\", "/"),
        "world_physics": physics.attrib.get("type"),
        "wgs84": True,
        "model_dependencies": sorted(includes),
    }


def _verify_target_model(
    world_path: Path,
    target: object,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    """Bind the reusable structural target to one mainline world include."""

    if not isinstance(target, dict):
        raise ManifestError("mainline scenario target_model is missing")
    if target.get("uri") != "model://multidetect_search_target_v1":
        raise ManifestError("mainline target_model URI is not the approved local asset")
    model_path = _check_asset(repository_root, target.get("model"), "target_model.model")
    config_path = _check_asset(repository_root, target.get("config"), "target_model.config")
    model_root = ET.parse(model_path).getroot()
    model = model_root.find("model")
    if model_root.tag != "sdf" or model is None:
        raise ManifestError("target_model.model is not a valid SDF model")
    if model.attrib.get("name") != "multidetect_search_target_v1":
        raise ManifestError("target_model.model identity is invalid")
    config_root = ET.parse(config_path).getroot()
    if config_root.tag != "model" or config_root.findtext("name") != "multidetect_search_target_v1":
        raise ManifestError("target_model.config identity is invalid")
    model_text = model_path.read_text(encoding="utf-8").lower()
    if "billboard" in model_text or "ground_truth" in model_text or "expected_answer" in model_text:
        raise ManifestError("target model contains a fixture or evaluator label")

    expected_name = target.get("include_name")
    expected_pose = target.get("pose")
    if not isinstance(expected_name, str) or not expected_name.strip():
        raise ManifestError("target_model.include_name is invalid")
    if not isinstance(expected_pose, str) or not expected_pose.strip():
        raise ManifestError("target_model.pose is invalid")
    world = ET.parse(world_path).getroot().find("world")
    assert world is not None
    matches = [
        include
        for include in world.findall("include")
        if (include.findtext("uri") or "").strip() == target["uri"]
        and (include.findtext("name") or "").strip() == expected_name
    ]
    if len(matches) != 1:
        raise ManifestError(
            f"{world_path} must contain exactly one include for {target['uri']} "
            f"named {expected_name}"
        )
    if (matches[0].findtext("pose") or "").strip() != expected_pose:
        raise ManifestError(f"{world_path} target include pose differs from manifest")
    return {
        "uri": target["uri"],
        "model": str(model_path.relative_to(repository_root)).replace("\\", "/"),
        "config": str(config_path.relative_to(repository_root)).replace("\\", "/"),
        "include_name": expected_name,
        "pose": expected_pose,
    }


def verify_manifest(
    manifest_path: str | Path = DEFAULT_MANIFEST, *, repository_root: str | Path = ROOT
) -> dict[str, Any]:
    """Verify one manifest and return a machine-readable audit summary."""

    root = Path(repository_root).resolve()
    manifest_candidate = Path(manifest_path).expanduser()
    if not manifest_candidate.is_absolute():
        manifest_candidate = root / manifest_candidate
    try:
        manifest_candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ManifestError("manifest escapes the repository root") from exc
    path = _path(root, str(manifest_candidate.resolve().relative_to(root.resolve())), "manifest")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ManifestError("manifest schema_version must be 1")
    if document.get("manifest_id") != "gazebo-3d-mainline-v1":
        raise ManifestError("unexpected manifest_id")
    claim = document.get("claim_boundary")
    policy = document.get("asset_policy")
    if not isinstance(claim, dict) or claim.get("deterministic_asset_audit") is not True:
        raise ManifestError("claim_boundary must enable deterministic asset audit")
    for key in (
        "external_gazebo_runtime",
        "hardware_acceptance",
        "hil_acceptance",
        "outdoor_gnss_acceptance",
        "final_release",
    ):
        if claim.get(key) != "not_claimed":
            raise ManifestError(f"claim_boundary.{key} must remain not_claimed")
    if (
        not isinstance(policy, dict)
        or policy.get("billboard_perception_fixtures_excluded") is not True
    ):
        raise ManifestError("billboard perception fixtures must be excluded from the mainline")

    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) < 3:
        raise ManifestError("manifest must contain the three primary airframe scenarios")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    # Import lazily so static help and JSON parsing do not import the full stack.
    from multidetect.simulator.stack import stack_plan_from_scenario

    for index, raw in enumerate(scenarios):
        if not isinstance(raw, dict):
            raise ManifestError(f"scenarios[{index}] must be an object")
        scenario_id = _require_text(raw.get("scenario_id"), f"scenarios[{index}].scenario_id")
        if scenario_id in seen:
            raise ManifestError(f"duplicate scenario_id: {scenario_id}")
        seen.add(scenario_id)
        scenario_path = _check_asset(
            root, raw.get("scenario_manifest"), f"{scenario_id}.scenario_manifest"
        )
        runtime_path = _check_asset(root, raw.get("runtime_input"), f"{scenario_id}.runtime_input")
        world_path = _check_asset(root, raw.get("world_sdf"), f"{scenario_id}.world_sdf")
        map_path = _check_asset(root, raw.get("map_asset"), f"{scenario_id}.map_asset")
        dem_path = _check_asset(root, raw.get("dem_asset"), f"{scenario_id}.dem_asset")
        vehicle = raw.get("vehicle")
        if not isinstance(vehicle, dict):
            raise ManifestError(f"{scenario_id}.vehicle must be an object")
        model_path = _check_asset(
            root, vehicle.get("composition_model"), f"{scenario_id}.vehicle.composition_model"
        )
        _check_asset(
            root, vehicle.get("composition_config"), f"{scenario_id}.vehicle.composition_config"
        )
        model_name = model_path.parent.name
        runtime_document = json.loads(runtime_path.read_text(encoding="utf-8"))
        if runtime_document.get("scenario_id") != scenario_id:
            raise ManifestError(f"{scenario_id} runtime scenario identity mismatch")
        runtime_vehicle = runtime_document.get("vehicle")
        if not isinstance(runtime_vehicle, dict):
            raise ManifestError(f"{scenario_id} runtime vehicle is invalid")
        if runtime_vehicle.get("model") != vehicle.get("runtime_model"):
            raise ManifestError(f"{scenario_id} runtime vehicle model mismatch")
        if runtime_vehicle.get("airframe_profile_id") != vehicle.get("airframe_profile_id"):
            raise ManifestError(f"{scenario_id} airframe profile mismatch")
        runtime_sensors = {
            (item.get("sensor_id"), item.get("kind"))
            for item in runtime_document.get("sensors", [])
            if isinstance(item, dict)
        }
        declared_sensors = {
            (item.get("sensor_id"), item.get("kind"))
            for item in raw.get("sensors", [])
            if isinstance(item, dict)
        }
        if runtime_sensors != declared_sensors:
            raise ManifestError(f"{scenario_id} sensor contract differs from runtime input")
        sdf_summary = _verify_sdf(
            world_path,
            model_path,
            model_name=model_name,
            deps=set(vehicle.get("upstream_dependencies", [])),
            repository_root=root,
        )
        target_summary = _verify_target_model(
            world_path,
            raw.get("target_model"),
            repository_root=root,
        )
        plan = stack_plan_from_scenario(
            scenario_path, repo_root=root, tool_capabilities={"gazebo": False, "px4": False}
        )
        if (
            plan.scenario_id != scenario_id
            or plan.model_path.resolve() != model_path.resolve()
            or plan.world_path.resolve() != world_path.resolve()
            or plan.map_path.resolve() != map_path.resolve()
            or plan.dem_path.resolve() != dem_path.resolve()
        ):
            raise ManifestError(
                f"{scenario_id} existing stack planner resolves a different 3-D asset"
            )
        aggregate = (
            raw.get("assets", {}).get("aggregate_sha256")
            if isinstance(raw.get("assets"), dict)
            else None
        )
        if aggregate != plan.assets_sha256:
            raise ManifestError(f"{scenario_id} aggregate asset hash differs from stack planner")
        physics = raw.get("physics")
        if not isinstance(physics, dict):
            raise ManifestError(f"{scenario_id}.physics is invalid")
        physics_node = ET.parse(world_path).getroot().find("world/physics")
        assert physics_node is not None
        if float(physics_node.findtext("max_step_size", "nan")) != float(
            physics.get("max_step_size", "nan")
        ):
            raise ManifestError(f"{scenario_id} max_step_size differs from SDF")
        if float(physics_node.findtext("real_time_update_rate", "nan")) != float(
            physics.get("real_time_update_rate", "nan")
        ):
            raise ManifestError(f"{scenario_id} real_time_update_rate differs from SDF")
        records.append(
            {
                "scenario_id": scenario_id,
                "vehicle_class": vehicle.get("class"),
                "assets_sha256": aggregate,
                "target_model": target_summary,
                **sdf_summary,
            }
        )
    return {
        "event": "gazebo_3d_mainline_manifest_verified",
        "manifest_id": document["manifest_id"],
        "manifest_path": str(path.relative_to(root)).replace("\\", "/"),
        "scenario_count": len(records),
        "scenarios": records,
        "deterministic_asset_audit": True,
        "external_gazebo_runtime": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "hil_acceptance": "not_claimed",
        "outdoor_gnss_acceptance": "not_claimed",
        "final_release": "not_claimed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_manifest(args.manifest, repository_root=args.repository_root)
    except (ManifestError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
