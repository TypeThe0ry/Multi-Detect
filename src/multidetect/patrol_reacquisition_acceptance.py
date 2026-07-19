from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import BoundingBox, VehicleTelemetry
from .patrol_advisory import PatrolAdvisoryEngine, PatrolPhase
from .unified_tracking import (
    AppearanceEmbedding,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackSnapshot,
    UnifiedTrackState,
)


@dataclass(frozen=True, slots=True)
class PatrolReacquisitionAcceptanceConfig:
    track_count: int = 10
    maximum_short_occlusion_s: float = 0.5
    maximum_lost_reacquisition_s: float = 2.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.track_count, bool)
            or not isinstance(self.track_count, int)
            or not 10 <= self.track_count <= 64
        ):
            raise ValueError("patrol reacquisition track_count must be an integer in [10, 64]")
        for name, value in (
            ("maximum_short_occlusion_s", self.maximum_short_occlusion_s),
            ("maximum_lost_reacquisition_s", self.maximum_lost_reacquisition_s),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


def run_patrol_reacquisition_acceptance(
    telemetry: VehicleTelemetry,
    config: PatrolReacquisitionAcceptanceConfig | None = None,
) -> dict[str, object]:
    """Exercise mode-1 identity recovery and revisit advice without pixels or control output."""

    cfg = config or PatrolReacquisitionAcceptanceConfig()
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            maximum_tracks=max(16, cfg.track_count),
            minimum_confirmed_hits=1,
            occluded_after_s=0.10,
            reacquisition_timeout_s=0.35,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.16,
        )
    )
    base_s = 100.0
    initial = pool.update(
        frame_id="patrol-reacquisition-000",
        captured_at_s=base_s,
        observations=_scene(cfg.track_count, include_primary=True, phase=0.0),
    )
    if len(initial.tracks) != cfg.track_count:
        raise RuntimeError("patrol reacquisition did not initialize the required target pool")
    primary_id = initial.tracks[0].track_id
    background_id = initial.tracks[1].track_id
    phase_sequence = [_state(initial, primary_id)]

    locked = pool.lock(primary_id, now_s=base_s + 0.01)
    pool.lock(background_id, now_s=base_s + 0.011)
    phase_sequence.append(locked.state)
    switch_away = pool.switch_primary(background_id, now_s=base_s + 0.012)
    switch_back = pool.switch_primary(primary_id, now_s=base_s + 0.013)

    tracking = pool.update(
        frame_id="patrol-reacquisition-001",
        captured_at_s=base_s + 0.05,
        observations=_scene(cfg.track_count, include_primary=True, phase=0.05),
    )
    phase_sequence.append(_state(tracking, primary_id))
    occluded_at_s = base_s + 0.13
    occluded = pool.update(
        frame_id="patrol-reacquisition-002",
        captured_at_s=occluded_at_s,
        observations=_scene(cfg.track_count, include_primary=False, phase=0.13),
    )
    phase_sequence.append(_state(occluded, primary_id))
    reacquiring = pool.update(
        frame_id="patrol-reacquisition-003",
        captured_at_s=base_s + 0.25,
        observations=_scene(cfg.track_count, include_primary=False, phase=0.25),
    )
    phase_sequence.append(_state(reacquiring, primary_id))
    short_recovered_at_s = base_s + 0.30
    short_recovered = pool.update(
        frame_id="patrol-reacquisition-004",
        captured_at_s=short_recovered_at_s,
        observations=_scene(cfg.track_count, include_primary=True, phase=0.30),
    )
    phase_sequence.append(_state(short_recovered, primary_id))
    short_recovery_s = short_recovered_at_s - occluded_at_s
    if not (
        short_recovered.recovered_track_ids == (primary_id,)
        and short_recovery_s <= cfg.maximum_short_occlusion_s
    ):
        raise RuntimeError("short mode-1 occlusion did not recover the original identity in time")

    resumed = pool.update(
        frame_id="patrol-reacquisition-005",
        captured_at_s=base_s + 0.35,
        observations=_scene(cfg.track_count, include_primary=True, phase=0.35),
    )
    if _state(resumed, primary_id) is not UnifiedTrackState.TRACKING:
        raise RuntimeError("recovered patrol identity did not return to TRACKING")

    lost_branch_states: list[UnifiedTrackState] = [UnifiedTrackState.TRACKING]
    for frame_id, captured_at_s in (
        ("patrol-reacquisition-006", base_s + 0.43),
        ("patrol-reacquisition-007", base_s + 0.55),
        ("patrol-reacquisition-008", base_s + 0.75),
    ):
        update = pool.update(
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            observations=_scene(
                cfg.track_count,
                include_primary=False,
                phase=captured_at_s - base_s,
            ),
        )
        lost_branch_states.append(_state(update, primary_id))
    lost = update
    if _state(lost, primary_id) is not UnifiedTrackState.LOST:
        raise RuntimeError("long mode-1 occlusion did not enter LOST")

    advisory_assessment = PatrolAdvisoryEngine().assess(
        tracks=lost.tracks,
        primary_target_id=primary_id,
        telemetry=telemetry,
        now_s=base_s + 0.75,
    )
    advisory = advisory_assessment.return_to_observe
    if advisory_assessment.phase is not PatrolPhase.LOST or advisory is None:
        raise RuntimeError("LOST patrol target produced no return-to-observe advice")
    if advisory.flight_control_enabled or not advisory.advisory_only:
        raise RuntimeError("return-to-observe acceptance exceeded its advisory-only authority")
    if telemetry.position_healthy is True and telemetry.link_healthy is True:
        if advisory.validity.value == "invalid":
            raise RuntimeError("fresh SITL navigation unexpectedly invalidated revisit advice")

    lost_at_s = base_s + 0.75
    lost_recovered_at_s = base_s + 0.90
    lost_recovered = pool.update(
        frame_id="patrol-reacquisition-009",
        captured_at_s=lost_recovered_at_s,
        observations=_scene(cfg.track_count, include_primary=True, phase=0.90),
    )
    lost_recovery_s = lost_recovered_at_s - lost_at_s
    primary_after_recovery = _snapshot(lost_recovered, primary_id)
    if not (
        lost_recovered.recovered_track_ids == (primary_id,)
        and primary_after_recovery.state is UnifiedTrackState.RECOVERED
        and primary_after_recovery.reid_confirmed
        and lost_recovery_s <= cfg.maximum_lost_reacquisition_s
    ):
        raise RuntimeError("strong-ReID LOST recovery did not preserve the original identity")

    final = pool.update(
        frame_id="patrol-reacquisition-010",
        captured_at_s=base_s + 0.95,
        observations=_scene(cfg.track_count, include_primary=True, phase=0.95),
    )
    final_by_id = {item.track_id: item for item in final.tracks}
    if not (
        len(final_by_id) == cfg.track_count
        and set(final_by_id) == {item.track_id for item in initial.tracks}
        and final_by_id[primary_id].state is UnifiedTrackState.TRACKING
        and final_by_id[primary_id].locked
        and final_by_id[primary_id].primary
        and final_by_id[background_id].locked
        and not final_by_id[background_id].primary
    ):
        raise RuntimeError("target pool identity or background lock changed after recovery")

    required_sequence = (
        UnifiedTrackState.DETECTED,
        UnifiedTrackState.LOCKED,
        UnifiedTrackState.TRACKING,
        UnifiedTrackState.OCCLUDED,
        UnifiedTrackState.REACQUIRING,
        UnifiedTrackState.RECOVERED,
    )
    if tuple(phase_sequence) != required_sequence:
        raise RuntimeError("mode-1 state sequence did not match the required lifecycle")
    if tuple(lost_branch_states) != (
        UnifiedTrackState.TRACKING,
        UnifiedTrackState.OCCLUDED,
        UnifiedTrackState.REACQUIRING,
        UnifiedTrackState.LOST,
    ):
        raise RuntimeError("mode-1 LOST branch did not pass through conservative reacquisition")

    return {
        "track_count": len(final.tracks),
        "primary_target_id": primary_id,
        "background_locked_target_id": background_id,
        "state_sequence": [state.value for state in phase_sequence],
        "lost_branch_state_sequence": [state.value for state in lost_branch_states],
        "short_occlusion_recovery_s": short_recovery_s,
        "short_occlusion_budget_s": cfg.maximum_short_occlusion_s,
        "lost_reacquisition_s": lost_recovery_s,
        "lost_reacquisition_budget_s": cfg.maximum_lost_reacquisition_s,
        "same_identity_after_short_occlusion": True,
        "same_identity_after_lost_reid": True,
        "reid_confirmed_after_lost": True,
        "background_lock_retained": True,
        "primary_switch_latency_ms": max(
            switch_away.switch_latency_ms,
            switch_back.switch_latency_ms,
        ),
        "return_to_observe": {
            "phase": advisory_assessment.phase.value,
            "direction": advisory.direction.value,
            "validity": advisory.validity.value,
            "evidence_age_s": advisory.evidence_age_s,
            "estimated_minimum_turn_radius_m": advisory.estimated_minimum_turn_radius_m,
            "reasons": list(advisory.reasons),
            "operator_confirmation_required": advisory.operator_confirmation_required,
            "sitl_validation_required": advisory.sitl_validation_required,
            "advisory_only": advisory.advisory_only,
            "flight_control_enabled": advisory.flight_control_enabled,
        },
        "camera_opened": False,
        "model_inference_executed": False,
        "metadata_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _state(update: object, track_id: str) -> UnifiedTrackState:
    return _snapshot(update, track_id).state


def _snapshot(update: object, track_id: str) -> UnifiedTrackSnapshot:
    tracks = getattr(update, "tracks", ())
    matches = tuple(item for item in tracks if item.track_id == track_id)
    if len(matches) != 1:
        raise RuntimeError(f"target pool does not contain exactly one {track_id}")
    return matches[0]


def _scene(
    count: int,
    *,
    include_primary: bool,
    phase: float,
) -> tuple[TargetObservation, ...]:
    columns = math.ceil(math.sqrt(count * 16.0 / 9.0))
    rows = math.ceil(count / columns)
    step_x = 0.80 / max(1, columns - 1)
    step_y = 0.70 / max(1, rows - 1)
    half_width = min(0.03, step_x * 0.30)
    half_height = min(0.04, step_y * 0.30)
    observations = []
    for index in range(count):
        if index == 0 and not include_primary:
            continue
        column = index % columns
        row = index // columns
        center_x = 0.10 + column * step_x + math.sin(phase * 3.0 + index) * 0.002
        center_y = 0.15 + row * step_y
        identity = [0.0] * max(16, count)
        identity[index] = 1.0
        observations.append(
            TargetObservation(
                label="car" if index % 2 else "flame",
                confidence=0.92,
                bbox=BoundingBox(
                    center_x - half_width,
                    center_y - half_height,
                    center_x + half_width,
                    center_y + half_height,
                ),
                appearance=AppearanceEmbedding(tuple(identity)),
                appearance_reliable=True,
                source="deterministic_patrol_reacquisition_hil",
            )
        )
    return tuple(observations)


__all__ = [
    "PatrolReacquisitionAcceptanceConfig",
    "run_patrol_reacquisition_acceptance",
]
