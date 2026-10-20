#!/usr/bin/env python3
"""Build Fuel catalog using transform-aware stdlib mesh bounds."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "artifacts/cache/gz-fuel/OpenRobotics"
OUT = ROOT / "configs/sim-assets/fuel-catalog.json"
SCENERY = {
    "Apartment",
    "House_1",
    "House_2",
    "House_3",
    "Collapsed_House",
    "Oak_tree",
    "Pine_Tree",
    "Telephone_pole",
    "Lamp_Post",
    "Water_tower",
    "Office_Building",
    "Gas_station",
    "Gazebo",
    "Playground",
    "Fountain",
}
META = {
    "Hatchback": ("car", ["car", "hatchback"], ["target", "distractor"], None),
    "Hatchback_red": ("car", ["car", "hatchback"], ["target", "distractor"], "red"),
    "Hatchback_blue": ("car", ["car", "hatchback"], ["target", "distractor"], "blue"),
    "SUV": ("suv", ["car", "suv"], ["target", "distractor"], None),
    "Pickup": ("pickup", ["pickup", "truck"], ["target", "distractor"], None),
    "Prius_Hybrid": ("car", ["car", "prius", "hybrid"], ["target", "distractor"], None),
    "Ambulance": (
        "ambulance",
        ["ambulance", "emergency vehicle"],
        ["target", "distractor"],
        "white",
    ),
    "Fire_truck": ("fire_truck", ["fire truck", "truck"], ["target", "distractor"], "red"),
    "Bus": ("bus", ["bus"], ["target", "distractor"], None),
    "Male_visitor": ("person", ["person", "man", "pedestrian"], ["target", "distractor"], None),
    "Casual_female": ("person", ["person", "woman", "pedestrian"], ["target", "distractor"], None),
    "Standing_person": ("person", ["person", "pedestrian"], ["target", "distractor"], None),
    "Walking_person": (
        "person",
        ["person", "pedestrian", "walking person"],
        ["target", "distractor"],
        None,
    ),
    "Construction_Cone": (
        "traffic_cone",
        ["traffic cone", "cone"],
        ["target", "distractor"],
        "orange",
    ),
    "Construction_Barrel": (
        "barrel",
        ["construction barrel", "barrel"],
        ["target", "distractor"],
        "orange",
    ),
    "Jersey_Barrier": (
        "jersey_barrier",
        ["jersey barrier", "road barrier"],
        ["target", "distractor"],
        None,
    ),
    "Fire_hydrant": ("fire_hydrant", ["fire hydrant", "hydrant"], ["target", "distractor"], None),
    "Dumpster": ("dumpster", ["dumpster", "trash container"], ["target", "distractor"], None),
    "Electrical_Box": (
        "electrical_box",
        ["electrical box", "utility box"],
        ["target", "distractor"],
        None,
    ),
    "Mailbox": ("mailbox", ["mailbox"], ["target", "distractor"], None),
    "Postbox": ("postbox", ["postbox", "mailbox"], ["target", "distractor"], None),
    "Cardboard_box": ("cardboard_box", ["cardboard box", "box"], ["target", "distractor"], "brown"),
    "Backpack": ("backpack", ["backpack", "bag"], ["target", "distractor"], None),
    "Stop_sign": ("stop_sign", ["stop sign", "road sign"], ["target", "distractor"], "red"),
}
for n in SCENERY:
    META.setdefault(n, ("scenery", [n.replace("_", " ").lower()], ["scenery-obstacle"], None))


def tag(e):
    return e.tag.rsplit("}", 1)[-1]


def nums(s):
    return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s or "")]


def mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def ident():
    return [[float(i == j) for j in range(4)] for i in range(4)]


def tp(m, p):
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3))


def rot(v):
    x, y, z, a = (v + [0, 0, 1, 0])[:4]
    n = math.sqrt(x * x + y * y + z * z) or 1
    x, y, z = x / n, y / n, z / n
    c, s, t = math.cos(math.radians(a)), math.sin(math.radians(a)), 1 - math.cos(math.radians(a))
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0],
        [0, 0, 0, 1],
    ]


def node_mat(n):
    o = ident()
    for c in n:
        k = tag(c)
        v = nums(c.text)
        if k == "matrix" and len(v) >= 16:
            q = [v[i : i + 4] for i in range(0, 16, 4)]
        elif k == "translate":
            q = ident()
            q[0][3], q[1][3], q[2][3] = (v + [0, 0, 0])[:3]
        elif k == "scale":
            q = ident()
            q[0][0], q[1][1], q[2][2] = (v + [1, 1, 1])[:3]
        elif k == "rotate":
            q = rot(v)
        else:
            continue
        o = mm(o, q)
    return o


def pose_mat(v):
    x, y, z, r, p, yaw = (v + [0] * 6)[:6]
    cr, sr, cp, sp, cy, sy = (
        math.cos(r),
        math.sin(r),
        math.cos(p),
        math.sin(p),
        math.cos(yaw),
        math.sin(yaw),
    )
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y],
        [-sp, cp * sr, cp * cr, z],
        [0, 0, 0, 1],
    ]


def dae(path):
    root = ET.fromstring(path.read_text(encoding="utf-8-sig").lstrip())
    asset = next((e for e in root if tag(e) == "asset"), None)
    meter = (
        float(next((e.attrib.get("meter", "1") for e in asset if tag(e) == "unit"), "1"))
        if asset is not None
        else 1.0
    )
    up = (
        (
            next((e.text or "Z_UP" for e in asset if tag(e) == "up_axis"), "Z_UP")
            if asset is not None
            else "Z_UP"
        )
        .strip()
        .upper()
    )
    src = {}
    for s in root.iter():
        if tag(s) != "source":
            continue
        a = next((e for e in s if tag(e) == "float_array"), None)
        v = nums(a.text if a is not None else "")
        if len(v) >= 3:
            src[s.attrib.get("id", "")] = v
    geoms = {}
    for g in root.iter():
        if tag(g) != "geometry":
            continue
        m = next((e for e in g if tag(e) == "mesh"), None)
        if m is None:
            continue
        sid = None
        for vs in m:
            if tag(vs) == "vertices":
                sid = next(
                    (
                        i.attrib.get("source", "").lstrip("#")
                        for i in vs
                        if i.attrib.get("semantic") == "POSITION"
                    ),
                    None,
                )
        v = src.get(sid or "", next(iter(src.values()), []))
        geoms[g.attrib.get("id", "")] = list(zip(v[0::3], v[1::3], v[2::3], strict=True))
    out = []
    scene = next((e for e in root.iter() if tag(e) == "visual_scene"), None)

    def visit(n, parent, seen):
        world = mm(parent, node_mat(n))
        nid = n.attrib.get("id", "")
        for c in n:
            if tag(c) == "instance_geometry":
                out.extend(tp(world, p) for p in geoms.get(c.attrib.get("url", "").lstrip("#"), []))
            elif tag(c) == "instance_node":
                ref = c.attrib.get("url", "").lstrip("#")
                q = next(
                    (x for x in root.iter() if tag(x) == "node" and x.attrib.get("id") == ref), None
                )
                if q is not None and ref not in seen:
                    visit(q, world, seen | {ref})
            elif tag(c) == "node":
                visit(c, world, seen | {nid})

    if scene is not None:
        for n in scene:
            if tag(n) == "node":
                visit(n, ident(), set())
    if not out:
        out = [p for v in geoms.values() for p in v]
    return [
        (
            (x * meter, z * meter, -y * meter)
            if up == "Y_UP"
            else (
                (-y * meter, x * meter, z * meter)
                if up == "X_UP"
                else (x * meter, y * meter, z * meter)
            )
        )
        for x, y, z in out
    ]


def verts(path):
    if path.suffix.lower() == ".obj":
        return [
            tuple((nums(line[1:]) + [0, 0, 0])[:3])
            for line in path.read_text(errors="ignore").splitlines()
            if line.startswith("v ")
        ]
    if path.suffix.lower() == ".stl":
        return [
            tuple(nums(line)[-3:])
            for line in path.read_text(errors="ignore").splitlines()
            if line.strip().lower().startswith("vertex ")
        ]
    return dae(path)


def sdf_points(asset):
    root = ET.fromstring((asset / "model.sdf").read_text(encoding="utf-8-sig").lstrip())
    out = []
    for v in root.iter():
        if tag(v) not in {"visual", "collision"}:
            continue
        g = next((e for e in v if tag(e) == "geometry"), None)
        if g is None:
            continue
        pose = pose_mat(nums(next((e.text for e in v if tag(e) == "pose"), " ")))
        mesh = next((e for e in g.iter() if tag(e) == "mesh"), None)
        if mesh is not None:
            uri = next((e.text or "" for e in mesh if tag(e) == "uri"), "")
            rel = uri.split("/", 1)[-1] if uri.startswith("model://") else uri
            p = asset / rel
            if not p.exists():
                p = next(asset.glob("**/" + Path(rel).name), p)
            try:
                ps = verts(p)
            except (OSError, ET.ParseError):
                ps = []
            s = (nums(next((e.text for e in mesh if tag(e) == "scale"), " ")) + [1, 1, 1])[:3]
            out.extend(tp(pose, (a * s[0], b * s[1], c * s[2])) for a, b, c in ps)
        else:
            b = next((e for e in g if tag(e) == "box"), None)
            s = (
                nums(next((e.text for e in b if tag(e) == "size"), "1 1 1"))
                if b is not None
                else [1, 1, 1]
            )
            x, y, z = (s + [1, 1, 1])[:3]
            out.extend(
                tp(pose, (i * x / 2, j * y / 2, k * z / 2))
                for i in (-1, 1)
                for j in (-1, 1)
                for k in (-1, 1)
            )
    if not out:
        actor = next((e for e in root.iter() if tag(e) == "actor"), None)
        if actor is not None:
            skin = next((e for e in actor if tag(e) == "skin"), None)
            if skin is not None:
                fn = next((e.text or "" for e in skin if tag(e) == "filename"), "")
                p = asset / fn
                if p.exists():
                    try:
                        ps = verts(p)
                        scale = float(next((e.text or "1" for e in skin if tag(e) == "scale"), "1"))
                        out.extend((x * scale, y * scale, z * scale) for x, y, z in ps)
                    except (OSError, ET.ParseError, ValueError):
                        pass
    return out


def measure(asset):
    p = sdf_points(asset) or [(0, 0, 0)]
    return tuple(min(v[i] for v in p) for i in range(3)), tuple(
        max(v[i] for v in p) for i in range(3)
    )


def valid(name, s):
    h = s[2]
    longest = max(s[:2])
    n = name.lower()
    if any(k in n for k in ("person", "visitor", "female")):
        ok, r = 1.5 <= h <= 2, "person height 1.5-2.0m"
    elif "cone" in n:
        ok, r = 0.3 <= h <= 1.2, "cone height 0.3-1.2m"
    elif "barrel" in n:
        ok, r = 0.6 <= h <= 1.3, "barrel height 0.6-1.3m"
    elif "tree" in n:
        ok, r = 3 <= h <= 20, "tree height 3-20m"
    elif any(
        k in n
        for k in ("house", "building", "station", "apartment", "gazebo", "playground", "fountain")
    ):
        ok, r = 2.5 <= h <= 30, "structure height 2.5-30m"
    elif any(k in n for k in ("hatchback", "suv", "pickup", "prius", "ambulance", "truck", "bus")):
        ok, r = 3.5 <= longest <= 13 and 1.2 <= h <= 4, "vehicle length 3.5-13m, height 1.2-4m"
    elif "stop_sign" in n:
        ok, r = 0.5 <= h <= 3, "stop sign height 0.5-3m"
    else:
        ok, r = all(0.01 <= x <= 30 for x in s), "component dimensions 0.01-30m"
    return ok, "" if ok else r


def _material_blocks(text):
    """Return legacy OGRE material blocks keyed by their material name."""
    blocks = {}
    for match in re.finditer(r"\bmaterial\s+([^\s{]+)\s*\{", text):
        depth = 1
        pos = match.end()
        while depth and pos < len(text):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        block = text[match.end() : pos - 1]
        texture = re.search(r"\btexture\s+([^\s}]+)", block)
        diffuse = re.search(r"\bdiffuse\s+([^\n}]+)", block)
        ambient = re.search(r"\bambient\s+([^\n}]+)", block)
        blocks[match.group(1)] = {
            "texture": texture.group(1) if texture else None,
            "color": (diffuse or ambient).group(1).strip() if (diffuse or ambient) else None,
        }
    return blocks


def _legacy_materials(asset):
    materials = {}
    scripts = asset / "materials" / "scripts"
    if scripts.is_dir():
        for path in scripts.rglob("*.material"):
            materials.update(_material_blocks(path.read_text(encoding="utf-8", errors="ignore")))
    return materials


def _dae_images_resolve(asset, mesh_uri):
    """Check that a COLLADA mesh's relative init_from images exist in the copied tree."""
    if not mesh_uri.lower().endswith(".dae"):
        return False
    mesh = asset / mesh_uri
    if not mesh.is_file():
        return False
    try:
        root = ET.parse(mesh).getroot()
    except (OSError, ET.ParseError):
        return False
    images = [
        e.text.strip() for e in root.iter() if tag(e) == "init_from" and e.text and e.text.strip()
    ]
    if not images:
        return False
    return all(
        (mesh.parent / image.replace("\\", "/")).is_file()
        for image in images
        if not image.startswith(("file:", "http:"))
    )


