"""Receive-only development admission for a multirotor continuation.

Original host receive envelopes and an ordinary parent journal are reopened;
``confirmed=True`` is not a flight fact. The host owns receiver authentication,
MAVLink decoding and clock attribution. Hashes establish integrity, not origin.
Sensor boot time is retained but never converted to a host/session epoch.

This module has no transport, grant, parameter-write or execution callback.
The first ordinary parent is supported. A RESUMED replacement-parent chain and
generation/cancellation admission remain explicitly unsupported/pending.
"""

from __future__ import annotations

import base64
import hashlib
import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..mission_journal import DurableMissionJournal, MissionTransactionState
from ..px4_mission import (
    MAV_CMD_NAV_LOITER_TIME,
    MAV_CMD_NAV_WAYPOINT,
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    PX4_AUTO_MISSION_CUSTOM_MODE,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    CompiledPx4Mission,
    Px4MissionItemInt,
    compile_px4_mission_items,
    px4_wire_hash,
)
from ..px4_mission_feedback import PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
from .contracts import ContextSnapshot, MissionCommand, MissionFrame
from .onboard_evidence_collector import (
    OnboardMissionTelemetryBinding,
    OnboardTelemetryObservation,
)
from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardMultimodalScope, Scope
from .serialization import (
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

if TYPE_CHECKING:
    from .onboard_continuation import ContinuationParentBinding

RECEIVE_CONTRACT = "multidetect.onboard-continuation-received-fact-v1"
_MAX_BYTES = 256 * 1024
_MAX_FACT_BYTES = 16 * 1024
_AGE_S = PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S


class ContinuationBindingError(ValueError):
    """Missing or inconsistent source facts; no execution authority is issued."""


def _scope(value: Scope) -> OnboardMultimodalScope:
    if isinstance(value, OnboardInferenceScope):
        value = OnboardMultimodalScope.from_inference_scope(value)
    if not isinstance(value, OnboardMultimodalScope):
        raise TypeError("scope must be a typed onboard scope")
    if (
        value.vehicle_class != "multirotor"
        or value.home_sha256 is None
        or value.airframe_profile_sha256 is None
    ):
        raise ContinuationBindingError("known multirotor/home/profile scope required")
    return value


def _original(payload: bytes, sha256: str, *, maximum: int = _MAX_FACT_BYTES) -> object:
    if type(payload) is not bytes or not 0 < len(payload) <= maximum:
        raise ContinuationBindingError("source must be bounded immutable original bytes")
    if hashlib.sha256(payload).hexdigest() != require_sha256(sha256, "source_sha256"):
        raise ContinuationBindingError("original source bytes/SHA mismatch")
    return strict_json_loads(payload, name="continuation receive fact")


def _encoded(payload: bytes) -> dict:
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "original_bytes_base64": base64.b64encode(payload).decode("ascii"),
    }


def _tuple(values, name, kind, count):
    if (
        type(values) is not tuple
        or len(values) != count
        or any(not isinstance(item, kind) for item in values)
    ):
        raise ContinuationBindingError(f"{name} requires {count} immutable typed facts")


