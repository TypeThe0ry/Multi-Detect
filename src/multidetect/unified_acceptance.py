from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .domain import BoundingBox
from .unified_tracking import (
    AppearanceEmbedding,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackState,
    _minimum_cost_candidate_assignment,
)


@dataclass(frozen=True, slots=True)
class UnifiedTrackingAcceptanceConfig:
    track_count: int = 10
    benchmark_frames: int = 300
    minimum_metadata_rate_hz: float = 15.0
    maximum_switch_latency_ms: float = 200.0
    maximum_short_occlusion_s: float = 0.5
    maximum_reacquisition_s: float = 2.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.track_count, bool)
            or not isinstance(self.track_count, int)
            or not 10 <= self.track_count <= 64
        ):
            raise ValueError("unified acceptance track_count must be an integer in [10, 64]")
        if (
            isinstance(self.benchmark_frames, bool)
            or not isinstance(self.benchmark_frames, int)
            or not 30 <= self.benchmark_frames <= 1_000_000
        ):
            raise ValueError(
                "unified acceptance benchmark_frames must be an integer in [30, 1000000]"
            )
        numeric = (
            self.minimum_metadata_rate_hz,
            self.maximum_switch_latency_ms,
            self.maximum_short_occlusion_s,
            self.maximum_reacquisition_s,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in numeric):
            raise ValueError("unified acceptance thresholds must be finite and positive")