def _convert_resolved_materials(asset, root, slug):
    materials = _legacy_materials(asset)
    unsupported = []
    for path in asset.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".tga", ".dds"}:
            unsupported.append(path.relative_to(asset).as_posix())
    for visual in (e for e in root.iter() if tag(e) == "visual"):
        material = next((e for e in visual if tag(e) == "material"), None)
        if material is None or not any(tag(e) == "script" for e in material):
            continue
        mesh = next((e for e in visual.iter() if tag(e) == "mesh"), None)
        uri = next((e.text or "" for e in mesh if tag(e) == "uri"), "") if mesh is not None else ""
        rel_uri = (
            uri.split("model://", 1)[-1].split("/", 1)[-1] if uri.startswith("model://") else uri
        )
        if _dae_images_resolve(asset, rel_uri):
            visual.remove(material)
            continue
        name = next((e.text or "" for e in material.iter() if tag(e) == "name"), "")
        info = materials.get(name)
        if info is None and name == "Gazebo/Grey":
            info = {"texture": None, "color": "0.7 0.7 0.7 1"}
        if info is None:
            continue
        for child in list(material):
            material.remove(child)
        if info["texture"]:
            ET.SubElement(material, "diffuse").text = "1 1 1 1"
            ET.SubElement(material, "specular").text = "0.1 0.1 0.1 1"
            pbr = ET.SubElement(material, "pbr")
            metal = ET.SubElement(pbr, "metal")
            ET.SubElement(
                metal, "albedo_map"
            ).text = f"model://{slug}/materials/textures/{info['texture']}"
            ET.SubElement(metal, "roughness").text = "0.8"
            ET.SubElement(metal, "metalness").text = "0"
        elif info["color"]:
            ET.SubElement(material, "diffuse").text = info["color"]
    return unsupported


