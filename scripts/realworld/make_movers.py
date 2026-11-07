#!/usr/bin/env python3
"""Rig Fuel vehicles as one-bone COLLADA actors, so scripted trajectories can drive them.

Gazebo's TrajectoryFollower has no speed control (a test car ran off at 22 m/s), while an actor
follows its waypoints kinematically: exact speed, on the terrain, with no physics cost.
"""

from __future__ import annotations

import shutil
import os
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instancing  # noqa: E402
from instancing import RESOLVED, load_parts  # noqa: E402

VEHICLES = ["hatchback", "hatchback_blue", "suv", "pickup", "prius_hybrid", "bus", "ambulance", "fire_truck"]
OUT = Path(os.environ.get("RW_MOVER_OUT", RESOLVED / "rw_movers"))
LIBRARY = Path("/data/datasets/multidetect-sim-assets/library/fuel/models")


def fl(values) -> str:
    return " ".join(f"{v:.5g}" for v in values)


def rig(slug: str, source_dir: Path | None = None) -> Path:
    old_resolved = instancing.RESOLVED
    if source_dir is not None:
        instancing.RESOLVED = source_dir.parent
    parts = load_parts(slug)
    instancing.RESOLVED = old_resolved
    images, effects, materials, geoms, controllers, nodes = [], [], [], [], [], []
    for i, p in enumerate(parts):
        n = len(p["v"])
        v = p["v"].reshape(-1)
        uv = p["uv"].reshape(-1)
        fa = p["v"].reshape(-1, 3, 3)
        nrm = np.cross(fa[:, 1] - fa[:, 0], fa[:, 2] - fa[:, 0])
        nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
        nrm = np.repeat(nrm, 3, axis=0).reshape(-1)
        idx = " ".join(f"{k} {k} {k}" for k in range(n))
        if p["tex"] is not None:
            dst = f"{slug}_{i}{p['tex'].suffix.lower()}"
            shutil.copy(p["tex"], OUT / "meshes" / dst)
            images.append(f'<image id="img{i}"><init_from>{dst}</init_from></image>')
            diffuse = f'<texture texture="samp{i}" texcoord="UV"/>'
            params = (f'<newparam sid="surf{i}"><surface type="2D"><init_from>img{i}</init_from></surface></newparam>'
                      f'<newparam sid="samp{i}"><sampler2D><source>surf{i}</source></sampler2D></newparam>')
        else:
            c = p["color"] or (0.08, 0.08, 0.08)
            diffuse = f"<color>{fl(c)} 1</color>"
            params = ""
        effects.append(f'<effect id="fx{i}"><profile_COMMON>{params}<technique sid="c"><lambert><diffuse>{diffuse}</diffuse></lambert></technique></profile_COMMON></effect>')
        materials.append(f'<material id="mat{i}"><instance_effect url="#fx{i}"/></material>')
        geoms.append(
            f'<geometry id="g{i}"><mesh>'
            f'<source id="g{i}p"><float_array id="g{i}pa" count="{n * 3}">{fl(v)}</float_array><technique_common><accessor source="#g{i}pa" count="{n}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>'
            f'<source id="g{i}n"><float_array id="g{i}na" count="{n * 3}">{fl(nrm)}</float_array><technique_common><accessor source="#g{i}na" count="{n}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>'
            f'<source id="g{i}t"><float_array id="g{i}ta" count="{n * 2}">{fl(uv)}</float_array><technique_common><accessor source="#g{i}ta" count="{n}" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>'
            f'<vertices id="g{i}v"><input semantic="POSITION" source="#g{i}p"/></vertices>'
            f'<triangles material="m{i}" count="{n // 3}"><input semantic="VERTEX" source="#g{i}v" offset="0"/><input semantic="NORMAL" source="#g{i}n" offset="1"/><input semantic="TEXCOORD" source="#g{i}t" offset="2" set="0"/><p>{idx}</p></triangles>'
            f"</mesh></geometry>")
        controllers.append(
            f'<controller id="c{i}"><skin source="#g{i}"><bind_shape_matrix>1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</bind_shape_matrix>'
            f'<source id="c{i}j"><Name_array id="c{i}ja" count="1">Root</Name_array><technique_common><accessor source="#c{i}ja" count="1"><param name="JOINT" type="name"/></accessor></technique_common></source>'
            f'<source id="c{i}b"><float_array id="c{i}ba" count="16">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</float_array><technique_common><accessor source="#c{i}ba" count="1" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common></source>'
            f'<source id="c{i}w"><float_array id="c{i}wa" count="1">1</float_array><technique_common><accessor source="#c{i}wa" count="1"><param name="WEIGHT" type="float"/></accessor></technique_common></source>'
            f'<joints><input semantic="JOINT" source="#c{i}j"/><input semantic="INV_BIND_MATRIX" source="#c{i}b"/></joints>'
            f'<vertex_weights count="{n}"><input semantic="JOINT" source="#c{i}j" offset="0"/><input semantic="WEIGHT" source="#c{i}w" offset="1"/><vcount>{" ".join(["1"] * n)}</vcount><v>{" ".join(["0 0"] * n)}</v></vertex_weights>'
            f"</skin></controller>")
        nodes.append(f'<node id="n{i}" name="n{i}" type="NODE"><instance_controller url="#c{i}"><skeleton>#Root</skeleton><bind_material><technique_common><instance_material symbol="m{i}" target="#mat{i}"><bind_vertex_input semantic="UV" input_semantic="TEXCOORD" input_set="0"/></instance_material></technique_common></bind_material></instance_controller></node>')
    anim = ('<library_animations><animation id="Root_pose"><source id="at"><float_array id="ata" count="2">0 10</float_array><technique_common><accessor source="#ata" count="2"><param name="TIME" type="float"/></accessor></technique_common></source>'
            '<source id="am"><float_array id="ama" count="32">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</float_array><technique_common><accessor source="#ama" count="2" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common></source>'
            '<source id="ai"><Name_array id="aia" count="2">LINEAR LINEAR</Name_array><technique_common><accessor source="#aia" count="2"><param name="INTERPOLATION" type="name"/></accessor></technique_common></source>'
            '<sampler id="as"><input semantic="INPUT" source="#at"/><input semantic="OUTPUT" source="#am"/><input semantic="INTERPOLATION" source="#ai"/></sampler><channel source="#as" target="Root/matrix"/></animation></library_animations>')
    # Add small emissive front/rear lamps as regular geometry. They move with the actor and
    # remain visible at night without introducing a world plugin or per-vehicle point lights.
    lamp_nodes = []
    for li, (x, color, name) in enumerate(((1.55, (1.0, 0.82, 0.35), "head"), (-1.55, (0.9, 0.04, 0.02), "tail"))):
        gid = f"lamp{li}"
        verts = [x - 0.08, -0.45, 0.38, x + 0.08, -0.45, 0.38, x + 0.08, 0.45, 0.38, x - 0.08, 0.45, 0.38,
                 x - 0.08, -0.45, 0.58, x + 0.08, -0.45, 0.58, x + 0.08, 0.45, 0.58, x - 0.08, 0.45, 0.58]
        faces = "0 1 2 0 2 3 4 6 5 4 7 6 0 4 5 0 5 1 1 5 6 1 6 2 2 6 7 2 7 3 3 7 4 3 4 0"
        geoms.append(f'<geometry id="{gid}"><mesh><source id="{gid}p"><float_array id="{gid}pa" count="24">{fl(verts)}</float_array><technique_common><accessor source="#{gid}pa" count="8" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source><vertices id="{gid}v"><input semantic="POSITION" source="#{gid}p"/></vertices><triangles material="lampmat{li}" count="12"><input semantic="VERTEX" source="#{gid}v" offset="0"/><p>{faces}</p></triangles></mesh></geometry>')
        effects.append(f'<effect id="lampfx{li}"><profile_COMMON><technique sid="c"><lambert><emission><color>{fl(color)} 1</color></emission><diffuse><color>{fl(color)} 1</color></diffuse></lambert></technique></profile_COMMON></effect>')
        materials.append(f'<material id="lampmat{li}"><instance_effect url="#lampfx{li}"/></material>')
        lamp_nodes.append(f'<node id="{gid}" name="{name}_lamp"><instance_geometry url="#{gid}"><bind_material><technique_common><instance_material symbol="lampmat{li}" target="#lampmat{li}"/></technique_common></bind_material></instance_geometry></node>')
    dae = ('<?xml version="1.0"?><COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1"><asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>'
           f'<library_images>{"".join(images)}</library_images><library_effects>{"".join(effects)}</library_effects><library_materials>{"".join(materials)}</library_materials>'
           f'<library_geometries>{"".join(geoms)}</library_geometries><library_controllers>{"".join(controllers)}</library_controllers>{anim}'
           '<library_visual_scenes><visual_scene id="Scene"><node id="Root" name="Root" sid="Root" type="JOINT"><matrix sid="matrix">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>'
           f'{"".join(lamp_nodes)}</node>'
           f'{"".join(nodes)}</visual_scene></library_visual_scenes><scene><instance_visual_scene url="#Scene"/></scene></COLLADA>\n')
    path = OUT / "meshes" / f"{slug}.dae"
    path.write_text(dae)
    return path


def main() -> None:
    (OUT / "meshes").mkdir(parents=True, exist_ok=True)
    (OUT / "model.config").write_text('<?xml version="1.0"?><model><name>rw_movers</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><description>Fuel vehicles rigged as one-bone actors</description></model>\n')
    (OUT / "model.sdf").write_text('<?xml version="1.0"?><sdf version="1.9"><model name="rw_movers"><static>true</static><link name="l"/></model></sdf>\n')
    for slug in VEHICLES:
        print(rig(slug), flush=True)
    boat_src = LIBRARY / "rc_boat"
    if boat_src.exists():
        temp_src = OUT.parent / "rc_boat"
        if not temp_src.exists():
            shutil.copytree(boat_src, temp_src)
        print(rig("rc_boat", temp_src), flush=True)


if __name__ == "__main__":
    main()
