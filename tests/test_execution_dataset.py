import copy

import pytest

from scripts.build_execution_dataset import validate_splits


def entries():
    return [
        {
            "split": split,
            "family": f"family-{i}",
            "record": {
                "inputs": {"mission_sha256": f"m{i}", "journal_sha256": f"j{i}"},
                "grouping": {
                    "scenario_id": f"s{i}",
                    "scenario_manifest_sha256": f"h{i}",
                    "task_semantics_sha256": f"t{i}",
                    "host_session_id": f"c{i}",
                },
            },
        }
        for i, split in enumerate(("train", "validation", "test"))
    ]


def test_all_splits_not_equivalent_to_independence():
    result = validate_splits(entries())
    assert result["all_splits_present"]
    assert not result["semantic_independence_proven"]
    assert not result["model_promotion_allowed"]


@pytest.mark.parametrize(
    "field", ["scenario_id", "scenario_manifest_sha256", "task_semantics_sha256", "host_session_id"]
)
def test_any_shared_identity_blocks_cross_split(field):
    rows = entries()
    rows[1]["record"]["grouping"][field] = rows[0]["record"]["grouping"][field]
    with pytest.raises(ValueError, match="crosses splits"):
        validate_splits(rows)


@pytest.mark.parametrize("field", ["mission_sha256", "journal_sha256"])
def test_repeated_artifacts_rejected_even_inside_train(field):
    rows = entries()[:2]
    rows[1]["split"] = "train"
    rows[1]["record"]["inputs"][field] = rows[0]["record"]["inputs"][field]
    with pytest.raises(ValueError, match="duplicate"):
        validate_splits(rows)


def test_repeated_family_only_allowed_in_same_split():
    rows = entries()[:2]
    rows[1]["family"] = rows[0]["family"]
    with pytest.raises(ValueError, match="crosses splits"):
        validate_splits(rows)
    rows[1]["split"] = "train"
    before = copy.deepcopy(rows)
    result = validate_splits(rows)
    assert not result["all_splits_present"]
    assert rows == before
