#!/usr/bin/env python3
"""Static rain streaks around the takeoff area.

The particle emitter needs a world <plugin>, and any world plugin replaces Gazebo's default
systems so the world never becomes ready. Rain is therefore a mesh of thin translucent quads
tilted with the wind. Streaks do not fall, which a single frame cannot show.
"""

from pathlib import Path

import numpy as np

RESOLVED = Path(__file__).resolve().parents[2] / "artifacts/cache/gz-fuel/resolved"
OUT = RESOLVED / "rw_rain"
N = 160000
HALF = 120.0
TOP = 55.0


def main() -> None:
    (OUT / "meshes").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    c = np.column_stack([rng.uniform(-HALF, HALF, N), rng.uniform(-HALF, HALF, N), rng.uniform(0, TOP, N)])
    length = rng.uniform(0.5, 1.1, N)
    tilt = np.array([0.08, 0.04, -1.0])
    tilt /= np.linalg.norm(tilt)
    ang = rng.uniform(0, np.pi, N)
    side = np.column_stack([np.cos(ang), np.sin(ang), np.zeros(N)]) * 0.007
    a = c - side - tilt[None] * length[:, None] / 2
    b = c + side - tilt[None] * length[:, None] / 2
    d = c + side + tilt[None] * length[:, None] / 2
    e = c - side + tilt[None] * length[:, None] / 2
    v = np.stack([a, b, d, e], axis=1).reshape(-1, 3)
    with (OUT / "meshes/streaks.obj").open("w") as f:
        np.savetxt(f, v, fmt="v %.2f %.2f %.3f")
        f.write("vn 0 0 1\n")
        idx = np.arange(N)[:, None] * 4 + 1 + np.array([0, 1, 2, 0, 2, 3])
        np.savetxt(f, idx.reshape(-1, 3), fmt="f %d//1 %d//1 %d//1")
    (OUT / "model.config").write_text('<?xml version="1.0"?><model><name>rw_rain</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><description>Static rain streak mesh</description></model>\n')
    (OUT / "model.sdf").write_text('<?xml version="1.0"?><sdf version="1.9"><model name="rw_rain"><static>true</static><link name="l"/></model></sdf>\n')


if __name__ == "__main__":
    main()
