"""Merge repeated Fuel props into a few big meshes.

Thousands of separate Gazebo models cost a lot per physics step, so scenery props become one
static link. Only objects the AI must tell apart stay separate, labelled models.
"""

from __future__ import annotations

import json
import math
import shutil
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import collada
import numpy as np
import scipy.ndimage as ndi
import trimesh
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
RESOLVED = ROOT / "artifacts/cache/gz-fuel/resolved"
CATALOG = {a["slug"]: a for a in json.loads((ROOT / "configs/sim-assets/fuel-catalog.json").read_text())["assets"]}
GRID_M = 500.0

warnings.filterwarnings("ignore")


def _find_file(model_dir: Path, name: str) -> Path | None:
    base = Path(name.replace("\\", "/").split("?")[0]).name
    for p in model_dir.rglob(base):
        return p
    return None


def _pose_matrix(text: str | None) -> np.ndarray:
    m = np.eye(4)
    if not text:
        return m
    x, y, z, r, p, w = [float(c) for c in text.split()]
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(w), math.sin(w)
    m[:3, :3] = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                 [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                 [-sp, cp * sr, cp * cr]]
    m[:3, 3] = [x, y, z]
    return m


def _walk(node, tf, owner, out):
    if isinstance(node, collada.scene.Node):
        tf = tf @ node.matrix
        owner = node.id or owner
        for c in node.children:
            _walk(c, tf, owner, out)
    elif isinstance(node, collada.scene.GeometryNode):
        out.append((node, tf, owner))
    elif isinstance(node, collada.scene.NodeNode):
        _walk(node.node, tf, owner, out)


def _dae_parts(path: Path, model_dir: Path, submesh: str | None) -> list[dict]:
    dae = collada.Collada(str(path), ignore=[collada.common.DaeUnsupportedError, collada.common.DaeBrokenRefError])
    unit = dae.assetInfo.unitmeter or 1.0
    up = np.eye(4)
    if dae.assetInfo.upaxis == "Y_UP":
        up = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]])
    geoms: list[tuple] = []
    for n in dae.scene.nodes:
        _walk(n, np.eye(4), None, geoms)
    parts = []
    for gn, tf, owner in geoms:
        if submesh and owner != submesh:
            continue
        syms = {m.symbol: m.target for m in gn.materials}
        for prim in gn.geometry.primitives:
            if not isinstance(prim, (collada.triangleset.TriangleSet, collada.polylist.Polylist, collada.polygons.Polygons)):
                continue
            tri = prim if isinstance(prim, collada.triangleset.TriangleSet) else prim.triangleset()
            vi = tri.vertex_index
            v = tri.vertex[vi.reshape(-1)]
            if len(tri.texcoordset):
                uv = tri.texcoordset[0][tri.texcoord_indexset[0].reshape(-1)][:, :2]
            else:
                uv = np.zeros((len(v), 2))
            v = (up @ tf @ np.c_[v * unit, np.ones(len(v))].T).T[:, :3] if False else (up @ tf @ np.c_[v, np.ones(len(v))].T).T[:, :3] * unit
            tex, col = None, None
            mat = syms.get(tri.material)
            if mat is not None and mat.effect is not None:
                d = mat.effect.diffuse
                if isinstance(d, collada.material.Map):
                    tex = _find_file(model_dir, d.sampler.surface.image.path)
                elif d is not None:
                    col = tuple(d[:3])
            parts.append({"v": v, "uv": uv, "tex": tex, "color": col})
    return parts


def _obj_parts(path: Path, model_dir: Path) -> list[dict]:
    scene = trimesh.load(str(path), force="scene", skip_materials=False)
    mtl = {}
    for f in model_dir.rglob("*.mtl"):
        cur = None
        for line in f.read_text(errors="ignore").splitlines():
            bits = line.strip().split()
            if len(bits) >= 2 and bits[0] == "newmtl":
                cur = bits[1]
            elif len(bits) >= 2 and bits[0].lower() == "map_kd" and cur:
                mtl[cur] = _find_file(model_dir, bits[-1])
    parts = []
    for node in scene.graph.nodes_geometry:
        tf, gname = scene.graph[node]
        g = scene.geometry[gname]
        uv = getattr(g.visual, "uv", None)
        uv = np.zeros((len(g.vertices), 2)) if uv is None else np.asarray(uv)
        fi = g.faces.reshape(-1)
        v = (tf @ np.c_[g.vertices, np.ones(len(g.vertices))].T).T[:, :3]
        name = getattr(getattr(g.visual, "material", None), "name", None) or gname
        parts.append({"v": v[fi], "uv": uv[fi][:, :2], "tex": mtl.get(name) or mtl.get(gname), "color": None})
    return parts


