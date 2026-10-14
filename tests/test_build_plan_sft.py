import json
import random
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from build_plan_sft import mission_v3  # noqa: E402

from multidetect.autonomy.mission_plan import validate_mission_plan  # noqa: E402


def _texts(path: Path) -> set[str]:
    return {item["text"] for item in json.loads(path.read_text(encoding="utf-8"))["tasks"]}


def test_v3_samples_validate_and_cover_disambiguating_phrases():
    rng = random.Random(20261015)
    rows = [mission_v3(rng) for _ in range(300)]
    for _zh, en, steps, _ in rows:
        validate_mission_plan({"plan_version": 1, "goal": en, "steps": steps, "report": ["status"]})
    text = " ".join(zh + en for zh, en, _, _ in rows)
    assert any(word in text for word in ("统计", "清点", "数一数", "How many", "Tally", "Count up"))
    assert any(
        word in text for word in ("跟随", "跟着", "盯住并跟着", "track and follow", "shadow")
    )
    assert any(
        word in text
        for word in ("拍照", "拍一张", "留存照片", "take a picture", "capture evidence")
    )
    assert "hover" in text and "悬停" in text


def test_v3_terminal_matches_explicit_ending():
    rng = random.Random(7)
    for _ in range(500):
        zh, en, steps, _ = mission_v3(rng)
        terminal = steps[-1]["skill"]
        ending = zh + en
        if any(token in ending for token in ("降落", "land")):
            assert terminal == "land"
        else:
            assert terminal == "return_home"


def test_eval_v2_is_disjoint_from_v1_and_generated_samples():
    v1 = _texts(ROOT / "configs/onboard/plan-eval-tasks.json")
    v2 = _texts(ROOT / "configs/onboard/plan-eval-tasks-v2.json")
    assert len(v2) == 60
    assert len(v2 & v1) == 0
    rng = random.Random(20261015)
    generated = set()
    for _ in range(20_000):
        zh, en, _, _ = mission_v3(rng)
        generated.add(" ".join(zh.split()))
        generated.add(" ".join(en.split()))
    assert not (generated & (v1 | v2))


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_cli_writes_requested_rows(version):
    output = ROOT / "tmp" / f"plan-sft-test-{version}-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_plan_sft.py"),
                "--output",
                str(output),
                "--rows",
                "8",
                "--val-rows",
                "2",
                "--version",
                version,
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout) == {"train": 8, "val": 2}
    finally:
        shutil.rmtree(output, ignore_errors=True)
