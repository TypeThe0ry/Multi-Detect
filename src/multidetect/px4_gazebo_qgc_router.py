"""Loopback-only PX4/Gazebo telemetry fan-out with bounded return paths.

The router is intentionally scoped to one owned disposable SITL process.  It
mirrors each validated PX4 MAVLink frame byte-for-byte to an isolated QGC UDP
endpoint and to an evaluator endpoint.  QGC may return only the existing
read-only allowlist from :mod:`multidetect.px4_sitl_qgc_router`; the evaluator
has a separate, identity-bound allowlist for GCS heartbeat, bounded vehicle
lifecycle commands, and the exact MAVLink Mission messages needed by the
disposable-SITL acceptance transaction.  The mission surface accepts only
mission type ``MAV_MISSION_TYPE_MISSION``, target system ``1`` and target
component ``0`` or ``1``, at most 64 items, and an explicit navigation/VTOL
command allowlist.  It never broadens QGC's read-only policy.
"""

from __future__ import annotations

import argparse
import json
import math
import select
import socket
import struct
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Final

from multidetect.px4_sitl_qgc_router import (
    QgcFrameDisposition,
    classify_qgc_frame,
)
from multidetect.qgc_readonly_bridge import (
    MSG_COMMAND_LONG,
    MSG_HEARTBEAT,
    MavlinkFrame,
    MavlinkFrameDecoder,
)

LOOPBACK_HOST: Final = "127.0.0.1"
PROTECTED_GROUND_STATION_PORT: Final = 14550
PX4_SYSTEM_ID: Final = 1
PX4_COMPONENT_ID: Final = 1
EVALUATOR_TARGET_COMPONENT_IDS: Final = frozenset({0, PX4_COMPONENT_ID})
MAXIMUM_ROUTER_DURATION_SECONDS: Final = 600.0
MAV_TYPE_GCS: Final = 6
MAV_AUTOPILOT_INVALID: Final = 8
MAV_CMD_COMPONENT_ARM_DISARM: Final = 400
MAV_CMD_DO_VTOL_TRANSITION: Final = 3000
EVALUATOR_COMMAND_ALLOWLIST: Final = frozenset(
    {MAV_CMD_COMPONENT_ARM_DISARM, MAV_CMD_DO_VTOL_TRANSITION}
)

# MAVLink common.xml message IDs used by the acceptance-only mission writer.
# Mission responses sent by PX4 are mirrored to the evaluator through the
# telemetry path; these are only the GCS->PX4 transaction messages.
MSG_SET_MODE: Final = 11
MSG_MISSION_ITEM: Final = 39
MSG_MISSION_REQUEST: Final = 40
MSG_MISSION_SET_CURRENT: Final = 41
MSG_MISSION_REQUEST_LIST: Final = 43
MSG_MISSION_COUNT: Final = 44
MSG_MISSION_ACK: Final = 47
MSG_MISSION_REQUEST_INT: Final = 51
MSG_MISSION_ITEM_INT: Final = 73

MAV_MISSION_TYPE_MISSION: Final = 0
MAV_MISSION_ACCEPTED: Final = 0
MAXIMUM_EVALUATOR_MISSION_ITEMS: Final = 64

MAV_MODE_FLAG_CUSTOM_MODE_ENABLED: Final = 1
PX4_CUSTOM_MAIN_MODE_AUTO: Final = 4
PX4_CUSTOM_SUB_MODE_AUTO_MISSION: Final = 4
PX4_AUTO_MISSION_CUSTOM_MODE: Final = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_MISSION << 24
)

MAV_FRAME_GLOBAL: Final = 0
MAV_FRAME_MISSION: Final = 2
MAV_FRAME_GLOBAL_RELATIVE_ALT: Final = 3
MAV_FRAME_GLOBAL_INT: Final = 5
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT: Final = 6
MAV_FRAME_GLOBAL_TERRAIN_ALT: Final = 10
MAV_FRAME_GLOBAL_TERRAIN_ALT_INT: Final = 11
EVALUATOR_GLOBAL_MISSION_FRAMES: Final = frozenset(
    {
        MAV_FRAME_GLOBAL,
        MAV_FRAME_GLOBAL_RELATIVE_ALT,
        MAV_FRAME_GLOBAL_TERRAIN_ALT,
    }
)
EVALUATOR_GLOBAL_MISSION_INT_FRAMES: Final = frozenset(
    {
        MAV_FRAME_GLOBAL_INT,
        MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        MAV_FRAME_GLOBAL_TERRAIN_ALT_INT,
    }
)

