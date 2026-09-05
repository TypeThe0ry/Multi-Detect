from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect import pixhawk_covariance_binding as binding
from multidetect.release_gate import evaluate_release_gate
from multidetect.release_gate_manifest import build_release_gate_manifest


def _sources(tmp_path: Path) -> list[tuple[str, str, Path]]:
    sources: list[tuple[str, str, Path]] = []
    for mode, vehicle in binding.REQUIRED_COMBINATIONS:
        path = tmp_path / "reports" / mode / f"{vehicle}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "runtime_mode": mode,
                    "gate_passed": True,
                    "event": f"{mode}_acceptance_finished",
                }
            ),
            encoding="utf-8",
        )
        sources.append((mode, vehicle, path))
    return sources


def _record(mode: str, vehicle: str, path: Path, *, manifest_directory: Path):
    del manifest_directory
    adapter_id, adapter_hash = binding._ADAPTERS[vehicle]
    marker = binding.REQUIRED_COMBINATIONS.index((mode, vehicle)) + 1
    digest = f"{marker:064x}"
    return {
        "runtime_mode": mode,
        "vehicle_class": vehicle,
        "report_path": str(path),
        "report_sha256": digest,
        "report_bytes": 2,
        "evidence_sha256": digest,
        "records_verified": 10,
        "endpoint": "udpin:127.0.0.1:14550",
        "baud": 57600,
        "mission_path": f"mission-{vehicle}.json",
        "mission_file_sha256": digest,
        "mission_id": f"mission-{vehicle}",
        "mission_sha256": digest,
        "airframe_adapter_id": adapter_id,
        "airframe_adapter_hash": adapter_hash,
        "mission_feedback_path": f"feedback-{mode}-{vehicle}.json",
        "mission_feedback_file_sha256": digest,
        "mission_feedback_evidence_sha256": digest,
        "runtime_profile_path": f"profile-{mode}-{vehicle}.json",
        "runtime_profile_file_sha256": digest,
        "runtime_profile_sha256": digest,
        "runtime_profile_id": "hil" if mode == "hil" else "flight",
        "heartbeat_identity": {
            "source_system_id": 1,
            "source_component_id": 1,
            "autopilot_id": 12,
            "vehicle_type_id": 2,
            "system_status_id": 4,
            "mavlink_version": 3,
        },
        "firmware_identity_hash": digest,
        "mode_binding_sha256": digest,
        "read_only": True,
        "hardware_control_enabled": False,
        "transport_link_healthy": True,
        "qualified_link_healthy": True,
        "gate_passed": True,
    }


def test_current_mode_binding_reuses_capture_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    class CaptureModule:
        @staticmethod
        def build_mode_binding(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"gate_passed": True, "checks": {"current": True}}

    monkeypatch.setattr(binding, "_capture_module", lambda: CaptureModule())
    result = binding.build_pixhawk_covariance_mode_binding(
        runtime_mode="hil",
        endpoint="COM3",
        baud=57600,
        diagnostics={"read_only": True},
        mission_path=tmp_path / "mission.json",
        mission_feedback_path=tmp_path / "feedback.json",
        runtime_profile_path=tmp_path / "profile.json",
    )

    assert result["gate_passed"] is True
    assert calls == [
        {
            "runtime_mode": "hil",
            "endpoint": "COM3",
            "baud": 57600,
            "diagnostics": {"read_only": True},
            "mission_path": tmp_path / "mission.json",
            "mission_feedback_path": tmp_path / "feedback.json",
            "runtime_profile_path": tmp_path / "profile.json",
        }
    ]


