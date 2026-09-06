from __future__ import annotations

import hashlib
import json
from pathlib import Path

from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate, main
from multidetect.simulator.safety_evidence import run_acceptance

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"


def _write_artifact(path: Path, mode: str) -> str:
    path.write_text(
        json.dumps(
            {
                "event": f"{mode}_acceptance_finished",
                "gate_passed": True,
                "runtime_mode": mode,
            }
        ),
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _simulator_safety_binding(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    evidence = tmp_path / "simulator-safety.json"
    document = run_acceptance()
    evidence.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return evidence, {
        "evidence_path": evidence.name,
        "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        "event": document["event"],
        "runtime_mode": document["runtime_mode"],
        "release_label": document["release_label"],
        "gate_passed": document["gate_passed"],
        "verifier_event": "simulator_safety_evidence_verified",
        "verifier_exit_code": 0,
        "checks": document["checks"],
    }


def _manifest(
    tmp_path: Path,
    *,
    parity_suffix: str = "",
    with_simulator_safety: bool = False,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    modes: dict[str, object] = {}
    for mode in ("replay", "sim", "hil", "real"):
        artifact = tmp_path / f"{mode}.json"
        artifact_sha = _write_artifact(artifact, mode)
        statuses = {
            "hardware_acceptance": "not_required",
            "real_control_acceptance": "not_required",
        }
        if mode in {"hil", "real"}:
            statuses = {
                "hardware_acceptance": "passed",
                "real_control_acceptance": "passed",
            }
        modes[mode] = {
            "evidence_path": artifact.name,
            "evidence_sha256": artifact_sha,
            "gate_passed": True,
            "parity": {
                field: (chr(97 + index) * 64)
                for index, field in enumerate(PARITY_FIELDS)
            },
            "operator_visual_acceptance": "passed",
            **statuses,
            "mission_path_acceptance": "passed",
            "artifact_event": f"{mode}_acceptance_finished",
        }
    if parity_suffix:
        modes["real"]["parity"]["ui_contract_hash"] = parity_suffix * 64
    simulator_safety = None
    if with_simulator_safety:
        _, simulator_safety = _simulator_safety_binding(tmp_path)
    manifest = tmp_path / "release-gate.json"
    document: dict[str, object] = {
        "schema_version": 1,
        "product": "Multi-Detect",
        "release_label": "0.3.0-pre.8",
        "modes": modes,
    }
    if simulator_safety is not None:
        document["simulator_safety"] = simulator_safety
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return manifest


def test_release_gate_requires_all_four_modes_and_reports_missing_evidence(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    del raw["modes"]["real"]
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["gate_passed"] is False
    assert report["release_allowed"] is False
    assert any(
        "missing required runtime mode" in reason
        for reason in report["release_blocked_reasons"]
    )
    assert report["modes"]["real"]["passed"] is False


def test_release_gate_checks_shared_parity_and_keeps_prerelease_blocked(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["gate_passed"] is True
    assert report["release_allowed"] is False
    assert all(report["parity"].values())
    assert any("channel is 'prerelease'" in reason for reason in report["release_blocked_reasons"])

    mismatched = _manifest(tmp_path / "mismatch", parity_suffix="f")
    mismatch_report = evaluate_release_gate(mismatched, version_manifest_path=VERSION_MANIFEST)
    assert mismatch_report["gate_passed"] is False
    assert mismatch_report["parity"]["ui_contract_hash"] is False


def test_release_gate_cli_report_only_writes_blocked_report(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    output = tmp_path / "report.json"

    exit_code = main(
        [
            str(manifest),
            "--version-manifest",
            str(VERSION_MANIFEST),
            "--out",
            str(output),
            "--report-only",
        ]
    )

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["gate_passed"] is True
    assert report["release_allowed"] is False


def test_release_gate_rejects_ambiguous_and_nonfinite_evidence_json(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "duplicate")
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    evidence = tmp_path / "duplicate" / "sim.json"
    evidence.write_text(
        '{"event":"sim_acceptance_finished","gate_passed":true,'
        '"gate_passed":false,"runtime_mode":"sim"}',
        encoding="utf-8",
    )
    raw["modes"]["sim"]["evidence_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["modes"]["sim"]["passed"] is False
    assert any(
        "duplicate JSON key 'gate_passed'" in reason
        for reason in report["modes"]["sim"]["reasons"]
    )

    nan_manifest = _manifest(tmp_path / "nonfinite")
    nan_raw = json.loads(nan_manifest.read_text(encoding="utf-8"))
    nan_evidence = tmp_path / "nonfinite" / "sim.json"
    nan_evidence.write_text(
        '{"event":"sim_acceptance_finished","gate_passed":NaN,"runtime_mode":"sim"}',
        encoding="utf-8",
    )
    nan_raw["modes"]["sim"]["evidence_sha256"] = hashlib.sha256(
        nan_evidence.read_bytes()
    ).hexdigest()
    nan_manifest.write_text(json.dumps(nan_raw), encoding="utf-8")

    nan_report = evaluate_release_gate(nan_manifest, version_manifest_path=VERSION_MANIFEST)

    assert nan_report["modes"]["sim"]["passed"] is False
    assert any(
        "non-finite JSON number NaN" in reason
        for reason in nan_report["modes"]["sim"]["reasons"]
    )


def test_release_gate_fixed_timestamp_is_byte_reproducible(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    first = tmp_path / "report-first.json"
    second = tmp_path / "report-second.json"
    fixed_time = "2026-09-02T00:00:00+08:00"

    first_exit = main(
        [
            str(manifest),
            "--version-manifest",
            str(VERSION_MANIFEST),
            "--out",
            str(first),
            "--report-only",
            "--evaluated-at-utc",
            fixed_time,
        ]
    )
    second_exit = main(
        [
            str(manifest),
            "--version-manifest",
            str(VERSION_MANIFEST),
            "--out",
            str(second),
            "--report-only",
            "--evaluated-at-utc",
            fixed_time,
        ]
    )

    assert first_exit == second_exit == 0
    assert first.read_bytes() == second.read_bytes()
    report = json.loads(first.read_text(encoding="utf-8"))
    assert report["evaluated_at_utc"] == "2026-09-01T16:00:00.000000Z"


def test_release_gate_rejects_naive_fixed_timestamp(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    try:
        evaluate_release_gate(
            manifest,
            version_manifest_path=VERSION_MANIFEST,
            evaluated_at_utc="2026-09-02T00:00:00",
        )
    except ValueError as exc:
        assert "must include a timezone" in str(exc)
    else:  # pragma: no cover - assertion documents the fail-closed contract
        raise AssertionError("naive evaluated_at_utc unexpectedly accepted")


def test_release_gate_rejects_unclaimed_visual_or_hardware_status(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["modes"]["sim"]["operator_visual_acceptance"] = "not_claimed"
    raw["modes"]["real"]["hardware_acceptance"] = "pending"
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["gate_passed"] is False
    assert report["release_allowed"] is False
    reasons = report["release_blocked_reasons"]
    assert any("operator_visual_acceptance is 'not_claimed'" in reason for reason in reasons)
    assert any("hardware_acceptance is 'pending'" in reason for reason in reasons)


def test_release_gate_normalizes_runtime_mode_and_event_whitespace(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    artifact_path = tmp_path / "sim.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["runtime_mode"] = "  SIM  "
    artifact["event"] = "  sim_acceptance_finished  "
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["modes"]["sim"]["evidence_sha256"] = hashlib.sha256(
        artifact_path.read_bytes()
    ).hexdigest()
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["modes"]["sim"]["passed"] is True
    assert report["modes"]["sim"]["evidence"]["runtime_mode"] == "sim"
    assert report["modes"]["sim"]["evidence"]["event"] == "sim_acceptance_finished"


def test_release_gate_rehashes_and_reverifies_simulator_safety_binding(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, with_simulator_safety=True)

    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    safety = report["simulator_safety"]
    assert safety["present"] is True
    assert safety["passed"] is True
    assert safety["verification"]["event"] == "simulator_safety_evidence_verified"
    assert safety["verification"]["gate_passed"] is True
    assert report["gate_passed"] is True

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["simulator_safety"]["checks"]["loopback_only"] = False
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    tampered_binding = evaluate_release_gate(
        manifest,
        version_manifest_path=VERSION_MANIFEST,
    )
    assert tampered_binding["gate_passed"] is False
    assert tampered_binding["simulator_safety"]["passed"] is False
    assert any(
        "simulator_safety checks do not match verifier output" in reason
        for reason in tampered_binding["release_blocked_reasons"]
    )

    safety_path = tmp_path / "simulator-safety.json"
    source = json.loads(safety_path.read_text(encoding="utf-8"))
    source["control"]["loopback_only"] = False
    safety_path.write_text(json.dumps(source), encoding="utf-8")
    tampered_source = evaluate_release_gate(
        manifest,
        version_manifest_path=VERSION_MANIFEST,
    )
    assert tampered_source["gate_passed"] is False
    assert tampered_source["simulator_safety"]["passed"] is False
    assert any(
        "simulator_safety evidence SHA-256 does not match" in reason
        for reason in tampered_source["release_blocked_reasons"]
    )


def test_release_gate_handles_malformed_simulator_safety_check_map(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, with_simulator_safety=True)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["simulator_safety"]["checks"] = []
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(manifest, version_manifest_path=VERSION_MANIFEST)

    assert report["gate_passed"] is False
    assert any(
        "simulator_safety.checks must be an object" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_final_release_requires_hash_bound_simulator_scenario_matrix(
    tmp_path: Path,
) -> None:
    """The final channel cannot be enabled by four-mode evidence alone."""

    manifest = _manifest(tmp_path)
    raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    raw_manifest["release_label"] = "0.3.0"
    manifest.write_text(json.dumps(raw_manifest), encoding="utf-8")

    release_version = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    release_version.update(
        {
            "release_label": "0.3.0",
            "python": "0.3.0",
            "channel": "release",
        }
    )
    release_version_path = tmp_path / "product-version-release.json"
    release_version_path.write_text(json.dumps(release_version), encoding="utf-8")

    report = evaluate_release_gate(
        manifest,
        version_manifest_path=release_version_path,
    )

    assert report["gate_passed"] is True
    assert report["release_allowed"] is False
    assert report["simulator_scenario_matrix"]["present"] is False
    assert report["simulator_scene_stream_set"]["present"] is False
    assert any(
        "final release requires provenance-complete replay_set calibration verification" in reason
        for reason in report["release_blocked_reasons"]
    )
    assert any(
        "final release requires a hash-bound three-airframe SIM scenario matrix" in reason
        for reason in report["release_blocked_reasons"]
    )
    assert any(
        "final release requires a hash-bound three-airframe SIM scene-stream set" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_final_release_requires_hash_bound_actuator_safety_binding(
    tmp_path: Path,
) -> None:
    """The final channel cannot omit the shared executioner safety contract."""

    manifest = _manifest(tmp_path)
    raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    raw_manifest["release_label"] = "0.3.0"
    manifest.write_text(json.dumps(raw_manifest), encoding="utf-8")

    release_version = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    release_version.update(
        {
            "release_label": "0.3.0",
            "python": "0.3.0",
            "channel": "release",
        }
    )
    release_version_path = tmp_path / "product-version-release.json"
    release_version_path.write_text(json.dumps(release_version), encoding="utf-8")

    report = evaluate_release_gate(
        manifest,
        version_manifest_path=release_version_path,
    )

    assert report["actuator_safety"]["present"] is False
    assert any(
        "final release requires a hash-bound actuator safety evidence" in reason
        for reason in report["release_blocked_reasons"]
    )
