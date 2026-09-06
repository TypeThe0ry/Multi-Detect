from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.alert_link_topology import load_alert_link_topology, parse_alert_link_topology

ROOT = Path(__file__).resolve().parents[1]


def _direct() -> dict[str, object]:
    return {
        "schema_version": 1,
        "topology_id": "gr01-aircraft-a-to-g20-a",
        "mode": "gr01_direct",
        "remote_host": "192.168.144.20",
        "remote_port": 14600,
        "expected_source_cidr": "192.168.144.0/24",
        "sender_id": "aircraft-1",
        "receiver_id": "ground-station-1",
        "hardware_id": "GR01-ALERT-001",
        "fallback_evidence": None,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "payload_release_requested": False,
    }


def test_direct_topology_is_canonical_hashable_and_matches_schema(tmp_path: Path) -> None:
    document = _direct()
    schema = json.loads(
        (ROOT / "configs/schemas/alert-link-topology.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(document)
    topology = parse_alert_link_topology(document, base_dir=tmp_path)
    assert topology.mode == "gr01_direct"
    assert topology.sha256 == topology.sha256
    assert len(topology.sha256) == 64


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("remote_host", "127.0.0.1", "non-loopback"),
        ("remote_host", "192.168.0.3", "belong"),
        ("expected_source_cidr", "192.168.144.1/24", "canonical"),
        ("flight_control_enabled", True, "must be false"),
    ],
)
def test_direct_topology_rejects_unsafe_or_ambiguous_values(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    document = _direct()
    document[field] = value
    with pytest.raises(ValueError, match=match):
        parse_alert_link_topology(document, base_dir=tmp_path)


def test_v6x_fallback_requires_and_hash_verifies_all_prerequisites(tmp_path: Path) -> None:
    refs: dict[str, dict[str, str]] = {}
    for name in (
        "gr01_direct_failure",
        "px4_sitl_routing",
        "v6x_actuator_disconnected_bench",
    ):
        path = tmp_path / f"{name}.json"
        payload = (name + "\n").encode()
        path.write_bytes(payload)
        refs[name] = {"path": path.name, "sha256": hashlib.sha256(payload).hexdigest()}
    document = _direct()
    document.update(
        mode="v6x_fallback",
        topology_id="v6x-fallback-aircraft-a",
        remote_host="192.168.0.3",
        expected_source_cidr="192.168.0.0/24",
        fallback_evidence=refs,
    )
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    topology = load_alert_link_topology(path)
    assert topology.mode == "v6x_fallback"
    assert topology.fallback_evidence == refs

    (tmp_path / "px4_sitl_routing.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash does not match"):
        load_alert_link_topology(path)


def test_v6x_fallback_rejects_missing_prerequisite(tmp_path: Path) -> None:
    document = _direct()
    document["mode"] = "v6x_fallback"
    document["fallback_evidence"] = {}
    with pytest.raises(ValueError, match="all three"):
        parse_alert_link_topology(document, base_dir=tmp_path)