def test_binding_requires_and_reverifies_exact_six_leaf_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(binding, "_record", _record)
    sources = _sources(tmp_path)
    record = binding.build_pixhawk_covariance_binding(
        sources,
        manifest_directory=tmp_path,
        output_identity=tmp_path / "release-gate.json",
        release_label="0.3.0-pre.8",
        px4_mission_path_hash="a" * 64,
    )

    result = binding.verify_pixhawk_covariance_binding(
        record,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash="a" * 64,
    )

    assert result["passed"] is True
    assert len(result["observations"]) == 6
    assert all(result["checks"].values())
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "schemas"
            / "release-gate-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert not list(
        validator.iter_errors(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0-pre.8",
                "modes": {
                    mode: {
                        "evidence_path": f"{mode}.json",
                        "evidence_sha256": "1" * 64,
                        "gate_passed": True,
                        "parity": {
                            "ui_contract_hash": "1" * 64,
                            "core_algorithm_hash": "2" * 64,
                            "task_contract_hash": "3" * 64,
                            "safety_policy_hash": "4" * 64,
                            "px4_mission_path_hash": "a" * 64,
                        },
                        "operator_visual_acceptance": "passed",
                        "hardware_acceptance": "not_required"
                        if mode in {"replay", "sim"}
                        else "passed",
                        "real_control_acceptance": (
                            "not_required" if mode in {"replay", "sim"} else "passed"
                        ),
                        "mission_path_acceptance": "passed",
                    }
                    for mode in ("replay", "sim", "hil", "real")
                },
                "pixhawk_covariance": record,
            }
        )
    )


def test_release_manifest_and_evaluator_consume_covariance_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(binding, "_record", _record)
    modes = ("replay", "sim", "hil", "real")
    evidence: dict[str, Path] = {}
    for mode in modes:
        path = tmp_path / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "runtime_mode": mode,
                    "gate_passed": True,
                    "event": f"{mode}_acceptance_finished",
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
    parity = {
        "ui_contract_hash": "1" * 64,
        "core_algorithm_hash": "2" * 64,
        "task_contract_hash": "3" * 64,
        "safety_policy_hash": "4" * 64,
        "px4_mission_path_hash": "a" * 64,
    }
    statuses = {
        mode: {
            "operator_visual_acceptance": "passed",
            "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "real_control_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
            "mission_path_acceptance": "passed",
        }
        for mode in modes
    }
    output = tmp_path / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=parity,
        statuses=statuses,
        output_path=output,
        pixhawk_covariance_evidence=_sources(tmp_path),
    )
    output.write_text(json.dumps(document), encoding="utf-8")

    result = evaluate_release_gate(output)

    assert document["pixhawk_covariance"]["gate_passed"] is True
    assert result["pixhawk_covariance"]["present"] is True
    assert result["pixhawk_covariance"]["passed"] is True
    assert result["pixhawk_covariance_required_for_release"] is True


def test_binding_rejects_duplicate_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binding, "_record", _record)
    sources = _sources(tmp_path)
    sources[-1] = sources[0]

    with pytest.raises(binding.PixhawkCovarianceBindingError, match="duplicate"):
        binding.build_pixhawk_covariance_binding(
            sources,
            manifest_directory=tmp_path,
            output_identity=tmp_path / "release-gate.json",
            release_label="0.3.0-pre.8",
            px4_mission_path_hash="a" * 64,
        )


def test_verifier_rejects_compact_record_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(binding, "_record", _record)
    record = binding.build_pixhawk_covariance_binding(
        _sources(tmp_path),
        manifest_directory=tmp_path,
        output_identity=tmp_path / "release-gate.json",
        release_label="0.3.0-pre.8",
        px4_mission_path_hash="a" * 64,
    )
    record["observations"][0]["firmware_identity_hash"] = "f" * 64

    result = binding.verify_pixhawk_covariance_binding(
        record,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash="a" * 64,
    )

    assert result["passed"] is False
    assert any("compact record does not match sources" in reason for reason in result["reasons"])


def test_prior_v86_report_is_rejected_after_current_parity_changes() -> None:
    report = (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "evaluation"
        / "terminal-continuation-v86"
        / "px4-sitl-covariance-schema-v3-v86-pre8-final.json"
    )
    if not report.exists():
        pytest.skip("external v86 evidence is not present in this checkout")

    with pytest.raises(
        binding.PixhawkCovarianceBindingError,
        match="does not reproduce from raw observations",
    ):
        binding._verify_covariance_file(report)
