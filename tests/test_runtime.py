from __future__ import annotations

import json

import pytest

from multidetect.autonomy import AutonomyContractError
from multidetect.runtime import (
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeEndpointClass,
    RuntimeMode,
    RuntimeProfile,
    RuntimeProfileId,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def test_runtime_mode_is_the_shared_four_value_contract() -> None:
    assert tuple(mode.value for mode in RuntimeMode) == ("replay", "sim", "hil", "real")


def test_runtime_descriptor_round_trip_and_hash_are_deterministic() -> None:
    descriptor = RuntimeDescriptor.from_mapping(
        {
            "schema_version": 1,
            "mode": "sim",
            "profile": RuntimeProfile.sim().to_mapping(),
            "backend_id": " px4-gazebo:river ",
            "airframe_profile_hash": HASH_A.upper(),
            "scenario_id": "river_rescue_vtol_001",
            "scenario_manifest_hash": HASH_B.upper(),
            "scenario_seed": 42017,
            "deterministic": True,
            "fault_injection_enabled": True,
        }
    )
    reordered = dict(reversed(tuple(descriptor.to_mapping().items())))

    assert descriptor.mode is RuntimeMode.SIM
    assert descriptor.backend_id == "px4-gazebo:river"
    assert descriptor.airframe_profile_hash == HASH_A
    assert descriptor.scenario_manifest_hash == HASH_B
    assert descriptor.is_simulated is True
    assert RuntimeDescriptor.from_mapping(descriptor.to_mapping()) == descriptor
    assert descriptor.canonical_hash == RuntimeDescriptor.from_mapping(reordered).canonical_hash
    assert len(descriptor.provenance_hash) == 64
    assert json.loads(descriptor.canonical_json)["mode"] == "sim"


def test_runtime_descriptor_rejects_unknown_fields() -> None:
    mapping = {
        "schema_version": 1,
        "mode": "sim",
        "profile": RuntimeProfile.sim().to_mapping(),
        "backend_id": "sim",
        "airframe_profile_hash": HASH_A,
        "scenario_id": "s",
        "scenario_manifest_hash": HASH_B,
        "scenario_seed": 1,
        "deterministic": False,
        "fault_injection_enabled": False,
        "skip_safety": True,
    }
    with pytest.raises(AutonomyContractError, match="unknown field"):
        RuntimeDescriptor.from_mapping(mapping)


def test_profiles_have_exact_non_escalatable_capabilities_and_endpoints() -> None:
    assert RuntimeProfile.observer().capabilities == (RuntimeCapability.OBSERVE,)
    assert RuntimeProfile.replay().endpoint_class is RuntimeEndpointClass.RECORDING
    assert RuntimeProfile.sim().profile_id is RuntimeProfileId.SIM
    with pytest.raises(AutonomyContractError, match="exactly"):
        RuntimeProfile(
            RuntimeProfileId.SIM,
            (RuntimeCapability.OBSERVE, RuntimeCapability.FLIGHT_CONTROL_OUTPUT),
            RuntimeEndpointClass.SIMULATOR,
        )


def test_real_flight_profile_requires_all_privileged_bindings() -> None:
    with pytest.raises(AutonomyContractError, match="requires vehicle"):
        RuntimeProfile(
            RuntimeProfileId.FLIGHT,
            (RuntimeCapability.OBSERVE, RuntimeCapability.FLIGHT_CONTROL_OUTPUT),
            RuntimeEndpointClass.REAL_VEHICLE,
        )
    profile = RuntimeProfile.flight(
        vehicle_identity_hash=HASH_A,
        airframe_profile_hash=HASH_B,
        signing_identity_hash=HASH_C,
        endpoint_binding_hash=HASH_D,
    )
    descriptor = RuntimeDescriptor(
        mode=RuntimeMode.REAL,
        profile=profile,
        backend_id="vehicle:1",
        airframe_profile_hash=HASH_B,
    )
    assert descriptor.profile.profile_id is RuntimeProfileId.FLIGHT


def test_mode_provenance_requirements_fail_closed() -> None:
    with pytest.raises(AutonomyContractError, match="requires recording_hash"):
        RuntimeDescriptor(
            mode=RuntimeMode.REPLAY,
            profile=RuntimeProfile.replay(),
            backend_id="replay",
            airframe_profile_hash=HASH_A,
            deterministic=True,
        )
    with pytest.raises(AutonomyContractError, match="requires scenario"):
        RuntimeDescriptor(
            mode=RuntimeMode.SIM,
            profile=RuntimeProfile.sim(),
            backend_id="sim",
            airframe_profile_hash=HASH_A,
        )
    with pytest.raises(AutonomyContractError, match="recording provenance"):
        RuntimeDescriptor(
            mode=RuntimeMode.REAL,
            profile=RuntimeProfile.observer(),
            backend_id="observer",
            airframe_profile_hash=HASH_A,
            recording_hash=HASH_B,
        )


def test_profile_mode_mismatch_is_rejected() -> None:
    with pytest.raises(AutonomyContractError, match="cannot run"):
        RuntimeDescriptor(
            mode=RuntimeMode.REAL,
            profile=RuntimeProfile.sim(),
            backend_id="invalid",
            airframe_profile_hash=HASH_A,
        )
