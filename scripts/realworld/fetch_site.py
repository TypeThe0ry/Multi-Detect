#!/usr/bin/env python3
"""Download DEM (USGS 3DEP), aerial imagery (USDA NAIP) and OSM vectors for one real site.

Everything lands in <asset_root>/sites/<site>/raw with a provenance.json. Rasters use a
transverse Mercator centred on the site origin, so +x is east, +y is north and metres
match the Gazebo ENU frame directly.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path

import numpy as np
import requests
import tifffile
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SITES = ROOT / "configs/sim-assets/realworld-sites.json"
DEM_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
UA = {"User-Agent": "multidetect-sim-assets/1.0 (research simulation)"}
TILE_M = 1000
TILE_PX = 4096
DEM_RES_M = 2.0


def local_wkt(lat: float, lon: float) -> str:
    return (
        'PROJCS["local",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        f'PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",{lon}],'
        f'PARAMETER["Scale_Factor",1.0],PARAMETER["Latitude_Of_Origin",{lat}],UNIT["Meter",1.0]]'
    )


def proj4(lat: float, lon: float) -> str:
    return f"+proj=tmerc +lat_0={lat} +lon_0={lon} +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m"


def export(url: str, wkt: str, bbox, size, **extra) -> np.ndarray:
    sr = json.dumps({"wkt": wkt})
    params = dict(bbox=",".join(f"{v:.3f}" for v in bbox), bboxSR=sr, imageSR=sr,
                  size=f"{size[0]},{size[1]}", f="image", format="tiff", **extra)
    for attempt in range(30):
        try:
            r = requests.get(url, params=params, timeout=300, headers=UA)
            if r.status_code == 200 and "tif" in r.headers.get("content-type", ""):
                return tifffile.imread(io.BytesIO(r.content))
            print("retry", r.status_code, flush=True)
        except requests.RequestException as exc:
            print("retry", exc, flush=True)
        time.sleep(min(60, 3 + attempt * 4))
    raise RuntimeError(f"export failed {url} {bbox}")


def fetch_dem(out: Path, wkt: str, half: float) -> None:
    path = out / "dem.npy"
    if path.exists():
        return
    # 3DEP caps exports near 8000 px; 2 m over at most 4 km is well inside that.
    n = int(2 * half / DEM_RES_M) + 1
    pad = DEM_RES_M / 2
    dem = export(DEM_URL, wkt, (-half - pad, -half - pad, half + pad, half + pad), (n, n),
                 pixelType="F32", interpolation="RSP_BilinearInterpolation")
    np.save(path, np.flipud(dem.astype(np.float32)))  # row 0 = south


def fetch_naip(out: Path, lat: float, lon: float, half: float) -> None:
    """NAIP cloud-optimised GeoTIFFs from the Planetary Computer mirror, warped to the local frame.

    The USGS image server fails on most large requests; this mirror takes seconds per tile.
    Newest scenes win; older ones only fill gaps at scene edges.
    """
    import planetary_computer
    import pystac_client
    import rasterio
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    catalog = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=planetary_computer.sign_inplace)
    s, w, n_, e = bbox_ll(lat, lon, half)
    items = list(catalog.search(collections=["naip"], bbox=[w, s, e, n_], sortby=[{"field": "datetime", "direction": "desc"}], max_items=120).items())
    years = sorted({i.datetime.year for i in items}, reverse=True)
    print("naip scenes", len(items), "years", years, flush=True)
    crs = CRS.from_proj4(proj4(lat, lon))
    n = int(round(2 * half / TILE_M))
    used = set()
    for j in range(n):
        for i in range(n):
            path = out / f"naip_{i}_{j}.png"
            if path.exists():
                continue
            x0, y0 = -half + i * TILE_M, -half + j * TILE_M
            img = np.zeros((4, TILE_PX, TILE_PX), np.uint8)
            transform = from_bounds(x0, y0, x0 + TILE_M, y0 + TILE_M, TILE_PX, TILE_PX)
            for item in items:
                if (img[0] == 0).mean() < 0.0005:
                    break
                for attempt in range(6):
                    try:
                        with rasterio.open(item.assets["image"].href) as src, WarpedVRT(
                            src, crs=crs, resampling=Resampling.bilinear, transform=transform, width=TILE_PX, height=TILE_PX
                        ) as vrt:
                            part = vrt.read()
                        break
                    except rasterio.errors.RasterioIOError as exc:
                        print("retry", exc, flush=True)
                        time.sleep(5 * (attempt + 1))
                        if attempt == 4:
                            item = next(iter(catalog.search(ids=[item.id], collections=["naip"]).items()))
                else:
                    continue
                hole = (img[0] == 0) & (part[0] > 0)
                if hole.any():
                    used.add(item.id)
                    img[:, hole] = part[:, hole]
            Image.fromarray(np.moveaxis(img, 0, -1), "RGBA").save(path, compress_level=3)
            print("naip", i, j, f"empty={(img[0] == 0).mean():.4f}", flush=True)
    (out / "naip_scenes.json").write_text(json.dumps(sorted(used)))


def bbox_ll(lat: float, lon: float, half: float):
    from pyproj import Transformer
    t = Transformer.from_crs(proj4(lat, lon), "EPSG:4326", always_xy=True)
    xs, ys = t.transform([-half, half, -half, half], [-half, -half, half, half])
    return min(ys), min(xs), max(ys), max(xs)


def fetch_osm(out: Path, lat: float, lon: float, half: float) -> None:
    path = out / "osm.json"
    if path.exists():
        return
    s, w, n, e = bbox_ll(lat, lon, half)
    box = f"({s},{w},{n},{e})"
    query = f"""[out:json][timeout:600];(
      way["building"]{box}; relation["building"]{box}; way["building:part"]{box};
      way["highway"]{box}; way["landuse"]{box}; relation["landuse"]{box};
      way["natural"]{box}; relation["natural"]{box}; way["waterway"]{box};
      way["leisure"]{box}; way["amenity"]{box}; way["railway"]{box}; way["man_made"]{box};
      way["power"]{box}; way["barrier"]{box}; way["aeroway"]{box};
      node["natural"="tree"]{box}; node["amenity"]{box}; node["highway"]{box}; node["man_made"]{box};
      node["power"]{box}; node["barrier"]{box};
    );out body;>;out skel qt;"""
    for attempt in range(8):
        url = OVERPASS[attempt % 2]
        try:
            r = requests.post(url, data={"data": query}, timeout=700, headers=UA)
            if r.status_code == 200:
                path.write_bytes(r.content)
                print("osm", len(r.content), flush=True)
                return
            print("osm retry", url, r.status_code, flush=True)
        except requests.RequestException as exc:
            print("osm retry", exc, flush=True)
        time.sleep(15 * (attempt + 1))
    raise RuntimeError("overpass failed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sites", nargs="*")
    args = ap.parse_args()
    cfg = json.loads(SITES.read_text())
    for name in args.sites or cfg["sites"]:
        site = cfg["sites"][name]
        out = Path(cfg["asset_root"]) / "sites" / name / "raw"
        out.mkdir(parents=True, exist_ok=True)
        half = site["size_m"] / 2
        wkt = local_wkt(site["lat"], site["lon"])
        print("==", name, flush=True)
        fetch_dem(out, wkt, half)
        fetch_osm(out, site["lat"], site["lon"], half)
        fetch_naip(out, site["lat"], site["lon"], half)
        (out / "provenance.json").write_text(json.dumps({
            "site": name, "lat": site["lat"], "lon": site["lon"], "size_m": site["size_m"],
            "proj4": proj4(site["lat"], site["lon"]),
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sources": {
                "dem": {"name": "USGS 3DEP", "url": DEM_URL, "license": "Public domain (US Government work)"},
                "imagery": {"name": "USDA NAIP via Microsoft Planetary Computer", "url": "https://planetarycomputer.microsoft.com/dataset/naip",
                            "license": "Public domain (US Government work)"},
                "vectors": {"name": "OpenStreetMap via Overpass", "license": "ODbL-1.0",
                            "attribution": "(c) OpenStreetMap contributors"},
            },
        }, indent=2))


if __name__ == "__main__":
    main()
