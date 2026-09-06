from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    ReleaseGateManifestError,
    build_release_gate_manifest,
    create_release_gate_manifest,
)
from multidetect.simulator_external_evidence import (
    REQUIRED_VEHICLES,
    build_simulator_external_evidence,
    revalidate_simulator_external_evidence,
    write_simulator_external_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"
PARITY = {field: hashlib.sha256(field.encode()).hexdigest() for field in PARITY_FIELDS}


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _vehicle_names(vehicle: str) -> tuple[str, str, str]:
    if vehicle == "vtol":
        return "river_rescue_vtol_001", "px4_vtol_standard", "px4_vtol_gazebo_v1"
    if vehicle == "fixed-wing":
        return (
            "coastal_patrol_fixed_wing_001",
            "px4_fixed_wing_standard",
            "px4_fixed_wing_gazebo_v1",
        )
    return "urban_inspection_multirotor_001", "px4_multirotor_standard", "px4_multirotor_gazebo_v1"


def _external_document(
    vehicle: str,
    *,
    ai: bool = False,
    image: str | None = None,
) -> dict[str, object]:
    scenario_id, model, airframe = _vehicle_names(vehicle)
    image = image or "px4io/px4-sitl-gazebo@sha256:" + _sha("image")
    document: dict[str, object] = {
        "schema": "multidetect.simulator-docker-acceptance",
        "schema_version": 1,
        "release_status": "v0.3.0-pre.8; final v0.3.0 remains blocked",
        "execution_requested": True,
        "gate_passed": True,
        "preflight_gate_passed": True,
        "runtime_gate_passed": True,
        "plan": {
            "scenario_id": scenario_id,
            "scenario_manifest_hash": _sha(f"manifest:{vehicle}"),
            "truth_free": True,
            "image": image,
            "vehicle": {"model": model, "model_name": airframe},
        },
        "scenario": {
            "scenario_id": scenario_id,
            "vehicle_model": model,
            "airframe_profile_id": airframe,
            "scenario_manifest_hash": _sha(f"manifest:{vehicle}"),
            "manifest_sha256": _sha(f"scenario:{vehicle}"),
            "truth_loaded_by_runtime": False,
        },
        "container_runtime": {
            "image": image,
            "external_process_started": True,
            "cleanup": {"attempted": True, "removed": True, "errors": []},
        },
        "hardware": {"status": "not_claimed", "hardware_control_enabled": False},
        "safety": {
            "truth_free": True,
            "hardware_control_enabled": False,
            "real_pixhawk_contacted": False,
        },
        "telemetry": {
            "qualified": True,
            "gps": {"fix_type": 3, "satellites_visible": 10},
        },
    }
    if ai:
        document.update(
            {
                "ai_policy_gate_passed": True,
                "target_policy_gate_passed": True,
                "ai_steps": 2,
                "ai_policy_steps": 2,
                "ai": {"armed_observed": True},
                "target_exercise": {
                    "accepted": True,
                    "truth_free": True,
                    "visual_accuracy": "not_claimed",
                    "decision": {"action": "track"},
                },
            }
        )
    return document


def _external_inputs(tmp_path: Path) -> tuple[dict[str, Path], dict[str, Path], Path]:
    runtime: dict[str, Path] = {}
    ai: dict[str, Path] = {}
    for vehicle in REQUIRED_VEHICLES:
        runtime_path = tmp_path / f"{vehicle}-runtime.json"
        ai_path = tmp_path / f"{vehicle}-ai.json"
        runtime_path.write_text(json.dumps(_external_document(vehicle)), encoding="utf-8")
        ai_path.write_text(
            json.dumps(_external_document(vehicle, ai=True)),
            encoding="utf-8",
        )
        runtime[vehicle] = runtime_path
        ai[vehicle] = ai_path
    parity_path = tmp_path / "release-parity.json"
    parity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0-pre.8",
                "parity": PARITY,
            }
        ),
        encoding="utf-8",
    )
    return runtime, ai, parity_path


