import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import build_plan_sft
import generate_r15_worlds
from multidetect.autonomy.plan_intent import check_plan_intent


def test_r20_profile_dry_run_isolated(capsys):
    assert generate_r15_worlds.main(["--profile", "r20", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 3
    assert payload["world_prefix"] == "gen_r20_w"


def test_r20_intent_pairs_cover_correction_and_unknown_place(tmp_path):
    args = type("Args", (), {"prompt_variant": "default"})()
    rows = build_plan_sft.r20_intent_rows(args, set(), {})
    assert {row["family"] for row in rows} == {
        "bad_return", "good_return", "bad_unknown_place", "good_return_alias"
    }
    refs = ("poi:gate (大门 / the gate)", "poi:pad (起降坪 / the pad)")
    for row in rows:
        findings = check_plan_intent(row["assistant_target"], row["task_text"], refs)
        if row["family"].startswith("bad_"):
            assert findings
        else:
            assert findings == []
