from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_identity_r10_inputs import validate


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    base = tmp_path / "base"
    base.mkdir()
    for split in ("train", "val", "test"):
        (base / f"{split}.jsonl").write_text('{"sample_id": "x"}\n', encoding="utf-8")
    campaign = tmp_path / "episodes"
    campaign.mkdir()
    proposals = tmp_path / "proposals.jsonl"
    proposals.write_text('{"image_path": "x"}\n', encoding="utf-8")
    worlds = []
    for direction, start in (("car->mailbox", 0), ("suv->postbox", 24)):
        for offset in range(24):
            worlds.append({
                "world_id": f"w{start + offset:04d}",
                "split": "train" if offset < 18 else "val",
                "r9r_direction": direction,
            })
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": worlds}), encoding="utf-8")
    return base, index, campaign, proposals


def test_r10_preflight_accepts_complete_cpu_inputs(tmp_path: Path) -> None:
    base, index, campaign, proposals = _fixture(tmp_path)
    report = validate(base_dataset=base, index=index, campaign=campaign, proposals=proposals)
    assert report["index_worlds"] == 48
    assert report["proposal_rows"] == 1


def test_r10_preflight_rejects_missing_full_proposals(tmp_path: Path) -> None:
    base, index, campaign, proposals = _fixture(tmp_path)
    proposals.unlink()
    with pytest.raises(SystemExit, match="missing required file"):
        validate(base_dataset=base, index=index, campaign=campaign, proposals=proposals)


def test_r10_preflight_rejects_wrong_split_counts(tmp_path: Path) -> None:
    base, index, campaign, proposals = _fixture(tmp_path)
    document = json.loads(index.read_text(encoding="utf-8"))
    document["worlds"][0]["split"] = "val"
    index.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SystemExit, match="unexpected world split counts"):
        validate(base_dataset=base, index=index, campaign=campaign, proposals=proposals)
