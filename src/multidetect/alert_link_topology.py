"""Strict, hash-bound selection of the physical alert-link topology."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .autonomy.serialization import canonical_sha256, strict_json_loads

ALERT_LINK_TOPOLOGY_SCHEMA_VERSION = 1
TOPOLOGY_MODES = frozenset({"gr01_direct", "v6x_fallback"})
_EVIDENCE_KEYS = (
    "gr01_direct_failure",
    "px4_sitl_routing",
    "v6x_actuator_disconnected_bench",
)


@dataclass(frozen=True, slots=True)
class AlertLinkTopology:
    topology_id: str
    mode: str
    remote_host: str
    remote_port: int
    expected_source_cidr: str
    sender_id: str
    receiver_id: str
    hardware_id: str
    fallback_evidence: dict[str, dict[str, str]] | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": ALERT_LINK_TOPOLOGY_SCHEMA_VERSION,
            "topology_id": self.topology_id,
            "mode": self.mode,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "expected_source_cidr": self.expected_source_cidr,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "hardware_id": self.hardware_id,
            "fallback_evidence": self.fallback_evidence,
            "advisory_only": True,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
            "payload_release_requested": False,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


def _text(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"alert-link topology {name} must be a trimmed non-empty string")
    return value


def _evidence_reference(
    value: object, *, name: str, base_dir: Path, verify_files: bool
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"alert-link topology {name} must contain only path and sha256")
    path_text = _text(value, "path")
    expected_sha256 = _text(value, "sha256").lower()
    if len(expected_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha256):
        raise ValueError(f"alert-link topology {name} sha256 is invalid")
    candidate = Path(path_text)
    if candidate.is_absolute():
        raise ValueError(f"alert-link topology {name} path must be relative")
    resolved_base = base_dir.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved != resolved_base and resolved_base not in resolved.parents:
        raise ValueError(f"alert-link topology {name} path escapes the topology directory")
    if verify_files:
        try:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f"alert-link topology {name} evidence is unreadable: {exc}") from exc
        if digest != expected_sha256:
            raise ValueError(f"alert-link topology {name} evidence hash does not match")
    return {"path": candidate.as_posix(), "sha256": expected_sha256}


def parse_alert_link_topology(
    document: object, *, base_dir: Path, verify_evidence_files: bool = True
) -> AlertLinkTopology:
    if not isinstance(document, dict):
        raise ValueError("alert-link topology must be an object")
    expected_keys = {
        "schema_version",
        "topology_id",
        "mode",
        "remote_host",
        "remote_port",
        "expected_source_cidr",
        "sender_id",
        "receiver_id",
        "hardware_id",
        "fallback_evidence",
        "advisory_only",
        "flight_control_enabled",
        "physical_release_enabled",
        "payload_release_requested",
    }
    if set(document) != expected_keys:
        raise ValueError("alert-link topology fields do not match schema version 1")
    if document.get("schema_version") != ALERT_LINK_TOPOLOGY_SCHEMA_VERSION:
        raise ValueError("alert-link topology schema_version must be 1")
    mode = _text(document, "mode")
    if mode not in TOPOLOGY_MODES:
        raise ValueError("alert-link topology mode is unsupported")
    try:
        remote = ipaddress.ip_address(_text(document, "remote_host"))
        network = ipaddress.ip_network(_text(document, "expected_source_cidr"), strict=True)
    except ValueError as exc:
        raise ValueError("alert-link topology addresses must use canonical IP notation") from exc
    if remote.version != 4 or network.version != 4 or remote.is_loopback or network.is_loopback:
        raise ValueError("alert-link topology requires non-loopback IPv4 addresses")
    if str(remote) != document["remote_host"] or str(network) != document["expected_source_cidr"]:
        raise ValueError("alert-link topology addresses must use canonical IP notation")
    if mode == "gr01_direct" and remote not in network:
        raise ValueError("GR01 direct endpoint must belong to expected_source_cidr")
    port = document.get("remote_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("alert-link topology remote_port must be in [1, 65535]")
    for name, expected in (
        ("advisory_only", True),
        ("flight_control_enabled", False),
        ("physical_release_enabled", False),
        ("payload_release_requested", False),
    ):
        if document.get(name) is not expected:
            raise ValueError(f"alert-link topology {name} must be {str(expected).lower()}")
    raw_fallback = document.get("fallback_evidence")
    fallback: dict[str, dict[str, str]] | None
    if mode == "gr01_direct":
        if raw_fallback is not None:
            raise ValueError("GR01 direct topology cannot carry V6X fallback evidence")
        fallback = None
    else:
        if not isinstance(raw_fallback, dict) or set(raw_fallback) != set(_EVIDENCE_KEYS):
            raise ValueError("V6X fallback requires all three prerequisite evidence references")
        fallback = {
            name: _evidence_reference(
                raw_fallback[name],
                name=name,
                base_dir=base_dir,
                verify_files=verify_evidence_files,
            )
            for name in _EVIDENCE_KEYS
        }
    return AlertLinkTopology(
        topology_id=_text(document, "topology_id"),
        mode=mode,
        remote_host=str(remote),
        remote_port=port,
        expected_source_cidr=str(network),
        sender_id=_text(document, "sender_id"),
        receiver_id=_text(document, "receiver_id"),
        hardware_id=_text(document, "hardware_id"),
        fallback_evidence=fallback,
    )


def load_alert_link_topology(path: Path) -> AlertLinkTopology:
    try:
        document = strict_json_loads(path.read_bytes(), name="alert-link topology")
    except OSError as exc:
        raise ValueError(f"unable to read alert-link topology: {exc}") from exc
    return parse_alert_link_topology(document, base_dir=path.parent)


__all__ = [
    "ALERT_LINK_TOPOLOGY_SCHEMA_VERSION",
    "AlertLinkTopology",
    "load_alert_link_topology",
    "parse_alert_link_topology",
]
