from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from math import isfinite, isnan
from uuid import uuid4

from .adaptive_ranging import AdaptiveRangingPolicy
from .approach_hil import ApproachHilAssessment, SlideConfirmationChallenge
from .autonomy.serialization import require_sha256
from .domain import AuthorizationChallenge, DeploymentWindowSolution, MissionPhase, VehicleTelemetry
from .mission import MissionStatus, ObservationOutcome
from .multimodal_ranging import RangeSolution, RangeValidity
from .operator_link import (
    MAX_RANGE_STATUS_SOURCE_AGE_S,
    MAX_TARGET_GEOLOCATION_SOURCE_AGE_S,
    ApproachChallengeStatusMessage,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    RuntimeBackendHealth,
    RuntimeMode,
    RuntimeStatusMessage,
    SafetyStatusMessage,
    SceneContextRegionEntry,
    SceneContextState,
    SceneContextStatusMessage,
    TargetGeolocationStatusMessage,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    operator_identifier_token,
)
from .patrol_advisory import PatrolModeAssessment
from .payload_target_gate import (
    PayloadSlideChallenge,
    PayloadSlideGrant,
    PayloadTargetResolution,
)
from .runtime import RuntimeCapability, RuntimeDescriptor
from .semantic_environment import SemanticContextSnapshot
from .semantic_environment import SemanticContextState as ModelContextState
from .target_geolocation import target_geolocation_from_ned_offset
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState

_RANGE_REASON_WIRE_ALIASES = {
    # The v2 range packet has a fixed 32-bit reason mask. Preserve the exact
    # inflated CI in the numeric fields while collapsing the two internal
    # inflation diagnostics to the registered degraded-range wire reason.
    "absolute_range_uncertainty_inflated": "direct_degraded_metric_range",
    "direct_range_uncertainty_inflated": "direct_degraded_metric_range",
    "target_ray_uncertainty_unavailable": "target_geometry_uncertainty_unbounded",
    "target_ray_uncertainty_crosses_horizon": "target_geometry_uncertainty_unbounded",
    "camera_offset_uncertainty_unbounded": "target_geometry_uncertainty_unbounded",
    "terrain_intersection_unavailable": "target_geometry_uncertainty_unbounded",
    "terrain_uncertainty_unbounded": "target_geometry_uncertainty_unbounded",
}


