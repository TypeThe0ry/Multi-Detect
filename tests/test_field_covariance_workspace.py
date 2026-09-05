from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect import field_covariance_campaign as campaign
from multidetect import field_covariance_workspace as workspace
from multidetect.compat import UTC
from multidetect.field_covariance_campaign import REQUIRED_COMBINATIONS, load_campaign_config


def _write_config(tmp_path: Path) -> Path:
    entries: list[dict[str, object]] = []
    for index, (mode, vehicle) in enumerate(REQUIRED_COMBINATIONS):
        source = tmp_path / "original" / mode / vehicle
        source.mkdir(parents=True)
        mission = source / "mission.json"
        feedback = source / "feedback.json"
        profile = source / "profile.json"
        mission.write_text(json.dumps({"kind": "mission", "leaf": index}), encoding="utf-8")
        feedback.write_text(json.dumps({"kind": "feedback", "leaf": index}), encoding="utf-8")
        profile.write_text(json.dumps({"kind": "profile", "leaf": index}), encoding="utf-8")
        entries.append(
            {
                "runtime_mode": mode,
                "vehicle_class": vehicle,
                "endpoint": f"serial:COM{index + 3}",
                "baud": 57600,
                "mission_path": str(mission.relative_to(tmp_path)),
                "mission_feedback_path": str(feedback.relative_to(tmp_path)),
                "runtime_profile_path": str(profile.relative_to(tmp_path)),
                "report_path": f"original-reports/{mode}/{vehicle}.json",
            }
        )
    config = tmp_path / "source-campaign.json"
    config.write_text(
        json.dumps(
            {
                "schema": "multidetect.pixhawk-covariance-field-campaign/v1",
                "schema_version": 1,
                "release_label": "0.3.0-pre.8",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return config


def _install_source_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    class Mission:
        mission_id = "portable-field"
        canonical_hash = "1" * 64
        airframe_profile_hash = "2" * 64

        def __init__(self, path: Path) -> None:
            self.mode = path.parent.parent.name
            self.vehicle = path.parent.name
            self.airframe_adapter_id, self.airframe_adapter_hash = (
                campaign._ADAPTER_BY_VEHICLE_CLASS[self.vehicle]
            )

    def load_mission(path: Path) -> Mission:
        return Mission(path)

    def load_feedback(
        path: Path, *, expected_mission: Mission, runtime_profile_path: Path
    ) -> dict[str, object]:
        assert path.parent == runtime_profile_path.parent
        assert expected_mission.mode == path.parent.parent.name
        assert expected_mission.vehicle == path.parent.name
        return {
            "runtime_mode": expected_mission.mode,
            "feedback_gate_passed": True,
            "transport": {
                "configured_endpoint": None,
                "baud": 57600,
                "read_only": True,
                "messages_transmitted": 0,
            },
            "runtime_binding": {
                "runtime_mode": expected_mission.mode,
                "gate_passed": True,
                "signature_valid": True,
                "profile_valid": True,
                "flight_control_enabled": False,
                "output_locked": True,
                "profile": {"profile_id": "hil" if expected_mission.mode == "hil" else "flight"},
                "observed_identity": {},
                "key_id": "field-key",
                "binding_sha256": "6" * 64,
            },
        }

    # Fill the endpoint from the expected campaign leaf at call time so the
    # same parser exercises both the original and relocated directory roots.
    def bound_feedback(
        path: Path, *, expected_mission: Mission, runtime_profile_path: Path
    ) -> dict[str, object]:
        result = load_feedback(
            path,
            expected_mission=expected_mission,
            runtime_profile_path=runtime_profile_path,
        )
        index = REQUIRED_COMBINATIONS.index((expected_mission.mode, expected_mission.vehicle))
        result["transport"]["configured_endpoint"] = f"serial:COM{index + 3}"
        return result

    monkeypatch.setattr(campaign, "load_executable_mission_document", load_mission)
    monkeypatch.setattr(campaign, "load_px4_mission_feedback_observation", bound_feedback)


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _install_source_parsers(monkeypatch)
    config = load_campaign_config(_write_config(tmp_path))
    return workspace.export_field_covariance_workspace(
        config,
        tmp_path / "portable",
        created_at=datetime(2026, 9, 6, 7, 0, tzinfo=UTC),
    )


def test_schema_is_closed() -> None:
    schema = json.loads(workspace.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["entry"]["additionalProperties"] is False
    assert schema["$defs"]["file"]["additionalProperties"] is False


def test_export_is_portable_and_verifies_after_root_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(tmp_path, monkeypatch)
    manifest = json.loads((exported / workspace.MANIFEST_NAME).read_text(encoding="utf-8"))
    campaign = json.loads((exported / workspace.CAMPAIGN_NAME).read_text(encoding="utf-8"))
    original_hash = manifest["workspace_sha256"]
    assert len(list(exported.rglob("*.json"))) == 20
    assert len(manifest["entries"]) == 6
    assert all(not Path(entry["mission_path"]).is_absolute() for entry in campaign["entries"])
    assert all("original" not in entry["mission_path"] for entry in campaign["entries"])
    assert manifest["claims"] == {
        "live_identity_observed": False,
        "covariance_observed": False,
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "flight_acceptance": False,
    }

    moved = tmp_path / "second-machine" / "campaign-workspace"
    moved.parent.mkdir()
    shutil.copytree(exported, moved)
    shutil.rmtree(exported)
    result = workspace.verify_field_covariance_workspace(moved)

    assert result["gate_passed"] is True
    assert result["leaf_count"] == 6
    assert result["source_file_count"] == 18
    assert result["workspace_sha256"] == original_hash
    assert result["campaign_path"] == str((moved / workspace.CAMPAIGN_NAME).resolve())
    assert "field_covariance_campaign preflight" in result["next_command"]


def test_verifier_rejects_source_tamper_and_unexpected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(tmp_path, monkeypatch)
    mission = next((exported / "sources").rglob("mission.json"))
    original = mission.read_bytes()
    mission.write_bytes(original + b"\n")
    with pytest.raises(workspace.FieldCovarianceWorkspaceError, match="hash or size"):
        workspace.verify_field_covariance_workspace(exported)
    mission.write_bytes(original)

    unexpected = exported / "unbound.txt"
    unexpected.write_text("not bound", encoding="utf-8")
    with pytest.raises(workspace.FieldCovarianceWorkspaceError, match="file set mismatch"):
        workspace.verify_field_covariance_workspace(exported)


def test_verifier_rejects_manifest_tamper_even_with_recomputed_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(tmp_path, monkeypatch)
    path = exported / workspace.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))

    changed = copy.deepcopy(manifest)
    changed["entries"][0]["endpoint"] = "serial:COM99"
    changed.pop("workspace_sha256")
    changed["workspace_sha256"] = workspace._canonical_sha256(changed)
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(workspace.FieldCovarianceWorkspaceError, match="transport mismatch"):
        workspace.verify_field_covariance_workspace(exported)


def test_verifier_rejects_noncanonical_or_escaping_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(tmp_path, monkeypatch)
    path = exported / workspace.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))

    for invalid in ("../mission.json", "sources//mission.json", "sources/a:stream"):
        changed = copy.deepcopy(manifest)
        changed["entries"][0]["sources"]["mission"]["path"] = invalid
        changed.pop("workspace_sha256")
        changed["workspace_sha256"] = workspace._canonical_sha256(changed)
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(workspace.FieldCovarianceWorkspaceError):
            workspace.verify_field_covariance_workspace(exported)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_verifier_rejects_current_parity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = _export(tmp_path, monkeypatch)
    release, px4_hash, parity_hash = workspace._current_parity()
    monkeypatch.setattr(
        workspace,
        "_current_parity",
        lambda: (release, "f" * 64 if px4_hash != "f" * 64 else "e" * 64, parity_hash),
    )
    with pytest.raises(workspace.FieldCovarianceWorkspaceError, match="parity"):
        workspace.verify_field_covariance_workspace(exported)


