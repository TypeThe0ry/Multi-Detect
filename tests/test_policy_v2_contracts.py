import hashlib

import pytest

from multidetect.autonomy.policy_v2 import (
    PolicyActionV2,
    PolicyContractError,
    StructuredSceneState,
    TaskSpecV1,
)


def _task() -> TaskSpecV1:
    return TaskSpecV1(prompt_sha256=hashlib.sha256(b"search red car").hexdigest())


def test_task_and_action_round_trip_and_hash_stability() -> None:
    task = _task()
    assert TaskSpecV1.from_json(task.to_json()).canonical_hash == task.canonical_hash
    action = PolicyActionV2("translate", forward_m=2.0, right_m=-1.0)
    assert PolicyActionV2.from_json(action.to_json()) == action
    assert action.to_primitive().kind == "translate"


def test_contracts_fail_closed_for_unknown_nonfinite_and_bounds() -> None:
    task = _task().to_mapping()
    task["unexpected"] = True
    with pytest.raises(PolicyContractError):
        TaskSpecV1.from_mapping(task)
    with pytest.raises(PolicyContractError):
        PolicyActionV2("translate", forward_m=9.0)
    with pytest.raises(PolicyContractError):
        PolicyActionV2.from_json('{"schema":"multidetect.policy-action-v2","kind":"hold","forward_m":0,"right_m":0,"up_m":0,"yaw_delta_deg":0,"seconds":NaN,"action_mask":[true,true,true,true]}')


def test_state_round_trip_requires_fixed_memory_and_history() -> None:
    state = StructuredSceneState(
        captured_at_s=1.0,
        dt_s=0.0,
        frame_id="frame-1",
        task_spec_hash=_task().canonical_hash,
        valid_mask={"pose": False},
        reset_mask={"history": True},
        memory=tuple(tuple(tuple(0.0 for _ in range(5)) for _ in range(16)) for _ in range(16)),
        history=tuple({} for _ in range(16)),
    )
    assert StructuredSceneState.from_json(state.to_json()).canonical_hash == state.canonical_hash
    malformed = state.to_mapping()
    malformed["memory"][0][0] = [0.0]
    with pytest.raises(PolicyContractError):
        StructuredSceneState.from_mapping(malformed)