def build():
    records = []
    for a in sorted(p for p in ASSET_ROOT.iterdir() if p.is_dir()):
        lo, hi = measure(a)
        size = tuple(hi[i] - lo[i] for i in range(3))
        slug = re.sub(r"[^a-z0-9]+", "_", a.name.lower()).strip("_")
        cat, words, roles, color = META.get(
            a.name, ("scenery", [a.name.lower()], ["scenery-obstacle"], None)
        )
        good, reason = valid(a.name, size)
        roles = roles if good else []
        resolved_sdf = ASSET_ROOT.parent / "resolved" / slug / "model.sdf"
        hash_path = resolved_sdf if resolved_sdf.is_file() else a / "model.sdf"
        records.append(
            {
                "slug": slug,
                "source": a.name,
                "source_url": f"https://fuel.gazebosim.org/1.0/OpenRobotics/models/{a.name}",
                "license": "CC0-1.0",
                "model_sdf_sha256": hashlib.sha256(hash_path.read_bytes()).hexdigest(),
                "category": cat,
                "semantic_words": words,
                "role_eligibility": roles,
                "color": color,
                "measured_size_m": [round(x, 6) for x in size],
                "mesh_origin_offset_m": {"z_bottom": round(-lo[2], 6)},
                "bbox_min_m": [round(x, 6) for x in lo],
                "bbox_max_m": [round(x, 6) for x in hi],
                "validated": good,
                "validation_reason": reason,
            }
        )
    return {
        "schema_version": 2,
        "asset_mode": "fuel_v1",
        "asset_root": "artifacts/cache/gz-fuel/OpenRobotics",
        "assets": records,
    }


