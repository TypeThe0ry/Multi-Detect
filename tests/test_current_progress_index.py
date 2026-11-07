from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import scripts.verify_current_progress_index as progress_verifier
from scripts.verify_current_progress_index import verify, verify_release_parity_sources

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def current_progress_evidence() -> None:
    artifacts = (ROOT / "artifacts").resolve()
    if not artifacts.is_relative_to(ROOT):
        pytest.skip(
            "current-progress 绑定测试需要仓库内完整证据包；"
            f"只读共享 artifacts 解析到 worktree 外：{artifacts}"
        )

    def artifact_paths(value: object) -> Iterator[str]:
        if isinstance(value, dict):
            for item in value.values():
                yield from artifact_paths(item)
        elif isinstance(value, list):
            for item in value:
                yield from artifact_paths(item)
        elif isinstance(value, str) and value.startswith("artifacts/"):
            yield value

    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    for value in sorted(set(artifact_paths(progress))):
        path = (ROOT / value).resolve()
        if not path.is_relative_to(ROOT):
            pytest.skip(f"current-progress 固定证据解析到仓库外：{value}")
        if not path.exists():
            pytest.skip(f"current-progress 缺少完整固定证据包：{value}")


@pytest.fixture
def progress_scratch() -> Iterator[Path]:
    directory = ROOT / ".tmp"
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="current-progress-", dir=directory) as name:
        yield Path(name)


def test_release_parity_sources_recompute_against_current_workspace() -> None:
    result = verify_release_parity_sources(ROOT)
    assert result["path"] == "configs/release-parity.json"
    current = json.loads((ROOT / "configs/release-parity.json").read_text(encoding="utf-8"))
    assert result["parity"] == current["parity"]


def test_release_parity_sources_reject_recomputed_document_drift(monkeypatch) -> None:
    original = progress_verifier.build_release_parity_document

    def drifted(*args, **kwargs):
        document = original(*args, **kwargs)
        document["parity"] = dict(document["parity"])
        document["parity"]["core_algorithm_hash"] = "0" * 64
        return document

    monkeypatch.setattr(progress_verifier, "build_release_parity_document", drifted)
    with pytest.raises(ValueError, match="release parity source recomputation drift"):
        verify_release_parity_sources(ROOT)