def run_unified_tracking_acceptance(
    config: UnifiedTrackingAcceptanceConfig | None = None,
) -> dict[str, object]:
    """Produce deterministic acceptance evidence for the shared multi-target foundation."""

    cfg = config or UnifiedTrackingAcceptanceConfig()
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            maximum_tracks=max(16, cfg.track_count),
            minimum_confirmed_hits=1,
        )
    )
    initial = pool.update(
        frame_id="unified-acceptance-0",
        captured_at_s=1.0,
        observations=_scene(cfg.track_count, phase=0.0),
    )
    if len(initial.tracks) != cfg.track_count:
        raise RuntimeError("unified target pool did not retain the required track count")
    expected_track_ids = {item.track_id for item in initial.tracks}
    first_id, second_id = initial.tracks[0].track_id, initial.tracks[1].track_id
    pool.lock(first_id, now_s=1.01)
    pool.lock(second_id, now_s=1.02)
    switched = pool.switch_primary(second_id, now_s=1.03)
    snapshots = {item.track_id: item for item in pool.snapshots()}
    if not (
        switched.switch_latency_ms <= cfg.maximum_switch_latency_ms
        and switched.primary_track_id == second_id
        and switched.background_locked_track_ids == (first_id,)
        and snapshots[first_id].locked
        and not snapshots[first_id].primary
        and snapshots[second_id].locked
        and snapshots[second_id].primary
    ):
        raise RuntimeError("primary switching or background lock retention acceptance failed")

    association_samples_ms: list[float] = []
    switch_samples_ms = [switched.switch_latency_ms]
    benchmark_started_s = time.perf_counter()
    for index in range(1, cfg.benchmark_frames + 1):
        update = pool.update(
            frame_id=f"unified-acceptance-{index}",
            captured_at_s=1.0 + index / 30.0,
            observations=_scene(cfg.track_count, phase=index / 30.0),
        )
        association_samples_ms.append(update.association_latency_ms)
        snapshots = {item.track_id: item for item in update.tracks}
        if set(snapshots) != expected_track_ids:
            raise RuntimeError("unified target identities changed during the sustained benchmark")
        if not snapshots[first_id].locked or not snapshots[second_id].locked:
            raise RuntimeError("background target lock was dropped during the sustained benchmark")
        if index % 60 == 0:
            next_primary = first_id if pool.primary_track_id == second_id else second_id
            switch_sample = pool.switch_primary(
                next_primary,
                now_s=1.0 + index / 30.0 + 0.001,
            )
            switch_samples_ms.append(switch_sample.switch_latency_ms)
            if switch_sample.switch_latency_ms > cfg.maximum_switch_latency_ms:
                raise RuntimeError("repeated primary switch exceeded its latency budget")
    benchmark_elapsed_s = time.perf_counter() - benchmark_started_s
    p50_ms = _percentile(association_samples_ms, 0.50)
    p95_ms = _percentile(association_samples_ms, 0.95)
    p99_ms = _percentile(association_samples_ms, 0.99)
    p95_capacity_hz = 1_000.0 / max(p95_ms, 1e-9)
    measured_end_to_end_rate_hz = cfg.benchmark_frames / max(benchmark_elapsed_s, 1e-9)
    if (
        p95_capacity_hz < cfg.minimum_metadata_rate_hz
        or measured_end_to_end_rate_hz < cfg.minimum_metadata_rate_hz
    ):
        raise RuntimeError("unified target pool cannot sustain the required metadata rate")

    short_recovery = _short_occlusion_acceptance(cfg)
    conservative = _lost_identity_acceptance(with_reid=False, cfg=cfg)
    reidentified = _lost_identity_acceptance(with_reid=True, cfg=cfg)
    crossing = _crossing_identity_acceptance()
    ambiguous = _ambiguous_identity_acceptance()
    kalman_prediction = _kalman_prediction_acceptance()
    global_assignment = _global_assignment_acceptance()
    confidence_cascade = _confidence_cascade_acceptance()
    return {
        "track_count": cfg.track_count,
        "background_locked_track_count": len(switched.background_locked_track_ids),
        "primary_switch_latency_ms": switched.switch_latency_ms,
        "maximum_repeated_switch_latency_ms": max(switch_samples_ms),
        "repeated_switch_latency_p95_ms": _percentile(switch_samples_ms, 0.95),
        "repeated_primary_switch_count": len(switch_samples_ms),
        "primary_switch_budget_ms": cfg.maximum_switch_latency_ms,
        "association_latency_p50_ms": p50_ms,
        "association_latency_p95_ms": p95_ms,
        "association_latency_p99_ms": p99_ms,
        "association_latency_maximum_ms": max(association_samples_ms),
        "benchmark_frame_count": cfg.benchmark_frames,
        "benchmark_elapsed_s": benchmark_elapsed_s,
        "measured_end_to_end_metadata_rate_hz": measured_end_to_end_rate_hz,
        "sustained_metadata_rate_hz_at_p95": p95_capacity_hz,
        "minimum_metadata_rate_hz": cfg.minimum_metadata_rate_hz,
        "short_occlusion": short_recovery,
        "lost_without_reid": conservative,
        "lost_with_strong_reid": reidentified,
        "crossing_identity": crossing,
        "ambiguous_identity": ambiguous,
        "ambiguous_identity_forced": ambiguous["identity_forced"],
        "kalman_prediction": kalman_prediction,
        "global_assignment": global_assignment,
        "confidence_cascade": confidence_cascade,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _global_assignment_acceptance() -> dict[str, object]:
    candidates = (
        (0, 0.10, "track-a", 0, None, False, False),
        (0, 0.20, "track-a", 1, None, False, False),
        (0, 0.11, "track-b", 0, None, False, False),
        (0, 0.90, "track-b", 1, None, False, False),
    )
    selected = _minimum_cost_candidate_assignment(candidates)
    pairs = sorted((candidate[2], candidate[3]) for candidate in selected)
    global_cost = round(sum(candidate[1] for candidate in selected), 6)
    greedy_cost = 1.0
    if pairs != [("track-a", 1), ("track-b", 0)] or global_cost >= greedy_cost:
        raise RuntimeError("global assignment acceptance did not improve the greedy counterexample")
    return {
        "algorithm": "cascaded_rectangular_hungarian",
        "greedy_cost": greedy_cost,
        "global_cost": global_cost,
        "selected_pairs": [[track_id, observation_index] for track_id, observation_index in pairs],
    }


def _confidence_cascade_acceptance() -> dict[str, object]:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_association_confidence=0.10,
            minimum_new_track_confidence=0.35,
            high_confidence_threshold=0.55,
        )
    )
    initial = pool.update(
        frame_id="confidence-acceptance-0",
        captured_at_s=60.0,
        observations=(_observation_with_confidence(0.20, 0.40, 0.90),),
    )
    original_id = initial.tracks[0].track_id
    update = pool.update(
        frame_id="confidence-acceptance-1",
        captured_at_s=60.1,
        observations=(
            _observation_with_confidence(0.22, 0.40, 0.20),
            _observation_with_confidence(0.80, 0.70, 0.20),
        ),
    )
    if not (
        len(update.tracks) == 1
        and update.tracks[0].track_id == original_id
        and update.tracks[0].observation_count == 2
        and update.dropped_observation_count == 1
    ):
        raise RuntimeError("confidence-cascade acceptance created a low-confidence identity")
    return {
        "low_confidence_track_continuation": True,
        "low_confidence_new_identity_created": False,
        "suppressed_new_identity_count": update.dropped_observation_count,
        "minimum_association_confidence": 0.10,
        "minimum_new_track_confidence": 0.35,
        "high_confidence_threshold": 0.55,
    }