@dataclass(frozen=True, slots=True)
class RangeSolutionFrameBinding:
    """Immutable identity/time binding for one range solution source frame.

    ``RangeSolution`` intentionally predates the operator transport and does
    not carry the camera capture timestamp.  Keeping that timestamp as a loose
    builder argument allowed a cached solution to be paired with a later
    frame.  This sidecar freezes the identifiers and timing values at the
    point where the solution is produced while preserving the legacy builder
    call shape for existing readers and invalid diagnostics.
    """

    target_id: str
    source_frame_id: str
    calibration_id: str
    source_captured_at_s: float
    evaluated_at_s: float
    data_freshness_s: float | None
    validity: RangeValidity

    @classmethod
    def from_solution(
        cls,
        solution: RangeSolution,
        *,
        source_frame_id: str,
        source_captured_at_s: float,
    ) -> RangeSolutionFrameBinding:
        if not isinstance(solution, RangeSolution):
            raise TypeError("solution must be a RangeSolution")
        binding = cls(
            target_id=solution.target_id,
            source_frame_id=source_frame_id,
            calibration_id=solution.calibration_id,
            source_captured_at_s=source_captured_at_s,
            evaluated_at_s=solution.evaluated_at_s,
            data_freshness_s=solution.data_freshness_s,
            validity=solution.validity,
        )
        binding.validate_solution(solution)
        return binding

    def __post_init__(self) -> None:
        for value, name in (
            (self.target_id, "target_id"),
            (self.source_frame_id, "source_frame_id"),
            (self.calibration_id, "calibration_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"range source binding {name} cannot be empty")
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isfinite(value)
            and value >= 0.0
            for value in (self.source_captured_at_s, self.evaluated_at_s)
        ):
            raise ValueError("range source binding timestamps must be finite and non-negative")
        if self.evaluated_at_s < self.source_captured_at_s:
            raise ValueError("range solution cannot predate its bound source frame")
        if self.data_freshness_s is not None and (
            not isinstance(self.data_freshness_s, (int, float))
            or isinstance(self.data_freshness_s, bool)
            or not isfinite(self.data_freshness_s)
            or self.data_freshness_s < 0.0
        ):
            raise ValueError("range source binding data freshness is invalid")
        if self.validity in {RangeValidity.VALID, RangeValidity.DEGRADED}:
            source_age_s = self.evaluated_at_s - self.source_captured_at_s
            if source_age_s > MAX_RANGE_STATUS_SOURCE_AGE_S:
                raise ValueError("range source binding frame is stale")
            if self.data_freshness_s is None:
                raise ValueError("valid range source binding requires data_freshness_s")
            if self.data_freshness_s > MAX_RANGE_STATUS_SOURCE_AGE_S:
                raise ValueError("range source binding data is stale")
            if self.data_freshness_s + 1.0e-9 < source_age_s:
                raise ValueError(
                    "range source binding data freshness is newer than its source frame"
                )

    def validate_solution(self, solution: RangeSolution) -> None:
        """Reject any relabelled/replaced solution against this binding."""

        if not isinstance(solution, RangeSolution):
            raise TypeError("solution must be a RangeSolution")
        if (
            solution.target_id != self.target_id
            or solution.frame_id != self.source_frame_id
            or solution.calibration_id != self.calibration_id
            or solution.evaluated_at_s != self.evaluated_at_s
            or solution.data_freshness_s != self.data_freshness_s
            or solution.validity is not self.validity
        ):
            raise ValueError("range solution does not match its immutable source binding")

    def effective_data_freshness(self, *, produced_at_s: float) -> float | None:
        if (
            not isinstance(produced_at_s, (int, float))
            or isinstance(produced_at_s, bool)
            or not isfinite(produced_at_s)
            or produced_at_s < self.evaluated_at_s
        ):
            raise ValueError("range status production time predates its bound solution")
        if self.data_freshness_s is None:
            return None
        return self.data_freshness_s + produced_at_s - self.evaluated_at_s


def _range_reasons_for_wire(reasons: tuple[str, ...]) -> tuple[str, ...]:
    """Collapse internal geometry diagnostics into the bounded wire vocabulary."""

    normalized: list[str] = []
    for reason in reasons:
        wire_reason = _RANGE_REASON_WIRE_ALIASES.get(reason, reason)
        if wire_reason not in normalized:
            normalized.append(wire_reason)
    return tuple(normalized)


_AUTHORIZED_PHASES = frozenset(
    {
        MissionPhase.DEPLOYMENT_READY,
        MissionPhase.DEPLOYING,
        MissionPhase.VERIFYING_RELEASE,
        MissionPhase.EGRESS,
    }
)


def build_runtime_status_message(
    *,
    sequence: int,
    mode: RuntimeMode,
    backend_health: RuntimeBackendHealth,
    airframe_profile_hash64: int,
    run_hash64: int,
    produced_at_s: float,
    scenario_seed: int | None = None,
    scenario_hash64: int | None = None,
    ground_truth_isolated: bool = False,
    hardware_outputs_present: bool = False,
    profile_valid: bool = False,
) -> RuntimeStatusMessage:
    """Build the signed environment identity consumed by the shared GCS UI."""

    scenario_present = scenario_seed is not None or scenario_hash64 is not None
    return RuntimeStatusMessage(
        sequence=sequence,
        mode=mode,
        backend_health=backend_health,
        scenario_seed=scenario_seed,
        scenario_hash64=scenario_hash64,
        airframe_profile_hash64=airframe_profile_hash64,
        run_hash64=run_hash64,
        scenario_present=scenario_present,
        ground_truth_isolated=ground_truth_isolated,
        hardware_outputs_present=hardware_outputs_present,
        profile_valid=profile_valid,
        produced_at_s=produced_at_s,
    )


def build_runtime_status_from_descriptor(
    *,
    sequence: int,
    descriptor: RuntimeDescriptor,
    backend_health: RuntimeBackendHealth,
    run_identity_hash: str,
    produced_at_s: float,
) -> RuntimeStatusMessage:
    """Build runtime wire identity from one validated runtime descriptor.

    The full hashes remain in the runtime/audit contracts.  The operator link
    carries their most-significant 64-bit prefixes, so a zero prefix is
    rejected instead of publishing an ambiguous "missing" identity.
    """

    if not isinstance(descriptor, RuntimeDescriptor):
        raise ValueError("descriptor must be a RuntimeDescriptor")

    scenario_hash64 = (
        _sha256_prefix64(
            descriptor.scenario_manifest_hash,
            "scenario_manifest_hash",
        )
        if descriptor.scenario_manifest_hash is not None
        else None
    )
    return build_runtime_status_message(
        sequence=sequence,
        mode=descriptor.mode,
        backend_health=backend_health,
        scenario_seed=descriptor.scenario_seed,
        scenario_hash64=scenario_hash64,
        airframe_profile_hash64=_sha256_prefix64(
            descriptor.airframe_profile_hash,
            "airframe_profile_hash",
        ),
        run_hash64=_sha256_prefix64(run_identity_hash, "run_identity_hash"),
        ground_truth_isolated=descriptor.mode is not RuntimeMode.REAL,
        hardware_outputs_present=(
            RuntimeCapability.FLIGHT_CONTROL_OUTPUT in descriptor.profile.capabilities
        ),
        profile_valid=True,
        produced_at_s=produced_at_s,
    )


def _sha256_prefix64(value: object, name: str) -> int:
    digest = require_sha256(value, name)
    prefix = int(digest[:16], 16)
    if prefix == 0:
        raise ValueError(f"{name} 64-bit wire prefix cannot be zero")
    return prefix


def build_mission_status_message(
    *,
    mission_id: str,
    sequence: int,
    status: MissionStatus,
    outcome: ObservationOutcome,
    produced_at_s: float,
) -> MissionStatusMessage:
    """Build read-only G20 status from the current mission decision snapshot."""

    preferred_target_id, decision = _preferred_decision(status=status, outcome=outcome)
    target_id = decision.target_id if decision is not None else preferred_target_id
    track = next((item for item in outcome.tracks if item.track_id == target_id), None)
    window = decision.deployment_window if decision is not None else None

    if status.phase is MissionPhase.AWAITING_AUTHORIZATION:
        authorization = AuthorizationDisplayState.PENDING
    elif status.phase in _AUTHORIZED_PHASES and status.active_target_id is not None:
        authorization = AuthorizationDisplayState.APPROVED
    else:
        authorization = AuthorizationDisplayState.NONE

    return MissionStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        mission_id=mission_id,
        phase=status.phase,
        authorization_state=authorization,
        release_window=window.status if window is not None else None,
        safety_allowed=decision.allowed if decision is not None else None,
        remaining_payload_count=status.remaining_payload_count,
        total_payload_count=len(status.payload_slots),
        target_id=target_id,
        active_payload_slot_id=status.active_payload_slot_id,
        target_confidence=track.confidence_mean if track is not None else None,
        relative_bearing_deg=window.relative_bearing_deg if window is not None else None,
        estimated_range_m=window.estimated_ground_range_m if window is not None else None,
        cross_track_error_m=window.cross_track_error_m if window is not None else None,
        along_track_error_m=window.along_track_error_m if window is not None else None,
        release_lead_distance_m=(window.release_lead_distance_m if window is not None else None),
        produced_at_s=produced_at_s,
    )


