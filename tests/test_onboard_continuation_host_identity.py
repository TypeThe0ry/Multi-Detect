"""Closed stored-field identity is not a serializer or an execution grant."""

from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path

import pytest

from multidetect.autonomy.contracts import GeoPoint, MissionCommand
from multidetect.autonomy.onboard_continuation_host import ContinuationHostError, _memory_identity
from multidetect.autonomy.onboard_continuation_paused_admission import PausedContinuationAdmission


@dataclass(frozen=True)
class _UnknownFrozen:
    value: int


@dataclass(frozen=True, slots=True)
class _UnknownSlotted:
    value: int


class _UnknownEnum(StrEnum):
    VALUE = "unknown"


class _PointSubclass(GeoPoint):
    pass


class _PathSubclass(type(Path())):
    pass


@pytest.mark.parametrize(
    "value",
    [
        _UnknownFrozen(1),
        _UnknownSlotted(1),
        _UnknownEnum.VALUE,
        _PointSubclass(1, 2),
        _PathSubclass("fixture"),
        [1],
        {"a": 1},
        {1},
    ],
)
def test_identity_refuses_unknown_or_mutable_nodes_and_subclasses(value):
    if isinstance(value, _UnknownFrozen):
        # Reproduce the reviewer: frozen alone permits non-field mutable state.
        value.__dict__["private_list"] = []
    with pytest.raises(ContinuationHostError, match="cannot be cached"):
        _memory_identity(value)


def test_identity_uses_exact_known_types_and_all_stored_values():
    point = GeoPoint(1, 2, 10, "relative_home")
    equivalent = GeoPoint(1, 2, 10, "relative_home")
    assert _memory_identity((point, Path("fixture"), MissionCommand.WAYPOINT)) == _memory_identity(
        (equivalent, Path("fixture"), MissionCommand.WAYPOINT)
    )
    original = _memory_identity(point)
    object.__setattr__(point, "altitude_m", 11)
    assert _memory_identity(point) != original
    assert _memory_identity(False) != _memory_identity(0)
    assert _memory_identity(1) != _memory_identity(1.0)


@pytest.mark.parametrize("extra_slots", [False, True])
def test_paused_root_refuses_subclass_before_its_overridden_validator_runs(tmp_path, extra_slots):
    from test_onboard_continuation_host_guards import _guard_fixture

    from multidetect.autonomy.onboard_continuation_host import _paused_identity

    host, paused, _, _ = _guard_fixture(tmp_path)

    def forbidden(self, **kwargs):
        raise AssertionError("unknown paused subclass validator ran")

    attributes = {"validate_binding": forbidden}
    if extra_slots:
        attributes["__slots__"] = ("private_state",)
    unknown = type("UnknownPaused", (PausedContinuationAdmission,), attributes)
    clone = unknown(**{item.name: getattr(paused, item.name) for item in fields(paused)})
    if extra_slots:
        object.__setattr__(clone, "private_state", [])
    else:
        clone.__dict__["private_state"] = []
    with pytest.raises(ContinuationHostError, match="unknown paused"):
        _paused_identity(clone)
    with pytest.raises(ContinuationHostError, match="unknown paused"):
        host._bind_paused(clone)
