"""Extract the PX4 x500_gimbal source closure from an exported image root."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multidetect.simulator.px4_image import PINNED_IMAGE

CONTRACT = "multidetect.px4-x500-gimbal-source-assets-v3"
FILES = {
    "x500_gimbal_model.sdf": "share/gz/models/x500_gimbal/model.sdf",
    "gimbal_model.sdf": "share/gz/models/gimbal/model.sdf",
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
    for name, source in sources.items():
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"source is not a regular image asset: {name}")
    x500_root = ET.parse(sources["x500_gimbal_model.sdf"]).getroot()
    gimbal_root = ET.parse(sources["gimbal_model.sdf"]).getroot()
    x500_model = x500_root.find("model")
    if x500_model is None or x500_model.attrib.get("name") != "x500_gimbal":
        raise ValueError("PX4 x500_gimbal source does not declare the x500_gimbal model")
    includes = {uri.text for uri in x500_model.findall("include/uri") if uri.text}
    if not {"model://x500", "model://gimbal"}.issubset(includes):
        raise ValueError("PX4 x500_gimbal model is missing its x500/gimbal component includes")
    gimbal_links = {item.attrib.get("name") for item in gimbal_root.findall(".//link")}
    if "camera_link" not in gimbal_links or "cgo3_mount_link" not in gimbal_links:
        raise ValueError("PX4 gimbal model is missing camera_link or cgo3_mount_link")
    startup = sources["4019_gz_x500_gimbal"].read_text(encoding="utf-8")
    if not re.search(r"(?m)^PX4_SIM_MODEL=\$\{PX4_SIM_MODEL:=x500_gimbal\}\s*$", startup):
        raise ValueError("PX4 4019 airframe does not default PX4_SIM_MODEL to x500_gimbal")
    if not re.search(r"(?m)^\. \$\{R\}etc/init\.d-posix/airframes/4001_gz_x500\s*$", startup):
        raise ValueError("PX4 4019 airframe does not source 4001_gz_x500")

    target.mkdir(parents=True)
    records: dict[str, object] = {}
    for name, source in sources.items():
        destination = target / name
        shutil.copyfile(source, destination)
        records[name] = {
            "image_path": "/opt/px4-gazebo/" + FILES[name],
            "sha256": _sha256(destination),
        }
    contract = {
        "schema_version": 1,
        "contract": CONTRACT,
        "image": image_ref,
        "image_revision": image_ref,
        "assets": records,
    }
    (target / "extraction.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default=PINNED_IMAGE)
    args = parser.parse_args(argv)
    contract = extract_assets(args.image_root, args.output, args.image)
    print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
