#!/usr/bin/env python3
"""离线 STEP -> Gazebo 网格（CAD mm -> CG 原点 MC FLU m）。

使用 /data/projects/f1-aero/.venv/bin/python，依赖 build123d/OCP、numpy、trimesh。
默认写入 delta_tailsitter/meshes/visual.stl；也支持无额外依赖的 DAE。
真实网格放入本机 meshes 后会仅替换 SDF 外观，碰撞和质量属性仍由报告定义。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.delta_tailsitter import (  # noqa: E402
    CAD_CG_MM,
    CAD_TO_BASE_ROTATION,
    select_visual_mesh,
)

MAX_BYTES = 20_000_000  # 单文件严格 <20 MB（十进制）；STL 每三角形 50 byte。

# CATIA's repeated labels are not unique, so keep the stable top-level indices
# from the supplied delta_11_3.step report.  The CAD hub is at y=605 mm and
# maps to the existing rotor link origin in the generated SDF.
ROTOR_PART_INDICES = (13, 5, 14, 12)
ROTOR_CAD_HUBS_MM = ((-600.0, -150.0), (600.0, 150.0), (600.0, -150.0), (-600.0, 150.0))
C11_PART_INDEX = 15


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须为有限正数")
    return number


def _cluster(mesh, target_faces: int):
    """没有 fast_simplification 时用顶点网格聚类；不随机丢弃面或破坏坐标系。

    保留每个网格的代表顶点、去掉塌缩/重复三角形。仅用于外观，不据此估质量。
    容差/面数和简化前后包围盒会打印，便于发现小零件被合并。
    """
    import numpy as np
    import trimesh

    if len(mesh.faces) <= target_faces:
        return mesh
    original = mesh
    pitch = max(original.extents) / math.sqrt(target_faces)
    for _ in range(40):
        cells = np.floor((original.vertices - original.bounds[0]) / pitch).astype(np.int64)
        _, representatives, inverse = np.unique(
            cells, axis=0, return_index=True, return_inverse=True
        )
        faces = inverse[original.faces]
        valid = (
            (faces[:, 0] != faces[:, 1])
            & (faces[:, 1] != faces[:, 2])
            & (faces[:, 0] != faces[:, 2])
        )
        faces = faces[valid]
        _, unique = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
        simplified = trimesh.Trimesh(
            vertices=original.vertices[representatives], faces=faces[unique], process=True
        )
        if 0 < len(simplified.faces) <= target_faces:
            return simplified
        if len(simplified.faces) == 0:
            raise ValueError("面数预算过小，简化后没有有效三角形")
        pitch *= 1.3
    raise ValueError("不能在面数预算内完成聚类；请增大 STEP 三角化容差")


def _dae(mesh) -> bytes:
    """生成单几何 Collada 1.4.1；单位 m、Z_UP，不需要 pycollada。"""
    ns = "http://www.collada.org/2005/11/COLLADASchema"
    ET.register_namespace("", ns)

    def tag(parent, local_name, text=None, **attrs):
        element = ET.SubElement(parent, f"{{{ns}}}{local_name}", attrs)
        if text is not None:
            element.text = str(text)
        return element

    root = ET.Element(f"{{{ns}}}COLLADA", version="1.4.1")
    asset = tag(root, "asset")
    tag(asset, "created", "2026-11-04T00:00:00Z")
    tag(asset, "modified", "2026-11-04T00:00:00Z")
    tag(asset, "unit", name="meter", meter="1")
    tag(asset, "up_axis", "Z_UP")
    geometries = tag(root, "library_geometries")
    geometry = tag(geometries, "geometry", id="aircraft", name="delta_tailsitter")
    part = tag(geometry, "mesh")
    source = tag(part, "source", id="positions")
    tag(
        source,
        "float_array",
        " ".join(f"{x:.9g}" for x in mesh.vertices.flat),
        id="positions-array",
        count=str(mesh.vertices.size),
    )
    accessor = tag(
        tag(source, "technique_common"),
        "accessor",
        source="#positions-array",
        count=str(len(mesh.vertices)),
        stride="3",
    )
    for axis in "XYZ":
        tag(accessor, "param", name=axis, type="float")
    vertices = tag(part, "vertices", id="vertices")
    tag(vertices, "input", semantic="POSITION", source="#positions")
    triangles = tag(part, "triangles", count=str(len(mesh.faces)))
    tag(triangles, "input", semantic="VERTEX", source="#vertices", offset="0")
    tag(triangles, "p", " ".join(str(x) for x in mesh.faces.flat))
    scene = tag(tag(root, "library_visual_scenes"), "visual_scene", id="Scene", name="Scene")
    node = tag(scene, "node", id="Aircraft", name="Aircraft", type="NODE")
    tag(node, "instance_geometry", url="#aircraft")
    tag(tag(root, "scene"), "instance_visual_scene", url="#Scene")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _mesh_bytes(mesh, suffix: str) -> bytes:
    if suffix == ".dae":
        return _dae(mesh)
    # 不依赖 trimesh 的版本相关 STL 导出选项，始终输出二进制。
    result = bytearray(b"Multi-Detect CAD -> CG MC FLU, metres".ljust(80, b"\0"))
    result.extend(struct.pack("<I", len(mesh.faces)))
    for normal, triangle in zip(mesh.face_normals, mesh.triangles, strict=True):
        result.extend(struct.pack("<12fH", *normal, *triangle.flatten(), 0))
    return bytes(result)


def _atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)



def _tessellate(shape, linear_deflection_mm: float, angular_deflection_rad: float):
    """逐面三角化；CATIA 导出的个别退化面没有三角化结果时跳过并计数，而不是整体失败。"""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape.wrapped, linear_deflection_mm, False, angular_deflection_rad, True)
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    skipped = 0
    explorer = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face(explorer.Current())
        explorer.Next()
        location = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face, location)
        if poly is None:
            skipped += 1
            continue
        trsf = location.Transformation()
        offset = len(vertices)
        for i in range(1, poly.NbNodes() + 1):
            point = poly.Node(i).Transformed(trsf)
            vertices.append((point.X(), point.Y(), point.Z()))
        reverse = face.Orientation() == TopAbs_REVERSED
        for i in range(1, poly.NbTriangles() + 1):
            a, b, c = poly.Triangle(i).Get()
            if reverse:
                b, c = c, b
            faces.append((offset + a - 1, offset + b - 1, offset + c - 1))
    return vertices, faces, skipped


def _part_mesh(part, *, origin_mm, rotation, linear_deflection_mm, angular_deflection_rad):
    """Tessellate one STEP part and express it in a link-local frame."""
    import numpy as np
    import trimesh

    vertices, faces, skipped = _tessellate(part, linear_deflection_mm, angular_deflection_rad)
    if not vertices or not faces:
        return None, skipped
    points = (np.asarray(vertices, dtype=float) - np.asarray(origin_mm, dtype=float))
    transformed = points @ np.asarray(rotation).T * 0.001
    mesh = trimesh.Trimesh(vertices=transformed, faces=np.asarray(faces), process=True)
    if not np.isfinite(mesh.vertices).all() or len(mesh.faces) == 0:
        return None, skipped
    return mesh, skipped


def _write_mesh(mesh, path: Path, *, max_faces: int):
    """Simplify and atomically write one visual link mesh."""
    mesh = _cluster(mesh, min(max_faces, (MAX_BYTES - 85) // 50))
    data = _mesh_bytes(mesh, path.suffix.lower())
    while len(data) >= MAX_BYTES:
        target = max(12, int(len(mesh.faces) * MAX_BYTES / len(data) * 0.85))
        if target >= len(mesh.faces):
            raise ValueError(f"不能满足 <20 MB 限制：{path.name}")
        mesh = _cluster(mesh, target)
        data = _mesh_bytes(mesh, path.suffix.lower())
    _atomic_write(path, data)
    return mesh, data

def convert(
    source: Path,
    output: Path,
    *,
    linear_deflection_mm=0.6,
    angular_deflection_rad=0.2,
    max_faces=300_000,
    collision: Path | None = None,
) -> dict:
    """读 STEP、打印可审计属性、三角化并转换；质量不由实体 CAD 体积推断。"""
    import numpy as np
    import trimesh
    from build123d import import_step

    if output.suffix.lower() not in {".stl", ".dae"}:
        raise ValueError("输出只能是 .stl 或 .dae")
    if collision is not None and collision.suffix.lower() not in {".stl", ".dae"}:
        raise ValueError("碰撞输出只能是 .stl 或 .dae")
    if collision is not None and collision.resolve() == output.resolve():
        raise ValueError("外观和碰撞输出不能为同一文件")
    if max_faces < 12:
        raise ValueError("max_faces 至少为 12")
    for value in (linear_deflection_mm, angular_deflection_rad):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("三角化容差必须为有限正数")
    shape = import_step(source)
    if shape.is_null:
        raise ValueError("STEP 中没有有效形状")
    # build123d 的 STEP reader 将长度归一为 mm；保留装配位置再三角化。
    parts = list(shape.children) or list(shape.solids())
    part_records = []
    for index, part in enumerate(parts):
        bounds = part.bounding_box()
        part_records.append(
            {
                "index": index,
                "name": part.label or f"part_{index}",
                "volume_mm3": float(part.volume),
                "volume_m3": float(part.volume) * 1e-9,
                "cad_bounds_mm": [list(bounds.min), list(bounds.max)],
                "solid_count": len(part.solids()),
            }
        )
    # Keep the complete top-level part list for the audit report, then build
    # link-local meshes.  This prevents rotating propellers from being baked
    # into the static body visual.
    skipped_faces = 0
    body_meshes = []
    rotor_meshes = {}
    c11_mesh = None
    rotation = np.asarray(CAD_TO_BASE_ROTATION)
    for index, part in enumerate(parts):
        if index in ROTOR_PART_INDICES:
            hub_x, hub_z = ROTOR_CAD_HUBS_MM[ROTOR_PART_INDICES.index(index)]
            mesh_part, skipped = _part_mesh(
                part,
                origin_mm=(hub_x, 605.0, hub_z),
                rotation=rotation,
                linear_deflection_mm=linear_deflection_mm,
                angular_deflection_rad=angular_deflection_rad,
            )
            skipped_faces += skipped
            if mesh_part is not None:
                rotor_meshes[ROTOR_PART_INDICES.index(index)] = mesh_part
        elif index == C11_PART_INDEX:
            mesh_part, skipped = _part_mesh(
                part,
                origin_mm=(0.0, 258.0, -55.0),
                rotation=rotation,
                linear_deflection_mm=linear_deflection_mm,
                angular_deflection_rad=angular_deflection_rad,
            )
            skipped_faces += skipped
            c11_mesh = mesh_part
        else:
            mesh_part, skipped = _part_mesh(
                part,
                origin_mm=CAD_CG_MM,
                rotation=rotation,
                linear_deflection_mm=linear_deflection_mm,
                angular_deflection_rad=angular_deflection_rad,
            )
            skipped_faces += skipped
            if mesh_part is not None:
                body_meshes.append(mesh_part)
    if not body_meshes:
        raise ValueError("STEP 三角化没有生成机身有效三角形")
    mesh = trimesh.util.concatenate(body_meshes)
    if not np.isfinite(mesh.vertices).all() or len(mesh.faces) == 0:
        raise ValueError("网格为空或含无效坐标")
    original_bounds = mesh.bounds.tolist()
    original_faces = len(mesh.faces)
    mesh, data = _write_mesh(mesh, output, max_faces=max_faces)
    model_dir = output.parent.parent
    link_meshes = {}
    if output.suffix.lower() == ".stl" and model_dir.name == "delta_tailsitter":
        for index, rotor in sorted(rotor_meshes.items()):
            link_meshes[f"rotor_{index}"] = _write_mesh(
                rotor, output.parent / f"rotor_{index}.stl", max_faces=max(12, max_faces // 8)
            )[0]
        if c11_mesh is not None:
            link_meshes["c11"] = _write_mesh(
                c11_mesh, output.parent / "c11.stl", max_faces=max(12, max_faces // 8)
            )[0]
    cad_min = [min(record["cad_bounds_mm"][0][axis] for record in part_records) for axis in range(3)]
    cad_max = [max(record["cad_bounds_mm"][1][axis] for record in part_records) for axis in range(3)]
    report = {
        "source": str(source),
        "skipped_faces": skipped_faces,
        "output": str(output),
        "units": "m",
        "cad_cg_mm": CAD_CG_MM,
        "cad_to_base_rotation": CAD_TO_BASE_ROTATION,
        "cad_bounds_mm": [cad_min, cad_max],
        "base_bounds_before_simplification_m": original_bounds,
        "base_bounds_m": mesh.bounds.tolist(),
        "faces_before_simplification": original_faces,
        "faces": len(mesh.faces),
        "bytes": len(data),
        "solid_count": len(shape.solids()),
        "total_volume_mm3": float(shape.volume),
        "parts": part_records,
        "excluded_parts": {
            "rotors": [part_records[index] for index in ROTOR_PART_INDICES if index < len(part_records)],
            "c11": part_records[C11_PART_INDEX] if C11_PART_INDEX < len(part_records) else None,
        },
        "link_meshes": sorted(link_meshes),
        "note": "实体 CAD 体积仅供几何核对；不是报告壳体方案 E 的材料体积/质量。",
    }
    if collision is not None:
        hull = mesh.convex_hull
        collision_data = _mesh_bytes(hull, collision.suffix.lower())
        if len(collision_data) >= MAX_BYTES:
            raise ValueError("碰撞凸包超过 <20 MB 限制")
        _atomic_write(collision, collision_data)
        report["collision"] = {
            "output": str(collision),
            "faces": len(hull.faces),
            "bytes": len(collision_data),
            "method": "convex_hull",
        }
    if (
        output.parent.name == "meshes"
        and output.stem == "visual"
        and model_dir.name == "delta_tailsitter"
        and (model_dir / "model.sdf").is_file()
    ):
        selected = select_visual_mesh(model_dir)
        if selected is not None:
            _atomic_write(model_dir / "model.sdf", selected[0].encode("utf-8"))
            report["selected_visual"] = str(selected[1])
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "simulation/gazebo/models/delta_tailsitter/meshes/visual.stl",
    )
    parser.add_argument("--collision", type=Path, help="可选凸包，仅导出，不替换四支脚碰撞")
    parser.add_argument("--linear-deflection-mm", type=_positive, default=0.6)
    parser.add_argument("--angular-deflection-rad", type=_positive, default=0.2)
    parser.add_argument("--max-faces", type=int, default=300_000)
    parser.add_argument("--report", type=Path, help="另存包含各部件体积/包围盒的 JSON")
    args = parser.parse_args(argv)
    try:
        report = convert(
            args.step,
            args.output,
            linear_deflection_mm=args.linear_deflection_mm,
            angular_deflection_rad=args.angular_deflection_rad,
            max_faces=args.max_faces,
            collision=args.collision,
        )
    except (ImportError, OSError, ValueError) as exc:
        parser.exit(2, f"STEP 转换失败：{exc}\n")
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report is not None:
        _atomic_write(args.report, text.encode("utf-8"))
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
