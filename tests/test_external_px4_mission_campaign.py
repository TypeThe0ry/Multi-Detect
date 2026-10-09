from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_external_px4_mission_transaction as module
from scripts.run_px4_airframe_transaction_acceptance import _context

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def test_external_mission_bridge_dry_run_stays_preflight_only(tmp_path: Path) -> None:
    evidence = tmp_path / "external-mission-dry-run.json"
    result = module.main(
        [
            "--scenario-manifest",
            str(SCENARIO),
            "--evidence",
            str(evidence),
            "--vehicle-class",
            "vtol",
            "--transaction-id",
            "dry-run",
        ]
    )
    assert result == 0
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["execution_requested"] is False
    assert payload["mission_transaction"]["status"] == "not_requested"
    assert payload["mission_transaction_gate_passed"] is None
    assert payload["hardware"]["hardware_control_enabled"] is False


def test_external_mission_bridge_requires_artifacts_for_execute() -> None:
    result = module.main(
        [
            "--vehicle-class",
            "vtol",
            "--transaction-id",
            "missing-inputs",
            "--execute",
        ]
    )
    assert result == 2


def test_live_gps_context_binding_rejects_wrong_precompiled_origin() -> None:
    context = _context("multirotor")
    telemetry = {
        "qualified": True,
        "gps": {"fix_type": 3, "lat": 13000000, "lon": 1038000000},
    }
    assert module._verify_live_gps_context(telemetry, context)["qualified"] is True
    wrong = {
        "qualified": True,
        "gps": {"fix_type": 3, "lat": 12905000, "lon": 1038505000},
    }
    with pytest.raises(module.ExternalMissionCampaignError, match="does not match live"):
        module._verify_live_gps_context(wrong, context)
