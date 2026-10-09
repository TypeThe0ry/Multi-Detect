from __future__ import annotations

import pytest

from scripts.verify_onboard_history_migration import validate_scaffold_metadata


def _provenance():
    parent = "a" * 64
    return (
        {
            "parent_model_sha256": parent,
            "temporal_training_performed": "false",
            "candidate_promoted": "false",
            "history_steps": "8",
        },
        {"parent_model_sha256": parent, "history_steps": 8},
        parent,
    )


def test_actual_graph_provenance_matches_report_and_reference():
    validate_scaffold_metadata(*_provenance())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("parent_model_sha256", "b" * 64),
        ("temporal_training_performed", "true"),
        ("candidate_promoted", "true"),
        ("history_steps", "4"),
        ("temporal_training_performed", None),
    ],
)
def test_mismatched_graph_provenance_is_rejected(key, value):
    metadata, report, parent = _provenance()
    if value is None:
        del metadata[key]
    else:
        metadata[key] = value
    with pytest.raises(ValueError, match="metadata"):
        validate_scaffold_metadata(metadata, report, parent)


def test_report_parent_cannot_override_actual_reference():
    metadata, report, parent = _provenance()
    report["parent_model_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="metadata"):
        validate_scaffold_metadata(metadata, report, parent)