def _trusted(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or len(values) > 8 or len(set(values)) != len(values):
        raise ContinuationBindingError("trusted_source_ids must be a bounded unique tuple")
    for item in values:
        if len(require_text(item, "source_id")) > 128:
            raise ContinuationBindingError("source ID is too long")


def _receive_item_payload(message: dict) -> dict:
    """Receive-only syntax; no outbound normalization or identity admission."""
    original = {key: value for key, value in message.items() if key != "message_type"}
    strict_object(
        original,
        name="received MISSION_ITEM_INT",
        required=frozenset(
            {
                "target_system",
                "target_component",
                "sequence",
                "frame",
                "command",
                "current",
                "autocontinue",
                "param1",
                "param2",
                "param3",
                "param4",
                "x",
                "y",
                "z",
                "mission_type",
            }
        ),
    )
    for name in ("target_system", "target_component"):
        require_int(original[name], name, minimum=1, maximum=255)
    require_int(original["current"], "current", minimum=0, maximum=1)
    require_int(original["sequence"], "sequence", minimum=0, maximum=63)
    require_int(original["autocontinue"], "autocontinue", minimum=1, maximum=1)
    require_int(original["mission_type"], "mission_type", minimum=0, maximum=0)
    require_int(
        original["frame"],
        "frame",
        minimum=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        maximum=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    )
    command = require_int(original["command"], "command", minimum=0, maximum=65535)
    if command not in {MAV_CMD_NAV_WAYPOINT, MAV_CMD_NAV_LOITER_TIME}:
        raise ContinuationBindingError("received child command is not WP/LOITER_TIME")
    require_int(original["x"], "x", minimum=-900000000, maximum=900000000)
    require_int(original["y"], "y", minimum=-1800000000, maximum=1800000000)
    for name in ("param1", "param2", "param3", "param4", "z"):
        require_real(original[name], name)
    return original


def _receive_item_comparison(message: dict) -> Px4MissionItemInt:
    """After receiver/source/cursor admission, normalize only a comparison copy.

    PX4 send_mission_item addresses the requesting GCS and reports its cursor.
    Outbound Px4MissionItemInt addresses PX4 with seq0 current. Original receive
    bytes/fields remain untouched; geometry/parameters/frame/sequence stay exact.
    """
    original = _receive_item_payload(message)
    seq = original["sequence"]
    comparison = {**original, "target_system": 1, "target_component": 1, "current": int(seq == 0)}
    return Px4MissionItemInt.from_mapping(comparison)


@dataclass(frozen=True, slots=True)
class ContinuationReceivedFact:
    """Exact decoded host receive envelope, not a model assertion or ACK flag.

    Envelope metadata binds one host receiver/session/vehicle/Mission generation.
    It does not claim those fields exist on a raw MAVLink message. ``time_boot_ms``
    and HOME's wire ``time_usec`` stay in the message's own clock domain.
    """

    source_receipt_bytes: bytes
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        self.to_mapping()

    def to_mapping(self) -> dict:
        raw = strict_object(
            _original(self.source_receipt_bytes, self.source_receipt_sha256),
            name="continuation receive envelope",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "message_id",
                    "scope_sha256",
                    "session_id",
                    "aircraft_id",
                    "observed_at_s",
                    "clock_epoch",
                    "source_id",
                    "source_system_id",
                    "source_component_id",
                    "vehicle_identity_sha256",
                    "mission_sha256",
                    "transaction_id",
                    "message",
                }
            ),
            optional=frozenset({"wire_bytes_base64", "wire_bytes", "wire_sha256"}),
        )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ContinuationBindingError("unsupported receive schema")
        if raw["contract"] != RECEIVE_CONTRACT:
            raise ContinuationBindingError("unsupported receive contract")
        for name in ("scope_sha256", "vehicle_identity_sha256", "mission_sha256"):
            require_sha256(raw[name], name)
        for name in (
            "message_id",
            "session_id",
            "aircraft_id",
            "clock_epoch",
            "source_id",
            "transaction_id",
        ):
            if len(require_text(raw[name], name)) > 128:
                raise ContinuationBindingError(f"{name} exceeds bound")
        require_real(raw["observed_at_s"], "observed_at_s", minimum=0)
        for name in ("source_system_id", "source_component_id"):
            require_int(raw[name], name, minimum=1, maximum=255)
        message = raw["message"]
        if not isinstance(message, dict):
            raise ContinuationBindingError("receive message must be object")
        kind = message.get("message_type")
        fields = {
            "HOME_POSITION": {"latitude", "longitude", "altitude", "time_usec"},
            "GLOBAL_POSITION_INT": {"lat", "lon", "alt", "relative_alt", "time_boot_ms"},
            "PARAM_VALUE": {
                "param_id",
                "param_type",
                "param_count",
                "param_index",
                "parameter_encoding",
                "raw_value_hex",
            },
            "MISSION_COUNT": {"count", "mission_type", "target_system", "target_component"},
            "MISSION_CURRENT": {"seq"},
        }.get(kind)
        if kind == "MISSION_ITEM_INT":
            _receive_item_payload(message)
        elif fields is None:
            raise ContinuationBindingError("unsupported receive fact type")
        else:
            strict_object(
                message, name="receive message", required=frozenset(fields | {"message_type"})
            )
            if kind in {"HOME_POSITION", "GLOBAL_POSITION_INT"}:
                for name, low, high in (
                    ("latitude" if kind == "HOME_POSITION" else "lat", -900000000, 900000000),
                    ("longitude" if kind == "HOME_POSITION" else "lon", -1800000000, 1800000000),
                    ("altitude" if kind == "HOME_POSITION" else "alt", -(2**31), 2**31 - 1),
                ):
                    require_int(message[name], name, minimum=low, maximum=high)
                if kind == "GLOBAL_POSITION_INT":
                    require_int(
                        message["relative_alt"], "relative_alt", minimum=-(2**31), maximum=2**31 - 1
                    )
                    require_int(
                        message["time_boot_ms"], "time_boot_ms", minimum=0, maximum=2**32 - 1
                    )
                else:
                    require_int(message["time_usec"], "time_usec", minimum=0, maximum=2**64 - 1)
            elif kind == "PARAM_VALUE":
                require_text(message["param_id"], "param_id")
                require_int(message["param_type"], "param_type", minimum=1, maximum=10)
                count = require_int(message["param_count"], "param_count", minimum=1, maximum=65535)
                require_int(message["param_index"], "param_index", minimum=0, maximum=count - 1)
                if message["parameter_encoding"] not in {"bytewise", "c_cast"}:
                    raise ContinuationBindingError("parameter encoding must be explicit")
                value = message["raw_value_hex"]
                if (
                    not isinstance(value, str)
                    or len(value) != 8
                    or bytes.fromhex(value).hex() != value
                ):
                    raise ContinuationBindingError("PARAM_VALUE requires exact four original bytes")
            elif kind == "MISSION_COUNT":
                require_int(message["count"], "count", minimum=1, maximum=512)
                require_int(message["mission_type"], "mission_type", minimum=0, maximum=0)
                for name in ("target_system", "target_component"):
                    require_int(message[name], name, minimum=1, maximum=255)
            else:
                require_int(message["seq"], "seq", minimum=0, maximum=511)
        wire = {"wire_bytes_base64", "wire_bytes", "wire_sha256"} & raw.keys()
        if wire:
            if len(wire) != 3 or not isinstance(raw["wire_bytes_base64"], str):
                raise ContinuationBindingError("wire provenance must be complete")
            packet = base64.b64decode(raw["wire_bytes_base64"], validate=True)
            if len(packet) != require_int(
                raw["wire_bytes"], "wire_bytes", minimum=1, maximum=4096
            ) or hashlib.sha256(packet).hexdigest() != require_sha256(
                raw["wire_sha256"], "wire_sha256"
            ):
                raise ContinuationBindingError("wire bytes/SHA mismatch")
        return dict(raw)


