from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

from scripts import render_onboard_sdf_blender_dataset as renderer

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAMES = (
    "multidetect_search_rover_long_v1",
    "multidetect_search_cargo_cart_v1",
    "multidetect_search_utility_van_v1",
)


def load_asset(name: str) -> tuple[ET.Element, list[renderer.Primitive]]:
    path = ROOT / "simulation/gazebo/models" / name / "model.sdf"
    sdf = ET.parse(path).getroot()
    assert sdf.tag == "sdf"
    assert sdf.attrib["version"] == "1.9"
    model = sdf.find("model")
    assert model is not None
    assert model.attrib["name"] == name
    return model, renderer._model_primitives(model, renderer.IDENTITY, True, name)


def bounds(primitive: renderer.Primitive) -> tuple[tuple[float, float], ...]:
    points = renderer._corners(primitive)
    return tuple((min(p[axis] for p in points), max(p[axis] for p in points)) for axis in range(3))


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_assets_are_static_self_contained_solid_3d_models(name: str) -> None:
    model, primitives = load_asset(name)
    assert model.findtext("static") == "true"
    assert len(primitives) >= 15
    assert {part.geometry for part in primitives} <= {"box", "cylinder", "sphere"}
    assert {part.geometry for part in primitives} >= {"box", "cylinder"}
    assert all(part.target for part in primitives)
    assert all(
        value > 0 and math.isfinite(value) for part in primitives for value in part.dimensions
    )
    for unsupported in ("include", "frame", "mesh", "uri", "plugin", "joint", "script", "pbr"):
        assert model.find(f".//{unsupported}") is None

    links = model.findall("link")
    assert len({link.attrib["name"] for link in links}) == len(links)
    assert len({part.name for part in primitives}) == len(primitives)
    for link in links:
        visual, collision = link.find("visual"), link.find("collision")
        assert visual is not None and collision is not None
        assert renderer._geometry(visual) == renderer._geometry(collision)
        assert renderer.pose_matrix(visual) == renderer.pose_matrix(collision)


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_wheel_axes_and_ground_contact_use_metric_model_frame(name: str) -> None:
    _, primitives = load_asset(name)
    wheels = [part for part in primitives if "/wheel_" in part.name]
    assert len(wheels) == (6 if "rover_long" in name else 4)
    for wheel in wheels:
        assert wheel.geometry == "cylinder"
        radius, _ = wheel.dimensions
        assert wheel.matrix[2][3] == pytest.approx(radius)
        assert abs(wheel.matrix[1][2]) == pytest.approx(1)
        assert wheel.matrix[0][2] == pytest.approx(0, abs=1e-10)
        assert wheel.matrix[2][2] == pytest.approx(0, abs=1e-10)
        assert bounds(wheel)[2][0] == pytest.approx(0, abs=1e-10)
    assert min(bounds(part)[2][0] for part in primitives) >= -1e-10


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_solids_form_one_grounded_structure_not_disconnected_billboards(name: str) -> None:
    _, primitives = load_asset(name)
    boxes = [bounds(part) for part in primitives]
    # All rotations are axis aligned. AABB contact is a structural sanity
    # check, not a proof of dynamic collision or renderer appearance.
    connected = {0}
    while True:
        additions = {
            index
            for index, box in enumerate(boxes)
            if index not in connected
            and any(
                all(
                    box[axis][0] <= boxes[other][axis][1] + 1e-9
                    and boxes[other][axis][0] <= box[axis][1] + 1e-9
                    for axis in range(3)
                )
                for other in connected
            )
        }
        if not additions:
            break
        connected.update(additions)
    assert len(connected) == len(primitives)


def test_shape_diversity_is_geometric_not_only_material_variation() -> None:
    all_parts = [load_asset(name)[1] for name in MODEL_NAMES]
    signatures = {
        tuple(sorted((part.geometry, part.dimensions) for part in parts)) for parts in all_parts
    }
    assert len(signatures) == 3
    extents = []
    for parts in all_parts:
        boxes = [bounds(part) for part in parts]
        extents.append(
            tuple(
                max(box[axis][1] for box in boxes) - min(box[axis][0] for box in boxes)
                for axis in range(3)
            )
        )
    rover, cart, van = extents
    assert rover == pytest.approx((4.52, 1.90, 1.86))
    assert cart == pytest.approx((2.245, 1.32, 1.82))
    assert van == pytest.approx((4.69, 2.24, 2.54))
    assert van[2] > cart[2] > rover[2] - 0.10
    assert cart[0] < rover[0] * 0.6
    assert Counter(part.geometry for part in all_parts[0])["cylinder"] == 6
    assert Counter(part.geometry for part in all_parts[1])["cylinder"] == 7


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_gazebo_model_config_names_and_development_scope(name: str) -> None:
    config = ET.parse(ROOT / "simulation/gazebo/models" / name / "model.config").getroot()
    assert config.findtext("name") == name
    sdf = config.find("sdf")
    assert sdf is not None and sdf.text == "model.sdf" and sdf.attrib["version"] == "1.9"
    description = config.findtext("description", "").lower()
    assert "low-poly development asset" in description
    assert "synthetic" in description
    assert "flight" in description or "real-world recognition" in description