def build_safety_status_message(
    *,
    mission_id: str,
    sequence: int,
    status: MissionStatus,
    outcome: ObservationOutcome,
    produced_at_s: float,
) -> SafetyStatusMessage | None:
    """Build explanatory rule masks only when a concrete decision exists."""

    _preferred_target_id, decision = _preferred_decision(status=status, outcome=outcome)
    if decision is None:
        return None
    return SafetyStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        mission_id=mission_id,
        target_id=decision.target_id,
        ruleset_version=decision.ruleset_version,
        checks=decision.checks,
        produced_at_s=produced_at_s,
    )


def build_authorization_challenge_status_message(
    *,
    challenge: AuthorizationChallenge,
    sequence: int,
    produced_at_s: float,
    challenge_clock_now_s: float | None = None,
) -> AuthorizationChallengeStatusMessage:
    """Tokenize one pending challenge for G20 without exposing its nonce."""

    clock_offset_s = 0.0 if challenge_clock_now_s is None else produced_at_s - challenge_clock_now_s

    return AuthorizationChallengeStatusMessage(
        challenge_token=operator_identifier_token(challenge.challenge_id),
        mission_token=operator_identifier_token(challenge.mission_id),
        target_token=operator_identifier_token(challenge.target_id),
        scene_token=operator_identifier_token(challenge.scene_digest),
        ruleset_token=operator_identifier_token(challenge.ruleset_version),
        payload_slot_token=operator_identifier_token(challenge.payload_slot_id),
        target_revision=challenge.target_revision,
        created_at_s=challenge.created_at_s + clock_offset_s,
        expires_at_s=challenge.expires_at_s + clock_offset_s,
        sequence=sequence,
        produced_at_s=produced_at_s,
    )


