from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.extract_px4_x500_gimbal_assets import extract_assets
from scripts.run_simulator_docker import PINNED_IMAGE

REAL_SOURCE_ASSETS = Path(__file__).resolve().parents[1] / "simulation/gazebo/source-assets/x500_gimbal"
REAL_AIRFRAMES = {
    name: (REAL_SOURCE_ASSETS / name).read_text(encoding="utf-8")
    for name in ("4019_gz_x500_gimbal", "4001_gz_x500")
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_extracts_composition_camera_gimbal_and_4019_from_one_image_revision(tmp_path: Path) -> None:
    image_root = tmp_path / "image-root"
    files = {
        "share/gz/models/x500_gimbal/model.sdf": (
            '<sdf version="1.9"><model name="x500_gimbal">'
            '<include><uri>model://x500</uri></include>'
            '<include><uri>model://gimbal</uri></include></model></sdf>'
        ),
        "share/gz/models/gimbal/model.sdf": (
            '<sdf version="1.9"><model name="gimbal">'
            '<link name="cgo3_mount_link"/><link name="camera_link"/></model></sdf>'
        ),
        "etc/init.d-posix/airframes/4019_gz_x500_gimbal": REAL_AIRFRAMES["4019_gz_x500_gimbal"],
        "etc/init.d-posix/airframes/4001_gz_x500": REAL_AIRFRAMES["4001_gz_x500"],
    }
    for relative, content in files.items():
        source = image_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")

    output = tmp_path / "snapshot"
    result = extract_assets(image_root, output, PINNED_IMAGE)
    contract = json.loads((output / "extraction.json").read_text(encoding="utf-8"))

    assert result == contract
    assert contract["image_revision"] == contract["image"]
    assert set(contract["assets"]) == {
        "x500_gimbal_model.sdf",
        "gimbal_model.sdf",
        "4019_gz_x500_gimbal",
        "4001_gz_x500",
    }
    assert all(record["sha256"] == _sha256(output / name) for name, record in contract["assets"].items())


def test_extraction_requires_camera_link_in_gimbal_component(tmp_path: Path) -> None:
    image_root = tmp_path / "image-root"
    files = {
        "share/gz/models/x500_gimbal/model.sdf": (
            '<sdf><model name="x500_gimbal"><include><uri>model://x500</uri></include>'
            '<include><uri>model://gimbal</uri></include></model></sdf>'
        ),
        "share/gz/models/gimbal/model.sdf": (
            '<sdf><model name="gimbal"><link name="cgo3_mount_link"/></model></sdf>'
        ),
        "etc/init.d-posix/airframes/4019_gz_x500_gimbal": REAL_AIRFRAMES["4019_gz_x500_gimbal"],
        "etc/init.d-posix/airframes/4001_gz_x500": REAL_AIRFRAMES["4001_gz_x500"],
    }
    for relative, content in files.items():
        source = image_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="gimbal model is missing camera_link"):
        extract_assets(image_root, tmp_path / "snapshot", PINNED_IMAGE)


def test_extraction_rejects_mutable_revision_and_existing_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exact pinned PX4 image ref"):
        extract_assets(tmp_path, tmp_path / "out", "latest")

    with pytest.raises(FileExistsError, match="overwrite"):
        extract_assets(tmp_path, tmp_path, PINNED_IMAGE)
