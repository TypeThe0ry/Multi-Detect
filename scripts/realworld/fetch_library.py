#!/usr/bin/env python3
"""Build a pinned asset library: Fuel models, ambientCG/Poly Haven CC0 textures and HDRIs.

Each download gets a line in <asset_root>/library/manifest.jsonl with source URL, licence and sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path

import requests

ASSET_ROOT = Path("/data/datasets/multidetect-sim-assets")
LIB = ASSET_ROOT / "library"
UA = {"User-Agent": "multidetect-sim-assets/1.0 (research simulation)"}
FUEL = "https://fuel.gazebosim.org/1.0"
# Reusable licences only: CC0, CC BY, CC BY-SA, Apache-style. NC/ND are excluded.
FUEL_KEEP_LICENSE = re.compile(r"(Attribution 4\.0|Attribution-ShareAlike|CC0|Zero|Apache|MIT|Public Domain)", re.I)
FUEL_WORDS = re.compile(
    r"(car|suv|sedan|hatchback|pickup|truck|van\b|bus\b|ambulance|fire.?truck|prius|vehicle|tractor|trailer|forklift|motorcycle|bike|bicycle|"
    r"person|people|pedestrian|actor|casual|visitor|rescue|randy|nurse|doctor|worker|walking|standing|"
    r"tree|pine|oak|palm|bush|shrub|grass|plant|forest|rock|boulder|"
    r"house|building|apartment|office|warehouse|school|church|hospital|gas.?station|depot|shed|garage|tower|cottage|barn|silo|hangar|"
    r"mailbox|postbox|post.?box|hydrant|lamp|street.?light|traffic|stop.?sign|sign|cone|barrel|barrier|bench|dumpster|trash|bin\b|pole|fence|"
    r"container|pallet|crate|drum|helipad|runway|road|bridge|fountain|playground|gazebo|backpack|box|boat|ship|buoy|wind.?turbine|solar)",
    re.I,
)
FUEL_SKIP = re.compile(r"(robot|sensor|lidar|arm\b|gripper|cave|tunnel|subt|artifact|anymal|husky|marble|x\d+ config|explorer|uav|drone|gate|panda|yumi|fanuc|shelf|table|chair|cup|mug|toy|lego)", re.I)
TEXTURE_QUERIES = ["asphalt", "road", "concrete", "pavingstones", "grass", "ground", "gravel", "bricks", "facade", "roofingtiles", "corrugatedsteel", "planks", "rock", "metal", "leaf", "bark", "sand", "snow", "marble", "tiles"]
HDRI_WANT = 24


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record(**row) -> None:
    with (LIB / "manifest.jsonl").open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get(url: str, **kw) -> requests.Response:
    for attempt in range(5):
        try:
            r = requests.get(url, timeout=kw.pop("timeout", 300), headers=UA, **kw)
            if r.status_code in (200, 404):
                return r
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(url)


def fuel_catalog() -> list[dict]:
    path = LIB / "fuel" / "catalog_all.json"
    if path.exists():
        return json.loads(path.read_text())
    rows: list[dict] = []
    page = 1
    while True:
        r = get(f"{FUEL}/models", params={"per_page": 100, "page": page})
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        rows += batch
        page += 1
        if len(batch) < 100:
            break
    path.write_text(json.dumps(rows))
    return rows


def fetch_fuel(limit: int) -> None:
    (LIB / "fuel").mkdir(parents=True, exist_ok=True)
    rows = fuel_catalog()
    keep = []
    for m in rows:
        text = " ".join([m.get("name", ""), " ".join(m.get("tags") or [])])
        if not FUEL_KEEP_LICENSE.search(m.get("license_name", "") or ""):
            continue
        if not FUEL_WORDS.search(text) or FUEL_SKIP.search(text):
            continue
        if (m.get("filesize") or 0) > 120_000_000:
            continue
        keep.append(m)
    # Popular models first; OpenRobotics is the best-curated owner.
    keep.sort(key=lambda m: (m.get("owner") != "OpenRobotics", -(m.get("downloads") or 0)))
    seen = set()
    done = 0
    print("fuel candidates", len(rows), "->", len(keep), flush=True)
    for m in keep:
        slug = re.sub(r"[^a-z0-9]+", "_", m["name"].lower()).strip("_")
        if slug in seen:
            continue
        seen.add(slug)
        if done >= limit:
            break
        done += 1
        dest = LIB / "fuel" / "models" / slug
        if (dest / "model.sdf").exists():
            continue
        url = f"{FUEL}/{m['owner']}/models/{m['name']}.zip".replace(" ", "%20")
        r = get(url)
        if r.status_code != 200:
            print("miss", m["name"], flush=True)
            continue
        try:
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(dest)
        except zipfile.BadZipFile:
            print("badzip", m["name"], flush=True)
            continue
        record(kind="fuel_model", slug=slug, name=m["name"], owner=m["owner"], url=url, license=m.get("license_name"),
               sha256=sha(r.content), bytes=len(r.content), tags=m.get("tags"))
        print("fuel", done, slug, len(r.content), flush=True)


def fetch_ambientcg(per_query: int) -> None:
    out = LIB / "ambientcg"
    out.mkdir(parents=True, exist_ok=True)
    seen = set()
    for q in TEXTURE_QUERIES:
        r = get("https://ambientcg.com/api/v2/full_json", params={"q": q, "type": "Material", "limit": per_query, "sort": "Popular", "include": "downloadData"})
        if r.status_code != 200:
            print("acg miss", q, r.status_code, flush=True)
            continue
        for asset in r.json().get("foundAssets", []):
            aid = asset["assetId"]
            if aid in seen:
                continue
            seen.add(aid)
            dest = out / aid
            if dest.exists():
                continue
            dl = None
            for cat in (asset.get("downloadFolders", {}).get("default", {}).get("downloadFiletypeCategories", {}).get("zip", {}).get("downloads", []) or []):
                if cat.get("attribute") == "2K-JPG":
                    dl = cat
            if not dl:
                continue
            z = get(dl["fullDownloadPath"])
            if z.status_code != 200:
                continue
            zipfile.ZipFile(io.BytesIO(z.content)).extractall(dest)
            record(kind="ambientcg_texture", slug=aid, query=q, url=dl["fullDownloadPath"], license="CC0-1.0", sha256=sha(z.content), bytes=len(z.content))
            print("acg", aid, len(z.content), flush=True)


def fetch_polyhaven() -> None:
    out = LIB / "polyhaven"
    (out / "hdri").mkdir(parents=True, exist_ok=True)
    (out / "models").mkdir(parents=True, exist_ok=True)
    hdris = get("https://api.polyhaven.com/assets", params={"t": "hdris"}).json()
    # Outdoor skies across conditions, chosen by category so every weather preset has several.
    wanted: dict[str, list[str]] = {"skies": [], "sunrise-sunset": [], "overcast": [], "night": [], "midday": [], "morning-afternoon": []}
    for slug, meta in sorted(hdris.items(), key=lambda kv: -kv[1].get("download_count", 0)):
        cats = meta.get("categories", [])
        if "outdoor" not in cats and "skies" not in cats:
            continue
        for key in wanted:
            if key in cats and len(wanted[key]) < HDRI_WANT // len(wanted) + 2:
                wanted[key].append(slug)
    for key, slugs in wanted.items():
        for slug in slugs:
            dest = out / "hdri" / f"{slug}_4k.hdr"
            if dest.exists():
                continue
            files = get(f"https://api.polyhaven.com/files/{slug}").json()
            url = files["hdri"]["4k"]["hdr"]["url"]
            data = get(url).content
            dest.write_bytes(data)
            record(kind="polyhaven_hdri", slug=slug, category=key, url=url, license="CC0-1.0", sha256=sha(data), bytes=len(data))
            print("hdri", key, slug, len(data), flush=True)
    models = get("https://api.polyhaven.com/assets", params={"t": "models"}).json()
    word = re.compile(r"(tree|pine|bush|shrub|plant|rock|boulder|stone|bench|lamp|light|barrel|crate|cone|bollard|sign|fence|hydrant|trash|bin|dumpster|pallet|tire|wheel|container|grass|fern|stump|log|root)", re.I)
    count = 0
    for slug, meta in sorted(models.items(), key=lambda kv: -kv[1].get("download_count", 0)):
        if not word.search(slug + " " + " ".join(meta.get("tags", []))):
            continue
        dest = out / "models" / slug
        if (dest / "done").exists():
            continue
        files = get(f"https://api.polyhaven.com/files/{slug}").json()
        gltf = files.get("gltf", {}).get("1k", {}).get("gltf")
        if not gltf:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        data = get(gltf["url"]).content
        (dest / f"{slug}.gltf").write_bytes(data)
        for rel, inc in (gltf.get("include") or {}).items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(get(inc["url"]).content)
        (dest / "done").write_text("ok")
        record(kind="polyhaven_model", slug=slug, url=gltf["url"], license="CC0-1.0", sha256=sha(data), bytes=len(data), tags=meta.get("tags"))
        count += 1
        print("phmodel", slug, flush=True)
        if count >= 120:
            break


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuel", type=int, default=400)
    ap.add_argument("--acg", type=int, default=12)
    ap.add_argument("what", nargs="*", default=["fuel", "acg", "ph"])
    a = ap.parse_args()
    LIB.mkdir(parents=True, exist_ok=True)
    if "ph" in a.what:
        fetch_polyhaven()
    if "acg" in a.what:
        fetch_ambientcg(a.acg)
    if "fuel" in a.what:
        fetch_fuel(a.fuel)


if __name__ == "__main__":
    main()