_FUEL_FILE_URL = re.compile(r"https?://fuel\.[^\s/]+/[^\s]*?/models/[^\s/]+/[^\s/]+/files/(\S+)")


def _localize_mtl_textures(model_dir, slug):
    """Point OBJ material textures at the model's own files instead of Fuel URLs.

    Some OpenRobotics meshes (hatchback, ambulance) reference their textures by
    https://fuel.gazebosim.org/... URL; Gazebo then downloads the model while
    loading the world and, offline or with a slow Fuel server, never finishes
    (PX4 timed out waiting for the world in SITL).  A URL whose file exists in
    the model is replaced by a path relative to the .mtl file; a texture the
    model does not ship (the wheels) loses its map line and renders untextured.
    """
    for mtl in model_dir.rglob("*.mtl"):
        text = mtl.read_text(encoding="utf-8", errors="surrogateescape")
        lines = []
        for line in text.splitlines(keepends=True):
            match = _FUEL_FILE_URL.search(line)
            if match is None:
                lines.append(line)
                continue
            target = model_dir / match.group(1)
            if not target.is_file():
                print(f"warning: {slug} {mtl.name} drops a texture it does not ship: {target.name}")
                continue
            local = os.path.relpath(target, mtl.parent).replace(os.sep, "/")
            lines.append(line[: match.start()] + local + line[match.end() :])
        rewritten = "".join(lines)
        if rewritten != text:
            mtl.write_text(rewritten, encoding="utf-8", errors="surrogateescape", newline="")


