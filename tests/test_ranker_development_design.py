import json
from itertools import product
from pathlib import Path

from scripts.train_task_ranker import load_dataset


def test_development_experiment_changes_grid_not_cases():
    root = Path(__file__).resolve().parents[1] / "configs" / "training"
    source = json.loads((root / "task-sequence-heterogeneous-v1.json").read_text())
    path = root / "task-ranker-heterogeneous-development-v2.json"
    experiment = json.loads(path.read_text())
    assert experiment["cases"] == source["cases"]
    assert experiment["coefficient_candidates"] == [
        list(values) for values in product((0, 1, 5), (0, -0.005, -0.05), (0, -0.05, -0.2, -1))
    ]
    assert "not unseen holdout" in experiment["provenance"]
    groups, grid, _ = load_dataset(path)
    assert len(grid) == 36
    assert {key: len(value) for key, value in groups.items()} == {
        "train": 4,
        "validation": 4,
        "test": 4,
    }
