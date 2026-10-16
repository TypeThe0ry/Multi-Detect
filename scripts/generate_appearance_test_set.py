#!/usr/bin/env python3
"""Mirror the six generated-v2 test worlds with seeded lighting profiles.

The source Fuel meshes are already resolved in the repository's generated set;
this tool therefore transforms the existing SDF/channel files and never needs a
network fetch.  Object poses, tasks and truth coordinates are copied exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.world_gen import APPEARANCE_PRESETS, _appearance_values  # noqa: E402
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402


def _write_json(path: Path, value: object) -> str:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _replace(value: object, source: str, target: str) -> object:
    if isinstance(value, str):
        return value.replace(source, target)
    if isinstance(value, list):
        return [_replace(item, source, target) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, source, target) for key, item in value.items()}
    return value


def _set_child(parent: ET.Element, name: str, text: str) -> None:
    child = parent.find(name)
    if child is None:
        child = ET.SubElement(parent, name)
    child.text = text


def _appearance_sdf(source: Path, source_id: str, target_id: str, preset: str, seed: int) -> bytes:
    root = ET.fromstring(source.read_text(encoding="utf-8"))
    world = root.find("world")
    if world is None:
        raise ValueError(f"SDF has no world: {source}")
    scene = world.find("scene")
    light = world.find("light[@name='sun']")
    if light is None:
        light = world.find("light")
    if scene is None or light is None:
        raise ValueError(f"SDF has no scene/sun: {source}")
    values = _appearance_values(preset, __import__("random").Random(seed))
    _set_child(scene, "ambient", " ".join(f"{v:.3f}" for v in values["ambient"]) + " 1")
    _set_child(scene, "background", " ".join(f"{v:.3f}" for v in values["background"]) + " 1")
    _set_child(scene, "shadows", str(values["shadows"]).lower())
    _set_child(light, "direction", " ".join(f"{v:.4f}" for v in values["direction"]))
    _set_child(light, "cast_shadows", str(values["shadows"]).lower())
    _set_child(light, "intensity", f"{values['intensity']:.3f}")
    _set_child(light, "diffuse", " ".join(f"{v:.3f}" for v in values["diffuse"]) + " 1")
    _set_child(light, "specular", " ".join(f"{v:.3f}" for v in values["specular"]) + " 1")
    for element in root.iter():
        if element.text:
            element.text = element.text.replace(source_id, target_id)
        for key, value in list(element.attrib.items()):
            element.attrib[key] = value.replace(source_id, target_id)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def generate_appearance_test_set(
    *,
    source_worlds: Path = ROOT / "simulation/gazebo/worlds",
    source_scenarios: Path = ROOT / "configs/scenarios/generated-v2",
    out_worlds: Path = ROOT / "simulation/gazebo/worlds",
    out_scenarios: Path = ROOT / "configs/scenarios/generated-v2-appearance",
    presets: tuple[str, ...] = ("morning_low_sun", "evening_low_sun", "overcast_flat", "dusk_dim", "haze", "noon_clear"),
) -> dict:
    source_index = json.loads((source_scenarios / "index.json").read_text(encoding="utf-8"))
    source_entries = [entry for entry in source_index["worlds"] if entry.get("split") == "test"]
    if len(source_entries) != 6:
        raise ValueError(f"expected six generated-v2 test worlds, found {len(source_entries)}")
    if not presets or any(p not in APPEARANCE_PRESETS for p in presets):
        raise ValueError(f"presets must be from {APPEARANCE_PRESETS}")
    out_worlds.mkdir(parents=True, exist_ok=True)
    out_scenarios.mkdir(parents=True, exist_ok=True)
    # The runtime channel points at builtin://maps and builtin://terrain.  Keep
    # those assets alongside the mirrored world when the source set has them;
    # temporary/unit-test source roots may intentionally omit them.
    source_resolved = source_worlds.resolve()
    output_resolved = out_worlds.resolve()
    source_root = source_resolved.parents[1] if source_resolved.parent.name == "gazebo" else source_resolved.parent
    output_root = output_resolved.parents[1] if output_resolved.parent.name == "gazebo" else output_resolved.parent
    for asset_dir, suffix in (("maps", ".geojson"), ("terrain", ".dem.json")):
        (output_root / asset_dir).mkdir(parents=True, exist_ok=True)
    entries = []
    for index, source_entry in enumerate(source_entries):
        source_id = str(source_entry["world_id"])
        world_id = f"gen_v2a_w{index:04d}"
        preset = presets[index % len(presets)]
        source_sdf = source_worlds / f"{source_id}.sdf"
        world_path = out_worlds / f"{world_id}.sdf"
        world_path.write_bytes(_appearance_sdf(source_sdf, source_id, world_id, preset, int(source_entry.get("seed", index))))
        channel_values = {}
        for suffix in ("runtime", "runtime_input", "truth", "expectations"):
            source_path = source_scenarios / f"{source_id}.{suffix}.json"
            if not source_path.is_file() and suffix == "runtime":
                continue
            value = _replace(json.loads(source_path.read_text(encoding="utf-8")), source_id, world_id)
            channel_values[suffix] = value
            _write_json(out_scenarios / f"{world_id}.{suffix}.json", value)
        for asset_dir, suffix in (("maps", ".geojson"), ("terrain", ".dem.json")):
            source_asset = source_root / asset_dir / f"{source_id}{suffix}"
            if source_asset.is_file():
                destination = output_root / asset_dir / f"{world_id}{suffix}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                raw = source_asset.read_bytes().replace(source_id.encode(), world_id.encode())
                destination.write_bytes(raw)
        scenario = {"schema_version": 1, "scenario_id": world_id, "scenario_version": 1, "channels": {
            "runtime_input": {"path": f"{world_id}.runtime_input.json", "sha256": canonical_sha256(channel_values["runtime_input"])},
            "truth": {"path": f"{world_id}.truth.json", "sha256": canonical_sha256(channel_values["truth"])},
            "expectations": {"path": f"{world_id}.expectations.json", "sha256": canonical_sha256(channel_values["expectations"])},
        }}
        scenario_hash = _write_json(out_scenarios / f"{world_id}.scenario.json", scenario)
        entries.append({
            "world_id": world_id,
            "source_world_id": source_id,
            "split": "test",
            "seed": source_entry.get("seed", index),
            "appearance": preset,
            "task_text": source_entry.get("task_text", ""),
            "task_text_en": source_entry.get("task_text_en", ""),
            "asset_mode": source_entry.get("asset_mode", "fuel_v1"),
            "target_combo": source_entry.get("target_combo"),
            "world_file": world_path.name,
            "true_target_id": str(source_entry.get("true_target_id", "")).replace(source_id, world_id),
            "files": {
                "world": {"path": world_path.name, "sha256": hashlib.sha256(world_path.read_bytes()).hexdigest()},
                "scenario": {"path": f"{world_id}.scenario.json", "sha256": scenario_hash},
                **{suffix: {"path": f"{world_id}.{suffix}.json", "sha256": hashlib.sha256((out_scenarios / f"{world_id}.{suffix}.json").read_bytes()).hexdigest()} for suffix in ("runtime", "runtime_input", "truth", "expectations") if suffix in channel_values},
            },
        })
    document = {"schema_version": 1, "generator": "scripts.generate_appearance_test_set", "asset_mode": "fuel_v1", "count": 6, "seed": source_index.get("seed"), "split_ratios": [0.0, 0.0, 1.0], "appearance_presets": list(presets), "worlds": entries}
    _write_json(out_scenarios / "index.json", document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-worlds", type=Path, default=ROOT / "simulation/gazebo/worlds")
    parser.add_argument("--source-scenarios", type=Path, default=ROOT / "configs/scenarios/generated-v2")
    parser.add_argument("--out-worlds", type=Path, default=ROOT / "simulation/gazebo/worlds")
    parser.add_argument("--out-scenarios", type=Path, default=ROOT / "configs/scenarios/generated-v2-appearance")
    parser.add_argument("--presets", nargs="+", choices=APPEARANCE_PRESETS, default=["morning_low_sun", "evening_low_sun", "overcast_flat", "dusk_dim", "haze", "noon_clear"])
    args = parser.parse_args(argv)
    document = generate_appearance_test_set(source_worlds=args.source_worlds, source_scenarios=args.source_scenarios, out_worlds=args.out_worlds, out_scenarios=args.out_scenarios, presets=tuple(args.presets))
    print(json.dumps({"count": document["count"], "out_scenarios": str(args.out_scenarios), "presets": document["appearance_presets"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
