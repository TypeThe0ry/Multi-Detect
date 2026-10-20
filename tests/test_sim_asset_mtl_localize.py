from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_sim_asset_catalog as catalog  # noqa: E402

URL = "https://fuel.gazebosim.org/1.0/OpenRobotics/models/hatchback/tip/files/"


def test_mtl_fuel_urls_point_at_shipped_textures_and_missing_ones_are_dropped(
    tmp_path: Path,
) -> None:
    model = tmp_path / "hatchback_blue"
    (model / "meshes").mkdir(parents=True)
    (model / "materials" / "textures").mkdir(parents=True)
    (model / "materials" / "textures" / "hatchback.png").write_bytes(b"png")
    mtl = model / "meshes" / "hatchback.mtl"
    mtl.write_text(
        "newmtl Hatchback\n"
        f"\tmap_Kd {URL}materials/textures/hatchback.png\n"
        "newmtl Wheels3\n"
        f"\tmap_Kd {URL}materials/textures/wheels3.png\n"
        "\tKd 0.5 0.5 0.5\n",
        encoding="utf-8",
    )
    catalog._localize_mtl_textures(model, "hatchback_blue")
    text = mtl.read_text(encoding="utf-8")
    # No URL is left for Gazebo to download while it loads the world.
    assert "fuel.gazebosim.org" not in text
    assert "\tmap_Kd ../materials/textures/hatchback.png\n" in text
    assert "wheels3" not in text and "\tKd 0.5 0.5 0.5\n" in text
