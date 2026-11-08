#!/usr/bin/env python3
"""Prepare interleaved mover/position controls without changing source scenarios."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.autonomy.serialization import canonical_sha256


def prepare(root: Path, out: Path, repeats: int = 3) -> Path:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    # A new directory prevents an accidental rerun from hiding earlier failures.
    out.mkdir(parents=True, exist_ok=False)
    source = root / "configs/scenarios/generated-movers"
    stationary = out / "stationary"
    stationary.mkdir()
    scenario = "gen_mov_w0000"
    manifest = json.loads((source / f"{scenario}.scenario.json").read_text())
    for ref in manifest["channels"].values():
        shutil.copyfile(source / ref["path"], stationary / ref["path"])
    truth_path = stationary / manifest["channels"]["truth"]["path"]
    truth = json.loads(truth_path.read_text())
    for target in truth["targets"]:
        if target.get("true_target"):
            target["behavior"] = "stationary"
            target.pop("trajectory", None)
    truth_path.write_text(json.dumps(truth, indent=2) + "\n")
    for ref in manifest["channels"].values():
        ref["sha256"] = canonical_sha256(json.loads((stationary / ref["path"]).read_text()))
    (stationary / f"{scenario}.scenario.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(source / "index.json", stationary / "index.json")
    suite = json.loads((root / "configs/onboard/suite-follow-r21idr3-r24plan.json").read_text())
    base = suite["missions"][0]
    missions = []
    for repeat in range(repeats):
        for label, world, index in (
            ("w0000-moving", "gen_mov_w0000", source / "index.json"),
            ("w0000-stationary", "gen_mov_w0000", stationary / "index.json"),
            ("w0002-moving", "gen_mov_w0002", source / "index.json"),
        ):
            mission = copy.deepcopy(base)
            mission.update(id=f"{label}-{repeat:02d}", world=world,
                           index=str(index.resolve()), airframe="x500_mono_cam")
            missions.append(mission)
    suite["missions"] = missions
    suite_path = out / "suite.json"
    suite_path.write_text(json.dumps(suite, indent=2) + "\n")
    facts = {}
    for world in ("gen_mov_w0000", "gen_mov_w0002"):
        path = root / "simulation/gazebo/worlds" / f"{world}.sdf"
        sdf = ET.parse(path).getroot().find("world")
        physics = ET.tostring(sdf.find("physics"), encoding="unicode")
        facts[world] = dict(
            sdf_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), physics=physics,
            plugins=[p.attrib for p in sdf.iter("plugin")],
            target_pose=sdf.find("model[@name='target_true_" + world + "']/pose").text,
        )
    (out / "experiment.json").write_text(json.dumps(dict(
        repeats=repeats, episodes_per_mission=1, worlds=facts,
        physics_equal=facts["gen_mov_w0000"]["physics"] == facts["gen_mov_w0002"]["physics"],
        plugins_equal=facts["gen_mov_w0000"]["plugins"] == facts["gen_mov_w0002"]["plugins"],
        interpretation="Stationary is a clock control; retain original follow assertions and report skill failures separately.",
    ), indent=2) + "\n")
    return suite_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(prepare(ROOT, args.out.resolve(), args.repeats))


if __name__ == "__main__":
    main()