def _fresh(raw, now_s, earliest_s):
    stamp = raw["observed_at_s"]
    if stamp < earliest_s or not 0 <= now_s - stamp <= _AGE_S:
        raise ContinuationBindingError("receive fact is future, stale or predates binding")


def _receive_binding(raw, scope, mission_binding, transaction_id, trusted, now_s, earliest_s):
    _fresh(raw, now_s, earliest_s)
    if (
        raw["scope_sha256"] != scope.canonical_hash
        or raw["session_id"] != scope.session_id
        or raw["aircraft_id"] != scope.aircraft_id
        or raw["clock_epoch"] != scope.clock_epoch
        or raw["source_id"] not in trusted
        or (raw["source_system_id"], raw["source_component_id"]) != (1, 1)
        or raw["vehicle_identity_sha256"] != mission_binding.vehicle_identity_sha256
        or raw["transaction_id"] != transaction_id
    ):
        raise ContinuationBindingError("receive scope/source/vehicle/transaction mismatch")


@dataclass(frozen=True, slots=True)
class ContinuationParentEvidence:
    scope: Scope
    mission_binding: OnboardMissionTelemetryBinding
    journal_context: ContextSnapshot
    current_context: ContextSnapshot
    telemetry: tuple[OnboardTelemetryObservation, ...]
    navigation_facts: tuple[ContinuationReceivedFact, ...]
    trusted_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _scope(self.scope)
        if not isinstance(self.mission_binding, OnboardMissionTelemetryBinding):
            raise TypeError("parent requires a typed Mission telemetry binding")
        if not all(
            isinstance(item, ContextSnapshot)
            for item in (self.journal_context, self.current_context)
        ):
            raise TypeError("parent requires original/current ContextSnapshot")
        _tuple(self.telemetry, "telemetry", OnboardTelemetryObservation, 3)
        _tuple(self.navigation_facts, "navigation_facts", ContinuationReceivedFact, 3)
        _trusted(self.trusted_source_ids)
        if (
            sum(
                len(item.source_receipt_bytes) for item in (*self.telemetry, *self.navigation_facts)
            )
            > _MAX_BYTES
        ):
            raise ContinuationBindingError("parent receive evidence exceeds bound")

    def _journal(self):
        binding = self.mission_binding
        if binding.journal_path.stat().st_size > _MAX_BYTES:
            raise ContinuationBindingError("parent journal exceeds admission byte bound")
        original = binding.journal_path.read_bytes()
        if (
            len(original) != binding.journal_bytes
            or hashlib.sha256(original).hexdigest() != binding.journal_sha256
        ):
            raise ContinuationBindingError("parent journal original bytes/SHA mismatch")
        scope = _scope(self.scope)
        scope.validate_context(self.journal_context)
        scope.validate_context(self.current_context)
        mission = binding.mission
        if (
            mission.airframe_adapter_id != PX4_MULTIROTOR_ADAPTER_ID
            or mission.airframe_adapter_hash != PX4_MULTIROTOR_ADAPTER_HASH
        ):
            raise ContinuationBindingError("parent requires exact ordinary multirotor adapter")
        if not (
            self.journal_context.captured_at_s
            <= binding.bound_at_s
            <= self.current_context.captured_at_s
        ):
            raise ContinuationBindingError("parent journal/context/binding clock order mismatch")
        # Only an original ordinary parent is supported here; a continuation
        # parent would need the independently reviewed RESUMED chain as well.
        commands = tuple(item.command for item in mission.items)
        if commands[0] is not MissionCommand.TAKEOFF or commands[-1] is not MissionCommand.LAND:
            raise ContinuationBindingError("continuation parent RESUMED chain is pending")
        with DurableMissionJournal.open(binding.journal_path) as journal:
            identity = journal.identity
            if (
                journal.recovery_required
                or journal.state is not MissionTransactionState.START_PERMITTED
            ):
                raise ContinuationBindingError(
                    "parent needs intact ordinary START_PERMITTED journal"
                )
            expected = (
                identity.executable_mission_hash == mission.canonical_hash
                and identity.protocol_hash == px4_wire_hash(compile_px4_mission_items(mission))
                and identity.mission_item_count == len(mission.items)
                and identity.airframe_adapter_id == mission.airframe_adapter_id
                and identity.bindings.prompt_hash == scope.prompt_sha256
                and identity.bindings.task_spec_hash == binding.task.canonical_hash
                and identity.bindings.context_snapshot_hash == self.journal_context.canonical_hash
                and identity.bindings.plan_hash == mission.platform_plan_hash
                and identity.bindings.airframe_profile_hash == scope.airframe_profile_sha256
                and identity.bindings.vehicle_identity_hash == binding.vehicle_identity_sha256
                and binding.task.task_id == scope.selected_task_id
                and binding.clock_epoch == scope.clock_epoch
                and mission.airframe_profile_hash == scope.airframe_profile_sha256
            )
            if not expected:
                raise ContinuationBindingError(
                    "parent journal identity does not match actual scope/Mission"
                )
            identity_map = identity.to_mapping()
        if binding.journal_path.read_bytes() != original:
            raise ContinuationBindingError("parent journal changed during verification")
        return original, identity_map

    def validate_binding(
        self, *, current_scope: Scope, current_context: ContextSnapshot, now_s: float
    ) -> ContinuationParentBinding:
        from .onboard_continuation import ContinuationParentBinding

        self.__post_init__()
        scope = _scope(self.scope)
        if _scope(current_scope) != scope or current_context != self.current_context:
            raise ContinuationBindingError("parent evidence differs from current scope/context")
        now = require_real(now_s, "now_s", minimum=0)
        if not 0 <= now - current_context.captured_at_s <= _AGE_S:
            raise ContinuationBindingError("current context is future or stale")
        _, identity = self._journal()
        binding = self.mission_binding
        raw_telemetry = [item.to_mapping() for item in self.telemetry]
        kinds = [item["message"]["message_type"] for item in raw_telemetry]
        if kinds != ["HEARTBEAT", "EXTENDED_SYS_STATE", "MISSION_CURRENT"]:
            raise ContinuationBindingError(
                "ordered HEARTBEAT/in-air/MISSION_CURRENT facts required"
            )
        previous = binding.bound_at_s
        ids = set()
        for raw in raw_telemetry:
            _fresh(raw, now, previous)
            previous = raw["observed_at_s"]
            if (
                raw["clock_epoch"] != scope.clock_epoch
                or raw["source_id"] not in self.trusted_source_ids
                or (raw["source_system_id"], raw["source_component_id"]) != (1, 1)
                or raw["mission_sha256"] != binding.mission.canonical_hash
                or raw["transaction_id"] != identity["transaction_id"]
                or raw["message_id"] in ids
            ):
                raise ContinuationBindingError("parent airborne source/Mission/clock/ID mismatch")
            ids.add(raw["message_id"])
        heartbeat, lifecycle, cursor = (raw["message"] for raw in raw_telemetry)
        if (
            not heartbeat["base_mode"] & 128
            or not heartbeat["base_mode"] & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            or heartbeat["custom_mode"] != PX4_AUTO_MISSION_CUSTOM_MODE
            or lifecycle["landed_state"] != 2
        ):
            raise ContinuationBindingError("actual armed AUTO.MISSION/in-air facts required")
        sequence = cursor["seq"]
        if not 1 <= sequence < len(binding.mission.items) - 1 or binding.mission.items[
            sequence
        ].command not in {MissionCommand.WAYPOINT, MissionCommand.LOITER_TIME}:
            raise ContinuationBindingError("parent cursor is not an in-flight navigation body item")
        facts = [item.to_mapping() for item in self.navigation_facts]
        if [item["message"]["message_type"] for item in facts] != [
            "HOME_POSITION",
            "PARAM_VALUE",
            "GLOBAL_POSITION_INT",
        ]:
            raise ContinuationBindingError("ordered actual HOME/parameter/position facts required")
        previous = binding.bound_at_s
        for raw in facts:
            _receive_binding(
                raw,
                scope,
                binding,
                identity["transaction_id"],
                self.trusted_source_ids,
                now,
                previous,
            )
            previous = raw["observed_at_s"]
            if raw["mission_sha256"] != binding.mission.canonical_hash or raw["message_id"] in ids:
                raise ContinuationBindingError("navigation Mission/message ID mismatch")
            ids.add(raw["message_id"])
        home, parameter, position = (item["message"] for item in facts)
        if parameter["param_id"] != "MIS_TKO_LAND_REQ" or parameter["param_type"] != 6:
            raise ContinuationBindingError("actual INT32 MIS_TKO_LAND_REQ parameter required")
        raw_value = bytes.fromhex(parameter["raw_value_hex"])
        value = struct.unpack(
            "<i" if parameter["parameter_encoding"] == "bytewise" else "<f", raw_value
        )[0]
        if not math.isfinite(value) or value != 0:
            raise ContinuationBindingError(
                "MIS_TKO_LAND_REQ has not measuredly allowed missing takeoff/land"
            )
        context_home = current_context.home_position
        aircraft = current_context.aircraft_position
        if (
            abs(context_home.latitude_deg - home["latitude"] / 1e7) > 0.5e-7 + 1e-12
            or abs(context_home.longitude_deg - home["longitude"] / 1e7) > 0.5e-7 + 1e-12
            or aircraft.altitude_reference != "relative_home"
            or aircraft.altitude_m is None
            or aircraft.altitude_m <= 0
            or abs(aircraft.latitude_deg - position["lat"] / 1e7) > 0.5e-7 + 1e-12
            or abs(aircraft.longitude_deg - position["lon"] / 1e7) > 0.5e-7 + 1e-12
            or abs(aircraft.altitude_m - position["relative_alt"] / 1000) > 0.001
            or abs(position["alt"] - home["altitude"] - position["relative_alt"]) > 1
            or facts[-1]["observed_at_s"] != current_context.captured_at_s
        ):
            raise ContinuationBindingError(
                "actual HOME/relative-altitude/position differs from context"
            )
        if context_home.altitude_m is not None and (
            (context_home.altitude_reference == "relative_home" and context_home.altitude_m != 0)
            or (
                context_home.altitude_reference == "amsl"
                and abs(context_home.altitude_m - home["altitude"] / 1000) > 0.001
            )
            or context_home.altitude_reference not in {"relative_home", "amsl"}
        ):
            raise ContinuationBindingError("known Home altitude contradicts actual HOME_POSITION")
        return ContinuationParentBinding(
            parent_mission_id=binding.mission.mission_id,
            parent_mission_sha256=binding.mission.canonical_hash,
            parent_wire_sha256=identity["protocol_hash"],
            parent_journal_sha256=binding.journal_sha256,
            airframe_profile_sha256=scope.airframe_profile_sha256,
            home_position=context_home,
            observed_current_sequence=sequence,
            observed_at_s=raw_telemetry[-1]["observed_at_s"],
            source_system=1,
            source_component=1,
        )

    def to_mapping(self) -> dict:
        self.__post_init__()
        for item in (*self.telemetry, *self.navigation_facts):
            item.to_mapping()
        original, identity = self._journal()
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-continuation-parent-evidence-v1",
            "scope": _scope(self.scope).to_mapping(),
            "journal_context_sha256": self.journal_context.canonical_hash,
            "current_context_sha256": self.current_context.canonical_hash,
            "parent_mission": self.mission_binding.mission.to_mapping(),
            "task_sha256": self.mission_binding.task.canonical_hash,
            "vehicle_identity_sha256": self.mission_binding.vehicle_identity_sha256,
            "bound_at_s": self.mission_binding.bound_at_s,
            "journal_identity": identity,
            "journal_original": _encoded(original),
            "telemetry_original": [_encoded(item.source_receipt_bytes) for item in self.telemetry],
            "navigation_original": [
                _encoded(item.source_receipt_bytes) for item in self.navigation_facts
            ],
            "trusted_source_ids": list(self.trusted_source_ids),
            "source_authentication": "host_owned_receiver_not_proven_by_hash",
            "execution_authority": False,
            "transport_created": False,
            "parameter_writes_performed": False,
            "replacement_parent_chain_verified": False,
            "generation_cancellation_verified": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ContinuationChildEvidence:
    scope: Scope
    parent: ContinuationParentEvidence
    compiled_mission: CompiledPx4Mission
    readback_facts: tuple[ContinuationReceivedFact, ...]
    cursor_fact: ContinuationReceivedFact
    child_transaction_id: str
    trusted_source_ids: tuple[str, ...] = ()
    receiver_system_id: int | None = None
    receiver_component_id: int | None = None

    def __post_init__(self) -> None:
        _scope(self.scope)
        if not isinstance(self.parent, ContinuationParentEvidence) or not isinstance(
            self.compiled_mission, CompiledPx4Mission
        ):
            raise TypeError("child requires typed parent evidence and compiled Mission")
        _tuple(self.readback_facts, "readback_facts", ContinuationReceivedFact, 4)
        if not isinstance(self.cursor_fact, ContinuationReceivedFact):
            raise TypeError("child requires an actual cursor receive fact")
        require_text(self.child_transaction_id, "child_transaction_id")
        _trusted(self.trusted_source_ids)
        if (self.receiver_system_id is None) != (self.receiver_component_id is None):
            raise ContinuationBindingError("receiver system/component must be known together")
        if self.receiver_system_id is not None:
            for name in ("receiver_system_id", "receiver_component_id"):
                require_int(getattr(self, name), name, minimum=1, maximum=255)
            if (self.receiver_system_id, self.receiver_component_id) == (1, 1):
                raise ContinuationBindingError("receiver cannot be the source PX4 endpoint")

    def validate_binding(
        self, *, current_scope: Scope, current_context: ContextSnapshot, now_s: float
    ) -> dict:
        from .onboard_continuation import CONTINUATION_ADAPTER_HASH, CONTINUATION_ADAPTER_ID

        self.__post_init__()
        if self.receiver_system_id is None:
            raise ContinuationBindingError("explicit registered GCS receiver identity required")
        scope = _scope(self.scope)
        if scope != _scope(current_scope) or scope != _scope(self.parent.scope):
            raise ContinuationBindingError("child crosses parent/current scope")
        parent = self.parent.validate_binding(
            current_scope=current_scope, current_context=current_context, now_s=now_s
        )
        mission = self.compiled_mission.executable_mission
        wire = self.compiled_mission.wire_items
        if wire != compile_px4_mission_items(mission):
            raise ContinuationBindingError("child wire differs from actual ExecutableMission")
        if (
            mission.mission_id == parent.parent_mission_id
            or mission.airframe_profile_hash != parent.airframe_profile_sha256
            or mission.airframe_adapter_id != CONTINUATION_ADAPTER_ID
            or mission.airframe_adapter_hash != CONTINUATION_ADAPTER_HASH
            or len(mission.items) != 3
            or tuple(item.command for item in mission.items)
            != (MissionCommand.WAYPOINT, MissionCommand.WAYPOINT, MissionCommand.LOITER_TIME)
            or any(item.frame is not MissionFrame.GLOBAL_RELATIVE_ALT_INT for item in mission.items)
        ):
            raise ContinuationBindingError(
                "child must be new WP anchor0/WP cursor1/LOITER body only"
            )
        anchor, target, loiter = mission.items
        position = current_context.aircraft_position
        if (
            abs(anchor.latitude_deg - position.latitude_deg) > 0.5e-7 + 1e-12
            or abs(anchor.longitude_deg - position.longitude_deg) > 0.5e-7 + 1e-12
            or abs(anchor.altitude_m - position.altitude_m) > 0.001
            or (loiter.latitude_deg, loiter.longitude_deg, loiter.altitude_m)
            != (target.latitude_deg, target.longitude_deg, target.altitude_m)
            or loiter.param1 <= 0
        ):
            raise ContinuationBindingError(
                "child anchor/loiter is not the actual current/target position"
            )
        _, identity = self.parent._journal()
        if self.child_transaction_id == identity["transaction_id"]:
            raise ContinuationBindingError("child transaction ID reused parent")
        facts = [item.to_mapping() for item in self.readback_facts]
        cursor = self.cursor_fact.to_mapping()
        if [item["message"]["message_type"] for item in facts] != [
            "MISSION_COUNT",
            *(["MISSION_ITEM_INT"] * 3),
        ] or cursor["message"]["message_type"] != "MISSION_CURRENT":
            raise ContinuationBindingError("actual ordered COUNT/INT readback and cursor required")
        previous = max(parent.observed_at_s, current_context.captured_at_s)
        ids = set()
        for raw in (*facts, cursor):
            _receive_binding(
                raw,
                scope,
                self.parent.mission_binding,
                self.child_transaction_id,
                self.trusted_source_ids,
                now_s,
                previous,
            )
            previous = raw["observed_at_s"]
            if raw["mission_sha256"] != mission.canonical_hash or raw["message_id"] in ids:
                raise ContinuationBindingError(
                    "child readback/cursor Mission or message ID mismatch"
                )
            ids.add(raw["message_id"])
        for raw in facts:
            if (raw["message"]["target_system"], raw["message"]["target_component"]) != (
                self.receiver_system_id,
                self.receiver_component_id,
            ):
                raise ContinuationBindingError("actual readback target is not registered receiver")
        currents = tuple(raw["message"]["current"] for raw in facts[1:])
        if currents not in {(1, 0, 0), (0, 1, 0)}:
            raise ContinuationBindingError("readback cursor state is not a coherent anchor0/body1")
        if cursor["message"]["seq"] != 1:
            raise ContinuationBindingError(
                "actual child cursor must be exactly navigation sequence 1"
            )
        actual = tuple(_receive_item_comparison(raw["message"]) for raw in facts[1:])
        if (
            facts[0]["message"]["count"] != 3
            or tuple(item.sequence for item in actual) != (0, 1, 2)
            or px4_wire_hash(actual) != self.compiled_mission.wire_hash
        ):
            raise ContinuationBindingError("actual INT readback does not match exact child wire")
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-continuation-child-cursor-admission-v1",
            "scope_sha256": scope.canonical_hash,
            "parent_evidence_sha256": self.parent.canonical_hash,
            "parent_binding_sha256": parent.canonical_hash,
            "child_mission_sha256": mission.canonical_hash,
            "child_wire_sha256": self.compiled_mission.wire_hash,
            "child_transaction_id": self.child_transaction_id,
            "resume_sequence": 1,
            "observed_at_s": cursor["observed_at_s"],
            "readback_execution_current_normalized": currents == (0, 1, 0),
            "readback_directional_target_normalized": True,
            "comparison_normalization_fields": ["target_system", "target_component", "current"],
            "readback_cursor_sequence": currents.index(1),
            "receiver_identity": {
                "system_id": self.receiver_system_id,
                "component_id": self.receiver_component_id,
            },
            "readback_original": [
                _encoded(item.source_receipt_bytes) for item in self.readback_facts
            ],
            "cursor_original": _encoded(self.cursor_fact.source_receipt_bytes),
            "execution_authority": False,
            "transport_created": False,
            "child_commit_permit_verified": False,
            "compile_dispatch_causal_lineage_verified": False,
            "resume_auto_mission_verified": False,
            "generation_cancellation_verified": False,
        }


__all__ = [
    "RECEIVE_CONTRACT",
    "ContinuationBindingError",
    "ContinuationReceivedFact",
    "ContinuationParentEvidence",
    "ContinuationChildEvidence",
]