def _crossing_identity_acceptance() -> dict[str, object]:
    """Exercise appearance-aware association through crossing and one-frame occlusion."""

    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            maximum_center_distance=0.25,
            occluded_after_s=0.2,
            reacquisition_timeout_s=1.0,
            lost_retention_s=5.0,
            locked_lost_retention_s=5.0,
        )
    )
    initial = pool.update(
        frame_id="crossing-0",
        captured_at_s=30.0,
        observations=(
            _observation(0.25, 0.40, identity=2),
            _observation(0.75, 0.40, identity=3),
        ),
    )
    left_id, right_id = initial.tracks[0].track_id, initial.tracks[1].track_id
    pool.lock(left_id, now_s=30.001)
    pool.lock(right_id, now_s=30.002)
    pool.update(
        frame_id="crossing-1",
        captured_at_s=30.1,
        observations=(
            _observation(0.35, 0.40, identity=2),
            _observation(0.65, 0.40, identity=3),
        ),
    )
    pool.update(
        frame_id="crossing-2",
        captured_at_s=30.2,
        observations=(
            _observation(0.45, 0.40, identity=2),
            _observation(0.55, 0.40, identity=3),
        ),
    )
    occluded = pool.update(
        frame_id="crossing-3",
        captured_at_s=30.3,
        observations=(_observation(0.45, 0.40, identity=3),),
    )
    final = pool.update(
        frame_id="crossing-4",
        captured_at_s=30.4,
        # Reverse detector order after the targets cross.
        observations=(
            _observation(0.35, 0.40, identity=3),
            _observation(0.55, 0.40, identity=2),
        ),
    )
    occluded_by_id = {item.track_id: item for item in occluded.tracks}
    final_by_id = {item.track_id: item for item in final.tracks}
    passed = (
        len(final_by_id) == 2
        and occluded_by_id[left_id].state is UnifiedTrackState.OCCLUDED
        and left_id in final.recovered_track_ids
        and final_by_id[left_id].bbox.center[0] > final_by_id[right_id].bbox.center[0]
        and final_by_id[left_id].locked
        and final_by_id[right_id].locked
    )
    if not passed:
        raise RuntimeError("crossing-target ReID identity retention acceptance failed")
    return {
        "identity_switch_count": 0,
        "occluded_track_recovered": True,
        "background_locks_retained": True,
        "detector_order_reversed": True,
    }