def build_patrol_status_message(
    *,
    mission_id: str,
    sequence: int,
    assessment: PatrolModeAssessment,
    tracks: tuple[UnifiedTrackSnapshot, ...],
    source_frame_id: str,
    source_captured_at_s: float,
    produced_at_s: float,
) -> PatrolStatusMessage:
    """Build compact mode-1 metadata without route, attitude or actuator commands."""

    primary = next(
        (
            track
            for track in tracks
            if assessment.primary_target_id is not None
            and track.track_id == assessment.primary_target_id
        ),
        None,
    )
    advisory = assessment.return_to_observe
    return PatrolStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        mission_id=mission_id,
        phase=assessment.phase,
        primary_target_id=assessment.primary_target_id,
        target_state=assessment.target_state,
        bbox=primary.bbox if primary is not None else None,
        label=primary.label if primary is not None else None,
        confidence=primary.confidence if primary is not None else None,
        tracking_quality=primary.tracking_quality if primary is not None else None,
        total_track_count=len(tracks),
        locked_track_count=sum(track.locked for track in tracks),
        source_frame_id=source_frame_id,
        source_captured_at_s=source_captured_at_s,
        produced_at_s=produced_at_s,
        return_direction=advisory.direction if advisory is not None else None,
        return_validity=advisory.validity if advisory is not None else None,
        return_evidence_age_s=advisory.evidence_age_s if advisory is not None else None,
        estimated_minimum_turn_radius_m=(
            advisory.estimated_minimum_turn_radius_m if advisory is not None else None
        ),
    )


def build_range_status_message(
    *,
    sequence: int,
    solution: RangeSolution,
    source_captured_at_s: float | None = None,
    source_frame_id: str | None = None,
    source_binding: RangeSolutionFrameBinding | None = None,
    produced_at_s: float | None = None,
) -> RangeStatusMessage:
    """Build complete read-only range metadata for authenticated operator transport.

    New production callers should create ``source_binding`` when the range
    solution is produced and retain it with the cached solution.  The legacy
    timestamp argument remains accepted, but is immediately converted into
    the same frozen binding and may optionally carry ``source_frame_id`` for a
    strict frame-identity check.
    """

    if source_binding is not None:
        if source_captured_at_s is not None or source_frame_id is not None:
            raise ValueError("source_binding cannot be combined with loose range source fields")
        source_binding.validate_solution(solution)
        binding = source_binding
    else:
        if source_captured_at_s is None:
            raise ValueError("range source capture timestamp is required")
        binding = RangeSolutionFrameBinding.from_solution(
            solution,
            source_frame_id=(solution.frame_id if source_frame_id is None else source_frame_id),
            source_captured_at_s=source_captured_at_s,
        )
    publish_time = solution.evaluated_at_s if produced_at_s is None else produced_at_s
    effective_freshness_s = binding.effective_data_freshness(produced_at_s=publish_time)

    return RangeStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        target_id=solution.target_id,
        calibration_id=solution.calibration_id,
        source_frame_id=binding.source_frame_id,
        source_captured_at_s=binding.source_captured_at_s,
        produced_at_s=publish_time,
        validity=solution.validity,
        reasons=_range_reasons_for_wire(solution.reasons),
        sources=solution.sources,
        rejected_sources=solution.rejected_sources,
        slant_range_m=solution.slant_range_m,
        ground_range_m=solution.ground_range_m,
        slant_range_ci95_m=solution.slant_range_ci95_m,
        ground_range_ci95_m=solution.ground_range_ci95_m,
        relative_bearing_deg=solution.relative_bearing_deg,
        absolute_bearing_deg=solution.absolute_bearing_deg,
        bearing_sigma_deg=solution.bearing_sigma_deg,
        north_offset_m=solution.north_offset_m,
        east_offset_m=solution.east_offset_m,
        data_freshness_s=effective_freshness_s,
        sensor_consistency=solution.sensor_consistency,
        source_contributions=tuple(
            sorted(
                solution.source_contributions,
                key=lambda contribution: contribution.weight,
                reverse=True,
            )[:3]
        ),
        fusion_profile=solution.fusion_profile,
        vehicle_profile=solution.vehicle_profile,
        navigation_state=solution.navigation_state,
        motion_regime=solution.motion_regime,
    )


