import copy

import pytest

from multidetect.simulation.timestamp_contract import (
    build_timestamp_contract,
    capture_timestamp_contract,
    validate_timestamp_contract,
)
from scripts.mission_dynamics_capture import MissionDynamicsCapture


def test_new_capture_embeds_explicit_receive_only_contract():
    result = MissionDynamicsCapture().snapshot()
    status = capture_timestamp_contract(result)
    assert status["status"] == "explicit_receive_only_timestamp_contract"
    assert result["timestamp_contract"]["streams"]["ATTITUDE"]["source_timestamp_field"] == (
        "time_boot_ms"
    )
    assert result["timestamp_contract"]["streams"]["VFR_HUD"]["source_timestamp_field"] is None
    assert status["physical_synchrony_proven"] is False


def test_legacy_capture_is_explicitly_reported_not_promoted():
    legacy = MissionDynamicsCapture().snapshot()
    del legacy["timestamp_contract"]
    status = capture_timestamp_contract(legacy)
    assert status["status"] == "legacy_capture_missing_explicit_timestamp_contract"
    assert status["contract_present"] is False


@pytest.mark.parametrize("mutation", ["stream", "publication", "physical", "clock"])
def test_contract_rejects_unproven_timestamp_claims(mutation):
    contract = build_timestamp_contract()
    if mutation == "stream":
        contract["streams"]["ATTITUDE"]["source_timestamp_field"] = "received_at_s"
    elif mutation == "publication":
        contract["streams"]["ATTITUDE"]["publication_timestamp_available"] = True
    elif mutation == "physical":
        contract["physical_synchrony_proven"] = True
    else:
        contract["capture_clock"] = "source_boot_ms"
    with pytest.raises(ValueError):
        validate_timestamp_contract(contract)


def test_validation_does_not_mutate_contract():
    contract = build_timestamp_contract()
    original = copy.deepcopy(contract)
    validate_timestamp_contract(contract)
    assert contract == original