def _ambiguous_identity_acceptance() -> dict[str, object]:
    """Prove that near-tied full-frame candidates cannot steal a lost identity."""

    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.08,
            strict_reid_ambiguity_margin=0.035,
        )
    )
    initial = pool.update(
        frame_id="ambiguous-0",
        captured_at_s=40.0,
        observations=(_observation(0.12, 0.40, identity=4),),
    )
    original_id = initial.tracks[0].track_id
    pool.lock(original_id, now_s=40.001)
    pool.update(frame_id="ambiguous-1", captured_at_s=40.5, observations=())
    identity_a = [0.0] * 16
    identity_a[4] = 0.999
    identity_a[5] = 0.04
    identity_b = [0.0] * 16
    identity_b[4] = 0.998
    identity_b[5] = 0.06
    result = pool.update(
        frame_id="ambiguous-2",
        captured_at_s=40.7,
        observations=(
            _observation_with_embedding(0.76, 0.35, tuple(identity_a)),
            _observation_with_embedding(0.86, 0.45, tuple(identity_b)),
        ),
    )
    original = next(item for item in result.tracks if item.track_id == original_id)
    identity_forced = (
        bool(result.recovered_track_ids) or original.state is not UnifiedTrackState.LOST
    )
    if identity_forced or result.ambiguous_reid_recovery_count != 2:
        raise RuntimeError("ambiguous full-frame ReID acceptance forced an identity")
    return {
        "identity_forced": False,
        "blocked_candidate_count": result.ambiguous_reid_recovery_count,
        "original_state": original.state.value,
        "new_track_count": len(result.tracks) - 1,
    }


def _kalman_prediction_acceptance() -> dict[str, object]:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            maximum_center_distance=0.12,
            kalman_process_noise=0.02,
            kalman_measurement_noise=0.0002,
        )
    )
    for index, center_x in enumerate((0.20, 0.24, 0.28)):
        pool.update(
            frame_id=f"kalman-acceptance-{index}",
            captured_at_s=50.0 + index * 0.1,
            observations=(_observation(center_x, 0.40, identity=5),),
        )
    forecast = pool.update(
        frame_id="kalman-acceptance-gap",
        captured_at_s=50.3,
        observations=(),
    ).tracks[0]
    expected_center_x = 0.32
    error = abs(forecast.predicted_bbox.center[0] - expected_center_x)
    if forecast.state is not UnifiedTrackState.OCCLUDED or error > 0.025:
        raise RuntimeError("Kalman detector-gap prediction acceptance failed")
    return {
        "model": "constant_velocity_kalman",
        "prediction_error_normalized": error,
        "maximum_error_normalized": 0.025,
        "state": forecast.state.value,
    }


def _short_occlusion_acceptance(cfg: UnifiedTrackingAcceptanceConfig) -> dict[str, object]:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.2,
            reacquisition_timeout_s=1.0,
            lost_retention_s=5.0,
            locked_lost_retention_s=5.0,
        )
    )
    first = pool.update(
        frame_id="short-0",
        captured_at_s=10.0,
        observations=(_observation(0.20, 0.40, identity=0),),
    )
    track_id = first.tracks[0].track_id
    pool.update(
        frame_id="short-1",
        captured_at_s=10.1,
        observations=(_observation(0.24, 0.40, identity=0),),
    )
    occluded_at_s = 10.25
    occluded = pool.update(frame_id="short-2", captured_at_s=occluded_at_s, observations=())
    recovered_at_s = 10.40
    recovered = pool.update(
        frame_id="short-3",
        captured_at_s=recovered_at_s,
        observations=(_observation(0.31, 0.40, identity=0),),
    )
    recovery_s = recovered_at_s - occluded_at_s
    if not (
        occluded.tracks[0].state is UnifiedTrackState.OCCLUDED
        and recovered.recovered_track_ids == (track_id,)
        and recovered.tracks[0].track_id == track_id
        and recovered.tracks[0].state is UnifiedTrackState.RECOVERED
        and recovery_s <= cfg.maximum_short_occlusion_s
    ):
        raise RuntimeError("short-occlusion identity recovery acceptance failed")
    return {
        "recovery_s": recovery_s,
        "budget_s": cfg.maximum_short_occlusion_s,
        "same_track_id": True,
        "state": recovered.tracks[0].state.value,
    }