def build_target_geolocation_status_message(
    *,
    sequence: int,
    solution: RangeSolution,
    telemetry: VehicleTelemetry,
    source_captured_at_s: float | None = None,
    source_frame_id: str | None = None,
    source_binding: RangeSolutionFrameBinding | None = None,
    produced_at_s: float | None = None,
    adaptive_ranging_policy: AdaptiveRangingPolicy | None = None,
) -> TargetGeolocationStatusMessage:
    """Build qualified target coordinates or an explicit withholding reason.

    The result is display-only.  In particular, local-NED, vision-only, stale,
    or otherwise unqualified navigation must never be promoted to WGS-84.
    """

    if source_binding is not None:
        if source_captured_at_s is not None or source_frame_id is not None:
            raise ValueError(
                "source_binding cannot be combined with loose geolocation source fields"
            )
        source_binding.validate_solution(solution)
        binding = source_binding
        source_captured_at_s = binding.source_captured_at_s
        source_frame_id = binding.source_frame_id
    else:
        if source_captured_at_s is None:
            raise ValueError("target geolocation source capture timestamp is required")
        source_frame_id = solution.frame_id if source_frame_id is None else source_frame_id
        if source_frame_id != solution.frame_id:
            raise ValueError("target geolocation source frame does not match range solution")
        binding = None
    publish_time = solution.evaluated_at_s if produced_at_s is None else produced_at_s
    values: dict[str, object] = {
        "sequence": sequence,
        "target_id": solution.target_id,
        "source_frame_id": source_frame_id,
        "source_captured_at_s": source_captured_at_s,
        "produced_at_s": publish_time,
    }
    # A target coordinate is tied to the camera/range frame that produced the
    # N/E offset.  Keep the producer-side gate aligned with the Python/C++
    # wire decoders so a new signature cannot revive an old map point.
    try:
        source_time = float(source_captured_at_s)
    except (TypeError, ValueError):
        source_time = None
    if source_time is None or not isfinite(source_time) or source_time < 0.0:
        # A malformed frame timestamp cannot be represented by the target
        # geolocation wire contract.  Raise here so live.py can drop the
        # optional metadata atomically instead of constructing a contradictory
        # ``produced_at_s < source_captured_at_s`` status.
        raise ValueError("target geolocation source timestamp is invalid")
    if source_time > solution.evaluated_at_s or source_time > publish_time:
        # Future frame timestamps are a clock-domain error.  Do not allow them
        # to enter the motion compensator and create a point ahead of the
        # range solution's evaluation time.
        raise ValueError("target geolocation source frame is from the future")
    if publish_time - source_time > MAX_TARGET_GEOLOCATION_SOURCE_AGE_S:
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="geolocation_input_invalid",
        )
    if binding is not None:
        effective_freshness_s = binding.effective_data_freshness(produced_at_s=publish_time)
        if (
            effective_freshness_s is None
            or effective_freshness_s > MAX_TARGET_GEOLOCATION_SOURCE_AGE_S
        ):
            return TargetGeolocationStatusMessage(
                **values,
                available=False,
                reason="geolocation_input_invalid",
            )
    if solution.validity is RangeValidity.INVALID:
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="target_range_invalid",
        )
    if solution.navigation_state != "gps-aided":
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="gps_navigation_not_qualified",
        )
    if solution.north_offset_m is None or solution.east_offset_m is None:
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="target_offset_unavailable",
        )
    gps_quality = (adaptive_ranging_policy or AdaptiveRangingPolicy()).assess_gps_quality(
        telemetry,
        now_s=solution.evaluated_at_s,
    )
    if not gps_quality.qualified:
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="gps_navigation_not_qualified",
        )
    motion_kwargs = _geolocation_motion_kwargs(
        telemetry,
        source_captured_at_s=source_captured_at_s,
    )
    try:
        target = target_geolocation_from_ned_offset(
            aircraft_latitude_deg=telemetry.latitude_deg,
            aircraft_longitude_deg=telemetry.longitude_deg,
            north_offset_m=solution.north_offset_m,
            east_offset_m=solution.east_offset_m,
            aircraft_horizontal_sigma_m=telemetry.gps_horizontal_accuracy_m,
            ground_range_ci95_m=solution.ground_range_ci95_m,
            ground_range_m=solution.ground_range_m,
            bearing_sigma_deg=solution.bearing_sigma_deg,
            **motion_kwargs,
        )
    except ValueError:
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="geolocation_input_invalid",
        )
    if target.horizontal_sigma_m > 6553.4:
        return TargetGeolocationStatusMessage(
            **values,
            available=False,
            reason="target_uncertainty_out_of_wire_range",
        )
    return TargetGeolocationStatusMessage(
        **values,
        available=True,
        reason="gps_qualified",
        latitude_deg=target.latitude_deg,
        longitude_deg=target.longitude_deg,
        horizontal_sigma_m=target.horizontal_sigma_m,
    )


