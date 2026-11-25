from pathlib import Path
import importlib.util
import json
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts/realworld/x1_site_battery.py"
SPEC = importlib.util.spec_from_file_location("x1_site_battery", SOURCE)
battery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(battery)


def sample(**changes):
    return {"message_type": "BATTERY_STATUS", "_source_system": 1, "_source_component": 1,
            "id": 0, "current_battery": -1, "current_consumed": 0, "energy_consumed": -1,
            "battery_remaining": 17, "voltages": [14706], **changes}


def test_audit_filters_peer_and_pack_and_keeps_counter_sentinels(tmp_path):
    path = tmp_path / "mavlink.jsonl"
    rows = [sample(), sample(battery_remaining=19), sample(_source_component=2, energy_consumed=1),
            sample(id=1, current_consumed=7), dict(sample(), message_type="PARAM_VALUE",
                                                  param_id="SIM_BAT_DRAIN", param_value=1.0)]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    audit = battery.audit_telemetry(path, empty_mv=14400, full_mv=16200)
    assert audit["sample_count"] == 2
    assert audit["samples_with_counter"] == 0
    assert audit["counter_groups"] == [{"current_battery_ca": -1, "current_consumed_mah": 0,
                                        "energy_consumed_hj": -1, "samples": 2}]
    assert audit["percent_range"] == [17, 19]
    assert audit["observed_max_voltage_residual_mv"] == 36
    assert audit["param_value_readbacks"] == {"SIM_BAT_DRAIN": [1.0]}
    assert audit["mavlink"] == battery.provenance(path)


@pytest.mark.parametrize(("hj", "mah", "expected"), [
    (-1, 0, False), (0, 0, True), (-1, 1, True), (True, 0, False), (-1, True, False),
    (2**31, 2**31, False), (0.0, 1.0, False),
])
def test_counter_contract(hj, mah, expected):
    assert battery.valid_consumption(hj, mah) is expected


def test_missing_target_samples_fail_closed(tmp_path):
    path = tmp_path / "mavlink.jsonl"
    path.write_text(json.dumps(sample(_source_system=2)))
    with pytest.raises(ValueError, match="no target pack samples"):
        battery.audit_telemetry(path, empty_mv=14400, full_mv=16200)


def test_wrong_px4_metadata_fails_closed(tmp_path):
    path = tmp_path / "unbound.json.xz"
    path.write_bytes(b"unverified")
    with pytest.raises(ValueError, match="does not match"):
        battery.verified_input(path, battery.METADATA_SHA256, "PX4 parameter metadata")


def test_nominal_capacity_and_counter_cannot_publish_uncalibrated_bounds(tmp_path, monkeypatch):
    source = tmp_path / "asset"
    source.write_bytes(b"fixture")
    airframes = tmp_path / "simulation/px4/airframes"
    airframes.mkdir(parents=True)
    for name in ("4050_gz_delta_tailsitter", "4051_gz_delta_tailsitter_gimbal"):
        (airframes / name).write_bytes(b"fixture")
    for name in ("DEM_SHA256", "SCENE_SHA256", "PX4_ELF_SHA256", "METADATA_SHA256"):
        monkeypatch.setattr(battery, name, battery.provenance(source)["sha256"])
    monkeypatch.setattr(battery, "configured_parameters", lambda *_: (
        {"BAT1_V_EMPTY": 3.6, "BAT1_V_CHARGED": 4.05}, {"BAT1_N_CELLS": 4, "BAT1_CAPACITY": 5000},
    ))
    monkeypatch.setattr(battery, "audit_run", lambda *_args, **_kwargs: {"samples_with_counter": 1})
    args = SimpleNamespace(reference=tmp_path, archive=tmp_path, px4_metadata=source, px4_elf=source,
                           world=source, dem=source, scene=source)
    audit = battery.build_audit(args)
    assert audit["configured_defaults"]["BAT1_CAPACITY"] == 5000
    assert audit["synthetic_voltage_endpoints_v"] == [14.4, 16.2]
    assert audit["eligible_bounds_fields"] == []
    assert "battery" not in audit and "budget" not in audit
    assert audit["status"] == "unsupported"


def test_pr64_builder_omits_the_battery_fields_for_binding_only_input():
    from multidetect.sim_teacher.x1_live_evidence_builder import build_live_evidence

    digests = {"scope_sha256": "a" * 64, "dem_sha256": "b" * 64, "base_mission_sha256": "c" * 64}
    evidence, missing = build_live_evidence(
        SimpleNamespace(document={"budget": {"energy_wh": 74, "reserve_energy_wh": 14.8}}),
        terrain=SimpleNamespace(source_sha256=digests["dem_sha256"]),
        scope_sha256=digests["scope_sha256"], base_mission_sha256=digests["base_mission_sha256"],
        surveyed_bounds=digests,
    )
    assert evidence == {key: digests[key] for key in ("scope_sha256", "dem_sha256")}
    assert {"battery", "budget.battery_capacity_wh"} <= set(missing)


def test_pr64_consumer_fails_closed_even_with_fabricated_capacity_and_bounds():
    # The test uses the real consumer. Inject only its state to isolate metering.
    from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence

    evidence = LiveContinueEvidence.__new__(LiveContinueEvidence)
    evidence.budget = {"energy_wh": 74, "battery_capacity_wh": 72}
    evidence.battery_bounds = (16.201, 0, 0)
    evidence.consumption_high_water = {}
    assert evidence.remaining_energy_wh(sample(fault_bitmask=0, charge_state=1)) is None
    assert evidence.consumption_high_water == {}
    valid = sample(battery_remaining=50, energy_consumed=36, fault_bitmask=0, charge_state=1)
    assert evidence.remaining_energy_wh(valid) == 36
