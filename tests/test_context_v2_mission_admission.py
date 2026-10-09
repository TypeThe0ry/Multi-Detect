"""V2 cannot escape observation/planning into an unscoped Mission adapter."""

import pytest
from test_context_snapshot_v2 import _v2

from multidetect.px4_mission import (
    Px4MissionError,
    _validate_fixed_wing_plan_and_geometry,
    _validate_plan_and_geometry,
)
from multidetect.release_parity import DEFAULT_FILE_LIST


@pytest.mark.parametrize("vehicle", ["multirotor", "vtol", "fixed-wing"])
def test_new_context_refuses_unscoped_vehicle_mission_before_other_checks(vehicle):
    context = _v2(vehicle)
    validator = (
        _validate_fixed_wing_plan_and_geometry
        if vehicle == "fixed-wing"
        else _validate_plan_and_geometry
    )
    with pytest.raises(Px4MissionError, match="actual SIM/HIL/REAL runtime scope"):
        validator(None, context, None, None)


def test_task_parity_binds_canonical_context_semantics_and_both_schemas():
    assert {
        "src/multidetect/autonomy/contracts.py",
        "src/multidetect/autonomy/serialization.py",
        "configs/schemas/autonomy-context-snapshot.schema.json",
        "configs/schemas/autonomy-context-snapshot-v2.schema.json",
    } <= set(DEFAULT_FILE_LIST["task_contract"])
