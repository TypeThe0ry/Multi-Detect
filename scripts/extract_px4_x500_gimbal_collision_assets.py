"""Extract the pinned PX4 x500_gimbal collision/navigation SDF closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from multidetect.simulator.px4_image import PINNED_IMAGE

CONTRACT = "multidetect.px4-x500-gimbal-collision-source-assets-v1"
FILES = {
    "x500_gimbal_model.sdf": "share/gz/models/x500_gimbal/model.sdf",
    "gimbal_model.sdf": "share/gz/models/gimbal/model.sdf",
    "x500_model.sdf": "share/gz/models/x500/model.sdf",
    "x500_base_model.sdf": "share/gz/models/x500_base/model.sdf",
    "4019_gz_x500_gimbal": "etc/init.d-posix/airframes/4019_gz_x500_gimbal",
    "4001_gz_x500": "etc/init.d-posix/airframes/4001_gz_x500",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_assets(image_root: Path, output: Path, image_ref: str) -> dict[str, object]:
    if image_ref != PINNED_IMAGE:
        raise ValueError("source extraction must use the repository's exact pinned PX4 image ref")
    root = image_root.resolve(strict=True)
    target = output.resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite extraction output: {target}")
    sources = {name: (root / relative).resolve(strict=True) for name, relative in FILES.items()}
    if any(not path.is_relative_to(root) or not path.is_file() for path in sources.values()):
        raise ValueError("source closure contains a non-regular or out-of-root asset")
    models = {}
    for name in ("x500_gimbal_model.sdf", "gimbal_model.sdf", "x500_model.sdf", "x500_base_model.sdf"):
        sdf = ET.parse(sources[name]).getroot()
        model = sdf.find("model")
        expected = name.removesuffix("_model.sdf").replace("x500_base", "x500_base")
        if model is None or model.attrib.get("name") != expected:
            raise ValueError(f"source model identity differs for {name}")
        models[name] = model
    combined = models["x500_gimbal_model.sdf"]
    includes = {item.findtext("uri") for item in combined.findall("include")}
    if not {"model://x500", "model://gimbal"}.issubset(includes):
        raise ValueError("x500_gimbal composition is missing its x500/gimbal includes")
    gimbal = models["gimbal_model.sdf"]
    links = {item.attrib.get("name") for item in gimbal.findall("link")}
    if not {"cgo3_mount_link", "camera_link"}.issubset(links):
        raise ValueError("gimbal collision model is missing mount/camera links")
    xbase = models["x500_base_model.sdf"]
    base_links = {item.attrib.get("name") for item in xbase.findall("link")}
    if base_links != {"base_link", *(f"rotor_{i}" for i in range(4))}:
        raise ValueError("x500_base source must contain base_link and four rotor links")
    if sum(1 for _ in xbase.iter("collision")) != 9:
        raise ValueError("x500_base source collision closure differs from the reviewed nine")
    target.mkdir(parents=True)
    records = {}
    for name, source in sources.items():
        dest = target / name
        shutil.copyfile(source, dest)
        records[name] = {"image_path": "/opt/px4-gazebo/" + FILES[name], "sha256": _sha256(dest)}
    result = {
        "schema_version": 1,
        "contract": CONTRACT,
        "image": image_ref,
        "image_revision": image_ref,
        "assets": records,
    }
    (target / "extraction.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default=PINNED_IMAGE)
    args = parser.parse_args(argv)
    print(json.dumps(extract_assets(args.image_root, args.output, args.image), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