def load_parts(slug: str) -> list[dict]:
    model_dir = RESOLVED / slug
    root = ET.fromstring((model_dir / "model.sdf").read_text(encoding="utf-8-sig").lstrip())
    visuals = list(root.iter("visual"))
    out: list[dict] = []
    if not visuals:
        for f in model_dir.rglob("*.dae"):
            out += _dae_parts(f, model_dir, None)
            break
    seen = set()
    for vis in visuals:
        uri = vis.findtext("geometry/mesh/uri", "")
        key = (uri, vis.findtext("geometry/mesh/submesh/name"), vis.findtext("pose"))
        if not uri or key in seen:
            continue
        seen.add(key)
        rel = uri.split("/files/", 1)[1] if "/files/" in uri else uri.split("/", 3)[-1] if uri.startswith("model://") else uri
        path = model_dir / rel.replace("%20", " ")
        if not path.exists():
            found = _find_file(model_dir, rel)
            if found is None:
                continue
            path = found
        sub = vis.findtext("geometry/mesh/submesh/name")
        parts = _dae_parts(path, model_dir, sub) if path.suffix.lower() == ".dae" else _obj_parts(path, model_dir)
        scale = np.array([float(c) for c in (vis.findtext("geometry/mesh/scale") or "1 1 1").split()])
        pose = _pose_matrix(vis.findtext("pose"))
        albedo = None
        for t in vis.iter("albedo_map"):
            albedo = t.text
        for p in parts:
            v = (pose @ np.c_[p["v"] * scale, np.ones(len(p["v"]))].T).T[:, :3]
            p["v"] = v
            if albedo:
                found = _find_file(model_dir, albedo)
                if found:
                    p["tex"] = found
            out.append(p)
    # Unit handling differs between loaders. The catalogue holds what Gazebo really renders.
    cat = CATALOG.get(slug)
    if cat and out:
        allv = np.vstack([p["v"] for p in out])
        lo, hi = allv.min(0), allv.max(0)
        size = hi - lo
        k = cat["measured_size_m"][2] / max(size[2], 1e-6)
        target_lo = np.array(cat["bbox_min_m"])
        for p in out:
            p["v"] = (p["v"] - lo) * k + target_lo
    return out


def copy_texture(src: Path, dst: Path) -> None:
    """Bleed colour into transparent texels: mipmaps otherwise fade leaf cards toward white."""
    im = Image.open(src)
    if im.mode not in {"RGBA", "LA"}:
        shutil.copy(src, dst)
        return
    a = np.asarray(im.convert("RGBA"), np.float32)
    mask = a[..., 3] > 128
    if mask.all() or not mask.any():
        shutil.copy(src, dst)
        return
    _, (iy, ix) = ndi.distance_transform_edt(~mask, return_indices=True)
    a[..., :3] = a[iy, ix, :3]
    Image.fromarray(a.astype(np.uint8), "RGBA").save(dst)


class Merger:
    def __init__(self, site: str, out: Path) -> None:
        self.site, self.out = site, out
        self.groups: dict[tuple, dict] = {}
        self.parts: dict[str, list[dict]] = {}

    def add(self, slug: str, x, y, z, yaw, scale=1.0) -> None:
        if slug not in self.parts:
            self.parts[slug] = load_parts(slug)
            print("loaded", slug, [(len(p["v"]) // 3, p["tex"].name if p["tex"] else p["color"]) for p in self.parts[slug]], flush=True)
        c, s = math.cos(yaw), math.sin(yaw)
        rot = np.array([[c, -s], [s, c]])
        cell = (int(x // GRID_M), int(y // GRID_M))
        cat = CATALOG.get(slug)
        lift = -cat["bbox_min_m"][2] * scale if cat else 0.0
        for i, p in enumerate(self.parts[slug]):
            g = self.groups.setdefault((slug, i, cell), {"v": [], "uv": []})
            v = p["v"] * scale
            g["v"].append(np.c_[(rot @ v[:, :2].T).T + [x, y], v[:, 2] + z + lift])
            g["uv"].append(p["uv"])

    def write(self, write_obj, normals) -> list[str]:
        xml = []
        tex_done: dict[Path, str] = {}
        for k, ((slug, i, cell), g) in enumerate(sorted(self.groups.items(), key=lambda kv: str(kv[0]))):
            v = np.vstack(g["v"])
            uv = np.vstack(g["uv"])
            f = np.arange(len(v)).reshape(-1, 3)
            name = f"inst_{slug}_{i}_{cell[0]}_{cell[1]}".replace("-", "m")
            write_obj(self.out / "meshes" / f"{name}.obj", v, uv, normals(v, f), f)
            p = self.parts[slug][i]
            if p["tex"] is not None:
                if p["tex"] not in tex_done:
                    dst = f"inst_{slug}_{i}{p['tex'].suffix.lower()}"
                    copy_texture(p["tex"], self.out / "textures" / dst)
                    tex_done[p["tex"]] = dst
                mat = (f"<material><diffuse>1 1 1 1</diffuse><specular>0.1 0.1 0.1 1</specular><double_sided>true</double_sided><pbr><metal>"
                       f"<albedo_map>model://{self.site}/textures/{tex_done[p['tex']]}</albedo_map><roughness>0.8</roughness><metalness>0</metalness></metal></pbr></material>")
            else:
                col = p["color"] or (0.5, 0.5, 0.5)
                mat = f"<material><diffuse>{col[0]:.3f} {col[1]:.3f} {col[2]:.3f} 1</diffuse><specular>0.1 0.1 0.1 1</specular><double_sided>true</double_sided></material>"
            xml.append(f'<visual name="{name}"><cast_shadows>true</cast_shadows><geometry><mesh><uri>model://{self.site}/meshes/{name}.obj</uri></mesh></geometry>{mat}</visual>')
        return xml
