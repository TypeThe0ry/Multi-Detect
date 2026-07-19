from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from enum import IntEnum
from math import isfinite
from typing import TypeAlias
from uuid import UUID

from .approach_hil import ApproachHilPhase
from .domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    ReleaseTimingStatus,
    RuleCheck,
    Verdict,
)
from .multimodal_ranging import RangeValidity
from .operator_link import (
    APPROACH_REASON_IDS,
    OPERATOR_LINK_PROTOCOL_VERSION,
    OPERATOR_SAFETY_RULE_IDS,
    RANGING_REASON_IDS,
    RANGING_SOURCE_IDS,
    RELEASE_REASON_IDS,
    SAFETY_RULE_REGISTRY_VERSION,
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    SafetyStatusMessage,
    SceneContextRegionEntry,
    SceneContextState,
    SceneContextStatusMessage,
    SelectionAction,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from .patrol_advisory import AdvisoryValidity, PatrolPhase, ReturnObserveDirection
from .payload_target_gate import PayloadTargetEligibility
from .unified_tracking import UnifiedTrackState

MAX_TUNNEL_PAYLOAD_BYTES = 128
OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL = 42_000
AUTHENTICATION_TAG_BYTES = 16

_MAGIC = b"MD"
_HEADER = struct.Struct(">2sBBBBIQH")
_SELECTION = struct.Struct(">16s16sBIHHBHB4HBQ")
_ACK = struct.Struct(">16sBBI")
_TRACK = struct.Struct(">Q16sBIHHBBQB4HB16sBBQHhH")
_MISSION_STATUS = struct.Struct(">QQBBBBHHBQBQBhHhhH")
_SAFETY_STATUS = struct.Struct(">QQQBQIIIIB")
_AUTHORIZATION_CHALLENGE = struct.Struct(">QQQQQQIQQB")
_AUTHORIZATION_DECISION = struct.Struct(">QQQQQQQQIBQH")
_AUTHORIZATION_ACK = struct.Struct(">QBBI")
_PATROL_STATUS = struct.Struct(">QQBBBBBQ4HB16sBBHHQHHH")
_RANGE_STATUS = struct.Struct(">QQQQBBIHH6HhHHiiHHB")
_RELEASE_STATUS = struct.Struct(">QQQQBBI6i2Hh5HB")
_APPROACH_CHALLENGE = struct.Struct(">QQI16sQQB")
_APPROACH_CONFIRMATION = struct.Struct(">QQQQI16sHHBB")
_APPROACH_ACK = struct.Struct(">QBBI")
_APPROACH_STATUS = struct.Struct(">QIBI6hHHB")
_TARGET_POOL_HEADER = struct.Struct(">IBBBB")
_TARGET_POOL_ENTRY = struct.Struct(">QBB16sBB4HhHH")
_SCENE_CONTEXT_HEADER = struct.Struct(">IQQBBBB")
_SCENE_CONTEXT_ENTRY = struct.Struct(">B4H2H")
_PAYLOAD_TARGET_CHALLENGE = struct.Struct(">QQIQI16sQQB")
_PAYLOAD_TARGET_CONFIRMATION = struct.Struct(">QQQQIQI16sHHBB")
_PAYLOAD_TARGET_ACK = struct.Struct(">QBBI")
_PAYLOAD_TARGET_STATUS = struct.Struct(">16sQIQIBHB")


class OperatorProtocolError(ValueError):
    """Raised when a TUNNEL application payload is malformed or unauthenticated."""


class WireMessageType(IntEnum):
    TARGET_SELECTION = 1
    SELECTION_ACK = 2
    TRACK_STATUS = 3
    MISSION_STATUS = 4
    SAFETY_STATUS = 5
    AUTHORIZATION_CHALLENGE = 6
    AUTHORIZATION_DECISION = 7
    AUTHORIZATION_ACK = 8
    PATROL_STATUS = 9
    RANGE_STATUS = 10
    RELEASE_STATUS = 11
    APPROACH_CHALLENGE = 12
    APPROACH_CONFIRMATION = 13
    APPROACH_ACK = 14
    APPROACH_STATUS = 15
    TARGET_POOL_STATUS = 16
    SCENE_CONTEXT_STATUS = 17
    PAYLOAD_TARGET_CHALLENGE = 18
    PAYLOAD_TARGET_CONFIRMATION = 19
    PAYLOAD_TARGET_ACK = 20
    PAYLOAD_TARGET_STATUS = 21


class SelectionAckReason(IntEnum):
    ACCEPTED = 0
    STALE = 1
    STREAM_MISMATCH = 2
    GEOMETRY_MISMATCH = 3
    SEQUENCE_REJECTED = 4
    FUTURE_TIMESTAMP = 5
    COMMAND_ID_CONFLICT = 6
    INVALID = 255


class AuthorizationDecisionAckReason(IntEnum):
    ACCEPTED = 0
    NO_ACTIVE_CHALLENGE = 1
    CHALLENGE_MISMATCH = 2
    EXPIRED = 3
    SEQUENCE_REJECTED = 4
    COMMAND_TOKEN_CONFLICT = 5
    ALREADY_DECIDED = 6
    INVALID = 255


class ApproachConfirmationAckReason(IntEnum):
    ACCEPTED = 0
    NO_ACTIVE_CHALLENGE = 1
    BINDING_MISMATCH = 2
    EXPIRED = 3
    SEQUENCE_REJECTED = 4
    COMMAND_TOKEN_CONFLICT = 5
    INVALID_SLIDE = 6
    INVALID = 255


class PayloadTargetConfirmationAckReason(IntEnum):
    ACCEPTED = 0
    NO_ACTIVE_CHALLENGE = 1
    BINDING_MISMATCH = 2
    EXPIRED = 3
    SEQUENCE_REJECTED = 4
    COMMAND_TOKEN_CONFLICT = 5
    INVALID_SLIDE = 6
    INVALID = 255


@dataclass(frozen=True, slots=True)
class SelectionAck:
    command_id: str
    accepted: bool
    reason: SelectionAckReason
    acknowledged_sequence: int

    def __post_init__(self) -> None:
        _uuid_bytes(self.command_id, field_name="command_id")
        if not 0 <= self.acknowledged_sequence <= 0xFFFFFFFF:
            raise ValueError("acknowledged_sequence must fit in an unsigned 32-bit integer")
        if self.accepted and self.reason is not SelectionAckReason.ACCEPTED:
            raise ValueError("an accepted acknowledgement must use the ACCEPTED reason")
        if not self.accepted and self.reason is SelectionAckReason.ACCEPTED:
            raise ValueError("a rejected acknowledgement cannot use the ACCEPTED reason")


@dataclass(frozen=True, slots=True)
class AuthorizationDecisionAck:
    command_token: int
    accepted: bool
    reason: AuthorizationDecisionAckReason
    acknowledged_sequence: int

    def __post_init__(self) -> None:
        _bounded_uint(self.command_token, bits=64, field_name="authorization command token")
        _bounded_uint(
            self.acknowledged_sequence,
            bits=32,
            field_name="authorization acknowledged sequence",
        )
        if self.command_token == 0:
            raise ValueError("authorization command token cannot be zero")
        if self.accepted and self.reason is not AuthorizationDecisionAckReason.ACCEPTED:
            raise ValueError("accepted authorization ACK must use ACCEPTED reason")
        if not self.accepted and self.reason is AuthorizationDecisionAckReason.ACCEPTED:
            raise ValueError("rejected authorization ACK cannot use ACCEPTED reason")


@dataclass(frozen=True, slots=True)
class ApproachConfirmationAck:
    command_token: int
    accepted: bool
    reason: ApproachConfirmationAckReason
    acknowledged_sequence: int

    def __post_init__(self) -> None:
        _bounded_uint(self.command_token, bits=64, field_name="approach command token")
        _bounded_uint(
            self.acknowledged_sequence,
            bits=32,
            field_name="approach acknowledged sequence",
        )
        if self.command_token == 0:
            raise ValueError("approach command token cannot be zero")
        if self.accepted != (self.reason is ApproachConfirmationAckReason.ACCEPTED):
            raise ValueError("approach ACK acceptance and reason are inconsistent")


@dataclass(frozen=True, slots=True)
class PayloadTargetConfirmationAck:
    command_token: int
    accepted: bool
    reason: PayloadTargetConfirmationAckReason
    acknowledged_sequence: int

    def __post_init__(self) -> None:
        _bounded_uint(self.command_token, bits=64, field_name="payload target command token")
        _bounded_uint(
            self.acknowledged_sequence,
            bits=32,
            field_name="payload target acknowledged sequence",
        )
        if self.command_token == 0:
            raise ValueError("payload target command token cannot be zero")
        if self.accepted != (self.reason is PayloadTargetConfirmationAckReason.ACCEPTED):
            raise ValueError("payload target ACK acceptance and reason are inconsistent")


OperatorMessage: TypeAlias = (
    TargetSelectionCommand
    | SelectionAck
    | TrackStatusMessage
    | MissionStatusMessage
    | SafetyStatusMessage
    | AuthorizationChallengeStatusMessage
    | AuthorizationDecisionCommand
    | AuthorizationDecisionAck
    | PatrolStatusMessage
    | RangeStatusMessage
    | ReleaseStatusMessage
    | ApproachChallengeStatusMessage
    | ApproachConfirmationCommand
    | ApproachConfirmationAck
    | ApproachStatusMessage
    | TargetPoolStatusMessage
    | SceneContextStatusMessage
    | PayloadTargetChallengeStatusMessage
    | PayloadTargetConfirmationCommand
    | PayloadTargetConfirmationAck
    | PayloadTargetStatusMessage
)


@dataclass(frozen=True, slots=True)
class DecodedOperatorPacket:
    message_type: WireMessageType
    sequence: int
    sent_at_s: float
    message: OperatorMessage


class OperatorTunnelCodec:
    """Authenticated compact payload codec carried inside a MAVLink2 TUNNEL message."""

    def __init__(self, *, hmac_key: bytes, geometries: tuple[VideoGeometry, ...]) -> None:
        if len(hmac_key) < 32:
            raise ValueError("operator-link HMAC key must contain at least 32 bytes")
        if not geometries:
            raise ValueError("at least one video geometry must be registered")
        self._hmac_key = bytes(hmac_key)
        self._geometries: dict[int, VideoGeometry] = {}
        for geometry in geometries:
            stream_hash = _hash32(geometry.stream_id)
            existing = self._geometries.get(stream_hash)
            if existing is not None and existing.stream_id != geometry.stream_id:
                raise ValueError("video stream hash collision")
            self._geometries[stream_hash] = geometry

    def encode_selection(self, command: TargetSelectionCommand) -> bytes:
        geometry = command.geometry
        self._require_registered_stream(geometry.stream_id)
        action = {
            SelectionAction.SELECT: 1,
            SelectionAction.SWITCH: 2,
            SelectionAction.CANCEL: 3,
            SelectionAction.SELECT_TRK: 4,
            SelectionAction.PROMOTE_LCK: 5,
            SelectionAction.DEMOTE_TRK: 6,
            SelectionAction.CANCEL_TRK: 7,
        }[command.action]
        ttl_ms = _bounded_uint(
            round((command.expires_at_s - command.issued_at_s) * 1000.0),
            bits=16,
            field_name="selection TTL milliseconds",
        )
        if command.bbox is None:
            bbox_present = 0
            bbox = (0, 0, 0, 0)
        else:
            bbox_present = 1
            bbox = _encode_bbox(command.bbox)
        frame_present = int(command.displayed_frame_id is not None)
        frame_hash = _hash64(command.displayed_frame_id) if command.displayed_frame_id else 0
        body = _SELECTION.pack(
            _uuid_bytes(command.command_id, field_name="command_id"),
            _uuid_bytes(command.session_id, field_name="session_id"),
            action,
            _hash32(geometry.stream_id),
            geometry.width,
            geometry.height,
            _rotation_code(geometry.rotation_degrees),
            ttl_ms,
            bbox_present,
            *bbox,
            frame_present,
            frame_hash,
        )
        return self._encode_frame(
            WireMessageType.TARGET_SELECTION,
            sequence=command.sequence,
            sent_at_s=command.issued_at_s,
            body=body,
        )

    def encode_ack(self, ack: SelectionAck, *, sequence: int, sent_at_s: float) -> bytes:
        body = _ACK.pack(
            _uuid_bytes(ack.command_id, field_name="command_id"),
            int(ack.accepted),
            int(ack.reason),
            ack.acknowledged_sequence,
        )
        return self._encode_frame(
            WireMessageType.SELECTION_ACK,
            sequence=sequence,
            sent_at_s=sent_at_s,
            body=body,
        )

    def encode_track_status(self, status: TrackStatusMessage) -> bytes:
        geometry = status.geometry
        self._require_registered_stream(geometry.stream_id)
        state = {
            TrackingState.INITIALIZING: 1,
            TrackingState.TRACKING: 2,
            TrackingState.LOST: 3,
            TrackingState.CANCELLED: 4,
            TrackingState.REJECTED: 5,
        }[status.state]
        target_present = int(status.target_id is not None)
        target_hash = _hash64(status.target_id) if status.target_id else 0
        if status.bbox is None:
            bbox_present = 0
            bbox = (0, 0, 0, 0)
        else:
            bbox_present = 1
            bbox = _encode_bbox(status.bbox)
        label = (status.label or "").encode("utf-8")
        if len(label) > 16:
            raise ValueError("tracking label cannot exceed 16 UTF-8 bytes on the wire")
        confidence = _encode_ratio(status.confidence)
        quality = _encode_ratio(status.tracking_quality)
        source_age_ms = _bounded_uint(
            round((status.produced_at_s - status.source_captured_at_s) * 1000.0),
            bits=16,
            field_name="source frame age milliseconds",
        )
        bearing = _encode_bearing(status.relative_bearing_deg)
        distance = _encode_distance(status.estimated_range_m)
        body = _TRACK.pack(
            _hash64(status.status_id),
            _uuid_bytes(status.selection_command_id, field_name="selection_command_id"),
            state,
            _hash32(geometry.stream_id),
            geometry.width,
            geometry.height,
            _rotation_code(geometry.rotation_degrees),
            target_present,
            target_hash,
            bbox_present,
            *bbox,
            len(label),
            label.ljust(16, b"\0"),
            confidence,
            quality,
            _hash64(status.source_frame_id),
            source_age_ms,
            bearing,
            distance,
        )
        return self._encode_frame(
            WireMessageType.TRACK_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_mission_status(self, status: MissionStatusMessage) -> bytes:
        phase = list(MissionPhase).index(status.phase) + 1
        release_window = {
            None: 0,
            DeploymentWindowStatus.UNAVAILABLE: 1,
            DeploymentWindowStatus.WAIT: 2,
            DeploymentWindowStatus.READY: 3,
        }[status.release_window]
        safety_allowed = {None: 0, False: 1, True: 2}[status.safety_allowed]
        authorization = {
            AuthorizationDisplayState.NONE: 0,
            AuthorizationDisplayState.PENDING: 1,
            AuthorizationDisplayState.APPROVED: 2,
        }[status.authorization_state]
        target_present = int(status.target_id is not None)
        slot_present = int(status.active_payload_slot_id is not None)
        body = _MISSION_STATUS.pack(
            _hash64(status.status_id),
            _hash64(status.mission_id),
            phase,
            release_window,
            safety_allowed,
            authorization,
            status.remaining_payload_count,
            status.total_payload_count,
            target_present,
            _hash64(status.target_id) if status.target_id else 0,
            slot_present,
            _hash64(status.active_payload_slot_id) if status.active_payload_slot_id else 0,
            _encode_ratio(status.target_confidence),
            _encode_bearing(status.relative_bearing_deg),
            _encode_distance(status.estimated_range_m),
            _encode_signed_distance(status.cross_track_error_m),
            _encode_signed_distance(status.along_track_error_m),
            _encode_distance(status.release_lead_distance_m),
        )
        return self._encode_frame(
            WireMessageType.MISSION_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_safety_status(self, status: SafetyStatusMessage) -> bytes:
        index_by_rule = {rule_id: index for index, rule_id in enumerate(OPERATOR_SAFETY_RULE_IDS)}
        present_mask = pass_mask = deny_mask = unknown_mask = 0
        for check in status.checks:
            bit = 1 << index_by_rule[check.rule_id]
            present_mask |= bit
            if check.verdict is Verdict.PASS:
                pass_mask |= bit
            elif check.verdict is Verdict.DENY:
                deny_mask |= bit
            else:
                unknown_mask |= bit
        body = _SAFETY_STATUS.pack(
            _hash64(status.status_id),
            _hash64(status.mission_id),
            _hash64(status.ruleset_version),
            1,
            _hash64(status.target_id),
            present_mask,
            pass_mask,
            deny_mask,
            unknown_mask,
            status.registry_version,
        )
        return self._encode_frame(
            WireMessageType.SAFETY_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_patrol_status(self, status: PatrolStatusMessage) -> bytes:
        target_present = status.primary_target_id is not None
        bbox_present = status.bbox is not None
        return_present = status.return_direction is not None
        flags = int(target_present) | (int(bbox_present) << 1) | (int(return_present) << 2)
        bbox = _encode_bbox(status.bbox) if status.bbox is not None else (0, 0, 0, 0)
        label = (status.label or "").encode("utf-8")
        if len(label) > 16:
            raise ValueError("patrol target label cannot exceed 16 UTF-8 bytes on the wire")
        phase = list(PatrolPhase).index(status.phase) + 1
        target_state = (
            list(UnifiedTrackState).index(status.target_state) + 1
            if status.target_state is not None
            else 0
        )
        direction = {
            None: 0,
            ReturnObserveDirection.LEFT: 1,
            ReturnObserveDirection.RIGHT: 2,
            ReturnObserveDirection.ROUTE_REQUIRED: 3,
        }[status.return_direction]
        validity = {
            None: 0,
            AdvisoryValidity.VALID: 1,
            AdvisoryValidity.DEGRADED: 2,
            AdvisoryValidity.INVALID: 3,
        }[status.return_validity]
        source_age_ms = _bounded_uint(
            round((status.produced_at_s - status.source_captured_at_s) * 1000.0),
            bits=16,
            field_name="patrol source frame age milliseconds",
        )
        body = _PATROL_STATUS.pack(
            _hash64(status.status_id),
            _hash64(status.mission_id),
            phase,
            target_state,
            flags,
            direction,
            validity,
            _hash64(status.primary_target_id) if status.primary_target_id else 0,
            *bbox,
            len(label),
            label.ljust(16, b"\0"),
            _encode_ratio(status.confidence),
            _encode_ratio(status.tracking_quality),
            status.total_track_count,
            status.locked_track_count,
            _hash64(status.source_frame_id),
            source_age_ms,
            _encode_duration(status.return_evidence_age_s),
            _encode_distance(status.estimated_minimum_turn_radius_m),
        )
        return self._encode_frame(
            WireMessageType.PATROL_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_range_status(self, status: RangeStatusMessage) -> bytes:
        validity = {
            RangeValidity.VALID: 1,
            RangeValidity.DEGRADED: 2,
            RangeValidity.INVALID: 3,
        }[status.validity]
        reason_mask = _registry_mask(status.reasons, RANGING_REASON_IDS, "ranging reason")
        source_mask = _registry_mask(status.sources, RANGING_SOURCE_IDS, "ranging source")
        rejected_source_mask = _registry_mask(
            status.rejected_sources,
            RANGING_SOURCE_IDS,
            "rejected ranging source",
        )
        slant_ci = status.slant_range_ci95_m or (None, None)
        ground_ci = status.ground_range_ci95_m or (None, None)
        source_age_ms = _bounded_uint(
            round((status.produced_at_s - status.source_captured_at_s) * 1000.0),
            bits=16,
            field_name="ranging source frame age milliseconds",
        )
        body = _RANGE_STATUS.pack(
            _hash64(status.status_id),
            _hash64(status.target_id),
            _hash64(status.calibration_id),
            _hash64(status.source_frame_id),
            validity,
            0,
            reason_mask,
            source_mask,
            rejected_source_mask,
            _encode_distance(status.slant_range_m),
            _encode_distance(status.ground_range_m),
            _encode_distance(slant_ci[0]),
            _encode_distance(slant_ci[1]),
            _encode_distance(ground_ci[0]),
            _encode_distance(ground_ci[1]),
            _encode_bearing(status.relative_bearing_deg),
            _encode_unsigned_bearing(status.absolute_bearing_deg),
            _encode_unsigned_centidegrees(status.bearing_sigma_deg),
            _encode_signed_distance32(status.north_offset_m),
            _encode_signed_distance32(status.east_offset_m),
            source_age_ms,
            _encode_duration(status.data_freshness_s),
            _encode_ratio(status.sensor_consistency),
        )
        return self._encode_frame(
            WireMessageType.RANGE_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_release_status(self, status: ReleaseStatusMessage) -> bytes:
        timing = {
            ReleaseTimingStatus.INVALID: 1,
            ReleaseTimingStatus.TOO_EARLY: 2,
            ReleaseTimingStatus.WINDOW: 3,
            ReleaseTimingStatus.TOO_LATE: 4,
        }[status.timing_status]
        binding_present = status.range_target_id is not None
        range_ci = status.ground_range_ci95_m or (None, None)
        body = _RELEASE_STATUS.pack(
            _hash64(status.target_id),
            _hash64(status.range_target_id) if status.range_target_id is not None else 0,
            _hash64(status.range_frame_id) if status.range_frame_id is not None else 0,
            _hash64(status.calibration_id),
            timing,
            int(binding_present),
            _registry_mask(status.reasons, RELEASE_REASON_IDS, "release reason"),
            _encode_signed_distance32(status.target_north_offset_m),
            _encode_signed_distance32(status.target_east_offset_m),
            _encode_signed_distance32(status.impact_north_offset_m),
            _encode_signed_distance32(status.impact_east_offset_m),
            _encode_signed_distance32(status.along_track_error_m),
            _encode_signed_distance32(status.cross_track_error_m),
            _encode_distance(status.error_ellipse_major_m),
            _encode_distance(status.error_ellipse_minor_m),
            _encode_bearing(status.error_ellipse_orientation_deg),
            _encode_distance(status.estimated_ground_range_m),
            _encode_distance(range_ci[0]),
            _encode_distance(range_ci[1]),
            _encode_duration(status.payload_descent_time_s),
            _encode_distance(status.release_lead_distance_m),
            _encode_ratio(status.range_sensor_consistency),
        )
        return self._encode_frame(
            WireMessageType.RELEASE_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_approach_challenge(self, status: ApproachChallengeStatusMessage) -> bytes:
        body = _APPROACH_CHALLENGE.pack(
            status.challenge_token,
            status.target_token,
            status.target_revision,
            _uuid_bytes(status.selection_command_id, field_name="selection_command_id"),
            _timestamp_ms(status.issued_at_s),
            _timestamp_ms(status.expires_at_s),
            int(status.pending),
        )
        return self._encode_frame(
            WireMessageType.APPROACH_CHALLENGE,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_approach_confirmation(self, command: ApproachConfirmationCommand) -> bytes:
        ttl_ms = _bounded_uint(
            round((command.expires_at_s - command.issued_at_s) * 1000.0),
            bits=16,
            field_name="approach confirmation TTL milliseconds",
        )
        duration_ms = _bounded_uint(
            round(command.slide_duration_s * 1000.0),
            bits=16,
            field_name="approach slide duration milliseconds",
        )
        body = _APPROACH_CONFIRMATION.pack(
            command.command_token,
            command.session_token,
            command.challenge_token,
            command.target_token,
            command.target_revision,
            _uuid_bytes(command.selection_command_id, field_name="selection_command_id"),
            ttl_ms,
            duration_ms,
            _encode_ratio(command.completion_fraction),
            int(command.continuous),
        )
        return self._encode_frame(
            WireMessageType.APPROACH_CONFIRMATION,
            sequence=command.sequence,
            sent_at_s=command.issued_at_s,
            body=body,
        )

    def encode_approach_ack(
        self,
        acknowledgement: ApproachConfirmationAck,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> bytes:
        body = _APPROACH_ACK.pack(
            acknowledgement.command_token,
            int(acknowledgement.accepted),
            int(acknowledgement.reason),
            acknowledgement.acknowledged_sequence,
        )
        return self._encode_frame(
            WireMessageType.APPROACH_ACK,
            sequence=sequence,
            sent_at_s=sent_at_s,
            body=body,
        )

    def encode_approach_status(self, status: ApproachStatusMessage) -> bytes:
        phases = list(ApproachHilPhase)
        expiry_s = (
            max(0.0, status.confirmation_expires_at_s - status.produced_at_s)
            if status.confirmation_expires_at_s is not None
            else None
        )
        body = _APPROACH_STATUS.pack(
            _hash64(status.target_id) if status.target_id is not None else 0,
            status.target_revision or 0,
            phases.index(status.phase) + 1,
            _registry_mask(status.reasons, APPROACH_REASON_IDS, "approach reason"),
            _encode_bearing(status.yaw_error_deg),
            _encode_bearing(status.pitch_error_deg),
            _encode_bearing(status.yaw_advice_deg),
            _encode_bearing(status.pitch_advice_deg),
            _encode_bearing(status.bank_advice_deg),
            _encode_bearing(status.climb_pitch_advice_deg),
            _encode_distance(status.ground_range_m),
            _encode_duration(expiry_s),
            int(status.target_id is not None)
            | (int(status.flight_control_enabled) << 1)
            | (int(status.aim_control_active) << 2)
            | (int(status.pilot_input_cancelled) << 3),
        )
        return self._encode_frame(
            WireMessageType.APPROACH_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage,
    ) -> bytes:
        body = _PAYLOAD_TARGET_CHALLENGE.pack(
            status.challenge_token,
            status.selected_target_token,
            status.selected_target_revision,
            status.aimpoint_target_token,
            status.aimpoint_target_revision,
            _uuid_bytes(status.selection_command_id, field_name="selection_command_id"),
            _timestamp_ms(status.issued_at_s),
            _timestamp_ms(status.expires_at_s),
            int(status.pending),
        )
        return self._encode_frame(
            WireMessageType.PAYLOAD_TARGET_CHALLENGE,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_payload_target_confirmation(
        self,
        command: PayloadTargetConfirmationCommand,
    ) -> bytes:
        ttl_ms = _bounded_uint(
            round((command.expires_at_s - command.issued_at_s) * 1000.0),
            bits=16,
            field_name="payload target confirmation TTL milliseconds",
        )
        duration_ms = _bounded_uint(
            round(command.slide_duration_s * 1000.0),
            bits=16,
            field_name="payload target slide duration milliseconds",
        )
        body = _PAYLOAD_TARGET_CONFIRMATION.pack(
            command.command_token,
            command.session_token,
            command.challenge_token,
            command.selected_target_token,
            command.selected_target_revision,
            command.aimpoint_target_token,
            command.aimpoint_target_revision,
            _uuid_bytes(command.selection_command_id, field_name="selection_command_id"),
            ttl_ms,
            duration_ms,
            _encode_ratio(command.completion_fraction),
            int(command.continuous),
        )
        return self._encode_frame(
            WireMessageType.PAYLOAD_TARGET_CONFIRMATION,
            sequence=command.sequence,
            sent_at_s=command.issued_at_s,
            body=body,
        )

    def encode_payload_target_ack(
        self,
        acknowledgement: PayloadTargetConfirmationAck,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> bytes:
        body = _PAYLOAD_TARGET_ACK.pack(
            acknowledgement.command_token,
            int(acknowledgement.accepted),
            int(acknowledgement.reason),
            acknowledgement.acknowledged_sequence,
        )
        return self._encode_frame(
            WireMessageType.PAYLOAD_TARGET_ACK,
            sequence=sequence,
            sent_at_s=sent_at_s,
            body=body,
        )

    def encode_payload_target_status(self, status: PayloadTargetStatusMessage) -> bytes:
        eligibility = list(PayloadTargetEligibility).index(status.eligibility) + 1
        expiry_s = (
            max(0.0, status.confirmation_expires_at_s - status.produced_at_s)
            if status.confirmation_expires_at_s is not None
            else None
        )
        flags = (
            int(status.aimpoint_target_token is not None)
            | (int(status.confirmation_pending) << 1)
            | (int(status.confirmation_accepted) << 2)
        )
        body = _PAYLOAD_TARGET_STATUS.pack(
            _uuid_bytes(status.selection_command_id, field_name="selection_command_id"),
            status.selected_target_token,
            status.selected_target_revision,
            status.aimpoint_target_token or 0,
            status.aimpoint_target_revision or 0,
            eligibility,
            _encode_duration(expiry_s),
            flags,
        )
        return self._encode_frame(
            WireMessageType.PAYLOAD_TARGET_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_target_pool_status(self, status: TargetPoolStatusMessage) -> bytes:
        body = bytearray(
            _TARGET_POOL_HEADER.pack(
                status.pool_revision,
                status.page_index,
                status.page_count,
                status.total_track_count,
                len(status.entries),
            )
        )
        states = list(UnifiedTrackState)
        for entry in status.entries:
            label = entry.label.encode("utf-8")
            if len(label) > 16:
                raise ValueError("target-pool label cannot exceed 16 UTF-8 bytes on the wire")
            flags = (
                int(entry.locked)
                | (int(entry.primary) << 1)
                | (int(entry.actionable) << 2)
                | (int(entry.reid_confirmed) << 3)
                | (int(entry.bbox is not None) << 4)
                | (int(entry.operator_tracked) << 5)
            )
            bbox = _encode_bbox(entry.bbox) if entry.bbox is not None else (0, 0, 0, 0)
            body.extend(
                _TARGET_POOL_ENTRY.pack(
                    _hash64(entry.target_id),
                    states.index(entry.state) + 1,
                    flags,
                    label.ljust(16, b"\0"),
                    _encode_ratio(entry.confidence),
                    _encode_ratio(entry.tracking_quality),
                    *bbox,
                    _encode_bearing(entry.relative_bearing_deg),
                    _encode_distance(entry.estimated_range_m),
                    _encode_distance(entry.target_speed_mps),
                )
            )
        return self._encode_frame(
            WireMessageType.TARGET_POOL_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=bytes(body),
        )

    def encode_scene_context_status(self, status: SceneContextStatusMessage) -> bytes:
        states = list(SceneContextState)
        body = bytearray(
            _SCENE_CONTEXT_HEADER.pack(
                status.context_revision,
                _hash64(status.source_frame_id),
                _timestamp_ms(status.source_captured_at_s),
                states.index(status.state) + 1,
                status.page_index,
                status.page_count,
                status.total_region_count,
            )
        )
        label_codes = {"road": 1, "building": 2}
        for entry in status.entries:
            body.extend(
                _SCENE_CONTEXT_ENTRY.pack(
                    label_codes[entry.label],
                    *_encode_bbox(entry.bbox),
                    round(entry.frame_area_fraction * 65535.0),
                    round(entry.bbox_fill_fraction * 65535.0),
                )
            )
        return self._encode_frame(
            WireMessageType.SCENE_CONTEXT_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=bytes(body),
        )

    def encode_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
    ) -> bytes:
        body = _AUTHORIZATION_CHALLENGE.pack(
            status.challenge_token,
            status.mission_token,
            status.target_token,
            status.scene_token,
            status.ruleset_token,
            status.payload_slot_token,
            status.target_revision,
            _timestamp_ms(status.created_at_s),
            _timestamp_ms(status.expires_at_s),
            int(status.pending),
        )
        return self._encode_frame(
            WireMessageType.AUTHORIZATION_CHALLENGE,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def encode_authorization_decision(self, command: AuthorizationDecisionCommand) -> bytes:
        decision = {
            AuthorizationDecision.APPROVE: 1,
            AuthorizationDecision.DENY: 2,
        }[command.decision]
        ttl_ms = _bounded_uint(
            round((command.expires_at_s - command.issued_at_s) * 1000.0),
            bits=16,
            field_name="authorization decision TTL milliseconds",
        )
        body = _AUTHORIZATION_DECISION.pack(
            command.command_token,
            command.session_token,
            command.challenge_token,
            command.mission_token,
            command.target_token,
            command.scene_token,
            command.ruleset_token,
            command.payload_slot_token,
            command.target_revision,
            decision,
            command.operator_token,
            ttl_ms,
        )
        return self._encode_frame(
            WireMessageType.AUTHORIZATION_DECISION,
            sequence=command.sequence,
            sent_at_s=command.issued_at_s,
            body=body,
        )

    def encode_authorization_ack(
        self,
        acknowledgement: AuthorizationDecisionAck,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> bytes:
        body = _AUTHORIZATION_ACK.pack(
            acknowledgement.command_token,
            int(acknowledgement.accepted),
            int(acknowledgement.reason),
            acknowledgement.acknowledged_sequence,
        )
        return self._encode_frame(
            WireMessageType.AUTHORIZATION_ACK,
            sequence=sequence,
            sent_at_s=sent_at_s,
            body=body,
        )

    def decode(self, payload: bytes) -> DecodedOperatorPacket:
        if len(payload) < _HEADER.size + AUTHENTICATION_TAG_BYTES:
            raise OperatorProtocolError("operator-link payload is truncated")
        if len(payload) > MAX_TUNNEL_PAYLOAD_BYTES:
            raise OperatorProtocolError("operator-link payload exceeds MAVLink TUNNEL capacity")
        try:
            magic, version, type_value, flags, reserved, sequence, sent_ms, body_length = (
                _HEADER.unpack_from(payload)
            )
        except struct.error as exc:  # pragma: no cover - guarded by the length check
            raise OperatorProtocolError("operator-link header is malformed") from exc
        if magic != _MAGIC:
            raise OperatorProtocolError("operator-link magic does not match")
        if version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise OperatorProtocolError("unsupported operator-link wire version")
        if flags != 0 or reserved != 0:
            raise OperatorProtocolError("unsupported operator-link flags")
        expected_length = _HEADER.size + body_length + AUTHENTICATION_TAG_BYTES
        if len(payload) != expected_length:
            raise OperatorProtocolError("operator-link body length does not match the frame")
        signed = payload[:-AUTHENTICATION_TAG_BYTES]
        supplied_tag = payload[-AUTHENTICATION_TAG_BYTES:]
        expected_tag = hmac.digest(self._hmac_key, signed, "sha256")[:AUTHENTICATION_TAG_BYTES]
        if not hmac.compare_digest(supplied_tag, expected_tag):
            raise OperatorProtocolError("operator-link authentication failed")
        try:
            message_type = WireMessageType(type_value)
        except ValueError as exc:
            raise OperatorProtocolError("unknown operator-link message type") from exc
        body = payload[_HEADER.size : -AUTHENTICATION_TAG_BYTES]
        sent_at_s = sent_ms / 1000.0
        if message_type is WireMessageType.TARGET_SELECTION:
            message = self._decode_selection(body, sequence=sequence, sent_at_s=sent_at_s)
        elif message_type is WireMessageType.SELECTION_ACK:
            message = self._decode_ack(body)
        elif message_type is WireMessageType.TRACK_STATUS:
            message = self._decode_track(body, sequence=sequence, sent_at_s=sent_at_s)
        elif message_type is WireMessageType.MISSION_STATUS:
            message = self._decode_mission_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.SAFETY_STATUS:
            message = self._decode_safety_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.AUTHORIZATION_CHALLENGE:
            message = self._decode_authorization_challenge(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.AUTHORIZATION_DECISION:
            message = self._decode_authorization_decision(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.AUTHORIZATION_ACK:
            message = self._decode_authorization_ack(body)
        elif message_type is WireMessageType.PATROL_STATUS:
            message = self._decode_patrol_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.RANGE_STATUS:
            message = self._decode_range_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.RELEASE_STATUS:
            message = self._decode_release_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.APPROACH_CHALLENGE:
            message = self._decode_approach_challenge(body, sequence=sequence, sent_at_s=sent_at_s)
        elif message_type is WireMessageType.APPROACH_CONFIRMATION:
            message = self._decode_approach_confirmation(
                body, sequence=sequence, sent_at_s=sent_at_s
            )
        elif message_type is WireMessageType.APPROACH_ACK:
            message = self._decode_approach_ack(body)
        elif message_type is WireMessageType.APPROACH_STATUS:
            message = self._decode_approach_status(body, sequence=sequence, sent_at_s=sent_at_s)
        elif message_type is WireMessageType.TARGET_POOL_STATUS:
            message = self._decode_target_pool_status(body, sequence=sequence, sent_at_s=sent_at_s)
        elif message_type is WireMessageType.SCENE_CONTEXT_STATUS:
            message = self._decode_scene_context_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.PAYLOAD_TARGET_CHALLENGE:
            message = self._decode_payload_target_challenge(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.PAYLOAD_TARGET_CONFIRMATION:
            message = self._decode_payload_target_confirmation(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        elif message_type is WireMessageType.PAYLOAD_TARGET_ACK:
            message = self._decode_payload_target_ack(body)
        else:
            message = self._decode_payload_target_status(
                body,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        return DecodedOperatorPacket(message_type, sequence, sent_at_s, message)

    def _encode_frame(
        self,
        message_type: WireMessageType,
        *,
        sequence: int,
        sent_at_s: float,
        body: bytes,
    ) -> bytes:
        sequence = _bounded_uint(sequence, bits=32, field_name="wire sequence")
        sent_ms = _timestamp_ms(sent_at_s)
        header = _HEADER.pack(
            _MAGIC,
            OPERATOR_LINK_PROTOCOL_VERSION,
            int(message_type),
            0,
            0,
            sequence,
            sent_ms,
            len(body),
        )
        signed = header + body
        tag = hmac.digest(self._hmac_key, signed, "sha256")[:AUTHENTICATION_TAG_BYTES]
        frame = signed + tag
        if len(frame) > MAX_TUNNEL_PAYLOAD_BYTES:
            raise ValueError("encoded operator-link frame exceeds MAVLink TUNNEL capacity")
        return frame

    def _decode_selection(
        self, body: bytes, *, sequence: int, sent_at_s: float
    ) -> TargetSelectionCommand:
        if len(body) != _SELECTION.size:
            raise OperatorProtocolError("target-selection body has an invalid size")
        (
            command_id,
            session_id,
            action_value,
            stream_hash,
            width,
            height,
            rotation,
            ttl_ms,
            bbox_present,
            x1,
            y1,
            x2,
            y2,
            frame_present,
            frame_hash,
        ) = _SELECTION.unpack(body)
        action = {
            1: SelectionAction.SELECT,
            2: SelectionAction.SWITCH,
            3: SelectionAction.CANCEL,
            4: SelectionAction.SELECT_TRK,
            5: SelectionAction.PROMOTE_LCK,
            6: SelectionAction.DEMOTE_TRK,
            7: SelectionAction.CANCEL_TRK,
        }.get(action_value)
        if action is None:
            raise OperatorProtocolError("target-selection action is invalid")
        geometry = self._wire_geometry(stream_hash, width, height, rotation)
        if bbox_present not in {0, 1} or frame_present not in {0, 1}:
            raise OperatorProtocolError("target-selection presence flag is invalid")
        bbox = _decode_bbox((x1, y1, x2, y2)) if bbox_present else None
        frame_id = _hashed_identifier(frame_hash) if frame_present else None
        try:
            return TargetSelectionCommand(
                command_id=str(UUID(bytes=command_id)),
                session_id=str(UUID(bytes=session_id)),
                sequence=sequence,
                action=action,
                geometry=geometry,
                issued_at_s=sent_at_s,
                expires_at_s=sent_at_s + ttl_ms / 1000.0,
                bbox=bbox,
                displayed_frame_id=frame_id,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid target-selection content: {exc}") from exc

    def _decode_ack(self, body: bytes) -> SelectionAck:
        if len(body) != _ACK.size:
            raise OperatorProtocolError("selection-ack body has an invalid size")
        command_id, accepted, reason_value, acknowledged_sequence = _ACK.unpack(body)
        if accepted not in {0, 1}:
            raise OperatorProtocolError("selection-ack accepted flag is invalid")
        try:
            reason = SelectionAckReason(reason_value)
            return SelectionAck(
                command_id=str(UUID(bytes=command_id)),
                accepted=bool(accepted),
                reason=reason,
                acknowledged_sequence=acknowledged_sequence,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid selection acknowledgement: {exc}") from exc

    def _decode_track(self, body: bytes, *, sequence: int, sent_at_s: float) -> TrackStatusMessage:
        if len(body) != _TRACK.size:
            raise OperatorProtocolError("track-status body has an invalid size")
        (
            status_id,
            command_id,
            state_value,
            stream_hash,
            width,
            height,
            rotation,
            target_present,
            target_hash,
            bbox_present,
            x1,
            y1,
            x2,
            y2,
            label_length,
            label_bytes,
            confidence,
            quality,
            frame_hash,
            frame_age_ms,
            bearing,
            distance,
        ) = _TRACK.unpack(body)
        state = {
            1: TrackingState.INITIALIZING,
            2: TrackingState.TRACKING,
            3: TrackingState.LOST,
            4: TrackingState.CANCELLED,
            5: TrackingState.REJECTED,
        }.get(state_value)
        if state is None:
            raise OperatorProtocolError("track-status state is invalid")
        if target_present not in {0, 1} or bbox_present not in {0, 1}:
            raise OperatorProtocolError("track-status presence flag is invalid")
        if label_length > len(label_bytes):
            raise OperatorProtocolError("track-status label length is invalid")
        try:
            label = label_bytes[:label_length].decode("utf-8") or None
        except UnicodeDecodeError as exc:
            raise OperatorProtocolError("track-status label is not valid UTF-8") from exc
        geometry = self._wire_geometry(stream_hash, width, height, rotation)
        target_id = _hashed_identifier(target_hash) if target_present else None
        bbox = _decode_bbox((x1, y1, x2, y2)) if bbox_present else None
        try:
            return TrackStatusMessage(
                status_id=_hashed_identifier(status_id),
                selection_command_id=str(UUID(bytes=command_id)),
                sequence=sequence,
                geometry=geometry,
                state=state,
                target_id=target_id,
                bbox=bbox,
                label=label,
                confidence=_decode_ratio(confidence),
                tracking_quality=_decode_ratio(quality),
                source_frame_id=_hashed_identifier(frame_hash),
                source_captured_at_s=sent_at_s - frame_age_ms / 1000.0,
                produced_at_s=sent_at_s,
                relative_bearing_deg=_decode_bearing(bearing),
                estimated_range_m=_decode_distance(distance),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid track-status content: {exc}") from exc

    def _decode_mission_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> MissionStatusMessage:
        if len(body) != _MISSION_STATUS.size:
            raise OperatorProtocolError("mission-status body has an invalid size")
        (
            status_hash,
            mission_hash,
            phase_value,
            window_value,
            safety_value,
            authorization_value,
            remaining_payloads,
            total_payloads,
            target_present,
            target_hash,
            slot_present,
            slot_hash,
            confidence,
            bearing,
            estimated_range,
            cross_track,
            along_track,
            release_lead,
        ) = _MISSION_STATUS.unpack(body)
        phases = tuple(MissionPhase)
        if not 1 <= phase_value <= len(phases):
            raise OperatorProtocolError("mission-status phase is invalid")
        release_window = {
            0: None,
            1: DeploymentWindowStatus.UNAVAILABLE,
            2: DeploymentWindowStatus.WAIT,
            3: DeploymentWindowStatus.READY,
        }.get(window_value, ...)
        if release_window is ...:
            raise OperatorProtocolError("mission-status release window is invalid")
        safety_allowed = {0: None, 1: False, 2: True}.get(safety_value, ...)
        if safety_allowed is ...:
            raise OperatorProtocolError("mission-status safety state is invalid")
        authorization = {
            0: AuthorizationDisplayState.NONE,
            1: AuthorizationDisplayState.PENDING,
            2: AuthorizationDisplayState.APPROVED,
        }.get(authorization_value)
        if authorization is None:
            raise OperatorProtocolError("mission-status authorization state is invalid")
        if target_present not in {0, 1} or slot_present not in {0, 1}:
            raise OperatorProtocolError("mission-status presence flag is invalid")
        try:
            return MissionStatusMessage(
                status_id=_hashed_identifier(status_hash),
                sequence=sequence,
                mission_id=_hashed_identifier(mission_hash),
                phase=phases[phase_value - 1],
                authorization_state=authorization,
                release_window=release_window,
                safety_allowed=safety_allowed,
                remaining_payload_count=remaining_payloads,
                total_payload_count=total_payloads,
                target_id=_hashed_identifier(target_hash) if target_present else None,
                active_payload_slot_id=(_hashed_identifier(slot_hash) if slot_present else None),
                target_confidence=_decode_ratio(confidence),
                relative_bearing_deg=_decode_bearing(bearing),
                estimated_range_m=_decode_distance(estimated_range),
                cross_track_error_m=_decode_signed_distance(cross_track),
                along_track_error_m=_decode_signed_distance(along_track),
                release_lead_distance_m=_decode_distance(release_lead),
                produced_at_s=sent_at_s,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid mission-status content: {exc}") from exc

    def _decode_safety_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> SafetyStatusMessage:
        if len(body) != _SAFETY_STATUS.size:
            raise OperatorProtocolError("safety-status body has an invalid size")
        (
            status_hash,
            mission_hash,
            ruleset_hash,
            target_present,
            target_hash,
            present_mask,
            pass_mask,
            deny_mask,
            unknown_mask,
            registry_version,
        ) = _SAFETY_STATUS.unpack(body)
        if registry_version != SAFETY_RULE_REGISTRY_VERSION:
            raise OperatorProtocolError("safety-status registry version is unsupported")
        if target_present != 1:
            raise OperatorProtocolError("safety-status target must be present")
        verdict_union = pass_mask | deny_mask | unknown_mask
        if verdict_union != present_mask:
            raise OperatorProtocolError("safety-status verdict masks do not match present rules")
        if (pass_mask & deny_mask) or (pass_mask & unknown_mask) or (deny_mask & unknown_mask):
            raise OperatorProtocolError("safety-status verdict masks overlap")
        if present_mask >> len(OPERATOR_SAFETY_RULE_IDS):
            raise OperatorProtocolError("safety-status contains an unregistered rule bit")
        checks: list[RuleCheck] = []
        for index, rule_id in enumerate(OPERATOR_SAFETY_RULE_IDS):
            bit = 1 << index
            if not present_mask & bit:
                continue
            if pass_mask & bit:
                verdict = Verdict.PASS
            elif deny_mask & bit:
                verdict = Verdict.DENY
            else:
                verdict = Verdict.UNKNOWN
            checks.append(RuleCheck(rule_id, verdict, f"remote safety verdict: {verdict.value}"))
        try:
            return SafetyStatusMessage(
                status_id=_hashed_identifier(status_hash),
                sequence=sequence,
                mission_id=_hashed_identifier(mission_hash),
                target_id=_hashed_identifier(target_hash),
                ruleset_version=_hashed_identifier(ruleset_hash),
                checks=tuple(checks),
                produced_at_s=sent_at_s,
                registry_version=registry_version,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid safety-status content: {exc}") from exc

    def _decode_authorization_challenge(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> AuthorizationChallengeStatusMessage:
        if len(body) != _AUTHORIZATION_CHALLENGE.size:
            raise OperatorProtocolError("authorization-challenge body has an invalid size")
        (
            challenge_token,
            mission_token,
            target_token,
            scene_token,
            ruleset_token,
            slot_token,
            target_revision,
            created_ms,
            expires_ms,
            pending,
        ) = _AUTHORIZATION_CHALLENGE.unpack(body)
        if pending not in {0, 1}:
            raise OperatorProtocolError("authorization-challenge pending flag is invalid")
        try:
            return AuthorizationChallengeStatusMessage(
                challenge_token=challenge_token,
                mission_token=mission_token,
                target_token=target_token,
                scene_token=scene_token,
                ruleset_token=ruleset_token,
                payload_slot_token=slot_token,
                target_revision=target_revision,
                created_at_s=created_ms / 1000.0,
                expires_at_s=expires_ms / 1000.0,
                sequence=sequence,
                produced_at_s=sent_at_s,
                pending=bool(pending),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid authorization challenge: {exc}") from exc

    def _decode_authorization_decision(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> AuthorizationDecisionCommand:
        if len(body) != _AUTHORIZATION_DECISION.size:
            raise OperatorProtocolError("authorization-decision body has an invalid size")
        (
            command_token,
            session_token,
            challenge_token,
            mission_token,
            target_token,
            scene_token,
            ruleset_token,
            slot_token,
            target_revision,
            decision_value,
            operator_token,
            ttl_ms,
        ) = _AUTHORIZATION_DECISION.unpack(body)
        decision = {
            1: AuthorizationDecision.APPROVE,
            2: AuthorizationDecision.DENY,
        }.get(decision_value)
        if decision is None:
            raise OperatorProtocolError("authorization decision value is invalid")
        try:
            return AuthorizationDecisionCommand(
                command_token=command_token,
                session_token=session_token,
                challenge_token=challenge_token,
                mission_token=mission_token,
                target_token=target_token,
                scene_token=scene_token,
                ruleset_token=ruleset_token,
                payload_slot_token=slot_token,
                target_revision=target_revision,
                decision=decision,
                operator_token=operator_token,
                sequence=sequence,
                issued_at_s=sent_at_s,
                expires_at_s=sent_at_s + ttl_ms / 1000.0,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid authorization decision: {exc}") from exc

    def _decode_authorization_ack(self, body: bytes) -> AuthorizationDecisionAck:
        if len(body) != _AUTHORIZATION_ACK.size:
            raise OperatorProtocolError("authorization-ack body has an invalid size")
        command_token, accepted, reason_value, acknowledged_sequence = _AUTHORIZATION_ACK.unpack(
            body
        )
        if accepted not in {0, 1}:
            raise OperatorProtocolError("authorization-ack accepted flag is invalid")
        try:
            return AuthorizationDecisionAck(
                command_token=command_token,
                accepted=bool(accepted),
                reason=AuthorizationDecisionAckReason(reason_value),
                acknowledged_sequence=acknowledged_sequence,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid authorization acknowledgement: {exc}") from exc

    def _decode_patrol_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> PatrolStatusMessage:
        if len(body) != _PATROL_STATUS.size:
            raise OperatorProtocolError("patrol-status body has an invalid size")
        (
            status_hash,
            mission_hash,
            phase_value,
            target_state_value,
            flags,
            direction_value,
            validity_value,
            target_hash,
            x1,
            y1,
            x2,
            y2,
            label_length,
            label_bytes,
            confidence,
            quality,
            total_tracks,
            locked_tracks,
            frame_hash,
            frame_age_ms,
            evidence_age,
            turn_radius,
        ) = _PATROL_STATUS.unpack(body)
        if flags & ~0b111:
            raise OperatorProtocolError("patrol-status flags contain unsupported bits")
        target_present = bool(flags & 0b001)
        bbox_present = bool(flags & 0b010)
        return_present = bool(flags & 0b100)
        phases = tuple(PatrolPhase)
        states = tuple(UnifiedTrackState)
        if not 1 <= phase_value <= len(phases):
            raise OperatorProtocolError("patrol-status phase is invalid")
        if not 0 <= target_state_value <= len(states):
            raise OperatorProtocolError("patrol-status target state is invalid")
        direction = {
            0: None,
            1: ReturnObserveDirection.LEFT,
            2: ReturnObserveDirection.RIGHT,
            3: ReturnObserveDirection.ROUTE_REQUIRED,
        }.get(direction_value, ...)
        validity = {
            0: None,
            1: AdvisoryValidity.VALID,
            2: AdvisoryValidity.DEGRADED,
            3: AdvisoryValidity.INVALID,
        }.get(validity_value, ...)
        if direction is ... or validity is ...:
            raise OperatorProtocolError("patrol-status return advice is invalid")
        if return_present != (direction is not None and validity is not None):
            raise OperatorProtocolError("patrol-status return presence flag is inconsistent")
        if label_length > len(label_bytes):
            raise OperatorProtocolError("patrol-status label length is invalid")
        try:
            label = label_bytes[:label_length].decode("utf-8") or None
        except UnicodeDecodeError as exc:
            raise OperatorProtocolError("patrol-status label is not valid UTF-8") from exc
        try:
            return PatrolStatusMessage(
                status_id=_hashed_identifier(status_hash),
                sequence=sequence,
                mission_id=_hashed_identifier(mission_hash),
                phase=phases[phase_value - 1],
                primary_target_id=_hashed_identifier(target_hash) if target_present else None,
                target_state=(states[target_state_value - 1] if target_state_value else None),
                bbox=_decode_bbox((x1, y1, x2, y2)) if bbox_present else None,
                label=label,
                confidence=_decode_ratio(confidence),
                tracking_quality=_decode_ratio(quality),
                total_track_count=total_tracks,
                locked_track_count=locked_tracks,
                source_frame_id=_hashed_identifier(frame_hash),
                source_captured_at_s=sent_at_s - frame_age_ms / 1000.0,
                produced_at_s=sent_at_s,
                return_direction=direction,
                return_validity=validity,
                return_evidence_age_s=(_decode_duration(evidence_age) if return_present else None),
                estimated_minimum_turn_radius_m=(
                    _decode_distance(turn_radius) if return_present else None
                ),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid patrol-status content: {exc}") from exc

    def _decode_range_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> RangeStatusMessage:
        if len(body) != _RANGE_STATUS.size:
            raise OperatorProtocolError("range-status body has an invalid size")
        (
            status_hash,
            target_hash,
            calibration_hash,
            frame_hash,
            validity_value,
            flags,
            reason_mask,
            source_mask,
            rejected_source_mask,
            slant_range,
            ground_range,
            slant_low,
            slant_high,
            ground_low,
            ground_high,
            relative_bearing,
            absolute_bearing,
            bearing_sigma,
            north_offset,
            east_offset,
            frame_age_ms,
            data_freshness,
            consistency,
        ) = _RANGE_STATUS.unpack(body)
        if flags != 0:
            raise OperatorProtocolError("range-status flags contain unsupported bits")
        validity = {
            1: RangeValidity.VALID,
            2: RangeValidity.DEGRADED,
            3: RangeValidity.INVALID,
        }.get(validity_value)
        if validity is None:
            raise OperatorProtocolError("range-status validity is invalid")
        decoded_consistency = _decode_ratio(consistency)
        if decoded_consistency is None:
            raise OperatorProtocolError("range-status consistency is unavailable")
        try:
            return RangeStatusMessage(
                status_id=_hashed_identifier(status_hash),
                sequence=sequence,
                target_id=_hashed_identifier(target_hash),
                calibration_id=_hashed_identifier(calibration_hash),
                source_frame_id=_hashed_identifier(frame_hash),
                source_captured_at_s=sent_at_s - frame_age_ms / 1000.0,
                produced_at_s=sent_at_s,
                validity=validity,
                reasons=_decode_registry_mask(
                    reason_mask,
                    RANGING_REASON_IDS,
                    "ranging reason",
                ),
                sources=_decode_registry_mask(
                    source_mask,
                    RANGING_SOURCE_IDS,
                    "ranging source",
                ),
                rejected_sources=_decode_registry_mask(
                    rejected_source_mask,
                    RANGING_SOURCE_IDS,
                    "rejected ranging source",
                ),
                slant_range_m=_decode_distance(slant_range),
                ground_range_m=_decode_distance(ground_range),
                slant_range_ci95_m=_decode_optional_interval(slant_low, slant_high),
                ground_range_ci95_m=_decode_optional_interval(ground_low, ground_high),
                relative_bearing_deg=_decode_bearing(relative_bearing),
                absolute_bearing_deg=_decode_unsigned_bearing(absolute_bearing),
                bearing_sigma_deg=_decode_unsigned_centidegrees(bearing_sigma),
                north_offset_m=_decode_signed_distance32(north_offset),
                east_offset_m=_decode_signed_distance32(east_offset),
                data_freshness_s=_decode_duration(data_freshness),
                sensor_consistency=decoded_consistency,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid range-status content: {exc}") from exc

    def _decode_release_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> ReleaseStatusMessage:
        if len(body) != _RELEASE_STATUS.size:
            raise OperatorProtocolError("release-status body has an invalid size")
        (
            target_hash,
            range_target_hash,
            range_frame_hash,
            calibration_hash,
            timing_value,
            flags,
            reason_mask,
            target_north,
            target_east,
            impact_north,
            impact_east,
            along_error,
            cross_error,
            ellipse_major,
            ellipse_minor,
            ellipse_orientation,
            ground_range,
            range_low,
            range_high,
            descent_time,
            lead_distance,
            consistency,
        ) = _RELEASE_STATUS.unpack(body)
        if flags & ~0x01:
            raise OperatorProtocolError("release-status flags contain unsupported bits")
        binding_present = bool(flags & 0x01)
        if binding_present != bool(range_target_hash and range_frame_hash):
            raise OperatorProtocolError("release-status range binding flags are inconsistent")
        if not binding_present and (range_target_hash or range_frame_hash):
            raise OperatorProtocolError("release-status absent binding contains identifiers")
        timing = {
            1: ReleaseTimingStatus.INVALID,
            2: ReleaseTimingStatus.TOO_EARLY,
            3: ReleaseTimingStatus.WINDOW,
            4: ReleaseTimingStatus.TOO_LATE,
        }.get(timing_value)
        if timing is None:
            raise OperatorProtocolError("release-status timing value is invalid")
        try:
            return ReleaseStatusMessage(
                sequence=sequence,
                target_id=_hashed_identifier(target_hash),
                calibration_id=_hashed_identifier(calibration_hash),
                produced_at_s=sent_at_s,
                timing_status=timing,
                reasons=_decode_registry_mask(reason_mask, RELEASE_REASON_IDS, "release reason"),
                range_target_id=(
                    _hashed_identifier(range_target_hash) if binding_present else None
                ),
                range_frame_id=(_hashed_identifier(range_frame_hash) if binding_present else None),
                target_north_offset_m=_decode_signed_distance32(target_north),
                target_east_offset_m=_decode_signed_distance32(target_east),
                impact_north_offset_m=_decode_signed_distance32(impact_north),
                impact_east_offset_m=_decode_signed_distance32(impact_east),
                along_track_error_m=_decode_signed_distance32(along_error),
                cross_track_error_m=_decode_signed_distance32(cross_error),
                error_ellipse_major_m=_decode_distance(ellipse_major),
                error_ellipse_minor_m=_decode_distance(ellipse_minor),
                error_ellipse_orientation_deg=_decode_bearing(ellipse_orientation),
                estimated_ground_range_m=_decode_distance(ground_range),
                ground_range_ci95_m=_decode_optional_interval(range_low, range_high),
                payload_descent_time_s=_decode_duration(descent_time),
                release_lead_distance_m=_decode_distance(lead_distance),
                range_sensor_consistency=_decode_ratio(consistency),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid release-status content: {exc}") from exc

    def _decode_approach_challenge(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> ApproachChallengeStatusMessage:
        if len(body) != _APPROACH_CHALLENGE.size:
            raise OperatorProtocolError("approach-challenge body has an invalid size")
        (
            challenge_token,
            target_token,
            target_revision,
            selection_id,
            issued_ms,
            expires_ms,
            pending,
        ) = _APPROACH_CHALLENGE.unpack(body)
        if pending != 1:
            raise OperatorProtocolError("approach challenge must be pending")
        try:
            return ApproachChallengeStatusMessage(
                challenge_token=challenge_token,
                target_token=target_token,
                target_revision=target_revision,
                selection_command_id=str(UUID(bytes=selection_id)),
                issued_at_s=issued_ms / 1000.0,
                expires_at_s=expires_ms / 1000.0,
                sequence=sequence,
                produced_at_s=sent_at_s,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid approach-challenge content: {exc}") from exc

    def _decode_approach_confirmation(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> ApproachConfirmationCommand:
        if len(body) != _APPROACH_CONFIRMATION.size:
            raise OperatorProtocolError("approach-confirmation body has an invalid size")
        (
            command_token,
            session_token,
            challenge_token,
            target_token,
            target_revision,
            selection_id,
            ttl_ms,
            duration_ms,
            completion,
            continuous,
        ) = _APPROACH_CONFIRMATION.unpack(body)
        decoded_completion = _decode_ratio(completion)
        if ttl_ms == 0 or duration_ms == 0 or decoded_completion is None or continuous > 1:
            raise OperatorProtocolError("approach-confirmation content is invalid")
        try:
            return ApproachConfirmationCommand(
                command_token=command_token,
                session_token=session_token,
                challenge_token=challenge_token,
                target_token=target_token,
                target_revision=target_revision,
                selection_command_id=str(UUID(bytes=selection_id)),
                sequence=sequence,
                issued_at_s=sent_at_s,
                expires_at_s=sent_at_s + ttl_ms / 1000.0,
                slide_duration_s=duration_ms / 1000.0,
                completion_fraction=decoded_completion,
                continuous=bool(continuous),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid approach-confirmation content: {exc}") from exc

    def _decode_approach_ack(self, body: bytes) -> ApproachConfirmationAck:
        if len(body) != _APPROACH_ACK.size:
            raise OperatorProtocolError("approach-ack body has an invalid size")
        command_token, accepted, reason_value, acknowledged_sequence = _APPROACH_ACK.unpack(body)
        if accepted > 1:
            raise OperatorProtocolError("approach-ack accepted flag is invalid")
        try:
            reason = ApproachConfirmationAckReason(reason_value)
            return ApproachConfirmationAck(
                command_token=command_token,
                accepted=bool(accepted),
                reason=reason,
                acknowledged_sequence=acknowledged_sequence,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid approach-ack content: {exc}") from exc

    def _decode_approach_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> ApproachStatusMessage:
        if len(body) != _APPROACH_STATUS.size:
            raise OperatorProtocolError("approach-status body has an invalid size")
        (
            target_hash,
            target_revision,
            phase_value,
            reason_mask,
            yaw_error,
            pitch_error,
            yaw_advice,
            pitch_advice,
            bank_advice,
            climb_advice,
            ground_range,
            confirmation_ttl,
            flags,
        ) = _APPROACH_STATUS.unpack(body)
        if flags & ~0x0F:
            raise OperatorProtocolError("approach-status flags contain unsupported bits")
        target_present = bool(flags & 0x01)
        flight_control_enabled = bool(flags & 0x02)
        aim_control_active = bool(flags & 0x04)
        pilot_input_cancelled = bool(flags & 0x08)
        if target_present != bool(target_hash):
            raise OperatorProtocolError("approach-status target binding is inconsistent")
        phases = list(ApproachHilPhase)
        if not 1 <= phase_value <= len(phases):
            raise OperatorProtocolError("approach-status phase is invalid")
        expiry_delta_s = _decode_duration(confirmation_ttl)
        try:
            return ApproachStatusMessage(
                sequence=sequence,
                target_id=_hashed_identifier(target_hash) if target_present else None,
                target_revision=target_revision if target_present else None,
                phase=phases[phase_value - 1],
                reasons=_decode_registry_mask(reason_mask, APPROACH_REASON_IDS, "approach reason"),
                produced_at_s=sent_at_s,
                yaw_error_deg=_decode_bearing(yaw_error),
                pitch_error_deg=_decode_bearing(pitch_error),
                yaw_advice_deg=_decode_bearing(yaw_advice),
                pitch_advice_deg=_decode_bearing(pitch_advice),
                bank_advice_deg=_decode_bearing(bank_advice),
                climb_pitch_advice_deg=_decode_bearing(climb_advice),
                ground_range_m=_decode_distance(ground_range),
                confirmation_expires_at_s=(
                    sent_at_s + expiry_delta_s if expiry_delta_s is not None else None
                ),
                advisory_only=not flight_control_enabled,
                sitl_hil_only=not flight_control_enabled,
                flight_control_enabled=flight_control_enabled,
                aim_control_active=aim_control_active,
                pilot_input_cancelled=pilot_input_cancelled,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid approach-status content: {exc}") from exc

    def _decode_payload_target_challenge(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> PayloadTargetChallengeStatusMessage:
        if len(body) != _PAYLOAD_TARGET_CHALLENGE.size:
            raise OperatorProtocolError("payload-target-challenge body has an invalid size")
        (
            challenge_token,
            selected_target_token,
            selected_target_revision,
            aimpoint_target_token,
            aimpoint_target_revision,
            selection_id,
            issued_ms,
            expires_ms,
            pending,
        ) = _PAYLOAD_TARGET_CHALLENGE.unpack(body)
        if pending != 1:
            raise OperatorProtocolError("payload target challenge must be pending")
        try:
            return PayloadTargetChallengeStatusMessage(
                challenge_token=challenge_token,
                selected_target_token=selected_target_token,
                selected_target_revision=selected_target_revision,
                aimpoint_target_token=aimpoint_target_token,
                aimpoint_target_revision=aimpoint_target_revision,
                selection_command_id=str(UUID(bytes=selection_id)),
                issued_at_s=issued_ms / 1000.0,
                expires_at_s=expires_ms / 1000.0,
                sequence=sequence,
                produced_at_s=sent_at_s,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid payload-target-challenge content: {exc}") from exc

    def _decode_payload_target_confirmation(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> PayloadTargetConfirmationCommand:
        if len(body) != _PAYLOAD_TARGET_CONFIRMATION.size:
            raise OperatorProtocolError("payload-target-confirmation body has an invalid size")
        (
            command_token,
            session_token,
            challenge_token,
            selected_target_token,
            selected_target_revision,
            aimpoint_target_token,
            aimpoint_target_revision,
            selection_id,
            ttl_ms,
            duration_ms,
            completion,
            continuous,
        ) = _PAYLOAD_TARGET_CONFIRMATION.unpack(body)
        decoded_completion = _decode_ratio(completion)
        if ttl_ms == 0 or duration_ms == 0 or decoded_completion is None or continuous > 1:
            raise OperatorProtocolError("payload-target-confirmation content is invalid")
        try:
            return PayloadTargetConfirmationCommand(
                command_token=command_token,
                session_token=session_token,
                challenge_token=challenge_token,
                selected_target_token=selected_target_token,
                selected_target_revision=selected_target_revision,
                aimpoint_target_token=aimpoint_target_token,
                aimpoint_target_revision=aimpoint_target_revision,
                selection_command_id=str(UUID(bytes=selection_id)),
                sequence=sequence,
                issued_at_s=sent_at_s,
                expires_at_s=sent_at_s + ttl_ms / 1000.0,
                slide_duration_s=duration_ms / 1000.0,
                completion_fraction=decoded_completion,
                continuous=bool(continuous),
            )
        except ValueError as exc:
            raise OperatorProtocolError(
                f"invalid payload-target-confirmation content: {exc}"
            ) from exc

    def _decode_payload_target_ack(self, body: bytes) -> PayloadTargetConfirmationAck:
        if len(body) != _PAYLOAD_TARGET_ACK.size:
            raise OperatorProtocolError("payload-target-ack body has an invalid size")
        command_token, accepted, reason_value, acknowledged_sequence = _PAYLOAD_TARGET_ACK.unpack(
            body
        )
        if accepted > 1:
            raise OperatorProtocolError("payload-target-ack accepted flag is invalid")
        try:
            reason = PayloadTargetConfirmationAckReason(reason_value)
            return PayloadTargetConfirmationAck(
                command_token=command_token,
                accepted=bool(accepted),
                reason=reason,
                acknowledged_sequence=acknowledged_sequence,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid payload-target-ack content: {exc}") from exc

    def _decode_payload_target_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> PayloadTargetStatusMessage:
        if len(body) != _PAYLOAD_TARGET_STATUS.size:
            raise OperatorProtocolError("payload-target-status body has an invalid size")
        (
            selection_id,
            selected_target_token,
            selected_target_revision,
            aimpoint_target_token,
            aimpoint_target_revision,
            eligibility_value,
            confirmation_ttl,
            flags,
        ) = _PAYLOAD_TARGET_STATUS.unpack(body)
        if flags & ~0x07:
            raise OperatorProtocolError("payload-target-status flags contain unsupported bits")
        aimpoint_present = bool(flags & 0x01)
        confirmation_pending = bool(flags & 0x02)
        confirmation_accepted = bool(flags & 0x04)
        if aimpoint_present != bool(aimpoint_target_token):
            raise OperatorProtocolError("payload-target-status aimpoint binding is inconsistent")
        eligibility_values = list(PayloadTargetEligibility)
        if not 1 <= eligibility_value <= len(eligibility_values):
            raise OperatorProtocolError("payload-target-status eligibility is invalid")
        expiry_delta_s = _decode_duration(confirmation_ttl)
        try:
            return PayloadTargetStatusMessage(
                sequence=sequence,
                selection_command_id=str(UUID(bytes=selection_id)),
                selected_target_token=selected_target_token,
                selected_target_revision=selected_target_revision,
                eligibility=eligibility_values[eligibility_value - 1],
                produced_at_s=sent_at_s,
                aimpoint_target_token=(aimpoint_target_token if aimpoint_present else None),
                aimpoint_target_revision=(aimpoint_target_revision if aimpoint_present else None),
                confirmation_pending=confirmation_pending,
                confirmation_accepted=confirmation_accepted,
                confirmation_expires_at_s=(
                    sent_at_s + expiry_delta_s if expiry_delta_s is not None else None
                ),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid payload-target-status content: {exc}") from exc

    def _decode_target_pool_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> TargetPoolStatusMessage:
        if len(body) < _TARGET_POOL_HEADER.size:
            raise OperatorProtocolError("target-pool status body is truncated")
        pool_revision, page_index, page_count, total_count, entry_count = (
            _TARGET_POOL_HEADER.unpack_from(body)
        )
        expected_size = _TARGET_POOL_HEADER.size + entry_count * _TARGET_POOL_ENTRY.size
        if entry_count not in {0, 1, 2} or len(body) != expected_size:
            raise OperatorProtocolError("target-pool status entry count is invalid")
        states = list(UnifiedTrackState)
        entries: list[TargetPoolEntry] = []
        offset = _TARGET_POOL_HEADER.size
        for _ in range(entry_count):
            (
                target_hash,
                state_value,
                flags,
                label_bytes,
                confidence,
                quality,
                x1,
                y1,
                x2,
                y2,
                relative_bearing,
                estimated_range,
                target_speed,
            ) = _TARGET_POOL_ENTRY.unpack_from(body, offset)
            offset += _TARGET_POOL_ENTRY.size
            if target_hash == 0 or not 1 <= state_value <= len(states) or flags & ~0x3F:
                raise OperatorProtocolError("target-pool entry content is invalid")
            bbox_present = bool(flags & 0x10)
            if bbox_present and (x2 <= x1 or y2 <= y1):
                raise OperatorProtocolError("target-pool entry bbox is invalid")
            if not bbox_present and any((x1, y1, x2, y2)):
                raise OperatorProtocolError("target-pool entry bbox padding is not zeroed")
            terminator = label_bytes.find(b"\0")
            label_payload = label_bytes if terminator < 0 else label_bytes[:terminator]
            if terminator >= 0 and any(label_bytes[terminator:]):
                raise OperatorProtocolError("target-pool label padding is not zeroed")
            try:
                label = label_payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OperatorProtocolError("target-pool label is not valid UTF-8") from exc
            decoded_confidence = _decode_ratio(confidence)
            decoded_quality = _decode_ratio(quality)
            if decoded_confidence is None or decoded_quality is None:
                raise OperatorProtocolError("target-pool confidence is unavailable")
            try:
                entries.append(
                    TargetPoolEntry(
                        target_id=_hashed_identifier(target_hash),
                        state=states[state_value - 1],
                        label=label,
                        confidence=decoded_confidence,
                        tracking_quality=decoded_quality,
                        locked=bool(flags & 0x01),
                        primary=bool(flags & 0x02),
                        actionable=bool(flags & 0x04),
                        reid_confirmed=bool(flags & 0x08),
                        operator_tracked=bool(flags & 0x20),
                        bbox=_decode_bbox((x1, y1, x2, y2)) if bbox_present else None,
                        relative_bearing_deg=_decode_bearing(relative_bearing),
                        estimated_range_m=_decode_distance(estimated_range),
                        target_speed_mps=_decode_distance(target_speed),
                    )
                )
            except ValueError as exc:
                raise OperatorProtocolError(f"invalid target-pool entry: {exc}") from exc
        try:
            return TargetPoolStatusMessage(
                sequence=sequence,
                pool_revision=pool_revision,
                page_index=page_index,
                page_count=page_count,
                total_track_count=total_count,
                entries=tuple(entries),
                produced_at_s=sent_at_s,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid target-pool status: {exc}") from exc

    def _decode_scene_context_status(
        self,
        body: bytes,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> SceneContextStatusMessage:
        if len(body) < _SCENE_CONTEXT_HEADER.size:
            raise OperatorProtocolError("scene-context status body is truncated")
        (
            context_revision,
            source_frame_hash,
            source_captured_ms,
            state_value,
            page_index,
            page_count,
            total_count,
        ) = _SCENE_CONTEXT_HEADER.unpack_from(body)
        entry_bytes = len(body) - _SCENE_CONTEXT_HEADER.size
        if entry_bytes % _SCENE_CONTEXT_ENTRY.size:
            raise OperatorProtocolError("scene-context status entry bytes are malformed")
        entry_count = entry_bytes // _SCENE_CONTEXT_ENTRY.size
        if entry_count not in {0, 1, 2} or source_frame_hash == 0:
            raise OperatorProtocolError("scene-context status entry count or frame is invalid")
        states = list(SceneContextState)
        if not 1 <= state_value <= len(states):
            raise OperatorProtocolError("scene-context state is invalid")
        labels = {1: "road", 2: "building"}
        entries: list[SceneContextRegionEntry] = []
        offset = _SCENE_CONTEXT_HEADER.size
        for _ in range(entry_count):
            label_code, x1, y1, x2, y2, area, fill = _SCENE_CONTEXT_ENTRY.unpack_from(body, offset)
            offset += _SCENE_CONTEXT_ENTRY.size
            if label_code not in labels or area == 0 or fill == 0:
                raise OperatorProtocolError("scene-context entry content is invalid")
            try:
                entries.append(
                    SceneContextRegionEntry(
                        label=labels[label_code],
                        bbox=_decode_bbox((x1, y1, x2, y2)),
                        frame_area_fraction=area / 65535.0,
                        bbox_fill_fraction=fill / 65535.0,
                    )
                )
            except ValueError as exc:
                raise OperatorProtocolError(f"invalid scene-context entry: {exc}") from exc
        try:
            return SceneContextStatusMessage(
                sequence=sequence,
                context_revision=context_revision,
                source_frame_id=_hashed_identifier(source_frame_hash),
                source_captured_at_s=source_captured_ms / 1000.0,
                state=states[state_value - 1],
                page_index=page_index,
                page_count=page_count,
                total_region_count=total_count,
                entries=tuple(entries),
                produced_at_s=sent_at_s,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid scene-context status: {exc}") from exc

    def _require_registered_stream(self, stream_id: str) -> None:
        registered = self._geometries.get(_hash32(stream_id))
        if registered is None or registered.stream_id != stream_id:
            raise ValueError("video stream is not registered with the operator-link codec")

    def _wire_geometry(
        self, stream_hash: int, width: int, height: int, rotation_code: int
    ) -> VideoGeometry:
        registered = self._geometries.get(stream_hash)
        if registered is None:
            raise OperatorProtocolError("operator-link stream is not registered")
        try:
            rotation = {0: 0, 1: 90, 2: 180, 3: 270}[rotation_code]
            return VideoGeometry(registered.stream_id, width, height, rotation)
        except (KeyError, ValueError) as exc:
            raise OperatorProtocolError("operator-link video geometry is invalid") from exc


def _uuid_bytes(value: str, *, field_name: str) -> bytes:
    try:
        return UUID(value).bytes
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID for wire transport") from exc


def _hash32(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:4], "big")


def _hash64(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _hashed_identifier(value: int) -> str:
    return f"hash64:{value:016x}"


def _rotation_code(rotation_degrees: int) -> int:
    return {0: 0, 90: 1, 180: 2, 270: 3}[rotation_degrees]


def _timestamp_ms(value: float) -> int:
    if not isfinite(value) or value < 0.0:
        raise ValueError("wire timestamp must be finite and non-negative")
    return _bounded_uint(round(value * 1000.0), bits=64, field_name="wire timestamp")


def _bounded_uint(value: int, *, bits: int, field_name: str) -> int:
    if not 0 <= value <= (1 << bits) - 1:
        raise ValueError(f"{field_name} does not fit in uint{bits}")
    return value


def _encode_bbox(bbox: BoundingBox) -> tuple[int, int, int, int]:
    return tuple(round(value * 65535.0) for value in (bbox.x1, bbox.y1, bbox.x2, bbox.y2))


def _decode_bbox(values: tuple[int, int, int, int]) -> BoundingBox:
    try:
        return BoundingBox(*(value / 65535.0 for value in values))
    except ValueError as exc:
        raise OperatorProtocolError(f"invalid wire bounding box: {exc}") from exc


def _encode_ratio(value: float | None) -> int:
    return 255 if value is None else round(value * 254.0)


def _decode_ratio(value: int) -> float | None:
    return None if value == 255 else value / 254.0


def _encode_bearing(value: float | None) -> int:
    return -32768 if value is None else round(value * 100.0)


def _decode_bearing(value: int) -> float | None:
    return None if value == -32768 else value / 100.0


def _encode_unsigned_bearing(value: float | None) -> int:
    if value is None:
        return 0xFFFF
    if not isfinite(value) or not 0.0 <= value < 360.0:
        raise ValueError("unsigned bearing must be in [0, 360)")
    return _bounded_uint(round(value * 100.0), bits=16, field_name="unsigned bearing")


def _decode_unsigned_bearing(value: int) -> float | None:
    return None if value == 0xFFFF else value / 100.0


def _encode_unsigned_centidegrees(value: float | None) -> int:
    if value is None:
        return 0xFFFF
    encoded = _bounded_uint(
        round(value * 100.0),
        bits=16,
        field_name="unsigned centidegrees",
    )
    if encoded == 0xFFFF:
        raise ValueError("unsigned centidegrees cannot use the reserved null value")
    return encoded


def _decode_unsigned_centidegrees(value: int) -> float | None:
    return None if value == 0xFFFF else value / 100.0


def _encode_distance(value: float | None) -> int:
    if value is None:
        return 0xFFFF
    encoded = _bounded_uint(
        round(value * 10.0),
        bits=16,
        field_name="distance decimetres",
    )
    if encoded == 0xFFFF:
        raise ValueError("distance decimetres cannot use the reserved null value")
    return encoded


def _decode_distance(value: int) -> float | None:
    return None if value == 0xFFFF else value / 10.0


def _encode_duration(value: float | None) -> int:
    if value is None:
        return 0xFFFF
    encoded = _bounded_uint(
        round(value * 10.0),
        bits=16,
        field_name="duration deciseconds",
    )
    if encoded == 0xFFFF:
        raise ValueError("duration deciseconds cannot use the reserved null value")
    return encoded


def _decode_duration(value: int) -> float | None:
    return None if value == 0xFFFF else value / 10.0


def _encode_signed_distance(value: float | None) -> int:
    if value is None:
        return -32768
    encoded = round(value * 10.0)
    if not -32767 <= encoded <= 32767:
        raise ValueError("signed distance decimetres does not fit the wire range")
    return encoded


def _decode_signed_distance(value: int) -> float | None:
    return None if value == -32768 else value / 10.0


def _encode_signed_distance32(value: float | None) -> int:
    if value is None:
        return -(1 << 31)
    encoded = round(value * 10.0)
    if not -(1 << 31) + 1 <= encoded <= (1 << 31) - 1:
        raise ValueError("signed distance decimetres does not fit the int32 wire range")
    return encoded


def _decode_signed_distance32(value: int) -> float | None:
    return None if value == -(1 << 31) else value / 10.0


def _decode_optional_interval(low: int, high: int) -> tuple[float, float] | None:
    decoded = (_decode_distance(low), _decode_distance(high))
    if decoded == (None, None):
        return None
    if any(value is None for value in decoded):
        raise OperatorProtocolError("range-status confidence interval is partially absent")
    return (float(decoded[0]), float(decoded[1]))


def _registry_mask(values: tuple[str, ...], registry: tuple[str, ...], name: str) -> int:
    index_by_value = {value: index for index, value in enumerate(registry)}
    mask = 0
    for value in values:
        try:
            index = index_by_value[value]
        except KeyError as exc:
            raise ValueError(f"{name} is not registered: {value}") from exc
        mask |= 1 << index
    return mask


def _decode_registry_mask(mask: int, registry: tuple[str, ...], name: str) -> tuple[str, ...]:
    if mask >> len(registry):
        raise OperatorProtocolError(f"{name} mask contains unsupported bits")
    return tuple(value for index, value in enumerate(registry) if mask & (1 << index))


__all__ = [
    "AUTHENTICATION_TAG_BYTES",
    "ApproachConfirmationAck",
    "ApproachConfirmationAckReason",
    "AuthorizationDecisionAck",
    "AuthorizationDecisionAckReason",
    "DecodedOperatorPacket",
    "MAX_TUNNEL_PAYLOAD_BYTES",
    "OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL",
    "OperatorProtocolError",
    "OperatorTunnelCodec",
    "PayloadTargetConfirmationAck",
    "PayloadTargetConfirmationAckReason",
    "SelectionAck",
    "SelectionAckReason",
    "WireMessageType",
]