def _lost_identity_acceptance(
    *,
    with_reid: bool,
    cfg: UnifiedTrackingAcceptanceConfig,
) -> dict[str, object]:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    first = pool.update(
        frame_id="lost-0",
        captured_at_s=20.0,
        observations=(_observation(0.20, 0.40, identity=1 if with_reid else None),),
    )
    original_id = first.tracks[0].track_id
    lost = pool.update(frame_id="lost-1", captured_at_s=20.5, observations=())
    reappeared_at_s = 20.7
    reappeared = pool.update(
        frame_id="lost-2",
        captured_at_s=reappeared_at_s,
        observations=(_observation(0.24, 0.40, identity=1 if with_reid else None),),
    )
    recovery_s = reappeared_at_s - 20.5
    if lost.tracks[0].state is not UnifiedTrackState.LOST:
        raise RuntimeError("lost-state acceptance did not enter LOST")
    if with_reid:
        accepted = (
            reappeared.recovered_track_ids == (original_id,)
            and len(reappeared.tracks) == 1
            and reappeared.tracks[0].track_id == original_id
            and reappeared.tracks[0].reid_confirmed
            and recovery_s <= cfg.maximum_reacquisition_s
        )
        if not accepted:
            raise RuntimeError("strong-ReID lost-target recovery acceptance failed")
        return {
            "recovery_s": recovery_s,
            "budget_s": cfg.maximum_reacquisition_s,
            "same_track_id": True,
            "reid_confirmed": True,
        }
    original = next(item for item in reappeared.tracks if item.track_id == original_id)
    conservative = (
        not reappeared.recovered_track_ids
        and len(reappeared.tracks) == 2
        and original.state is UnifiedTrackState.LOST
    )
    if not conservative:
        raise RuntimeError("identity was forced without ReID evidence")
    return {
        "original_state": original.state.value,
        "new_track_created": True,
        "same_track_id": False,
        "reid_confirmed": False,
    }


def _scene(count: int, *, phase: float) -> tuple[TargetObservation, ...]:
    columns = math.ceil(math.sqrt(count * 16.0 / 9.0))
    rows = math.ceil(count / columns)
    return tuple(
        _observation(
            0.08
            + (index % columns) * (0.84 / max(1, columns - 1))
            + math.sin(phase + index) * 0.0005,
            0.16 + (index // columns) * (0.68 / max(1, rows - 1)),
            identity=index,
        )
        for index in range(count)
    )


def _observation(
    center_x: float,
    center_y: float,
    *,
    identity: int | None,
) -> TargetObservation:
    size = 0.06
    appearance = None
    if identity is not None:
        values = [0.0] * 16
        values[identity % len(values)] = 1.0
        appearance = AppearanceEmbedding(tuple(values))
    return TargetObservation(
        label="vehicle",
        confidence=0.92,
        bbox=BoundingBox(
            center_x - size / 2.0,
            center_y - size / 2.0,
            center_x + size / 2.0,
            center_y + size / 2.0,
        ),
        appearance=appearance,
    )


def _observation_with_embedding(
    center_x: float,
    center_y: float,
    embedding: tuple[float, ...],
) -> TargetObservation:
    size = 0.06
    return TargetObservation(
        label="vehicle",
        confidence=0.92,
        bbox=BoundingBox(
            center_x - size / 2.0,
            center_y - size / 2.0,
            center_x + size / 2.0,
            center_y + size / 2.0,
        ),
        appearance=AppearanceEmbedding(embedding),
    )


def _observation_with_confidence(
    center_x: float,
    center_y: float,
    confidence: float,
) -> TargetObservation:
    size = 0.06
    return TargetObservation(
        label="vehicle",
        confidence=confidence,
        bbox=BoundingBox(
            center_x - size / 2.0,
            center_y - size / 2.0,
            center_x + size / 2.0,
            center_y + size / 2.0,
        ),
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


__all__ = ["UnifiedTrackingAcceptanceConfig", "run_unified_tracking_acceptance"]
