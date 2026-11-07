"""折叠尾座三角翼的坐标合同和可选 CAD 外观选择（不启动仿真）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

# CAD: x 展向、y 向尾、z 翼面上方，单位 mm。原点平移到方案 E CG。
# Gazebo 尾座 MC FLU: +X 向机腹、+Y 展向、+Z 向机头。
# PX4 FRD = diag(1, -1, -1) @ Gazebo FLU。
CAD_CG_MM = (0.0, 646.0, -9.0)
CAD_TO_BASE_ROTATION = ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0))
ASSEMBLY_MASS_KG = 2.929
ASSEMBLY_INERTIA_KGM2 = (0.431, 0.097, 0.373)
CAMERA_POSITION_M = (0.046, 0.0, 0.388)
SPAWN_HEIGHT_M = 0.411  # 支脚最低 z=-0.409，离地 2 mm 后落稳。


def cad_to_base_m(point_mm: tuple[float, float, float]) -> tuple[float, float, float]:
    """CAD mm -> CG 原点的 Gazebo base_link m；只旋转，不镜像。"""
    shifted = tuple(point_mm[i] - CAD_CG_MM[i] for i in range(3))
    return tuple(sum(row[i] * shifted[i] for i in range(3)) * 0.001 for row in CAD_TO_BASE_ROTATION)


def select_visual_mesh(model_dir: Path) -> tuple[str, Path] | None:
    """有真实网格时替换占位外观；DAE 优先，STL 次之，碰撞/动力学不变。

    返回新 SDF 和选中的文件，调用方可以写入作业目录再只读挂载，避免启动
    规划修改仓库。STEP 工具也用同一逻辑更新目标模型。
    """
    mesh = next(
        (
            model_dir / "meshes" / f"visual.{ext}"
            for ext in ("dae", "stl")
            if (model_dir / "meshes" / f"visual.{ext}").is_file()
        ),
        None,
    )
    if mesh is None:
        return None
    # 保留 SDF 内的参数来源注释。
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring((model_dir / "model.sdf").read_text(encoding="utf-8"), parser=parser)
    base = root.find("model/link[@name='base_link']")
    if base is None:
        raise ValueError("delta_tailsitter 缺少 base_link")
    for visual in list(base.findall("visual")):
        if visual.get("name", "").startswith("placeholder_") or visual.get("name") == "cad_visual":
            base.remove(visual)
    # STEP 整机装配已含桨；当转换器已经生成 link-local 桨网格时保留它们，
    # 否则才移除旧的占位盒，避免重复显示。
    rotor_meshes = all((model_dir / "meshes" / f"rotor_{index}.stl").is_file() for index in range(4))
    if not rotor_meshes:
        for rotor in root.findall("model/link"):
            if rotor.get("name", "").startswith("rotor_"):
                for visual in list(rotor.findall("visual")):
                    if visual.get("name", "").startswith("propeller_"):
                        rotor.remove(visual)
    visual = ET.SubElement(base, "visual", name="cad_visual")
    geometry = ET.SubElement(visual, "geometry")
    mesh_element = ET.SubElement(geometry, "mesh")
    ET.SubElement(mesh_element, "uri").text = f"model://delta_tailsitter/meshes/{mesh.name}"
    ET.SubElement(mesh_element, "scale").text = "1 1 1"
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n", mesh