def test_export_fails_before_writing_when_source_preflight_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_campaign_config(_write_config(tmp_path))
    monkeypatch.setattr(
        workspace,
        "campaign_preflight",
        lambda config: {"gate_passed": False},
    )
    destination = tmp_path / "blocked"
    with pytest.raises(workspace.FieldCovarianceWorkspaceError, match="preflight blocked"):
        workspace.export_field_covariance_workspace(config, destination)
    assert not destination.exists()


def test_verifier_rejects_symbolic_workspace_root_without_os_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "linked-workspace"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self == root or original_is_symlink(self),
    )
    with pytest.raises(workspace.FieldCovarianceWorkspaceError, match="symbolic link"):
        workspace.verify_field_covariance_workspace(root)


def test_cli_export_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    _install_source_parsers(monkeypatch)
    config = _write_config(tmp_path)
    exported = tmp_path / "cli-portable"

    assert workspace.main(["export", "--config", str(config), "--out", str(exported)]) == 0
    export_result = json.loads(capsys.readouterr().out)
    assert export_result["gate_passed"] is True
    assert workspace.main(["verify", "--bundle", str(exported)]) == 0
    verify_result = json.loads(capsys.readouterr().out)
    assert verify_result["workspace_sha256"] == export_result["workspace_sha256"]
