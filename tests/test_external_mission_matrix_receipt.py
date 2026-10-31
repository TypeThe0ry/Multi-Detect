from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.create_external_mission_matrix_receipt import create_receipt

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = json.loads((ROOT / "docs/current-progress.json").read_text(encoding="utf-8"))
MATRIX_PATH = ROOT / PROGRESS["external_mission_matrix_20260927_r6"]["evidence"]
if not MATRIX_PATH.is_file():
    pytest.skip(f"Local mission matrix artifact is absent: {MATRIX_PATH}", allow_module_level=True)
MATRIX = json.loads(
    MATRIX_PATH.read_text(encoding="utf-8")
)
VTOL = ROOT / MATRIX["vehicles"]["vtol"][0]["campaign_receipt"]
FIXED = ROOT / MATRIX["vehicles"]["fixed-wing"]["evidence"]
MULTIROTOR = ROOT / MATRIX["vehicles"]["multirotor"]["evidence"]
for artifact in (VTOL, FIXED, MULTIROTOR):
    if not artifact.is_file():
        pytest.skip(f"Local mission matrix artifact is absent: {artifact}", allow_module_level=True)


def test_external_mission_matrix_receipt_binds_three_vehicle_runs() -> None:
    receipt = create_receipt(
        root=ROOT,
        vtol_receipt=VTOL,
        fixed_wing_evidence=FIXED,
        multirotor_evidence=MULTIROTOR,
        generated_at_utc="2026-09-27T00:00:00Z",
    )
    assert receipt["same_external_mission_path"] is True
    assert receipt["same_core_compiler_and_transaction"] is True
    assert receipt["current_release_parity_bound"] is True
    assert receipt["source_release_parity_binding_required_for_promotion"] is True
    assert receipt["vehicles"]["fixed-wing"]["planner_revalidated"] is True
    assert receipt["vehicles"]["fixed-wing"]["policy_applied_to_actuator"] is False
    assert receipt["vehicles"]["multirotor"]["planner_revalidated"] is True
    assert receipt["vehicles"]["multirotor"]["policy_applied_to_actuator"] is False
    assert (
        receipt["vehicles"]["fixed-wing"]["protocol_hash"]
        == receipt["vehicles"]["fixed-wing"]["readback_protocol_hash"]
    )
    assert (
        receipt["vehicles"]["multirotor"]["protocol_hash"]
        == receipt["vehicles"]["multirotor"]["readback_protocol_hash"]
    )
    assert len(receipt["vehicles"]["vtol"]) == 2
    assert all(
        item["mission_state_path"][-1] == "start_permitted"
        for item in receipt["vehicles"]["vtol"]
    )
    assert receipt["final_release_allowed"] is False


def test_external_mission_matrix_receipt_rejects_tampered_fixed_wing(tmp_path: Path) -> None:
    document = json.loads(FIXED.read_text(encoding="utf-8"))
    document["mission_transaction"]["flight_started"] = True
    tampered = tmp_path / "fixed.json"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="flight-start claim"):
        create_receipt(
            root=ROOT,
            vtol_receipt=VTOL,
            fixed_wing_evidence=tampered,
            multirotor_evidence=MULTIROTOR,
            generated_at_utc="2026-09-27T00:00:00Z",
        )
