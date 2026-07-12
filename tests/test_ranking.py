from __future__ import annotations

from multidetect.domain import BoundingBox, TrackSnapshot
from multidetect.ranking import TargetRanker, TargetRiskAssessment


def track(track_id: str, *, confirmed: bool = True, growth: float = 0.0) -> TrackSnapshot:
    return TrackSnapshot(
        track_id=track_id,
        revision=4,
        label="flame",
        bbox=BoundingBox(0.2, 0.2, 0.4, 0.4),
        first_seen_at_s=0.0,
        last_seen_at_s=3.0,
        observation_count=4,
        consecutive_observations=4,
        confidence_floor=0.9,
        confidence_mean=0.92,
        maximum_gap_s=1.0,
        area_growth_rate=growth,
        thermal_corroborated=True,
        confirmed=confirmed,
    )


def test_ranker_prioritizes_risk_and_is_deterministic() -> None:
    tracks = [track("low"), track("high", growth=0.2)]
    assessments = {
        "low": TargetRiskAssessment(existing_response_coverage=0.8),
        "high": TargetRiskAssessment(
            spread_risk=0.8,
            people_exposure_risk=0.7,
            thermal_intensity=0.9,
        ),
    }

    ranked = TargetRanker().rank(tracks, assessments)

    assert [item.track.track_id for item in ranked] == ["high", "low"]
    assert ranked[0].score > ranked[1].score


def test_ranker_ignores_unconfirmed_tracks() -> None:
    ranked = TargetRanker().rank([track("candidate", confirmed=False)])

    assert ranked == ()
