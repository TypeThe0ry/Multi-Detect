import hashlib
import json
import math

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


@pytest.mark.parametrize("field", ["unexpected", "schema_extra"])
def test_task_unknown_fields_are_rejected(field: str) -> None:
    raw = _task().to_mapping()
    raw[field] = True
    with pytest.raises(PolicyContractError):
        TaskSpecV1.from_mapping(raw)


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda: TaskSpecV1(prompt_sha256="a" * 64, confidence=math.nan), "confidence"),
        (lambda: TaskSpecV1(prompt_sha256="a" * 64, confidence=math.inf), "confidence"),
        (lambda: PolicyActionV2("translate", forward_m=math.nan), "forward_m"),
        (lambda: PolicyActionV2("translate", forward_m=-math.inf), "forward_m"),
    ],
)
def test_contracts_reject_nonfinite_numbers(factory, field: str) -> None:
    del field  # the parameter documents which input is intentionally poisoned
    with pytest.raises(PolicyContractError):
        factory()


@pytest.mark.parametrize(
    ("factory", "schema"),
    [
        (lambda: TaskSpecV1.from_mapping({**_task().to_mapping(), "schema": "v0"}), "task"),
        (
            lambda: PolicyActionV2.from_mapping(
                {**PolicyActionV2("hold", seconds=1.0).to_mapping(), "schema": "v0"}
            ),
            "action",
        ),
        (
            lambda: StructuredSceneState.from_mapping(
                {
                    **_state().to_mapping(),
                    "schema": "v0",
                }
            ),
            "state",
        ),
    ],
)
def test_contracts_reject_wrong_schema_versions(factory, schema: str) -> None:
    del schema
    with pytest.raises(PolicyContractError):
        factory()


@pytest.mark.parametrize(
    "kind,kwargs",
    [
        ("translate", {"forward_m": -2.01}),
        ("translate", {"forward_m": 8.01}),
        ("translate", {"right_m": -6.01}),
        ("translate", {"right_m": 6.01}),
        ("translate", {"up_m": -2.01}),
        ("translate", {"up_m": 2.01}),
        ("turn", {"yaw_delta_deg": -90.01}),
        ("turn", {"yaw_delta_deg": 90.01}),
        ("hold", {"seconds": 0.19}),
        ("hold", {"seconds": 2.01}),
        ("stop", {"forward_m": 1.0}),
    ],
)
def test_action_bounds_are_narrow_and_kind_specific(kind: str, kwargs: dict[str, float]) -> None:
    with pytest.raises(PolicyContractError):
        PolicyActionV2(kind, **kwargs)


@pytest.mark.parametrize(
    "mask",
    [(), (True, False, True), (True, False, True, False, True), (1, False, True, False)],
)
def test_action_mask_shape_and_types_are_rejected(mask: tuple[object, ...]) -> None:
    with pytest.raises(PolicyContractError):
        PolicyActionV2("hold", seconds=1.0, action_mask=mask)


@pytest.mark.parametrize("block", ["health", "pose", "task", "scene", "event"])
@pytest.mark.parametrize("key", ["labels", "truth", "privileged", "ground_truth", "expert"])
def test_privileged_label_keys_are_rejected(block: str, key: str) -> None:
    state = _state().to_mapping()
    state[block] = {key: "must never reach policy"}
    with pytest.raises(PolicyContractError):
        StructuredSceneState.from_mapping(state)


def test_truth_free_false_is_rejected() -> None:
    state = _state().to_mapping()
    state["truth_free"] = False
    with pytest.raises(PolicyContractError):
        StructuredSceneState.from_mapping(state)


def _state() -> StructuredSceneState:
    return StructuredSceneState(
        captured_at_s=1.0,
        dt_s=0.0,
        frame_id="frame-1",
        task_spec_hash=_task().canonical_hash,
        valid_mask={"pose": False},
        reset_mask={"history": True},
        memory=tuple(tuple(tuple(0.0 for _ in range(5)) for _ in range(16)) for _ in range(16)),
        history=tuple({} for _ in range(16)),
    )


def test_canonical_hash_is_stable_for_key_order_and_float_formatting() -> None:
    mapping = _task().to_mapping()
    reordered = {
        "resolver_id": mapping["resolver_id"],
        "unknown_fields": mapping["unknown_fields"],
        "confidence": 0.0000,
        "return_home": mapping["return_home"],
        "record_location": mapping["record_location"],
        "search_area": {"radius_m": None, "frame": "local_ned"},
        "target": mapping["target"],
        "intent": mapping["intent"],
        "prompt_sha256": mapping["prompt_sha256"],
        "schema": mapping["schema"],
    }
    first = TaskSpecV1.from_mapping(mapping)
    second = TaskSpecV1.from_json(json.dumps(reordered, separators=(",", ":")))
    assert first.canonical_hash == second.canonical_hash
    assert first.canonical_json == second.canonical_json


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