def _mode_inputs(tmp_path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for mode in ("replay", "sim", "hil", "real"):
        path = tmp_path / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "runtime_mode": mode,
                    "gate_passed": True,
                }
            ),
            encoding="utf-8",
        )
        result[mode] = path
    return result


def _statuses() -> dict[str, dict[str, str]]:
    return {
        mode: {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
        }
        for mode in ("replay", "sim", "hil", "real")
    }


def _write_external_pair(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    runtime, ai, parity_path = _external_inputs(tmp_path)
    join = tmp_path / "external-join.json"
    revalidation = tmp_path / "external-revalidation.json"
    document = build_simulator_external_evidence(runtime, ai, parity_path=parity_path)
    write_simulator_external_evidence(join, document)
    verification = revalidate_simulator_external_evidence(
        join,
        expected_release_label="0.3.0-pre.8",
    )
    write_simulator_external_evidence(revalidation, verification)
    return join, revalidation, _mode_inputs(tmp_path)


def test_release_gate_writer_binds_external_join_and_revalidation(tmp_path: Path) -> None:
    join, revalidation, modes = _write_external_pair(tmp_path)
    output = tmp_path / "release-gate.json"

    document = create_release_gate_manifest(
        output,
        modes["replay"],
        modes["sim"],
        modes["hil"],
        modes["real"],
        PARITY,
        statuses=_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        simulator_external_evidence=join,
        simulator_external_evidence_revalidation=revalidation,
    )

    binding = document["simulator_external_evidence"]
    assert binding["gate_passed"] is True
    assert binding["source_recomputed"] is True
    assert binding["checks"]["six_sources_distinct"] is True
    assert set(binding["source_hashes"]) == set(REQUIRED_VEHICLES)

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["simulator_external_evidence"]["passed"] is True
    assert report["gate_passed"] is True


def test_release_gate_external_binding_rejects_source_drift(tmp_path: Path) -> None:
    join, revalidation, modes = _write_external_pair(tmp_path)
    output = tmp_path / "release-gate.json"
    create_release_gate_manifest(
        output,
        modes["replay"],
        modes["sim"],
        modes["hil"],
        modes["real"],
        PARITY,
        statuses=_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        simulator_external_evidence=join,
        simulator_external_evidence_revalidation=revalidation,
    )

    join_document = json.loads(join.read_text(encoding="utf-8"))
    source_path = Path(join_document["vehicles"]["vtol"]["runtime"]["path"])
    source_path.write_text(source_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    external = report["simulator_external_evidence"]
    assert external["passed"] is False
    assert any(
        "simulator_external_evidence" in reason for reason in report["release_blocked_reasons"]
    )


def test_release_gate_external_writer_requires_current_parity(tmp_path: Path) -> None:
    join, revalidation, modes = _write_external_pair(tmp_path)
    mismatched = copy.deepcopy(PARITY)
    mismatched["core_algorithm_hash"] = _sha("different-core")

    with pytest.raises(
        ReleaseGateManifestError,
        match="external_evidence join parity hashes do not match",
    ):
        build_release_gate_manifest(
            modes,
            parity=mismatched,
            statuses=_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            simulator_external_evidence=join,
            simulator_external_evidence_revalidation=revalidation,
        )


def test_final_release_requires_external_runtime_ai_binding(tmp_path: Path) -> None:
    modes = _mode_inputs(tmp_path)
    manifest = tmp_path / "release-gate.json"
    document = build_release_gate_manifest(
        modes,
        parity=PARITY,
        statuses=_statuses(),
        output_path=manifest,
        version_manifest_path=VERSION_MANIFEST,
    )
    document["release_label"] = "0.3.0"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    release_version = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    release_version.update({"release_label": "0.3.0", "python": "0.3.0", "channel": "release"})
    release_version_path = tmp_path / "product-version-release.json"
    release_version_path.write_text(json.dumps(release_version), encoding="utf-8")

    report = evaluate_release_gate(manifest, version_manifest_path=release_version_path)
    assert report["simulator_external_evidence"]["present"] is False
    assert any(
        "final release requires a hash-bound six-source external SIM runtime/live-AI evidence"
        in reason
        for reason in report["release_blocked_reasons"]
    )
