from pathlib import Path

from scripts.replay_onboard_episode import Results, compare, read_rows


FIXTURE = Path(__file__).parent / "fixtures/onboard_replay"


def test_fixture_episode_loads_and_logged_results_are_supported():
    assert read_rows(FIXTURE)[0]["step_index"] == 0
    results = Results("logged", None)
    assert results.get("det:0", [{"bbox": [1, 2, 3, 4]}], "detector")[0]["bbox"] == [1, 2, 3, 4]


def test_cache_mode_writes_and_reads_cached_results(tmp_path):
    path = tmp_path / "cache.json"
    results = Results("cache", path)
    assert results.get("k", {"decision": "approach"}, "slow")["decision"] == "approach"
    results.save()
    again = Results("cache", path)
    assert again.get("k", None, "slow")["decision"] == "approach"
    assert again.stats["cache_hits"] == 1


def test_compare_reports_first_divergence():
    left = {"trace": [{"step": 3, "action": "hold", "calls": []}]}
    right = {"trace": [{"step": 3, "action": "turn", "calls": []}]}
    result = compare(left, right)
    assert result["first_divergence"] == 0
    assert result["step"] == 3
