import importlib.util
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

CATALOG = Path(__file__).parents[1] / "configs/sim-assets/fuel-catalog.json"


def _catalog_builder():
    path = CATALOG.parents[2] / "scripts/build_sim_asset_catalog.py"
    spec = importlib.util.spec_from_file_location("build_sim_asset_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fuel_catalog_bounds_are_finite_and_plausible():
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert data["asset_mode"] == "fuel_v1"
    assert len(data["assets"]) == 39
    for asset in data["assets"]:
        size = asset["measured_size_m"]
        assert len(size) == 3 and all(math.isfinite(v) and 0 < v < 1000 for v in size)
        assert all(math.isfinite(v) for v in asset["bbox_min_m"] + asset["bbox_max_m"])
    by_source = {a["source"]: a for a in data["assets"]}
    assert 3.5 <= max(by_source["Hatchback"]["measured_size_m"]) <= 6.0
    assert 1.5 <= by_source["Walking_person"]["measured_size_m"][2] <= 2.0
    assert 0.3 <= by_source["Construction_Cone"]["measured_size_m"][2] <= 1.2


def test_resolve_converts_legacy_script_to_harmonic_pbr(tmp_path):
    source = tmp_path / "source" / "Synthetic"
    (source / "materials/scripts").mkdir(parents=True)
    (source / "materials/textures").mkdir(parents=True)
    (source / "materials/textures/paint.png").write_bytes(b"png")
    (source / "materials/scripts/synthetic.material").write_text(
        "material Synthetic/Paint { technique { pass { texture_unit { texture paint.png } } } }\n",
        encoding="utf-8",
    )
    (source / "model.sdf").write_text(
        """<sdf version='1.7'><model name='Synthetic'><link name='link'>
        <visual name='visual'><geometry><box><size>1 1 1</size></box></geometry>
        <material><script><uri>model://Synthetic/materials/scripts</uri>
        <name>Synthetic/Paint</name></script></material></visual>
        </link></model></sdf>""",
        encoding="utf-8",
    )
    builder = _catalog_builder()
    out = builder.build_resolved(tmp_path / "source", tmp_path / "resolved")
    root = ET.parse(out / "synthetic" / "model.sdf").getroot()
    material = root.find(".//material")
    assert material.find("script") is None
    assert material.findtext("diffuse") == "1 1 1 1"
    assert material.findtext("pbr/metal/albedo_map") == "model://synthetic/materials/textures/paint.png"
