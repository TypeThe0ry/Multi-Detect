"""用 build123d 创建测试 STEP，覆盖实际 OCP 三角化；无网络、无仿真。"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.step_to_gazebo_mesh import _positive

ROOT = Path(__file__).resolve().parents[1]
CAD_PYTHON = Path(
    os.environ.get("MULTIDETECT_CAD_PYTHON", "/data/projects/f1-aero/.venv/bin/python")
)


def test_cad_cli_rejects_invalid_tolerances():
    import argparse

    for value in ("0", "-1", "inf", "nan"):
        with pytest.raises(argparse.ArgumentTypeError):
            _positive(value)


@pytest.mark.skipif(not CAD_PYTHON.is_file(), reason="需要本地 build123d/OCP CAD Python 环境")
def test_real_step_to_stl_dae_convex_collision_and_simplification(tmp_path):
    code = r"""
import json
import sys
from pathlib import Path
import build123d as b
import trimesh
from scripts import step_to_gazebo_mesh as converter

directory = Path(sys.argv[1])
first = b.Box(40, 60, 20).translate((0, 646, -9))
first.label = "body_test"
second = b.Box(20, 20, 20).translate((100, 700, 50))
second.label = "pod_test"
shape = b.Compound(children=[first, second])
step = directory / "small.step"
b.export_step(shape, step)
stl = directory / "visual.stl"
reports = [converter.convert(step, stl, collision=directory / "collision.stl")]
reports.append(converter.convert(step, directory / "visual.dae"))
reports.append({"loaded_bounds": trimesh.load(stl).bounds.tolist()})
# 人造曲面保证真的经过简化分支，而不是只检查 12 个三角形的方块。
b.export_step(b.Sphere(100).translate((0, 646, -9)), directory / "sphere.step")
reports.append(converter.convert(directory / "sphere.step", directory / "simplified.stl",
                                 linear_deflection_mm=0.1, max_faces=100))
for invalid in (0, -1, float("nan"), float("inf")):
    try:
        converter.convert(step, stl, linear_deflection_mm=invalid)
    except ValueError:
        continue
    raise AssertionError("invalid tolerance accepted")
(directory / "reports.json").write_text(json.dumps(reports), encoding="utf-8")
"""
    completed = subprocess.run(
        [str(CAD_PYTHON), "-c", code, str(tmp_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}", "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    stl, dae, loaded, simplified = json.loads((tmp_path / "reports.json").read_text("utf-8"))
    assert stl["solid_count"] == len(stl["parts"]) == 2
    assert stl["total_volume_mm3"] == pytest.approx(56_000)
    assert sorted(part["volume_mm3"] for part in stl["parts"]) == pytest.approx([8000, 48000])
    for bounds in (stl["base_bounds_m"], dae["base_bounds_m"], loaded["loaded_bounds"]):
        assert bounds[0] == pytest.approx([-0.069, -0.020, -0.064], abs=1e-8)
        assert bounds[1] == pytest.approx([0.010, 0.110, 0.030], abs=1e-8)
    for name in ("visual.stl", "visual.dae", "collision.stl", "simplified.stl"):
        assert 0 < (tmp_path / name).stat().st_size < 20_000_000
    binary = (tmp_path / "visual.stl").read_bytes()
    count = struct.unpack("<I", binary[80:84])[0]
    assert len(binary) == 84 + count * 50
    collada = ET.parse(tmp_path / "visual.dae").getroot()
    ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
    assert collada.findtext("c:asset/c:up_axis", namespaces=ns) == "Z_UP"
    assert collada.find("c:asset/c:unit", ns).get("meter") == "1"
    assert collada.find(".//c:triangles", ns).get("count") == str(dae["faces"])
    assert simplified["faces_before_simplification"] > simplified["faces"]
    assert 0 < simplified["faces"] <= 100