MAV_CMD_NAV_WAYPOINT: Final = 16
MAV_CMD_NAV_LOITER_TIME: Final = 19
MAV_CMD_NAV_RETURN_TO_LAUNCH: Final = 20
MAV_CMD_NAV_LAND: Final = 21
MAV_CMD_NAV_TAKEOFF: Final = 22
MAV_CMD_NAV_LOITER_TO_ALT: Final = 31
MAV_CMD_NAV_VTOL_TAKEOFF: Final = 84
MAV_CMD_NAV_VTOL_LAND: Final = 85
EVALUATOR_COORDINATE_MISSION_COMMANDS: Final = frozenset(
    {
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_LOITER_TIME,
        MAV_CMD_NAV_LAND,
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_LOITER_TO_ALT,
        MAV_CMD_NAV_VTOL_TAKEOFF,
        MAV_CMD_NAV_VTOL_LAND,
    }
)
EVALUATOR_MISSION_COMMAND_ALLOWLIST: Final = frozenset(
    {
        *EVALUATOR_COORDINATE_MISSION_COMMANDS,
        MAV_CMD_NAV_RETURN_TO_LAUNCH,
        MAV_CMD_DO_VTOL_TRANSITION,
    }
)
EVALUATOR_MISSION_MESSAGE_IDS: Final = frozenset(
    {
        MSG_SET_MODE,
        MSG_MISSION_ITEM,
        MSG_MISSION_REQUEST,
        MSG_MISSION_SET_CURRENT,
        MSG_MISSION_REQUEST_LIST,
        MSG_MISSION_COUNT,
        MSG_MISSION_ACK,
        MSG_MISSION_REQUEST_INT,
        MSG_MISSION_ITEM_INT,
    }
)
EVALUATOR_ALLOWED_MESSAGE_IDS: Final = frozenset(
    {MSG_HEARTBEAT, MSG_COMMAND_LONG, *EVALUATOR_MISSION_MESSAGE_IDS}
)


class Px4GazeboQgcRouterError(RuntimeError):
    """Raised when the isolated SIM router contract is invalid."""


class EvaluatorFrameDisposition(str, Enum):
    GCS_HEARTBEAT_TO_PX4 = "gcs_heartbeat_to_px4"
    CONTROL_COMMAND_TO_PX4 = "control_command_to_px4"
    MISSION_PROTOCOL_TO_PX4 = "mission_protocol_to_px4"
    BLOCKED = "blocked"


def _payload_mission_type(payload: bytes, offset: int) -> int:
    """Return MAVLink 2's optional mission_type, defaulting to base Mission.

    MAVLink 2 truncates zero-valued extension fields.  All mission messages in
    this gate use ``MAV_MISSION_TYPE_MISSION == 0``; an absent extension is
    therefore canonical and any non-zero extension fails closed.
    """

    return payload[offset] if len(payload) > offset else MAV_MISSION_TYPE_MISSION


def _target_matches(payload: bytes, system_offset: int, component_offset: int) -> bool:
    """Match the bounded PX4 target, including canonical component ``0``.

    MAVLink 2 strips zero bytes only from the payload tail.  For messages where
    ``target_component`` is the final non-extension field, an omitted byte is
    therefore the canonical wire representation of broadcast component ``0``.
    The target-system byte remains mandatory; only a missing trailing component
    is defaulted, and every caller still enforces its own message-specific
    minimum payload length before entering this helper.
    """

    if len(payload) <= system_offset or payload[system_offset] != PX4_SYSTEM_ID:
        return False
    target_component = payload[component_offset] if len(payload) > component_offset else 0
    return target_component in EVALUATOR_TARGET_COMPONENT_IDS


def _float_matches(value: float, allowed: frozenset[float]) -> bool:
    return math.isfinite(value) and any(
        math.isclose(value, candidate, rel_tol=0.0, abs_tol=1e-6) for candidate in allowed
    )


def _zero_tail(values: tuple[float, ...]) -> bool:
    return all(_float_matches(value, frozenset({0.0})) for value in values)


def _command_long_is_allowed(frame: MavlinkFrame) -> bool:
    payload = frame.payload
    # MAVLink 2 canonically truncates zero bytes from the end of a payload.
    # COMMAND_LONG's final field is ``confirmation``; pymavlink therefore emits
    # a 32-byte payload when confirmation is zero (the normal first attempt).
    # target_component=0 and confirmation=0 may both be canonically absent;
    # target_system is the last mandatory security-relevant byte.  Shorter
    # payloads still fail closed.
    if frame.message_id != MSG_COMMAND_LONG or len(payload) < 31:
        return False
    confirmation = payload[32] if len(payload) > 32 else 0
    if not _target_matches(payload, 30, 31) or confirmation != 0:
        return False
    parameters = struct.unpack_from("<7f", payload, 0)
    command = frame.command_long_id
    if command == MAV_CMD_COMPONENT_ARM_DISARM:
        return (
            _float_matches(parameters[0], frozenset({0.0, 1.0}))
            and _float_matches(parameters[1], frozenset({0.0, 21196.0}))
            and _zero_tail(parameters[2:])
        )
    if command == MAV_CMD_DO_VTOL_TRANSITION:
        return _float_matches(parameters[0], frozenset({3.0, 4.0})) and _zero_tail(parameters[1:])
    return False


def _mission_item_command_id(frame: MavlinkFrame) -> int | None:
    if frame.message_id not in {MSG_MISSION_ITEM, MSG_MISSION_ITEM_INT}:
        return None
    if len(frame.payload) < 37:
        return None
    return int.from_bytes(frame.payload[30:32], "little")


def _mission_item_sequence(frame: MavlinkFrame) -> int | None:
    if frame.message_id not in {MSG_MISSION_ITEM, MSG_MISSION_ITEM_INT}:
        return None
    if len(frame.payload) < 37:
        return None
    return int.from_bytes(frame.payload[28:30], "little")