def _geolocation_motion_kwargs(
    telemetry: VehicleTelemetry,
    *,
    source_captured_at_s: float,
) -> dict[str, object]:
    """Return optional time-alignment inputs when the telemetry is complete.

    Older telemetry records do not carry a GPS observation timestamp or N/E
    velocity.  Keep those records on the historical geolocation path rather
    than turning a missing optional field into a false invalid result.  A
    complete velocity pair is forwarded only with its own observation time;
    the WGS-84 projector then validates that timestamp against the GPS/frame
    horizon before extrapolating.  A finite acceleration uncertainty is
    forwarded independently of the velocity timestamp and contributes only
    to the bounded error budget when motion compensation is actually used.
    """

    try:
        gps_observed_at_s = float(telemetry.gps_observed_at_s)
        target_time_s = float(source_captured_at_s)
    except (TypeError, ValueError):
        return {}
    if not (
        isfinite(gps_observed_at_s)
        and gps_observed_at_s >= 0.0
        and isfinite(target_time_s)
        and target_time_s >= 0.0
    ):
        return {}
    motion_kwargs: dict[str, object] = {
        "aircraft_observed_at_s": gps_observed_at_s,
        "target_captured_at_s": target_time_s,
    }
    # Velocity is a separate MAVLink stream.  Only use it for temporal
    # extrapolation when its own observation timestamp is available; otherwise
    # the projector intentionally keeps the historical no-extrapolation path.
    try:
        velocity_time_s = float(telemetry.velocity_observed_at_s)
    except (TypeError, ValueError):
        velocity_time_s = float("nan")
    if isfinite(velocity_time_s) and velocity_time_s >= 0.0:
        motion_kwargs.update(
            {
                "aircraft_velocity_north_mps": telemetry.velocity_north_mps,
                "aircraft_velocity_east_mps": telemetry.velocity_east_mps,
                "aircraft_velocity_observed_at_s": velocity_time_s,
            }
        )
    acceleration_uncertainty = telemetry.acceleration_uncertainty_mps2
    # ``nan`` means that the telemetry producer has no calibrated kinematic
    # envelope.  Forward every other value so the shared projector validates
    # it and rejects a malformed/negative uncertainty rather than dropping it.
    if not (isinstance(acceleration_uncertainty, float) and isnan(acceleration_uncertainty)):
        motion_kwargs["acceleration_uncertainty_mps2"] = acceleration_uncertainty
    return motion_kwargs


def build_release_status_message(
    *,
    sequence: int,
    solution: DeploymentWindowSolution,
) -> ReleaseStatusMessage:
    """Build authenticated, display-only Mode-2 impact and timing advice."""

    return ReleaseStatusMessage(
        sequence=sequence,
        target_id=solution.target_id,
        calibration_id=solution.calibration_id,
        produced_at_s=solution.evaluated_at_s,
        timing_status=solution.timing_status,
        reasons=solution.reasons,
        range_target_id=solution.range_target_id,
        range_frame_id=solution.range_frame_id,
        target_north_offset_m=solution.target_north_offset_m,
        target_east_offset_m=solution.target_east_offset_m,
        impact_north_offset_m=solution.impact_north_offset_m,
        impact_east_offset_m=solution.impact_east_offset_m,
        along_track_error_m=solution.along_track_error_m,
        cross_track_error_m=solution.cross_track_error_m,
        error_ellipse_major_m=solution.error_ellipse_major_m,
        error_ellipse_minor_m=solution.error_ellipse_minor_m,
        error_ellipse_orientation_deg=solution.error_ellipse_orientation_deg,
        estimated_ground_range_m=solution.estimated_ground_range_m,
        ground_range_ci95_m=solution.ground_range_ci95_m,
        payload_descent_time_s=solution.payload_descent_time_s,
        release_lead_distance_m=solution.release_lead_distance_m,
        range_sensor_consistency=solution.range_sensor_consistency,
    )


def build_approach_challenge_status_message(
    *,
    challenge: SlideConfirmationChallenge,
    selection_command_id: str,
    sequence: int,
    produced_at_s: float,
    challenge_clock_now_s: float,
) -> ApproachChallengeStatusMessage:
    """Tokenize a Mode-3 slide challenge and translate monotonic time to wire time."""

    clock_offset_s = produced_at_s - challenge_clock_now_s
    return ApproachChallengeStatusMessage(
        challenge_token=operator_identifier_token(challenge.token),
        target_token=operator_identifier_token(challenge.target_id),
        target_revision=challenge.target_revision,
        selection_command_id=selection_command_id,
        issued_at_s=challenge.issued_at_s + clock_offset_s,
        expires_at_s=challenge.expires_at_s + clock_offset_s,
        sequence=sequence,
        produced_at_s=produced_at_s,
    )


def build_payload_target_challenge_status_message(
    *,
    challenge: PayloadSlideChallenge,
    sequence: int,
    produced_at_s: float,
    challenge_clock_now_s: float,
) -> PayloadTargetChallengeStatusMessage:
    """Tokenize a Mode-2 challenge binding both selected object and fire aimpoint."""

    clock_offset_s = produced_at_s - challenge_clock_now_s
    return PayloadTargetChallengeStatusMessage(
        challenge_token=operator_identifier_token(challenge.token),
        selected_target_token=operator_identifier_token(challenge.selected_target_id),
        selected_target_revision=challenge.selected_target_revision,
        aimpoint_target_token=operator_identifier_token(challenge.aimpoint_target_id),
        aimpoint_target_revision=challenge.aimpoint_target_revision,
        selection_command_id=challenge.selection_command_id,
        issued_at_s=challenge.issued_at_s + clock_offset_s,
        expires_at_s=challenge.expires_at_s + clock_offset_s,
        sequence=sequence,
        produced_at_s=produced_at_s,
    )


