from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.extract_px4_x500_gimbal_collision_assets import CONTRACT, FILES, extract_assets
from scripts.run_simulator_docker import PINNED_IMAGE

EXPORT = Path("/data/scratch/px4-gimbal-export/opt")


def test_extracts_full_gimbal_navigation_and_collision_closure(tmp_path: Path) -> None:
    if not (EXPORT / "share/gz/models/x500_base/model.sdf").is_file():
        pytest.skip("pinned image export is unavailable")
    output = tmp_path / "closure"
    result = extract_assets(EXPORT, output, PINNED_IMAGE)
    document = json.loads((output / "extraction.json").read_text(encoding="utf-8"))
    assert result == document
    assert document["contract"] == CONTRACT
    assert set(document["assets"]) == set(FILES)
    for name, record in document["assets"].items():
        assert record["sha256"] == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_rejects_non_pinned_image_and_existing_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact pinned PX4 image ref"):
        extract_assets(EXPORT, tmp_path / "out", "latest")
    with pytest.raises(FileExistsError, match="overwrite"):
        extract_assets(EXPORT, tmp_path, PINNED_IMAGE)