def build_resolved(root=ASSET_ROOT, output=None):
    output = output or root.parent / "resolved"
    output.mkdir(parents=True, exist_ok=True)
    for a in sorted(p for p in root.iterdir() if p.is_dir()):
        slug = re.sub(r"[^a-z0-9]+", "_", a.name.lower()).strip("_")
        d = output / slug
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(a, d)
        f = d / "model.sdf"
        t = f.read_text(encoding="utf-8-sig")
        t = re.sub(r"model://[^/\\s]+/", f"model://{slug}/", t)
        t = re.sub(r"https?://[^<\\s]+/", f"model://{slug}/", t)
        root_xml = ET.fromstring(t.lstrip())
        unsupported = _convert_resolved_materials(d, root_xml, slug)
        ET.ElementTree(root_xml).write(f, encoding="utf-8", xml_declaration=True)
        _localize_mtl_textures(d, slug)
        if unsupported:
            print(f"warning: {slug} contains unsupported texture formats: {', '.join(unsupported)}")
    return output


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=OUT)
    a = ap.parse_args()
    build_resolved()
    d = build()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(
        json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(d['assets'])} assets to {a.output}")
    print("slug,size_m,validated,roles")
    [
        print(
            f"{x['slug']},{x['measured_size_m']},{x['validated']},{'|'.join(x['role_eligibility'])}"
        )
        for x in d["assets"]
    ]