def build_payload_target_status_message(
    *,
    resolution: PayloadTargetResolution,
    challenge: PayloadSlideChallenge | None,
    grant: PayloadSlideGrant | None,
    sequence: int,
    produced_at_s: float,
    resolution_clock_now_s: float,
) -> PayloadTargetStatusMessage:
    """Build Mode-2 eligibility and confirmation metadata in the operator clock domain."""

    confirmation = grant if grant is not None else challenge
    clock_offset_s = produced_at_s - resolution_clock_now_s
    return PayloadTargetStatusMessage(
        sequence=sequence,
        selection_command_id=resolution.selection_command_id,
        selected_target_token=operator_identifier_token(resolution.selected_target_id),
        selected_target_revision=resolution.selected_target_revision,
        eligibility=resolution.eligibility,
        produced_at_s=produced_at_s,
        aimpoint_target_token=(
            operator_identifier_token(str(resolution.aimpoint_target_id))
            if resolution.aimpoint_target_id is not None
            else None
        ),
        aimpoint_target_revision=resolution.aimpoint_target_revision,
        confirmation_pending=challenge is not None and grant is None,
        confirmation_accepted=grant is not None,
        confirmation_expires_at_s=(
            confirmation.expires_at_s + clock_offset_s if confirmation is not None else None
        ),
    )


def build_approach_status_message(
    *,
    assessment: ApproachHilAssessment,
    sequence: int,
    produced_at_s: float,
    assessment_clock_now_s: float,
    flight_control_enabled: bool = False,
) -> ApproachStatusMessage:
    """Build Mode-3 state with explicit Jetson control-authority metadata."""

    clock_offset_s = produced_at_s - assessment_clock_now_s
    return ApproachStatusMessage(
        sequence=sequence,
        target_id=assessment.target_id,
        target_revision=assessment.target_revision,
        phase=assessment.phase,
        reasons=tuple(
            "fixed_wing_aim_active" if reason == "centering_advice_only" else reason
            for reason in assessment.reasons
        )
        if flight_control_enabled
        else assessment.reasons,
        produced_at_s=produced_at_s,
        yaw_error_deg=assessment.yaw_error_deg,
        pitch_error_deg=assessment.pitch_error_deg,
        yaw_advice_deg=assessment.yaw_advice_deg,
        pitch_advice_deg=assessment.pitch_advice_deg,
        bank_advice_deg=assessment.bank_advice_deg,
        climb_pitch_advice_deg=assessment.climb_pitch_advice_deg,
        ground_range_m=assessment.ground_range_m,
        confirmation_expires_at_s=(
            assessment.confirmation_expires_at_s + clock_offset_s
            if assessment.confirmation_expires_at_s is not None
            else None
        ),
        advisory_only=not flight_control_enabled,
        sitl_hil_only=not flight_control_enabled,
        flight_control_enabled=flight_control_enabled,
    )


def build_target_pool_status_messages(
    *,
    sequence_start: int,
    pool_revision: int,
    tracks: tuple[UnifiedTrackSnapshot, ...],
    produced_at_s: float,
    include_tentative: bool = True,
    operator_tracked_ids: Collection[str] = (),
    relative_bearing_by_target_id: Mapping[str, float] | None = None,
    estimated_range_by_target_id: Mapping[str, float] | None = None,
    target_speed_by_target_id: Mapping[str, float] | None = None,
) -> tuple[TargetPoolStatusMessage, ...]:
    """Build deterministic pages; stable typed targets lead generic fallback tracks."""

    if not isinstance(include_tentative, bool):
        raise ValueError("include_tentative must be a boolean")
    tracked_ids = frozenset(operator_tracked_ids)
    bearing_by_id = relative_bearing_by_target_id or {}
    range_by_id = estimated_range_by_target_id or {}
    speed_by_id = target_speed_by_target_id or {}
    # LOST tracks stay in the onboard bank for bounded reacquisition, but they are
    # not current click targets. Publishing their last boxes creates stale "+"
    # markers in QGC and lets the operator select empty image regions.
    visible_tracks = tuple(
        track
        for track in tracks
        if track.state is not UnifiedTrackState.LOST
        and (include_tentative or track.state is not UnifiedTrackState.DETECTED)
    )

    if not visible_tracks:
        return (
            TargetPoolStatusMessage(
                sequence=sequence_start,
                pool_revision=pool_revision,
                page_index=0,
                page_count=1,
                total_track_count=0,
                entries=(),
                produced_at_s=produced_at_s,
            ),
        )
    ordered = sorted(
        visible_tracks,
        key=lambda item: (
            not item.primary,
            not item.locked,
            not item.actionable,
            _target_label_priority(item.label),
            -item.tracking_quality,
            item.track_id,
        ),
    )
    if len(ordered) > 255:
        raise ValueError("target-pool wire status supports at most 255 tracks")
    pages = [ordered[index : index + 2] for index in range(0, len(ordered), 2)]
    messages: list[TargetPoolStatusMessage] = []
    for page_index, page in enumerate(pages):
        entries = tuple(
            TargetPoolEntry(
                target_id=track.track_id,
                state=track.state,
                label=track.label,
                confidence=track.confidence,
                tracking_quality=track.tracking_quality,
                locked=track.locked,
                primary=track.primary,
                actionable=track.actionable,
                reid_confirmed=track.reid_confirmed,
                operator_tracked=track.track_id in tracked_ids,
                bbox=track.bbox,
                relative_bearing_deg=bearing_by_id.get(track.track_id),
                estimated_range_m=range_by_id.get(track.track_id),
                target_speed_mps=speed_by_id.get(track.track_id),
            )
            for track in page
        )
        messages.append(
            TargetPoolStatusMessage(
                sequence=(sequence_start + page_index) & 0xFFFFFFFF,
                pool_revision=pool_revision,
                page_index=page_index,
                page_count=len(pages),
                total_track_count=len(ordered),
                entries=entries,
                produced_at_s=produced_at_s,
            )
        )
    return tuple(messages)