def _mission_item_is_allowed(frame: MavlinkFrame) -> bool:
    """Validate one acceptance mission item without trusting its CRC metadata.

    The surrounding UDP source and MAVLink source IDs already bind the writer.
    Here we additionally prevent arbitrary command, actuator, payload, rally,
    fence, target, frame, or unbounded-sequence traffic from crossing the test
    channel.  Exact ordered-item/hash verification remains the transaction
    runner's responsibility.
    """

    payload = frame.payload
    if frame.message_id not in {MSG_MISSION_ITEM, MSG_MISSION_ITEM_INT}:
        return False
    if len(payload) < 37:
        return False
    sequence = int.from_bytes(payload[28:30], "little")
    command = int.from_bytes(payload[30:32], "little")
    frame_id = payload[34]
    current = payload[35]
    autocontinue = payload[36]
    if not _target_matches(payload, 32, 33):
        return False
    if _payload_mission_type(payload, 37) != MAV_MISSION_TYPE_MISSION:
        return False
    if sequence >= MAXIMUM_EVALUATOR_MISSION_ITEMS:
        return False
    if command not in EVALUATOR_MISSION_COMMAND_ALLOWLIST:
        return False
    if current != (1 if sequence == 0 else 0) or autocontinue != 1:
        return False

    param1, param2, param3, param4 = struct.unpack_from("<4f", payload, 0)
    if frame.message_id == MSG_MISSION_ITEM_INT:
        latitude_e7, longitude_e7 = struct.unpack_from("<ii", payload, 16)
        latitude = latitude_e7 / 10_000_000.0
        longitude = longitude_e7 / 10_000_000.0
        global_frames = EVALUATOR_GLOBAL_MISSION_INT_FRAMES
    else:
        latitude, longitude = struct.unpack_from("<ff", payload, 16)
        global_frames = EVALUATOR_GLOBAL_MISSION_FRAMES
    altitude_m = struct.unpack_from("<f", payload, 24)[0]
    # NaN is a protocol-defined "unspecified" value for some parameters; +/-
    # infinity is never a valid mission value.  Position altitude must be real.
    if any(math.isinf(value) for value in (param1, param2, param3, param4)):
        return False
    if not math.isfinite(altitude_m):
        return False

    if command in EVALUATOR_COORDINATE_MISSION_COMMANDS:
        return (
            frame_id in global_frames
            and math.isfinite(latitude)
            and -90.0 <= latitude <= 90.0
            and math.isfinite(longitude)
            and -180.0 <= longitude <= 180.0
            and -50.0 <= altitude_m <= 1_000.0
        )
    if command == MAV_CMD_NAV_RETURN_TO_LAUNCH:
        return (
            frame_id == MAV_FRAME_MISSION
            and math.isclose(latitude, 0.0, rel_tol=0.0, abs_tol=1e-6)
            and math.isclose(longitude, 0.0, rel_tol=0.0, abs_tol=1e-6)
            and math.isclose(altitude_m, 0.0, rel_tol=0.0, abs_tol=1e-6)
            and _zero_tail((param1, param2, param3, param4))
        )
    if command == MAV_CMD_DO_VTOL_TRANSITION:
        return (
            frame_id == MAV_FRAME_MISSION
            and _float_matches(param1, frozenset({3.0, 4.0}))
            and _zero_tail((param2, param3, param4))
            and math.isclose(latitude, 0.0, rel_tol=0.0, abs_tol=1e-6)
            and math.isclose(longitude, 0.0, rel_tol=0.0, abs_tol=1e-6)
            and math.isclose(altitude_m, 0.0, rel_tol=0.0, abs_tol=1e-6)
        )
    return False


def _mission_protocol_frame_is_allowed(frame: MavlinkFrame) -> bool:
    payload = frame.payload
    if frame.message_id == MSG_SET_MODE:
        if len(payload) < 6:
            return False
        custom_mode = int.from_bytes(payload[0:4], "little")
        return (
            payload[4] == PX4_SYSTEM_ID
            and payload[5] == MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            and custom_mode == PX4_AUTO_MISSION_CUSTOM_MODE
        )
    if frame.message_id == MSG_MISSION_COUNT:
        if len(payload) < 3 or not _target_matches(payload, 2, 3):
            return False
        count = int.from_bytes(payload[0:2], "little")
        return (
            1 <= count <= MAXIMUM_EVALUATOR_MISSION_ITEMS
            and _payload_mission_type(payload, 4) == MAV_MISSION_TYPE_MISSION
        )
    if frame.message_id in {MSG_MISSION_ITEM, MSG_MISSION_ITEM_INT}:
        return _mission_item_is_allowed(frame)
    if frame.message_id == MSG_MISSION_REQUEST_LIST:
        return (
            len(payload) >= 1
            and _target_matches(payload, 0, 1)
            and _payload_mission_type(payload, 2) == MAV_MISSION_TYPE_MISSION
        )
    if frame.message_id in {MSG_MISSION_REQUEST, MSG_MISSION_REQUEST_INT}:
        if len(payload) < 3 or not _target_matches(payload, 2, 3):
            return False
        sequence = int.from_bytes(payload[0:2], "little")
        return (
            sequence < MAXIMUM_EVALUATOR_MISSION_ITEMS
            and _payload_mission_type(payload, 4) == MAV_MISSION_TYPE_MISSION
        )
    if frame.message_id == MSG_MISSION_ACK:
        acknowledgement = payload[2] if len(payload) > 2 else MAV_MISSION_ACCEPTED
        return (
            len(payload) >= 1
            and _target_matches(payload, 0, 1)
            and acknowledgement == MAV_MISSION_ACCEPTED
            and _payload_mission_type(payload, 3) == MAV_MISSION_TYPE_MISSION
        )
    if frame.message_id == MSG_MISSION_SET_CURRENT:
        if len(payload) < 3 or not _target_matches(payload, 2, 3):
            return False
        return int.from_bytes(payload[0:2], "little") < MAXIMUM_EVALUATOR_MISSION_ITEMS
    return False


