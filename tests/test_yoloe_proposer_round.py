from __future__ import annotations

from scripts.build_yoloe_proposer_round import (
    compare_histograms,
    manifest_histogram,
    size_bucket,
)


def test_size_buckets_match_small_object_contract() -> None:
    assert [size_bucket(value) for value in (0, 7.99, 8, 15.99, 16, 31.99, 32, 63.99, 64)] == [
        "<8", "<8", "8-16", "8-16", "16-32", "16-32", "32-64", "32-64", ">=64"
    ]


def test_histogram_comparison_preserves_old_and_new_counts() -> None:
    old = {"backpack": {"<8": 2, "8-16": 3}}
    new = {"backpack": {"<8": 5, "8-16": 1}, "cone": {">=64": 4}}
    assert manifest_histogram({"counts": {"category_size_histogram": old}}) == {
        "backpack": {"<8": 2, "8-16": 3, "16-32": 0, "32-64": 0, ">=64": 0}
    }
    result = compare_histograms(old, new)
    assert result["backpack"]["<8"] == {"old": 2, "new": 5}
    assert result["cone"][">=64"] == {"old": 0, "new": 4}
