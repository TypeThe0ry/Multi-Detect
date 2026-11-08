#!/usr/bin/env python3
"""Sort the downloaded library into categories and write a licence-checked catalogue to the repo."""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = Path("/data/datasets/multidetect-sim-assets/library")
RULES = [
    ("vehicle", r"(^|_)(car|suv|sedan|hatchback|pickup|truck|van|bus|ambulance|fire_?truck|prius|tractor|trailer|forklift|motorcycle|bicycle)(_|$)|cart$"),
    ("person", r"(person|pedestrian|actor|casual|visitor|rescue_randy|nurse|doctor$|worker|scrubs)"),
    ("vegetation", r"(tree|pine|oak|palm|bush|shrub|grass|plant|fern)"),
    ("rock", r"(rock|boulder)"),
    ("building", r"(house|building|apartment|office_b|warehouse|school|church|hospital|gas_?station|depot|shed|garage|tower|cottage|barn|silo|hangar|law_office|thrift|fast_food|salon)"),
    ("street", r"(mailbox|postbox|post_box|hydrant|lamp|street_?light|traffic|stop_sign|sign$|cone|barrel|barrier|bench|dumpster|trash|litter|pole|fence|fountain|playground|gazebo|road)"),
    ("cargo", r"(container|pallet|crate|drum|cardboard|box$|rack)"),
    ("marine", r"(boat|ship|buoy)"),
]
# Scanned retail goods that only matched on a stray word in their name.
TOYS = re.compile(r"(marvel|avengers|lego|hasbro|perricone|shoe|sneaker|jansport|backpack_|cereal|bottle|mug|toy|nintendo|playmobil|school_bus_|fisher|sanitizer|cream|duo$)")


def main() -> None:
    rows = [json.loads(line) for line in (LIB / "manifest.jsonl").read_text().splitlines()]
    out = {"schema_version": 1, "asset_root": str(LIB), "items": []}
    for r in rows:
        cat = None
        if r["kind"] == "fuel_model":
            if TOYS.search(r["slug"]):
                continue
            cat = next((c for c, pat in RULES if re.search(pat, r["slug"])), None)
            if cat is None:
                continue
        elif r["kind"] == "polyhaven_model":
            cat = next((c for c, pat in RULES if re.search(pat, r["slug"])), "prop")
        else:
            cat = {"ambientcg_texture": "texture", "polyhaven_hdri": "hdri"}[r["kind"]]
        out["items"].append({"kind": r["kind"], "slug": r["slug"], "category": cat, "license": r.get("license"), "url": r.get("url"), "sha256": r.get("sha256"), "owner": r.get("owner")})
    print(collections.Counter(i["category"] for i in out["items"]))
    print(collections.Counter(i["license"] for i in out["items"]))
    (ROOT / "configs/sim-assets/realworld-library.json").write_text(json.dumps(out, ensure_ascii=False, indent=0) + "\n")


if __name__ == "__main__":
    main()