def classify_evaluator_frame(
    frame: MavlinkFrame,
    *,
    evaluator_system_id: int,
    evaluator_component_id: int,
) -> EvaluatorFrameDisposition:
    """Classify the evaluator's bounded disposable-SITL surface fail-closed."""

    _validated_mavlink_id(evaluator_system_id, "evaluator_system_id")
    _validated_mavlink_id(evaluator_component_id, "evaluator_component_id")
    if (
        frame.source_system_id != evaluator_system_id
        or frame.source_component_id != evaluator_component_id
    ):
        return EvaluatorFrameDisposition.BLOCKED
    if frame.message_id == MSG_HEARTBEAT:
        if (
            len(frame.payload) >= 9
            and frame.payload[4] == MAV_TYPE_GCS
            and frame.payload[5] == MAV_AUTOPILOT_INVALID
            and frame.payload[8] == 3
        ):
            return EvaluatorFrameDisposition.GCS_HEARTBEAT_TO_PX4
        return EvaluatorFrameDisposition.BLOCKED
    if frame.message_id == MSG_COMMAND_LONG and _command_long_is_allowed(frame):
        return EvaluatorFrameDisposition.CONTROL_COMMAND_TO_PX4
    if frame.message_id in EVALUATOR_MISSION_MESSAGE_IDS and _mission_protocol_frame_is_allowed(
        frame
    ):
        return EvaluatorFrameDisposition.MISSION_PROTOCOL_TO_PX4
    return EvaluatorFrameDisposition.BLOCKED


