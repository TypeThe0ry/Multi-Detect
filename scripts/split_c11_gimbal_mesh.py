"""Split the STEP-derived C11 mesh into fixed mount / yaw neck / pitch ball parts (mesh frame, metres).

The STEP carries C11 as one product, so the split is geometric: the pod axis is the mesh X axis (mounting plate and
dampers at -X, ball at +X), the pitch axis is mesh Y (side caps) and the lens looks along +Z. Whole connected components
are assigned by their bounding-box centre along X; the single big shell (ball, caps and neck) is split by face centre.
The L-shaped roll arm is not a separate body in the STEP, so the roll link has no visual.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import trimesh

FIXED_MAX_X = -0.0275  # dampers and mounting plate
YAW_MAX_X = -0.0150  # yaw motor neck


def split(mesh: trimesh.Trimesh) -> dict[str, trimesh.Trimesh]:
    parts = mesh.split(only_watertight=False)
    biggest = max(parts, key=lambda item: len(item.faces))
    groups: dict[str, list[trimesh.Trimesh]] = {"fixed": [], "yaw": [], "ball": []}
    for part in parts:
        if part is biggest:
            centres = part.triangles_center[:, 0]
            yaw_faces = centres < YAW_MAX_X
            if yaw_faces.any():
                groups["yaw"].append(part.submesh([np.flatnonzero(yaw_faces)], append=True))
            groups["ball"].append(part.submesh([np.flatnonzero(~yaw_faces)], append=True))
            continue
        centre_x = float(part.bounds[:, 0].mean())
        key = "fixed" if centre_x < FIXED_MAX_X else "yaw" if centre_x < YAW_MAX_X else "ball"
        groups[key].append(part)
    return {name: trimesh.util.concatenate(items) for name, items in groups.items() if items}


def main(directory: Path) -> None:
    mesh = trimesh.load(directory / "c11.stl")
    for name, part in split(mesh).items():
        part.export(directory / f"c11_{name}.stl")
        print(name, len(part.faces), np.round(part.bounds, 4).tolist())


if __name__ == "__main__":
    main(Path(sys.argv[1]))