def _target_label_priority(label: str) -> int:
    normalized = label.strip().lower()
    if normalized in {"fire", "flame", "smoke", "smoldering_area", "burned_area"}:
        return 0
    if normalized in {"person", "firefighter"}:
        return 1
    if normalized in {
        "vehicle",
        "car",
        "truck",
        "bus",
        "motorcycle",
        "bicycle",
        "train",
        "boat",
    }:
        return 2
    if normalized in {"power_line", "flammable_tank", "building", "road"}:
        return 3
    return 4


def build_scene_context_status_messages(
    *,
    sequence_start: int,
    context_revision: int,
    snapshot: SemanticContextSnapshot,
    produced_at_s: float,
    maximum_age_s: float,
) -> tuple[SceneContextStatusMessage, ...]:
    """Build confidence-free road/building pages; stale or invalid evidence clears QGC."""

    age_s = produced_at_s - snapshot.produced_at_s
    if age_s < 0.0 or age_s > maximum_age_s:
        state = SceneContextState.STALE
        regions = ()
    elif snapshot.state is not ModelContextState.VALID:
        state = SceneContextState.INVALID
        regions = ()
    else:
        state = SceneContextState.VALID
        regions = snapshot.regions
    if len(regions) > 255:
        raise ValueError("scene-context wire status supports at most 255 regions")
    pages = [regions[index : index + 2] for index in range(0, len(regions), 2)] or [()]
    return tuple(
        SceneContextStatusMessage(
            sequence=(sequence_start + page_index) & 0xFFFFFFFF,
            context_revision=context_revision,
            source_frame_id=snapshot.frame_id,
            source_captured_at_s=snapshot.captured_at_s,
            state=state,
            page_index=page_index,
            page_count=len(pages),
            total_region_count=len(regions),
            entries=tuple(
                SceneContextRegionEntry(
                    label=region.label,
                    bbox=region.bbox,
                    frame_area_fraction=region.frame_area_fraction,
                    bbox_fill_fraction=region.bbox_fill_fraction,
                )
                for region in page
            ),
            produced_at_s=produced_at_s,
        )
        for page_index, page in enumerate(pages)
    )


def _preferred_decision(
    *,
    status: MissionStatus,
    outcome: ObservationOutcome,
):
    preferred_target_id = status.active_target_id
    if preferred_target_id is None and outcome.challenge is not None:
        preferred_target_id = outcome.challenge.target_id
    decision = next(
        (
            item
            for item in outcome.decisions
            if preferred_target_id is not None and item.target_id == preferred_target_id
        ),
        None,
    )
    if decision is None and outcome.decisions:
        decision = max(outcome.decisions, key=lambda item: item.priority_score)
    return preferred_target_id, decision


__all__ = [
    "RangeSolutionFrameBinding",
    "build_authorization_challenge_status_message",
    "build_approach_challenge_status_message",
    "build_approach_status_message",
    "build_mission_status_message",
    "build_patrol_status_message",
    "build_payload_target_challenge_status_message",
    "build_payload_target_status_message",
    "build_range_status_message",
    "build_release_status_message",
    "build_runtime_status_from_descriptor",
    "build_runtime_status_message",
    "build_safety_status_message",
    "build_scene_context_status_messages",
    "build_target_pool_status_messages",
    "build_target_geolocation_status_message",
]