@dataclass(slots=True)
class RouterSummary:
    started_monotonic_s: float
    ready_event_supplied: bool = False
    stop_event_supplied: bool = False
    ready_barrier_reached_monotonic_s: float | None = None
    ready_event_set: bool = False
    all_udp_sockets_bound_before_ready: bool = False
    termination_reason: str = "not_started"
    px4_datagrams_received: int = 0
    px4_frames_received: int = 0
    px4_frames_forwarded_to_qgc: int = 0
    px4_frames_forwarded_to_evaluator: int = 0
    qgc_frames_received: int = 0
    qgc_read_only_frames_forwarded: int = 0
    qgc_operator_tunnel_frames_local_only: int = 0
    qgc_housekeeping_frames_blocked: int = 0
    qgc_forbidden_frames_blocked: int = 0
    evaluator_frames_received: int = 0
    evaluator_gcs_heartbeats_forwarded: int = 0
    evaluator_control_commands_forwarded: int = 0
    evaluator_mission_frames_forwarded: int = 0
    evaluator_mission_items_forwarded: int = 0
    evaluator_auto_mission_mode_changes_forwarded: int = 0
    evaluator_forbidden_frames_blocked: int = 0
    non_loopback_datagrams_blocked: int = 0
    unexpected_udp_endpoint_datagrams_blocked: int = 0
    udp_connection_resets: int = 0
    px4_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_forwarded_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_blocked_message_ids: Counter[int] = field(default_factory=Counter)
    evaluator_message_ids: Counter[int] = field(default_factory=Counter)
    evaluator_blocked_message_ids: Counter[int] = field(default_factory=Counter)
    evaluator_forwarded_command_ids: Counter[int] = field(default_factory=Counter)
    evaluator_blocked_command_ids: Counter[int] = field(default_factory=Counter)
    evaluator_forwarded_mission_message_ids: Counter[int] = field(default_factory=Counter)
    evaluator_forwarded_mission_command_ids: Counter[int] = field(default_factory=Counter)
    evaluator_forwarded_mission_item_sequences: Counter[int] = field(default_factory=Counter)

    def as_dict(
        self,
        *,
        qgc_port: int,
        qgc_router_port: int,
        evaluator_port: int,
        evaluator_router_port: int,
        px4_telemetry_port: int,
        px4_input_port: int,
        evaluator_system_id: int,
        evaluator_component_id: int,
        px4_discarded_bytes: int,
        qgc_discarded_bytes: int,
        evaluator_discarded_bytes: int,
    ) -> dict[str, object]:
        ready_barrier_reached = self.ready_barrier_reached_monotonic_s is not None
        ready_latency_seconds = (
            round(
                max(
                    0.0,
                    self.ready_barrier_reached_monotonic_s - self.started_monotonic_s,
                ),
                6,
            )
            if self.ready_barrier_reached_monotonic_s is not None
            else None
        )
        return {
            "schema_version": 2,
            "event": "px4_gazebo_qgc_router_finished",
            "duration_seconds": round(max(0.0, time.monotonic() - self.started_monotonic_s), 3),
            "termination_reason": self.termination_reason,
            "ready_barrier": {
                "reached": ready_barrier_reached,
                "ready_latency_seconds": ready_latency_seconds,
                "expected_udp_socket_count": 3,
                "bound_udp_socket_count": (3 if self.all_udp_sockets_bound_before_ready else 0),
                "all_udp_sockets_bound_before_ready": (self.all_udp_sockets_bound_before_ready),
                "ready_event_supplied": self.ready_event_supplied,
                "ready_event_set": self.ready_event_set,
                "stop_event_supplied": self.stop_event_supplied,
            },
            "endpoints": {
                "px4_telemetry_input": f"{LOOPBACK_HOST}:{px4_telemetry_port}",
                "px4_gcs_input": f"{LOOPBACK_HOST}:{px4_input_port}",
                "qgc": f"{LOOPBACK_HOST}:{qgc_port}",
                "qgc_router_return": f"{LOOPBACK_HOST}:{qgc_router_port}",
                "evaluator": f"{LOOPBACK_HOST}:{evaluator_port}",
                "evaluator_router_return": (f"{LOOPBACK_HOST}:{evaluator_router_port}"),
            },
            "evaluator_identity": {
                "system_id": evaluator_system_id,
                "component_id": evaluator_component_id,
            },
            "px4_datagrams_received": self.px4_datagrams_received,
            "px4_frames_received": self.px4_frames_received,
            "px4_frames_forwarded_to_qgc": self.px4_frames_forwarded_to_qgc,
            "px4_frames_forwarded_to_evaluator": (self.px4_frames_forwarded_to_evaluator),
            "qgc_frames_received": self.qgc_frames_received,
            "qgc_read_only_frames_forwarded": self.qgc_read_only_frames_forwarded,
            "qgc_operator_tunnel_frames_local_only": (self.qgc_operator_tunnel_frames_local_only),
            "qgc_housekeeping_frames_blocked": self.qgc_housekeeping_frames_blocked,
            "qgc_forbidden_frames_blocked": self.qgc_forbidden_frames_blocked,
            "evaluator_frames_received": self.evaluator_frames_received,
            "evaluator_gcs_heartbeats_forwarded": (self.evaluator_gcs_heartbeats_forwarded),
            "evaluator_control_commands_forwarded": (self.evaluator_control_commands_forwarded),
            "evaluator_mission_frames_forwarded": self.evaluator_mission_frames_forwarded,
            "evaluator_mission_items_forwarded": self.evaluator_mission_items_forwarded,
            "evaluator_auto_mission_mode_changes_forwarded": (
                self.evaluator_auto_mission_mode_changes_forwarded
            ),
            "evaluator_forbidden_frames_blocked": (self.evaluator_forbidden_frames_blocked),
            "non_loopback_datagrams_blocked": self.non_loopback_datagrams_blocked,
            "unexpected_udp_endpoint_datagrams_blocked": (
                self.unexpected_udp_endpoint_datagrams_blocked
            ),
            "udp_connection_resets": self.udp_connection_resets,
            "px4_message_ids": _counter_mapping(self.px4_message_ids),
            "qgc_message_ids": _counter_mapping(self.qgc_message_ids),
            "qgc_forwarded_message_ids": _counter_mapping(self.qgc_forwarded_message_ids),
            "qgc_blocked_message_ids": _counter_mapping(self.qgc_blocked_message_ids),
            "evaluator_message_ids": _counter_mapping(self.evaluator_message_ids),
            "evaluator_blocked_message_ids": _counter_mapping(self.evaluator_blocked_message_ids),
            "evaluator_forwarded_command_ids": _counter_mapping(
                self.evaluator_forwarded_command_ids
            ),
            "evaluator_blocked_command_ids": _counter_mapping(self.evaluator_blocked_command_ids),
            "evaluator_forwarded_mission_message_ids": _counter_mapping(
                self.evaluator_forwarded_mission_message_ids
            ),
            "evaluator_forwarded_mission_command_ids": _counter_mapping(
                self.evaluator_forwarded_mission_command_ids
            ),
            "evaluator_forwarded_mission_item_sequences": _counter_mapping(
                self.evaluator_forwarded_mission_item_sequences
            ),
            "px4_discarded_bytes": px4_discarded_bytes,
            "qgc_discarded_bytes": qgc_discarded_bytes,
            "evaluator_discarded_bytes": evaluator_discarded_bytes,
            "policy": ("qgc_read_only_plus_identity_bound_bounded_evaluator_mission_control"),
            "qgc_policy_reuses_classify_qgc_frame": True,
            "evaluator_allowed_message_ids": sorted(EVALUATOR_ALLOWED_MESSAGE_IDS),
            "evaluator_allowed_command_ids": sorted(EVALUATOR_COMMAND_ALLOWLIST),
            "evaluator_allowed_mission_message_ids": sorted(EVALUATOR_MISSION_MESSAGE_IDS),
            "evaluator_allowed_mission_command_ids": sorted(EVALUATOR_MISSION_COMMAND_ALLOWLIST),
            "evaluator_mission_type": MAV_MISSION_TYPE_MISSION,
            "evaluator_maximum_mission_items": MAXIMUM_EVALUATOR_MISSION_ITEMS,
            "evaluator_mission_target_system_id": PX4_SYSTEM_ID,
            "evaluator_mission_target_component_ids": sorted(EVALUATOR_TARGET_COMPONENT_IDS),
            "maximum_router_duration_seconds": MAXIMUM_ROUTER_DURATION_SECONDS,
            "all_udp_endpoints_loopback": True,
            "all_ports_distinct": True,
            "protected_udp_14550_used": False,
            "px4_telemetry_frames_rewritten": False,
            "qgc_parameter_writes_allowed": False,
            "qgc_flight_commands_allowed": False,
            "qgc_mission_writes_allowed": False,
            "evaluator_bounded_mission_transaction_allowed": True,
            "evaluator_mission_clear_all_allowed": False,
            "evaluator_set_mode_restricted_to_px4_auto_mission": True,
            "evaluator_arbitrary_commands_allowed": False,
            "evaluator_identity_enforced": True,
            "acknowledge_owned_disposable_sitl": True,
            "software_only": True,
            "real_pixhawk_contacted": False,
            "physical_actuator_outputs_present": False,
            "payload_commands_allowed": False,
        }


