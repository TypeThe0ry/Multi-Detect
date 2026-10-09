# ruff: noqa: E402, E501 -- repository-local path bootstrap and immutable evidence paths.
"""Rebind retained depth Mission evidence to the current Gazebo model assets.

This is an identity-only maintenance operation.  It never starts Docker,
PX4, Gazebo, MAVLink, HIL, or hardware.  The output explicitly records that
the runtime was not rerun, so changed model files cannot be mistaken for a
fresh external-SIM result.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.simulator.stack import resolve_repository_model_asset_bundle
from scripts.verify_current_progress_index import _recompute_external_gazebo_sensor_binding_hash


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write_new(path: Path, value: dict[str, Any], *, allow_overwrite: bool = False) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == payload:
            return
        if not allow_overwrite:
            raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _parity_binding() -> dict[str, Any]:
    path = ROOT / "configs/release-parity.json"
    parity = _load(path)
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": path.stat().st_size,
        "file_sha256": _sha256(path),
        "hashes": parity["parity"],
        "release_label": parity["release_label"],
    }


def main() -> int:
    progress_path = ROOT / "docs/current-progress.json"
    progress = _load(progress_path)
    parity = _parity_binding()
    outputs: list[dict[str, Any]] = []

    # Rebind the current VTOL receipt to the summary that the progress index
    # already names.  The older depthguard-r1 receipt came from a different
    # retained campaign directory and therefore had a mismatched summary
    # identity even though its parity object was current.
    vtol_source = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261001-r31.receipt.json"
    vtol_target = ROOT / "artifacts/evaluation/shared-policy-current-vtol-campaign-20261004-depthguard-r4-current.receipt.json"
    vtol_receipt = _load(vtol_source)
    vtol_receipt["release_parity"] = dict(parity)
    vtol_receipt["release_parity"]["scope"] = "receipt_generation_file_binding"
    vtol_receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in vtol_receipt.items() if key != "receipt_sha256"}
    )
    _write_new(vtol_target, vtol_receipt)
    vtol_entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    vtol_entry["receipt"] = _relative(vtol_target)
    vtol_entry["receipt_bytes"] = vtol_target.stat().st_size
    vtol_entry["receipt_file_sha256"] = _sha256(vtol_target)
    vtol_entry["receipt_canonical_sha256"] = vtol_receipt["receipt_sha256"]

    # Keep the AI-flight join explicitly in retained-layer audit scope.  The
    # previous depthguard copy was made from an older receipt shape and lost
    # that fail-closed binding set.
    ai_source = ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261003-current-r10.receipt.json"
    ai_target = ROOT / "artifacts/evaluation/simulator-ai-flight-parity-20261004-depthguard-r4-current.receipt.json"
    ai_receipt = _load(ai_source)
    policy_layer_path = ROOT / "artifacts/evaluation/shared-policy-external-parent-20260927-r13-policycapture-parity-20261003-depthguard-r1.receipt.json"
    policy_layer = ai_receipt["shared_policy_mission_layer"]
    policy_layer["path"] = str(policy_layer_path)
    policy_layer["bytes"] = policy_layer_path.stat().st_size
    policy_layer["file_sha256"] = _sha256(policy_layer_path)
    policy_layer_document = _load(policy_layer_path)
    policy_layer["receipt_sha256"] = policy_layer_document["receipt_sha256"]
    ai_receipt["release_parity"] = dict(parity)
    ai_receipt["release_parity"].pop("verified", None)
    ai_receipt["release_parity"]["scope"] = "receipt_generation_file_binding"
    ai_receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in ai_receipt.items() if key != "receipt_sha256"}
    )
    _write_new(ai_target, ai_receipt)
    ai_entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    ai_entry["receipt"] = _relative(ai_target)
    ai_entry["receipt_bytes"] = ai_target.stat().st_size
    ai_entry["receipt_file_sha256"] = _sha256(ai_target)
    ai_entry["receipt_sha256"] = ai_receipt["receipt_sha256"]
    ai_entry["shared_policy_mission_layer"] = _relative(policy_layer_path)
    ai_entry["shared_policy_receipt_sha256"] = policy_layer_document["receipt_sha256"]

    sources = {
        "external_fixed_wing_shared_policy_depth_mission_20260930": (
            "external-fixed-wing-depth-shared-policy-20261003-depthguard-r1.json",
            "rc_cessna_mono_cam",
        ),
        "external_multirotor_shared_policy_depth_mission_20260930": (
            "external-multirotor-depth-shared-policy-20261003-depthguard-r1.json",
            "x500_mono_cam",
        ),
    }
    for entry_key, (source_name, model_name) in sources.items():
        entry = progress[entry_key]
        source = ROOT / "artifacts/evaluation" / source_name
        target = ROOT / "artifacts/evaluation" / source_name.replace(
            "depthguard-r1", "depthguard-r4-current"
        )
        evidence = _load(source)
        model_root = ROOT / "simulation/gazebo/models"
        bundle = resolve_repository_model_asset_bundle(model_root, repository_root=ROOT)
        assets = {
            "active_model_file_count": sum(
                1 for path in (model_root / model_name).rglob("*") if path.is_file()
            ),
            "active_model_name": model_name,
            **bundle.portable_mapping(),
        }
        sensor = evidence["sensor_transport"]
        transport = sensor["transport"]
        sensor["repository_model_assets"] = assets
        sensor["model_assets_sha256"] = assets["assets_sha256"]
        transport["model_assets_sha256"] = assets["assets_sha256"]
        evidence["release_parity"] = dict(parity)
        evidence["evidence_scope"] = "retained_files_audit"
        evidence["current_release_parity_bound"] = False
        evidence["current_runtime_parity_proven"] = False
        evidence["retained_file_model_assets_rebound"] = True
        evidence["runtime_source_model_assets_verified"] = False
        evidence["external_sim_rerun_performed"] = False
        sensor_binding_hash = _recompute_external_gazebo_sensor_binding_hash(
            ROOT, entry, evidence, model_name=model_name
        )
        sensor["sensor_binding_hash"] = sensor_binding_hash
        transport["sensor_binding_sha256"] = sensor_binding_hash
        _write_new(target, evidence)
        entry["evidence"] = _relative(target)
        entry["evidence_bytes"] = target.stat().st_size
        entry["evidence_sha256"] = _sha256(target)
        entry["model_assets_sha256"] = assets["assets_sha256"]
        entry["sensor_binding_hash"] = sensor_binding_hash
        outputs.append(
            {
                "entry": entry_key,
                "evidence": _relative(target),
                "evidence_bytes": target.stat().st_size,
                "evidence_sha256": _sha256(target),
                "model_assets_sha256": assets["assets_sha256"],
                "sensor_binding_hash": sensor_binding_hash,
            }
        )

    # The three-vehicle receipts are deterministic joins over the depth
    # evidence.  Point the current index at their already-created r2 copies;
    # do not overwrite either receipt or any historical r1 copy.
    for entry_key, receipt_name in (
        (
            "shared_policy_vehicle_matrix_20260927_r2",
            "artifacts/evaluation/shared-policy-vehicle-matrix-20261004-depthguard-r4-current.json",
        ),
        (
            "external_mission_matrix_20260927_r6",
            "docs/evidence/external-mission-matrix-20261004-depthguard-r4-current.json",
        ),
    ):
        receipt_path = ROOT / receipt_name
        if not receipt_path.is_file():
            # The first invocation intentionally only prepares the source
            # evidence; the deterministic join receipts are created by their
            # dedicated commands immediately afterwards.
            continue
        receipt = _load(receipt_path)
        entry = progress[entry_key]
        entry["evidence"] = _relative(receipt_path)
        entry["evidence_bytes"] = receipt_path.stat().st_size
        entry["evidence_sha256"] = _sha256(receipt_path)
        entry["receipt_sha256"] = receipt.get("receipt_sha256")
        if entry_key == "shared_policy_vehicle_matrix_20260927_r2":
            entry["vtol_receipt"] = _relative(vtol_target)
            entry["vtol_receipt_sha256"] = _sha256(vtol_target)
        else:
            entry["vtol_campaign_receipt"] = _relative(vtol_target)
            entry["vtol_campaign_receipt_sha256"] = _sha256(vtol_target)

    # Bind the release-gate reference to an audit whose report/manifest paths
    # are the same paths recorded by the current index.  The prior audit was
    # correctly fail-closed but still pointed at an older report directory.
    audit_path = ROOT / "docs/evidence/shared-policy-release-parity-policy-helper-20261004-depthguard-r4-current.json"
    if audit_path.is_file():
        audit = _load(audit_path)
        release_ref = progress["evidence"]["release_gate_report"]
        release_ref["audit_summary_path"] = _relative(audit_path)
        release_ref["manifest_local_path"] = audit["manifest_path"]
        release_ref["local_path"] = audit["report_path"]
        release_ref["file_bytes"] = audit["report_bytes"]
        release_ref["file_sha256"] = audit["report_sha256"]
        release_ref["manifest_bytes"] = audit["manifest_bytes"]
        release_ref["manifest_sha256"] = audit["manifest_sha256"]
        release_ref["current_release_parity_bound"] = audit["current_release_parity_bound"]
        release_ref["blocker_count"] = audit["blocker_count"]

    audit_progress_path = ROOT / "docs/current-progress-retained-model-assets-audit.json"
    _write_new(audit_progress_path, progress, allow_overwrite=True)
    print(f"retained audit written: {audit_progress_path}")
    print(json.dumps(outputs, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
