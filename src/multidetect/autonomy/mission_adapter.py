"""Vehicle-specific execution seams for the shared PX4 Mission contract.

The planner and compiler remain vehicle-neutral.  An execution adapter owns
only the final transport/lifecycle boundary for one vehicle class.  The
in-process adapter below is deliberately deterministic and software-only; an
external PX4/Gazebo adapter can implement the same protocol without copying
policy selection, planner validation, or Mission compilation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from multidetect.autonomy import ContextSnapshot, normalize_vehicle_class
from multidetect.mission_planning import MissionPolicy, PlannedMissionCompilation
from multidetect.px4_external_mission import ExternalRuntimeAttestation

_AIRFRAME_PROFILE_BY_VEHICLE = {
    "vtol": "px4_vtol_gazebo_v1",
    "fixed-wing": "px4_fixed_wing_gazebo_v1",
    "multirotor": "px4_multirotor_gazebo_v1",
}
_AIRFRAME_ADAPTER_BY_VEHICLE = {
    "vtol": "px4-vtol-mission-int-v1",
    "fixed-wing": "px4-fixed-wing-mission-int-v1",
    "multirotor": "px4-multirotor-mission-int-v1",
}


@runtime_checkable
class MissionExecutionAdapter(Protocol):
    """Execute one already-compiled Mission for exactly one vehicle class."""

    vehicle_class: str
    airframe_profile_id: str

    def execute(
        self,
        compilation: PlannedMissionCompilation,
        *,
        context: ContextSnapshot,
        policy: MissionPolicy,
        journal_path: str | Path,
        now_utc: datetime,
        transaction_id: str,
    ) -> Mapping[str, object]:
        """Run the adapter-owned transport/lifecycle and return evidence."""


class InProcessPx4MissionExecutionAdapter:
    """Deterministic software adapter for all three canonical PX4 airframes.

    This adapter calls the existing journalized Mission transaction only.  It
    never opens a socket, starts PX4, arms a vehicle, or emits actuator output.
    """

    __slots__ = ("vehicle_class", "airframe_profile_id")

    def __init__(self, vehicle_class: str, airframe_profile_id: str | None = None) -> None:
        normalized = normalize_vehicle_class(vehicle_class)
        expected_profile = _AIRFRAME_PROFILE_BY_VEHICLE[normalized]
        if airframe_profile_id is not None and airframe_profile_id != expected_profile:
            raise ValueError("airframe_profile_id does not match vehicle_class")
        self.vehicle_class = normalized
        self.airframe_profile_id = expected_profile

    def execute(
        self,
        compilation: PlannedMissionCompilation,
        *,
        context: ContextSnapshot,
        policy: MissionPolicy,
        journal_path: str | Path,
        now_utc: datetime,
        transaction_id: str,
    ) -> Mapping[str, object]:
        if not isinstance(compilation, PlannedMissionCompilation):
            raise TypeError("compilation must be a PlannedMissionCompilation")
        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if compilation.vehicle_class != self.vehicle_class:
            raise ValueError("compiled Mission vehicle_class does not match adapter")
        if context.airframe_profile_id != self.airframe_profile_id:
            raise ValueError("context airframe profile does not match adapter")
        if (
            compilation.compiled_mission.executable_mission.airframe_adapter_id
            != _AIRFRAME_ADAPTER_BY_VEHICLE[self.vehicle_class]
        ):
            raise ValueError("executable Mission adapter identity does not match adapter")

        # Import the transaction layer lazily so the autonomy package remains
        # independent from the script entry points and external runtimes.
        from multidetect.px4_mission_acceptance import run_px4_mission_transaction_acceptance

        result = run_px4_mission_transaction_acceptance(
            vehicle_class=self.vehicle_class,
            compiled=compilation.compiled_mission,
            context_snapshot=context,
            platform_plan=compilation.plan,
            policy=policy,
            journal_path=journal_path,
            now_utc=now_utc,
            transaction_id=transaction_id,
        )
        return {
            **result,
            "execution_adapter": "in_process_px4_mission",
            "external_runtime": False,
            "actuator_output_enabled": False,
        }


class ExternalPx4MissionExecutionAdapter:
    """External-SITL Mission transaction adapter.

    The connection is injected by an ownership/telemetry orchestrator.  This
    class intentionally stops after ``permit_start``: mode changes, arming,
    flight observation and cleanup remain explicit lifecycle steps owned by the
    vehicle-specific external runner.
    """

    __slots__ = (
        "vehicle_class",
        "airframe_profile_id",
        "_connection",
        "_receive",
        "_clock",
        "_runtime_attestation",
    )

    def __init__(
        self,
        vehicle_class: str,
        connection: Any,
        *,
        airframe_profile_id: str | None = None,
        receive: Any = None,
        clock: Any = None,
        runtime_attestation: ExternalRuntimeAttestation | Mapping[str, object] | None = None,
    ) -> None:
        normalized = normalize_vehicle_class(vehicle_class)
        expected_profile = _AIRFRAME_PROFILE_BY_VEHICLE[normalized]
        if airframe_profile_id is not None and airframe_profile_id != expected_profile:
            raise ValueError("airframe_profile_id does not match vehicle_class")
        if connection is None or not hasattr(connection, "mav"):
            raise TypeError("external adapter requires an injected MAVLink connection")
        if receive is not None and not callable(receive):
            raise TypeError("receive must be callable when provided")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable when provided")
        if isinstance(runtime_attestation, Mapping):
            runtime_attestation = ExternalRuntimeAttestation.from_mapping(runtime_attestation)
        if runtime_attestation is not None and not isinstance(
            runtime_attestation, ExternalRuntimeAttestation
        ):
            raise TypeError("runtime_attestation has the wrong type")
        if runtime_attestation is not None and runtime_attestation.vehicle_class != normalized:
            raise ValueError("runtime_attestation vehicle_class does not match adapter")
        self.vehicle_class = normalized
        self.airframe_profile_id = expected_profile
        self._connection = connection
        self._receive = receive
        self._clock = clock
        self._runtime_attestation = runtime_attestation

    def execute(
        self,
        compilation: PlannedMissionCompilation,
        *,
        context: ContextSnapshot,
        policy: MissionPolicy,
        journal_path: str | Path,
        now_utc: datetime,
        transaction_id: str,
    ) -> Mapping[str, object]:
        if not isinstance(compilation, PlannedMissionCompilation):
            raise TypeError("compilation must be a PlannedMissionCompilation")
        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if compilation.vehicle_class != self.vehicle_class:
            raise ValueError("compiled Mission vehicle_class does not match adapter")
        if context.airframe_profile_id != self.airframe_profile_id:
            raise ValueError("context airframe profile does not match adapter")
        if (
            compilation.compiled_mission.executable_mission.airframe_adapter_id
            != _AIRFRAME_ADAPTER_BY_VEHICLE[self.vehicle_class]
        ):
            raise ValueError("executable Mission adapter identity does not match adapter")
        from multidetect.px4_external_mission import run_external_px4_mission_transaction

        return run_external_px4_mission_transaction(
            connection=self._connection,
            vehicle_class=self.vehicle_class,
            compiled=compilation.compiled_mission,
            context_snapshot=context,
            platform_plan=compilation.plan,
            policy=policy,
            journal_path=journal_path,
            now_utc=now_utc,
            transaction_id=transaction_id,
            receive=self._receive,
            clock=self._clock,
            runtime_attestation=self._runtime_attestation,
        )


def build_in_process_px4_adapter(
    vehicle_class: str,
    *,
    airframe_profile_id: str | None = None,
) -> InProcessPx4MissionExecutionAdapter:
    """Build a deterministic adapter after validating vehicle/profile identity."""

    return InProcessPx4MissionExecutionAdapter(vehicle_class, airframe_profile_id)


def build_external_px4_adapter(
    vehicle_class: str,
    connection: Any,
    *,
    airframe_profile_id: str | None = None,
    receive: Any = None,
    clock: Any = None,
    runtime_attestation: ExternalRuntimeAttestation | Mapping[str, object] | None = None,
) -> ExternalPx4MissionExecutionAdapter:
    """Build an external transaction adapter around an owned connection."""

    return ExternalPx4MissionExecutionAdapter(
        vehicle_class,
        connection,
        airframe_profile_id=airframe_profile_id,
        receive=receive,
        clock=clock,
        runtime_attestation=runtime_attestation,
    )


__all__ = [
    "ExternalPx4MissionExecutionAdapter",
    "ExternalRuntimeAttestation",
    "InProcessPx4MissionExecutionAdapter",
    "MissionExecutionAdapter",
    "build_external_px4_adapter",
    "build_in_process_px4_adapter",
]
