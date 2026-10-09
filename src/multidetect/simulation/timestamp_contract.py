"""Explicit timestamp provenance for receive-only MAVLink captures."""

from __future__ import annotations

from collections.abc import Mapping

CAPTURE_CLOCK = "observer_relative_host_receive_time"
SOURCE_BOOT_DOMAIN = "autopilot_boot_ms"
HOST_RECEIPT_DOMAIN = "observer_capture_clock"


def build_timestamp_contract() -> dict:
    """Return the immutable provenance declaration embedded in new captures."""

    def stream(source_field: str | None, source_domain: str | None) -> dict:
        return {
            "source_timestamp_field": source_field,
            "source_timestamp_domain": source_domain,
            "source_timestamp_is_publication_time": False,
            "source_timestamp_is_physical_sample_time": False,
            "host_receipt_timestamp_field": "received_at_s",
            "host_receipt_timestamp_domain": HOST_RECEIPT_DOMAIN,
            "publication_timestamp_available": False,
            "source_to_host_clock_mapping_proven": False,
        }

    return {
        "schema_version": 1,
        "capture_clock": CAPTURE_CLOCK,
        "capture_clock_origin": "capture_session_origin",
        "streams": {
            "ATTITUDE": stream("time_boot_ms", SOURCE_BOOT_DOMAIN),
            "ATTITUDE_TARGET": stream("time_boot_ms", SOURCE_BOOT_DOMAIN),
            "VFR_HUD": stream(None, None),
            "NAV_CONTROLLER_OUTPUT": stream(None, None),
        },
        "physical_synchrony_proven": False,
        "publication_time_semantics_proven": False,
    }


def validate_timestamp_contract(value: object) -> dict:
    """Validate a timestamp contract and return a detached copy.

    The contract intentionally rejects claims of publication or physical sample
    timing unless a future schema adds an explicit source field and evidence.
    """

    if not isinstance(value, Mapping):
        raise ValueError("timestamp contract must be an object")
    expected = build_timestamp_contract()
    if value.get("schema_version") != expected["schema_version"]:
        raise ValueError("unsupported timestamp contract schema")
    if value.get("capture_clock") != CAPTURE_CLOCK:
        raise ValueError("timestamp contract capture clock mismatch")
    if value.get("capture_clock_origin") != "capture_session_origin":
        raise ValueError("timestamp contract origin mismatch")
    streams = value.get("streams")
    if not isinstance(streams, Mapping) or set(streams) != set(expected["streams"]):
        raise ValueError("timestamp contract stream set mismatch")
    for name, expected_stream in expected["streams"].items():
        actual = streams[name]
        if not isinstance(actual, Mapping) or dict(actual) != expected_stream:
            raise ValueError(f"timestamp contract mismatch for {name}")
    if value.get("physical_synchrony_proven") is not False:
        raise ValueError("physical synchrony cannot be asserted by this capture")
    if value.get("publication_time_semantics_proven") is not False:
        raise ValueError("publication time cannot be asserted by this capture")
    return {key: value[key] for key in value}


def capture_timestamp_contract(capture: Mapping) -> dict:
    """Validate embedded metadata; report legacy captures explicitly."""

    contract = capture.get("timestamp_contract")
    if contract is None:
        return {
            "status": "legacy_capture_missing_explicit_timestamp_contract",
            "contract_present": False,
            "physical_synchrony_proven": False,
            "publication_time_semantics_proven": False,
        }
    validate_timestamp_contract(contract)
    return {
        "status": "explicit_receive_only_timestamp_contract",
        "contract_present": True,
        "physical_synchrony_proven": False,
        "publication_time_semantics_proven": False,
    }
