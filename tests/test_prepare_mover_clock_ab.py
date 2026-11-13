from pathlib import Path
import json

import pytest

from scripts.prepare_mover_clock_ab import ROOT, prepare
from multidetect.autonomy.serialization import canonical_sha256


def test_controls_preserve_physics_budgets_and_verified_channels(tmp_path):
    out = tmp_path / "ab"
    suite = json.loads(prepare(ROOT, out, repeats=3).read_text())
    original = json.loads((ROOT / "configs/onboard/suite-follow-r21idr3-r24plan.json").read_text())
    assert suite["defaults"] == original["defaults"]
    assert len(suite["missions"]) == 9
    assert len({m["id"] for m in suite["missions"]}) == 9
    for mission in suite["missions"]:
        for key in ("expect", "steps", "max_episode_s", "task_text"):
            assert mission[key] == original["missions"][0][key]
    manifest = json.loads((out / "stationary/gen_mov_w0000.scenario.json").read_text())
    for reference in manifest["channels"].values():
        payload = json.loads((out / "stationary" / reference["path"]).read_text())
        assert canonical_sha256(payload) == reference["sha256"]
    truth = json.loads((out / "stationary/gen_mov_w0000.truth.json").read_text())
    target = next(t for t in truth["targets"] if t.get("true_target"))
    assert target["behavior"] == "stationary"
    assert "trajectory" not in target
    facts = json.loads((out / "experiment.json").read_text())
    assert facts["physics_equal"] and facts["plugins_equal"]
    with pytest.raises(FileExistsError):
        prepare(ROOT, out)
