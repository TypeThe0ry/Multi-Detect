from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .domain import TrackSnapshot


@dataclass(frozen=True, slots=True)
class TargetRiskAssessment:
    """Normalized advisory features supplied by independent scene models or planners."""

    spread_risk: float = 0.0
    people_exposure_risk: float = 0.0
    building_exposure_risk: float = 0.0
    thermal_intensity: float = 0.0
    flight_distance_normalized: float = 0.0
    existing_response_coverage: float = 0.0
    model_uncertainty: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("spread_risk", self.spread_risk),
            ("people_exposure_risk", self.people_exposure_risk),
            ("building_exposure_risk", self.building_exposure_risk),
            ("thermal_intensity", self.thermal_intensity),
            ("flight_distance_normalized", self.flight_distance_normalized),
            ("existing_response_coverage", self.existing_response_coverage),
            ("model_uncertainty", self.model_uncertainty),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class RankedTarget:
    track: TrackSnapshot
    score: float
    assessment: TargetRiskAssessment


class TargetRanker:
    """Orders confirmed candidates but has no authority to approve deployment."""

    def rank(
        self,
        tracks: Iterable[TrackSnapshot],
        assessments: Mapping[str, TargetRiskAssessment] | None = None,
    ) -> tuple[RankedTarget, ...]:
        assessments = assessments or {}
        ranked: list[RankedTarget] = []
        for track in tracks:
            if not track.confirmed:
                continue
            assessment = assessments.get(track.track_id, TargetRiskAssessment())
            score = self._score(track, assessment)
            ranked.append(RankedTarget(track=track, score=score, assessment=assessment))
        ranked.sort(key=lambda item: (-item.score, item.track.track_id))
        return tuple(ranked)

    @staticmethod
    def _score(track: TrackSnapshot, assessment: TargetRiskAssessment) -> float:
        # Personnel/building exposure raises response priority, while the safety engine
        # independently denies any deployment whose exclusion zone is occupied.
        return round(
            2.0 * max(0.0, min(track.area_growth_rate, 1.0))
            + 2.4 * assessment.people_exposure_risk
            + 1.8 * assessment.building_exposure_risk
            + 1.8 * assessment.spread_risk
            + 1.4 * assessment.thermal_intensity
            + 0.6 * track.confidence_mean
            - 0.7 * assessment.flight_distance_normalized
            - 1.5 * assessment.existing_response_coverage
            - 1.2 * assessment.model_uncertainty,
            6,
        )