@pytest.mark.usefixtures("current_progress_evidence")
def test_current_progress_index_is_bound() -> None:
    result = verify(ROOT)
    assert result["status"] == "passed"
    manifest = json.loads((ROOT / "configs/product-version.json").read_text(encoding="utf-8"))
    assert result["release_label"] == manifest["release_label"]
    assert result["release_allowed"] is False
    assert result["canonical_external_repeat_bound"] is True
    assert result["release_gate_report_bound"] is True
    assert result["field_link_bound"] is True
    assert result["observed_session_bound"] is True
    assert result["current_shared_policy_observed_session_bound"] is True
    assert result["shared_policy_campaign_receipts_bound"] is True
    assert result["shared_policy_vehicle_matrix_bound"] is True
    assert result["current_shared_policy_vtol_campaign_bound"] is True
    assert result["shared_policy_runtime_matrix_bound"] is True
    assert result["shared_policy_training_holdout_bound"] is True
    assert result["shared_policy_trained_artifact_external_bound"] is True
    assert result["replay_set_readiness_bound"] is True
    assert result["external_mission_matrix_bound"] is True
    assert result["external_fixed_wing_shared_policy_depth_mission_bound"] is True
    assert result["current_combined_sim_qgc_bound"] is True
    assert result["v030_pre1_foundation_bound"] is True


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_v030_pre1_foundation_receipt() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["v030_pre1_foundation_acceptance_20261003_r5"]
    evidence_path = ROOT / entry["evidence"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert entry["evidence_bytes"] == evidence_path.stat().st_size
    assert entry["evidence_file_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    unsigned = {key: value for key, value in evidence.items() if key != "sha256"}
    assert entry["evidence_sha256"] == evidence["sha256"]
    assert evidence["sha256"] == progress_verifier._canonical_sha256(unsigned)
    assert evidence["gate_passed"] is True
    assert all(value["passed"] is True for value in evidence["checks"].values())


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_v030_pre1_foundation_source_drift(monkeypatch) -> None:
    original_sha256 = progress_verifier._sha256
    drifted_source = (ROOT / "scripts/verify_v030_pre1_foundation.py").resolve()

    def drifted_sha256(path: Path) -> str:
        if path.resolve() == drifted_source:
            return "0" * 64
        return original_sha256(path)

    monkeypatch.setattr(progress_verifier, "_sha256", drifted_sha256)
    with pytest.raises(ValueError, match="v0.3.0-pre.1 foundation source hash drift"):
        verify(ROOT)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_current_qgc_combined_sim() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["combined_sim_qgc_external_vtol_pass_20261003_r4"]
    source_path = ROOT / entry["source_evidence"]
    summary_path = ROOT / entry["evidence"]
    source = json.loads(source_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert entry["source_evidence_bytes"] == source_path.stat().st_size
    assert entry["source_evidence_sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert entry["evidence_bytes"] == summary_path.stat().st_size
    assert entry["evidence_sha256"] == hashlib.sha256(summary_path.read_bytes()).hexdigest()
    assert source["gate_passed"] is True
    assert source["failure"] is None
    assert source["qgc"]["native_target_selection"]["selection_ack_correlated"] is True
    assert source["qgc"]["native_target_selection"]["qgc_operator_tracked_adoption_proved"] is True
    assert source["integrated_sim_qgc"]["result"]["processed_frames"] == 900
    assert source["docker"]["container_stopped_and_removed"] is True
    assert summary["release_parity_sha256"] == hashlib.sha256(
        (ROOT / "configs/release-parity.json").read_bytes()
    ).hexdigest()


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_current_qgc_combined_sim_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["combined_sim_qgc_external_vtol_pass_20261003_r4"]["source_evidence_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="current QGC combined-SIM source evidence SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_external_fixed_wing_shared_policy_depth_mission() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["external_fixed_wing_shared_policy_depth_mission_20260930"]
    evidence_path = ROOT / entry["evidence"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert entry["evidence_bytes"] == evidence_path.stat().st_size
    assert entry["evidence_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert entry["scenario_runtime_depth_sensor"] == "forward-depth"
    assert evidence["gate_passed"] is True
    assert evidence["shared_policy_used"] is True
    assert evidence["mission_transaction_gate_passed"] is True
    assert evidence["sensor_transport_gate_passed"] is True
    assert evidence["sensor_transport"]["transport"]["depth_companion_count"] == 2
    assert evidence["sensor_transport"]["transport"]["depth_enabled"] is True
    assert evidence["mission_transaction"]["flight_started"] is False


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_external_mission_matrix() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    matrix = progress["external_mission_matrix_20260927_r6"]
    matrix_path = ROOT / matrix["evidence"]
    document = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert matrix["evidence_bytes"] == matrix_path.stat().st_size
    assert matrix["receipt_sha256"] == document["receipt_sha256"]
    assert document["same_external_mission_path"] is True
    assert document["same_core_compiler_and_transaction"] is True
    assert document["current_release_parity_bound"] is True
    assert document["source_release_parity_binding_required_for_promotion"] is True
    assert document["flight_started"] is False


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_current_shared_policy_vtol_campaign() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    receipt_path = ROOT / entry["receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    parity = json.loads((ROOT / "configs/release-parity.json").read_text(encoding="utf-8"))
    assert entry["receipt_file_sha256"] == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    assert entry["receipt_canonical_sha256"] == receipt["receipt_sha256"]
    assert receipt["release_parity"]["file_sha256"] == hashlib.sha256(
        (ROOT / "configs/release-parity.json").read_bytes()
    ).hexdigest()
    assert receipt["release_parity"]["hashes"] == parity["parity"]
    assert receipt["shared_policy_used"] is True
    assert receipt["planner_revalidated"] is True
    assert receipt["final_release_allowed"] is False


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_current_vtol_receipt_bound_to_old_parity(
    tmp_path: Path,
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["shared_policy_current_vtol_campaign_20260928_r2"]
    old_receipt_path = ROOT / (
        "artifacts/evaluation/shared-policy-current-vtol-campaign-20260928-r2.receipt.json"
    )
    entry["receipt"] = str(old_receipt_path.relative_to(ROOT))
    entry["receipt_bytes"] = old_receipt_path.stat().st_size
    entry["receipt_file_sha256"] = hashlib.sha256(old_receipt_path.read_bytes()).hexdigest()
    old_receipt = json.loads(old_receipt_path.read_text(encoding="utf-8"))
    entry["receipt_canonical_sha256"] = old_receipt["receipt_sha256"]
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="current shared-policy VTOL release parity binding"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_current_shared_policy_runtime_matrix() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["shared_policy_runtime_matrix_20260927_r3"]
    evidence_path = ROOT / entry["evidence"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert entry["evidence_bytes"] == evidence_path.stat().st_size
    assert entry["evidence_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert document["same_policy_artifact"] is True
    assert document["same_release_parity"] is True
    assert document["actuator_capability_granted"] is False
    assert document["mavlink_sent"] is False


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_shared_policy_runtime_matrix_hash_drift(
    tmp_path: Path,
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_runtime_matrix_20260927_r3"]["evidence_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="shared-policy runtime matrix file identity"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_external_mission_matrix_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["external_mission_matrix_20260927_r6"]["evidence_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="external Mission matrix SHA-256"):
        verify(ROOT, path)


def test_release_gate_audit_keeps_current_and_historical_field_link_bindings() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    audit = json.loads(
        (ROOT / progress["evidence"]["release_gate_report"]["audit_summary_path"]).read_text(
            encoding="utf-8"
        )
    )
    current = audit["current_field_link_probe"]
    historical = audit["field_link_probe"]
    assert current["preflight_path"] == progress["evidence"][
        "field_link_read_only_current_probe"
    ]["local_path"]
    assert current["preflight_sha256"] != historical["preflight_sha256"]
    assert current["hardware_link_ready"] is False
    assert historical["read_only"] is True


@pytest.mark.parametrize("field", ["file_sha256", "bytes", "hashes"])
def test_progress_index_rejects_parity_drift(tmp_path: Path, field: str) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    if field == "hashes":
        progress["release_parity"][field] = {}
    elif field == "bytes":
        progress["release_parity"][field] += 1
    else:
        progress["release_parity"][field] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="parity"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_external_report_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_external_campaign_api_update"][
        "external_repeat_3x3_sim_energy_evaluation_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="external repeat"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_shared_policy_training_report_hash_drift(
    tmp_path: Path,
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_training_holdout_20261001_r1"][
        "training_report_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="shared-policy training report file identity"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_trained_artifact_receipt_canonical_drift(
    tmp_path: Path,
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_trained_artifact_external_vtol_campaign_20261001_r1"][
        "receipt_canonical_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="trained-artifact receipt safety/canonical binding"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_all_external_gazebo_fault_sources() -> None:
    result = verify(ROOT)
    assert result["external_gazebo_sensor_fault_runs_bound"] is True


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_external_multirotor_shared_policy_depth_mission() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["external_multirotor_shared_policy_depth_mission_20260930"]
    evidence_path = ROOT / entry["evidence"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert entry["evidence_bytes"] == evidence_path.stat().st_size
    assert entry["evidence_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert evidence["gate_passed"] is True
    assert evidence["mission_transaction_gate_passed"] is True
    assert evidence["sensor_transport_gate_passed"] is True
    assert evidence["shared_policy_used"] is True
    assert evidence["task_binding"]["planner_revalidated"] is True
    assert evidence["mission_transaction"]["flight_started"] is False
    assert evidence["sensor_transport"]["transport"]["depth_companion_count"] == 2
    assert evidence["external_airframe_profile_sha256"] == evidence["sensor_transport"][
        "airframe_profile_sha256"
    ]
    assert (
        evidence["sensor_transport"]["airframe_profile_sha256"]
        == evidence["mission_transaction"]["airframe_profile_hash"]
        == entry["external_airframe_profile_sha256"]
    )
    assert evidence["sensor_runtime_binding"]["same_container"] is True
    assert (
        evidence["sensor_runtime_binding"]["container_id"]
        == evidence["mission_transaction"]["runtime_attestation"]["container_id"]
        == evidence["container_runtime"]["container_id"]
    )
    assert (
        evidence["mission_transaction"]["executable_mission_hash"]
        == evidence["task_binding"]["executable_mission_sha256"]
    )
    assert evidence["mission_transaction"]["protocol_hash"] == evidence["task_binding"]["wire_hash"]


@pytest.mark.usefixtures("current_progress_evidence")
@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("container", "external multirotor shared-policy depth Mission safety binding"),
        ("mission", "external multirotor shared-policy depth Mission safety binding"),
        ("wire", "external multirotor shared-policy depth Mission safety binding"),
        ("airframe", "external multirotor policy artifact canonical binding"),
    ],
)
def test_progress_index_rejects_external_multirotor_cross_binding_drift(
    tmp_path: Path, progress_scratch: Path, field: str, message: str
) -> None:
    temp_dir = progress_scratch / "cross-binding"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    try:
        entry = progress["external_multirotor_shared_policy_depth_mission_20260930"]
        evidence = json.loads((ROOT / entry["evidence"]).read_text(encoding="utf-8"))
        if field == "container":
            evidence["mission_transaction"]["runtime_attestation"]["container_id"] = "different"
        elif field == "mission":
            evidence["task_binding"]["executable_mission_sha256"] = "0" * 64
        elif field == "wire":
            evidence["task_binding"]["wire_hash"] = "0" * 64
        else:
            evidence["sensor_transport"]["airframe_profile_sha256"] = "0" * 64
        evidence_path = temp_dir / "evidence.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        entry["evidence"] = str(evidence_path.relative_to(ROOT))
        entry["evidence_bytes"] = evidence_path.stat().st_size
        entry["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        progress_path = tmp_path / "progress.json"
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            verify(ROOT, progress_path)
    finally:
        shutil.rmtree(temp_dir)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_external_multirotor_ranker_semantic_drift(
    tmp_path: Path,
    progress_scratch: Path,
) -> None:
    temp_dir = progress_scratch / "ranker-drift"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    try:
        entry = progress["external_multirotor_shared_policy_depth_mission_20260930"]
        ranker = json.loads((ROOT / entry["ranker"]).read_text(encoding="utf-8"))
        ranker["coefficients"]["priority"] = 999.0
        ranker_path = temp_dir / "ranker.json"
        ranker_path.write_text(json.dumps(ranker), encoding="utf-8")
        entry["ranker"] = str(ranker_path.relative_to(ROOT))
        entry["ranker_file_sha256"] = hashlib.sha256(ranker_path.read_bytes()).hexdigest()
        progress_path = tmp_path / "progress.json"
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        with pytest.raises(
            ValueError, match="external multirotor policy artifact canonical binding"
        ):
            verify(ROOT, progress_path)
    finally:
        shutil.rmtree(temp_dir)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_external_multirotor_depth_evidence_hash_drift(
    tmp_path: Path,
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["external_multirotor_shared_policy_depth_mission_20260930"][
        "evidence_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="external multirotor shared-policy depth Mission SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("topic", "external fixed-wing depth scenario binding"),
        ("scenario", "external fixed-wing depth scenario binding"),
        ("safety", "external fixed-wing shared-policy depth Mission safety binding"),
        ("stamp", "external fixed-wing depth companion binding"),
    ],
)
def test_progress_index_rejects_external_fixed_wing_depth_binding_drift(
    tmp_path: Path, progress_scratch: Path, mutation: str, message: str
) -> None:
    temp_dir = progress_scratch / "fixed-wing"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    try:
        entry = progress["external_fixed_wing_shared_policy_depth_mission_20260930"]
        evidence = json.loads((ROOT / entry["evidence"]).read_text(encoding="utf-8"))
        if mutation == "topic":
            evidence["sensor_transport"]["transport"]["depth_topic"] = "/wrong/topic"
        elif mutation == "scenario":
            evidence["plan"]["vehicle"]["model_name"] = "wrong-model"
        elif mutation == "safety":
            evidence["safety"]["flight_control_commands_sent"] = 99
        else:
            evidence["sensor_transport"]["frames"][0]["depth_source_stamp_ns"] += 1
        evidence_path = temp_dir / "fixed-wing-evidence.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        entry["evidence"] = str(evidence_path.relative_to(ROOT))
        entry["evidence_bytes"] = evidence_path.stat().st_size
        entry["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        progress_path = tmp_path / "progress.json"
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            verify(ROOT, progress_path)
    finally:
        shutil.rmtree(temp_dir)


@pytest.mark.usefixtures("current_progress_evidence")
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("active_model", "external fixed-wing depth Mission provenance binding"),
        ("assets", "external fixed-wing depth Mission provenance binding"),
        ("inspect", "external fixed-wing depth Mission provenance binding"),
        ("sensor_binding", "external fixed-wing depth Mission provenance binding"),
        ("zero_frames", "external fixed-wing depth Mission provenance binding"),
        ("duplicate_frames", "external fixed-wing depth frame coverage drift"),
    ],
)
def test_progress_index_rejects_external_fixed_wing_runtime_identity_drift(
    tmp_path: Path, progress_scratch: Path, mutation: str, message: str
) -> None:
    temp_dir = progress_scratch / "fixed-wing"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    try:
        entry = progress["external_fixed_wing_shared_policy_depth_mission_20260930"]
        evidence = json.loads((ROOT / entry["evidence"]).read_text(encoding="utf-8"))
        sensor = evidence["sensor_transport"]
        if mutation == "active_model":
            sensor["repository_model_assets"]["active_model_name"] = "wrong-model"
        elif mutation == "assets":
            sensor["repository_model_assets"]["assets_sha256"] = "0" * 64
        elif mutation == "inspect":
            evidence["container_runtime"]["inspect_checks"]["running"] = False
        elif mutation == "sensor_binding":
            sensor["sensor_binding_hash"] = "0" * 64
            sensor["transport"]["sensor_binding_sha256"] = "0" * 64
        elif mutation == "zero_frames":
            sensor["requested_frame_count"] = 0
            sensor["transport"]["expected_frame_count"] = 0
            sensor["transport"]["received_frame_count"] = 0
            sensor["transport"]["depth_companion_count"] = 0
            sensor["frames"] = []
            entry["received_sensor_frames"] = 0
            entry["depth_companion_count"] = 0
        else:
            sensor["frames"][1]["sequence"] = sensor["frames"][0]["sequence"]
        evidence_path = temp_dir / "fixed-wing-runtime-identity-evidence.json"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        entry["evidence"] = str(evidence_path.relative_to(ROOT))
        entry["evidence_bytes"] = evidence_path.stat().st_size
        entry["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        progress_path = tmp_path / "progress.json"
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            verify(ROOT, progress_path)
    finally:
        shutil.rmtree(temp_dir)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_external_gazebo_fault_evidence_hash_drift(
    tmp_path: Path,
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["external_gazebo_sensor_fault_contract_20260927_r1"]["external_runs"][2][
        "evidence_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="external Gazebo odometry fault evidence SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_ai_flight_parity_receipt_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["external_docker_ai_flight_parity_receipt_20260927_r2"][
        "receipt_file_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="AI flight parity receipt SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_ai_flight_parity_canonical_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["external_docker_ai_flight_parity_receipt_20260927_r2"][
        "receipt_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="AI flight parity receipt canonical identity"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_field_link_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["evidence"]["field_link_read_only_current_probe"]["file_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="field-link preflight"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_field_link_byte_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["evidence"]["field_link_read_only_current_probe"]["file_bytes"] += 1
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="field-link preflight byte"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_observed_session_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_observed_session_20260924"]["evidence_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="observed session evidence SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_observed_session_completion_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_observed_session_20260924"]["all_tasks_completed"] = False
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="observed session safety/completion"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_binds_current_shared_policy_observed_session() -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    entry = progress["shared_policy_observed_session_20260927_r17"]
    evidence_path = ROOT / entry["evidence"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert entry["evidence_bytes"] == evidence_path.stat().st_size
    assert entry["evidence_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert document["all_tasks_completed"] is True
    assert document["truth_free"] is True
    assert document["session"]["selected_task_ids"] == [
        "synthetic-east",
        "synthetic-west",
    ]


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_current_observed_session_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_observed_session_20260927_r17"]["evidence_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="current observed session evidence file identity"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_campaign_receipt_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_campaign_receipts_20260925"]["parent_receipt_sha256"] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="parent campaign receipt SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_campaign_receipt_parity_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["shared_policy_campaign_receipts_20260925"]["release_parity_bound"] = True
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="campaign receipt release parity"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_positive_replay_accuracy_hash_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    progress["evidence"]["replay_set_positive_fixture_20260924"][
        "accuracy_report_sha256"
    ] = "0" * 64
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="positive replay accuracy report SHA-256"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
@pytest.mark.parametrize("missing_field", ["report_path", "manifest_path"])
def test_progress_index_rejects_missing_release_gate_artifact(
    missing_field: str, progress_scratch: Path
) -> None:
    temp_dir = progress_scratch / "missing"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    try:
        progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
        audit = json.loads(
            (ROOT / progress["evidence"]["release_gate_report"]["audit_summary_path"])
            .read_text(encoding="utf-8")
        )
        audit_path = temp_dir / "audit.json"
        missing_path = (temp_dir / f"missing-{missing_field}.json").relative_to(ROOT).as_posix()
        audit[missing_field] = missing_path
        progress["evidence"]["release_gate_report"]["audit_summary_path"] = str(
            audit_path.relative_to(ROOT)
        ).replace("\\", "/")
        progress["evidence"]["release_gate_report"][
            "manifest_local_path" if missing_field == "manifest_path" else "local_path"
        ] = missing_path
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        progress_path = temp_dir / "progress.json"
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        with pytest.raises(ValueError, match=f"release-gate {missing_field[:-5]}"):
            verify(ROOT, progress_path)
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_field_link_report_binding_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    field = progress["evidence"]["field_link_read_only_current_probe"]
    field["local_path"] = "artifacts/evaluation/field-link-refresh-20260924-r3/preflight.json"
    field["file_bytes"] = 15756
    field["file_sha256"] = "f2ac80228b28b703eaa028d519630d7e4235fed47d500558d46c961165e65d75"
    field["interface_diagnostic_local_path"] = (
        "artifacts/evaluation/field-link-refresh-20260924-r3/interface-diagnostic.json"
    )
    field["interface_diagnostic_file_bytes"] = 4383
    field["interface_diagnostic_file_sha256"] = (
        "1c36a0eef63b79fb9d40d342a2a3c21783fc8871f66d6bf39d701712fc3275a9"
    )
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="release-gate field-link report path"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
@pytest.mark.parametrize("field", ["file_sha256", "file_bytes", "local_path"])
def test_progress_index_rejects_release_gate_report_binding_drift(
    tmp_path: Path, field: str
) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    reference = progress["evidence"]["release_gate_report"]
    if field == "file_sha256":
        reference[field] = "0" * 64
    elif field == "file_bytes":
        reference[field] += 1
    else:
        reference[field] = "artifacts/evaluation/nonexistent-release-report.json"
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="release-gate report"):
        verify(ROOT, path)


@pytest.mark.usefixtures("current_progress_evidence")
def test_progress_index_rejects_release_gate_parity_binding_drift(tmp_path: Path) -> None:
    progress = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
    reference = progress["evidence"]["release_gate_report"]
    reference["current_release_parity_bound"] = not reference["current_release_parity_bound"]
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    with pytest.raises(ValueError, match="release-gate progress parity"):
        verify(ROOT, path)