def _counter_mapping(counter: Counter[int]) -> dict[str, int]:
    """Return a canonical-JSON-safe counter mapping.

    MAVLink IDs are integers internally, but evidence objects are hashed by the
    strict autonomy serializer, which intentionally rejects non-string object
    keys.  Normalize at the router evidence boundary so an otherwise valid
    live run cannot fail only when its final bundle is canonicalized.
    """

    return {str(key): value for key, value in sorted(counter.items())}


def _validated_mavlink_id(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
        raise Px4GazeboQgcRouterError(f"{name} must be an integer in 1..255")


def _validated_duration_seconds(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 1.0 <= float(value) <= MAXIMUM_ROUTER_DURATION_SECONDS
    ):
        raise Px4GazeboQgcRouterError("duration_seconds must be in 1..600 seconds")
    return float(value)


def _validated_ports(ports: tuple[int, ...]) -> None:
    if any(
        isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535
        for port in ports
    ):
        raise Px4GazeboQgcRouterError("all UDP ports must be integers in 1024..65535")
    if len(set(ports)) != len(ports):
        raise Px4GazeboQgcRouterError("all PX4/QGC/evaluator router ports must be distinct")
    if PROTECTED_GROUND_STATION_PORT in ports:
        raise Px4GazeboQgcRouterError(
            "UDP 14550 is protected and cannot be used by owned disposable SITL"
        )


def _recv_datagram(channel: socket.socket) -> tuple[bytes, tuple[str, int]] | None:
    try:
        return channel.recvfrom(8192)
    except ConnectionResetError:
        return None


def _source_allowed(
    source: tuple[str, int],
    *,
    expected_port: int | None,
    summary: RouterSummary,
) -> bool:
    if source[0] != LOOPBACK_HOST:
        summary.non_loopback_datagrams_blocked += 1
        return False
    if expected_port is not None and source[1] != expected_port:
        summary.unexpected_udp_endpoint_datagrams_blocked += 1
        return False
    return True


def run_router(
    *,
    qgc_port: int,
    qgc_router_port: int,
    evaluator_port: int,
    evaluator_router_port: int,
    px4_telemetry_port: int,
    px4_input_port: int,
    evaluator_system_id: int,
    evaluator_component_id: int,
    duration_seconds: float,
    acknowledge_owned_disposable_sitl: bool = False,
    ready_event: Event | None = None,
    stop_event: Event | None = None,
) -> dict[str, object]:
    """Run the bounded loopback router and return a machine-readable summary.

    When supplied, ``ready_event`` is set only after all three UDP sockets are
    bound and configured for non-blocking I/O.  ``stop_event`` provides a
    graceful external stop request without weakening the bounded duration.
    """

    if acknowledge_owned_disposable_sitl is not True:
        raise Px4GazeboQgcRouterError("explicit owned-disposable-SITL acknowledgement is required")
    ports = (
        qgc_port,
        qgc_router_port,
        evaluator_port,
        evaluator_router_port,
        px4_telemetry_port,
        px4_input_port,
    )
    _validated_ports(ports)
    _validated_mavlink_id(evaluator_system_id, "evaluator_system_id")
    _validated_mavlink_id(evaluator_component_id, "evaluator_component_id")
    validated_duration_seconds = _validated_duration_seconds(duration_seconds)
    if ready_event is not None and (
        not callable(getattr(ready_event, "set", None))
        or not callable(getattr(ready_event, "clear", None))
    ):
        raise Px4GazeboQgcRouterError("ready_event must provide clear() and set()")
    if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
        raise Px4GazeboQgcRouterError("stop_event must provide is_set()")
    if ready_event is not None:
        # A caller may reuse an Event from an earlier router run.  Clear stale
        # state before opening any sockets so observers can only pass the
        # barrier after this run has completed all binds.
        ready_event.clear()

    summary = RouterSummary(
        started_monotonic_s=time.monotonic(),
        ready_event_supplied=ready_event is not None,
        stop_event_supplied=stop_event is not None,
    )
    px4_decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
    qgc_decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
    evaluator_decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
    deadline = summary.started_monotonic_s + validated_duration_seconds
    channels: list[socket.socket] = []
    try:
        px4_channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        channels.append(px4_channel)
        qgc_channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        channels.append(qgc_channel)
        evaluator_channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        channels.append(evaluator_channel)
        px4_channel.bind((LOOPBACK_HOST, px4_telemetry_port))
        qgc_channel.bind((LOOPBACK_HOST, qgc_router_port))
        evaluator_channel.bind((LOOPBACK_HOST, evaluator_router_port))
        px4_channel.setblocking(False)
        qgc_channel.setblocking(False)
        evaluator_channel.setblocking(False)
        summary.all_udp_sockets_bound_before_ready = True
        summary.ready_barrier_reached_monotonic_s = time.monotonic()
        if ready_event is not None:
            ready_event.set()
            summary.ready_event_set = True

        while True:
            if stop_event is not None and stop_event.is_set():
                summary.termination_reason = "stop_event_set"
                break
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0.0:
                summary.termination_reason = "duration_elapsed"
                break
            readable, _, _ = select.select(
                [px4_channel, qgc_channel, evaluator_channel],
                [],
                [],
                min(0.1, remaining_seconds),
            )
            if px4_channel in readable:
                received = _recv_datagram(px4_channel)
                if received is None:
                    summary.udp_connection_resets += 1
                else:
                    datagram, source = received
                    if _source_allowed(source, expected_port=None, summary=summary):
                        summary.px4_datagrams_received += 1
                        for frame in px4_decoder.feed(datagram):
                            summary.px4_frames_received += 1
                            summary.px4_message_ids[frame.message_id] += 1
                            qgc_channel.sendto(frame.encoded, (LOOPBACK_HOST, qgc_port))
                            evaluator_channel.sendto(
                                frame.encoded,
                                (LOOPBACK_HOST, evaluator_port),
                            )
                            summary.px4_frames_forwarded_to_qgc += 1
                            summary.px4_frames_forwarded_to_evaluator += 1

            if qgc_channel in readable:
                received = _recv_datagram(qgc_channel)
                if received is None:
                    summary.udp_connection_resets += 1
                else:
                    datagram, source = received
                    if _source_allowed(
                        source,
                        expected_port=qgc_port,
                        summary=summary,
                    ):
                        for frame in qgc_decoder.feed(datagram):
                            summary.qgc_frames_received += 1
                            summary.qgc_message_ids[frame.message_id] += 1
                            disposition = classify_qgc_frame(frame)
                            if disposition is QgcFrameDisposition.READ_ONLY_TO_PX4:
                                px4_channel.sendto(
                                    frame.encoded,
                                    (LOOPBACK_HOST, px4_input_port),
                                )
                                summary.qgc_read_only_frames_forwarded += 1
                                summary.qgc_forwarded_message_ids[frame.message_id] += 1
                            elif disposition is QgcFrameDisposition.OPERATOR_TUNNEL_LOCAL_ONLY:
                                summary.qgc_operator_tunnel_frames_local_only += 1
                            elif disposition is QgcFrameDisposition.BLOCKED_HOUSEKEEPING:
                                summary.qgc_housekeeping_frames_blocked += 1
                                summary.qgc_blocked_message_ids[frame.message_id] += 1
                            else:
                                summary.qgc_forbidden_frames_blocked += 1
                                summary.qgc_blocked_message_ids[frame.message_id] += 1

            if evaluator_channel in readable:
                received = _recv_datagram(evaluator_channel)
                if received is None:
                    summary.udp_connection_resets += 1
                else:
                    datagram, source = received
                    if _source_allowed(
                        source,
                        expected_port=evaluator_port,
                        summary=summary,
                    ):
                        for frame in evaluator_decoder.feed(datagram):
                            summary.evaluator_frames_received += 1
                            summary.evaluator_message_ids[frame.message_id] += 1
                            disposition = classify_evaluator_frame(
                                frame,
                                evaluator_system_id=evaluator_system_id,
                                evaluator_component_id=evaluator_component_id,
                            )
                            if disposition is EvaluatorFrameDisposition.GCS_HEARTBEAT_TO_PX4:
                                px4_channel.sendto(
                                    frame.encoded,
                                    (LOOPBACK_HOST, px4_input_port),
                                )
                                summary.evaluator_gcs_heartbeats_forwarded += 1
                            elif disposition is EvaluatorFrameDisposition.CONTROL_COMMAND_TO_PX4:
                                px4_channel.sendto(
                                    frame.encoded,
                                    (LOOPBACK_HOST, px4_input_port),
                                )
                                summary.evaluator_control_commands_forwarded += 1
                                command_id = frame.command_long_id
                                summary.evaluator_forwarded_command_ids[
                                    -1 if command_id is None else command_id
                                ] += 1
                            elif disposition is EvaluatorFrameDisposition.MISSION_PROTOCOL_TO_PX4:
                                px4_channel.sendto(
                                    frame.encoded,
                                    (LOOPBACK_HOST, px4_input_port),
                                )
                                summary.evaluator_mission_frames_forwarded += 1
                                summary.evaluator_forwarded_mission_message_ids[
                                    frame.message_id
                                ] += 1
                                if frame.message_id == MSG_SET_MODE:
                                    summary.evaluator_auto_mission_mode_changes_forwarded += 1
                                if frame.message_id in {
                                    MSG_MISSION_ITEM,
                                    MSG_MISSION_ITEM_INT,
                                }:
                                    summary.evaluator_mission_items_forwarded += 1
                                    command_id = _mission_item_command_id(frame)
                                    sequence = _mission_item_sequence(frame)
                                    summary.evaluator_forwarded_mission_command_ids[
                                        -1 if command_id is None else command_id
                                    ] += 1
                                    summary.evaluator_forwarded_mission_item_sequences[
                                        -1 if sequence is None else sequence
                                    ] += 1
                            else:
                                summary.evaluator_forbidden_frames_blocked += 1
                                summary.evaluator_blocked_message_ids[frame.message_id] += 1
                                if frame.message_id == MSG_COMMAND_LONG:
                                    command_id = frame.command_long_id
                                    summary.evaluator_blocked_command_ids[
                                        -1 if command_id is None else command_id
                                    ] += 1
    finally:
        for channel in reversed(channels):
            channel.close()

    return summary.as_dict(
        qgc_port=qgc_port,
        qgc_router_port=qgc_router_port,
        evaluator_port=evaluator_port,
        evaluator_router_port=evaluator_router_port,
        px4_telemetry_port=px4_telemetry_port,
        px4_input_port=px4_input_port,
        evaluator_system_id=evaluator_system_id,
        evaluator_component_id=evaluator_component_id,
        px4_discarded_bytes=px4_decoder.discarded_bytes,
        qgc_discarded_bytes=qgc_decoder.discarded_bytes,
        evaluator_discarded_bytes=evaluator_decoder.discarded_bytes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mirror owned PX4/Gazebo SITL telemetry to isolated QGC and evaluator "
            "endpoints with independent fail-closed return policies."
        )
    )
    parser.add_argument("--qgc-port", type=int, default=14669)
    parser.add_argument("--qgc-router-port", type=int, default=14667)
    parser.add_argument("--evaluator-port", type=int, default=14671)
    parser.add_argument("--evaluator-router-port", type=int, default=14670)
    parser.add_argument("--px4-telemetry-port", type=int, default=14668)
    parser.add_argument("--px4-input-port", type=int, default=18570)
    parser.add_argument("--evaluator-system-id", type=int, default=254)
    parser.add_argument("--evaluator-component-id", type=int, default=191)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--acknowledge-owned-disposable-sitl", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.acknowledge_owned_disposable_sitl:
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "event": "px4_gazebo_qgc_router_failed",
                    "error": ("explicit owned-disposable-SITL acknowledgement is required"),
                    "acknowledge_owned_disposable_sitl": False,
                    "software_only": True,
                    "real_pixhawk_contacted": False,
                    "physical_actuator_outputs_present": False,
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        report = run_router(
            qgc_port=args.qgc_port,
            qgc_router_port=args.qgc_router_port,
            evaluator_port=args.evaluator_port,
            evaluator_router_port=args.evaluator_router_port,
            px4_telemetry_port=args.px4_telemetry_port,
            px4_input_port=args.px4_input_port,
            evaluator_system_id=args.evaluator_system_id,
            evaluator_component_id=args.evaluator_component_id,
            duration_seconds=args.duration_seconds,
            acknowledge_owned_disposable_sitl=True,
        )
    except (OSError, Px4GazeboQgcRouterError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "event": "px4_gazebo_qgc_router_failed",
                    "error": str(error),
                    "acknowledge_owned_disposable_sitl": True,
                    "software_only": True,
                    "real_pixhawk_contacted": False,
                    "physical_actuator_outputs_present": False,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVALUATOR_ALLOWED_MESSAGE_IDS",
    "EVALUATOR_COMMAND_ALLOWLIST",
    "EVALUATOR_MISSION_COMMAND_ALLOWLIST",
    "EVALUATOR_MISSION_MESSAGE_IDS",
    "EVALUATOR_TARGET_COMPONENT_IDS",
    "EvaluatorFrameDisposition",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_DO_VTOL_TRANSITION",
    "MAV_CMD_NAV_VTOL_LAND",
    "MAV_CMD_NAV_VTOL_TAKEOFF",
    "MAXIMUM_EVALUATOR_MISSION_ITEMS",
    "MAXIMUM_ROUTER_DURATION_SECONDS",
    "MAV_TYPE_GCS",
    "MSG_MISSION_ACK",
    "MSG_MISSION_COUNT",
    "MSG_MISSION_ITEM",
    "MSG_MISSION_ITEM_INT",
    "MSG_MISSION_REQUEST",
    "MSG_MISSION_REQUEST_INT",
    "MSG_MISSION_REQUEST_LIST",
    "MSG_MISSION_SET_CURRENT",
    "MSG_SET_MODE",
    "PX4_AUTO_MISSION_CUSTOM_MODE",
    "Px4GazeboQgcRouterError",
    "RouterSummary",
    "classify_evaluator_frame",
    "main",
    "run_router",
]
