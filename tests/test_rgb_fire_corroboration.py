from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox, Detection, SensorKind
from multidetect.rgb_fire_corroboration import (
    RGB_FIRE_EVIDENCE_CONTRACT_VERSION,
    IndependentRgbFireCorroborationConfig,
    IndependentRgbFireCorroborator,
    is_qualified_independent_rgb_fire_evidence,
)

PRIMARY_HASH = "1" * 64
VERIFIER_HASH = "2" * 64
BOX = BoundingBox(0.2, 0.2, 0.5, 0.6)


def _corroborator(**changes) -> IndependentRgbFireCorroborator:
    values = {
        "evidence_qualified": True,
        "primary_artifact_sha256": PRIMARY_HASH,
        "verifier_artifact_sha256": VERIFIER_HASH,
    }
    values.update(changes)
    return IndependentRgbFireCorroborator(IndependentRgbFireCorroborationConfig(**values))


def _primary(**changes) -> Detection:
    values = {
        "label": "flame",
        "confidence": 0.91,
        "bbox": BOX,
        "sensor": SensorKind.RGB,
        "model_version": "primary-v1",
    }
    values.update(changes)
    return Detection(**values)


def _verifier(**changes) -> Detection:
    values = {
        "label": "flame",
        "confidence": 0.88,
        "bbox": BoundingBox(0.22, 0.22, 0.51, 0.61),
        "sensor": SensorKind.RGB,
        "model_version": "verifier-v1",
    }
    values.update(changes)
    return Detection(**values)


def test_distinct_qualified_rgb_model_corroborates_one_primary_candidate() -> None:
    result = _corroborator().corroborate((_primary(),), (_verifier(),))

    (detection,) = result.detections
    assert result.corroborated_detection_count == 1
    assert detection.metadata["independent_rgb_corroborated"] is True
    assert (
        detection.metadata["independent_rgb_evidence_contract_version"]
        == RGB_FIRE_EVIDENCE_CONTRACT_VERSION
    )
    assert detection.metadata["independent_rgb_verifier_model_version"] == "verifier-v1"
    assert detection.metadata["independent_rgb_primary_artifact_sha256"] == PRIMARY_HASH
    assert detection.metadata["independent_rgb_verifier_artifact_sha256"] == VERIFIER_HASH
    assert is_qualified_independent_rgb_fire_evidence(detection) is True


def test_unqualified_evidence_never_corroborates_even_when_boxes_match() -> None:
    result = IndependentRgbFireCorroborator(
        IndependentRgbFireCorroborationConfig(evidence_qualified=False)
    ).corroborate((_primary(),), (_verifier(),))

    assert result.corroborated_detection_count == 0
    assert result.detections[0].metadata["independent_rgb_corroborated"] is False


def test_upstream_self_asserted_corroboration_is_removed_without_verifier_match() -> None:
    primary = _primary(
        metadata={
            "independent_rgb_corroborated": True,
            "independent_rgb_verifier_model_version": "forged",
            "unrelated": "preserved",
        }
    )

    result = _corroborator().fail_closed((primary,))

    assert result.detections[0].metadata == {
        "unrelated": "preserved",
        "independent_rgb_corroborated": False,
    }


def test_same_model_version_cannot_be_its_own_independent_verifier() -> None:
    result = _corroborator().corroborate(
        (_primary(),),
        (_verifier(model_version="primary-v1"),),
    )

    assert result.rejected_same_model_count == 1
    assert result.corroborated_detection_count == 0


def test_mismatched_label_low_confidence_and_low_iou_all_fail_closed() -> None:
    for verifier in (
        _verifier(label="smoke"),
        _verifier(confidence=0.2),
        _verifier(bbox=BoundingBox(0.7, 0.7, 0.9, 0.9)),
    ):
        result = _corroborator().corroborate((_primary(),), (verifier,))
        assert result.corroborated_detection_count == 0
        assert result.detections[0].metadata["independent_rgb_corroborated"] is False


def test_verifier_only_detection_never_enters_target_population() -> None:
    person = Detection("person", 0.9, BOX, model_version="safety-v1")
    result = _corroborator().corroborate((person,), (_verifier(),))

    assert result.detections == (person,)
    assert result.primary_fire_candidate_count == 0
    assert result.verifier_candidate_count == 1


def test_global_one_to_one_matching_prevents_one_verifier_box_confirming_two_targets() -> None:
    first = _primary(bbox=BoundingBox(0.1, 0.1, 0.4, 0.5))
    second = _primary(bbox=BoundingBox(0.2, 0.1, 0.5, 0.5))
    verifier = _verifier(bbox=BoundingBox(0.12, 0.1, 0.48, 0.5))

    result = _corroborator(minimum_iou=0.2).corroborate((first, second), (verifier,))

    assert result.corroborated_detection_count == 1
    assert (
        sum(detection.metadata["independent_rgb_corroborated"] for detection in result.detections)
        == 1
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("minimum_iou", 0.0),
        ("minimum_iou", float("nan")),
        ("minimum_verifier_confidence", 1.1),
        ("evidence_qualified", 1),
        ("primary_artifact_sha256", "bad"),
        ("verifier_artifact_sha256", "bad"),
    ),
)
def test_config_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        IndependentRgbFireCorroborationConfig(**{field: value})


def test_qualified_config_requires_distinct_hash_bound_artifacts() -> None:
    with pytest.raises(ValueError, match="both artifact hashes"):
        IndependentRgbFireCorroborationConfig(evidence_qualified=True)
    with pytest.raises(ValueError, match="must be different"):
        IndependentRgbFireCorroborationConfig(
            evidence_qualified=True,
            primary_artifact_sha256=PRIMARY_HASH,
            verifier_artifact_sha256=PRIMARY_HASH,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("independent_rgb_evidence_contract_version", 2),
        ("independent_rgb_label", "smoke"),
        ("independent_rgb_iou", float("nan")),
        ("independent_rgb_confidence", 1.1),
        ("independent_rgb_verifier_model_version", "primary-v1"),
        ("independent_rgb_primary_artifact_sha256", VERIFIER_HASH),
    ),
)
def test_downstream_contract_validation_rejects_tampered_evidence(
    field: str,
    value: object,
) -> None:
    detection = _corroborator().corroborate((_primary(),), (_verifier(),)).detections[0]
    metadata = dict(detection.metadata)
    metadata[field] = value
    tampered = Detection(
        label=detection.label,
        confidence=detection.confidence,
        bbox=detection.bbox,
        sensor=detection.sensor,
        model_version=detection.model_version,
        metadata=metadata,
    )

    assert is_qualified_independent_rgb_fire_evidence(tampered) is False
