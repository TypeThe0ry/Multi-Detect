from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.domain import PayloadState
from multidetect.payload_inventory import (
    ConfiguredSimulationPayloadInventoryProvider,
    FailClosedPayloadInventoryProvider,
    FilePayloadInventoryProvider,
    ObservedPayloadSlot,
    PayloadInventorySnapshot,
    load_payload_inventory_snapshot,
    sign_payload_inventory_document,
    verify_payload_inventory,
)

ROOT = Path(__file__).resolve().parents[1]


def test_configured_simulation_inventory_matches_deployment_config() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    snapshot = ConfiguredSimulationPayloadInventoryProvider(config).snapshot(now_s=10.0)

    verification = verify_payload_inventory(config, snapshot, now_s=10.0)

    assert verification.allowed is True
    assert verification.simulation_only is True


def test_unknown_live_inventory_fails_closed() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    snapshot = FailClosedPayloadInventoryProvider().snapshot(now_s=10.0)

    verification = verify_payload_inventory(config, snapshot, now_s=10.0)

    assert verification.allowed is False
    assert "inventory is unknown" in " ".join(verification.reasons)


def test_inventory_rejects_missing_or_unlocked_slot() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    snapshot = PayloadInventorySnapshot(
        observed_at_s=10.0,
        source_id="hil-test",
        controller_healthy=True,
        installed_slots=(
            ObservedPayloadSlot(
                slot_id="payload-1",
                payload_type="fire_suppression_agent",
                state=PayloadState.ARMED,
            ),
        ),
        simulation_only=True,
    )

    verification = verify_payload_inventory(config, snapshot, now_s=10.0)

    assert verification.allowed is False
    assert any("do not match" in reason for reason in verification.reasons)
    assert any("not locked" in reason for reason in verification.reasons)


def test_json_hil_inventory_report_matches_configuration() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    snapshot = load_payload_inventory_snapshot(ROOT / "examples/payload_inventory.demo.json")

    verification = verify_payload_inventory(config, snapshot, now_s=1000.5)

    assert verification.allowed is True
    assert verification.source_id == "payload-hil-demo"


def test_inventory_rejects_unhealthy_presence_sensor() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    snapshot = load_payload_inventory_snapshot(ROOT / "examples/payload_inventory.demo.json")
    slots = list(snapshot.installed_slots or ())
    slots[0] = ObservedPayloadSlot(
        slot_id=slots[0].slot_id,
        payload_type=slots[0].payload_type,
        state=slots[0].state,
        present=True,
        presence_sensor_healthy=False,
    )
    snapshot = PayloadInventorySnapshot(
        observed_at_s=snapshot.observed_at_s,
        source_id=snapshot.source_id,
        controller_healthy=snapshot.controller_healthy,
        installed_slots=tuple(slots),
        simulation_only=snapshot.simulation_only,
        protocol_version=snapshot.protocol_version,
        module_id=snapshot.module_id,
        interlock_healthy=snapshot.interlock_healthy,
    )

    verification = verify_payload_inventory(config, snapshot, now_s=1000.5)

    assert verification.allowed is False
    assert any("presence sensor" in reason for reason in verification.reasons)


def _signed_report(tmp_path: Path, *, sequence: int = 2) -> tuple[Path, bytes]:
    document = json.loads(
        (ROOT / "examples/payload_inventory.demo.json").read_text(encoding="utf-8")
    )
    key = b"unit-test-payload-inventory-key"
    document["simulation_only"] = False
    document["sequence"] = sequence
    document["key_id"] = "payload-key-v1"
    document["signature_hmac_sha256"] = sign_payload_inventory_document(
        document,
        hmac_key=key,
    )
    path = tmp_path / "payload-inventory.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, key


def test_authenticated_file_provider_accepts_signed_real_report(tmp_path: Path) -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    path, key = _signed_report(tmp_path)
    provider = FilePayloadInventoryProvider(
        path,
        hmac_key=key,
        expected_key_id="payload-key-v1",
    )

    snapshot = provider.snapshot(now_s=1000.5)
    verification = verify_payload_inventory(config, snapshot, now_s=1000.5)

    assert snapshot.authenticated is True
    assert verification.allowed is True


def test_authenticated_file_provider_rejects_tampering(tmp_path: Path) -> None:
    path, key = _signed_report(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["controller_healthy"] = False
    path.write_text(json.dumps(document), encoding="utf-8")
    provider = FilePayloadInventoryProvider(path, hmac_key=key)

    with pytest.raises(ValueError, match="HMAC verification failed"):
        provider.snapshot(now_s=1000.5)


def test_authenticated_file_provider_rejects_sequence_rollback(tmp_path: Path) -> None:
    path, key = _signed_report(tmp_path, sequence=2)
    provider = FilePayloadInventoryProvider(path, hmac_key=key)
    provider.snapshot(now_s=1000.5)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["sequence"] = 1
    document["signature_hmac_sha256"] = sign_payload_inventory_document(
        document,
        hmac_key=key,
    )
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="moved backwards"):
        provider.snapshot(now_s=1000.6)


def test_authenticated_file_provider_rejects_changed_content_at_same_sequence(
    tmp_path: Path,
) -> None:
    path, key = _signed_report(tmp_path, sequence=2)
    provider = FilePayloadInventoryProvider(path, hmac_key=key)
    provider.snapshot(now_s=1000.5)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["controller_healthy"] = False
    document["signature_hmac_sha256"] = sign_payload_inventory_document(
        document,
        hmac_key=key,
    )
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="without a new sequence"):
        provider.snapshot(now_s=1000.6)


def test_payload_inventory_provider_rejects_empty_authentication_key(tmp_path: Path) -> None:
    path, _key = _signed_report(tmp_path)

    with pytest.raises(ValueError, match="cannot be empty"):
        FilePayloadInventoryProvider(path, hmac_key=b"")


def test_inventory_rejects_nonfinite_evaluation_time() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    snapshot = ConfiguredSimulationPayloadInventoryProvider(config).snapshot(now_s=10.0)

    with pytest.raises(ValueError, match="finite"):
        verify_payload_inventory(config, snapshot, now_s=float("nan"))
