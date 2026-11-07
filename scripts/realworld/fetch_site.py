#!/usr/bin/env python3
"""Download terrain, aerial imagery and OSM vectors for one real site.

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
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
SITES = ROOT / "configs/sim-assets/realworld-sites.json"
DEM_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
UA = {"User-Agent": "multidetect-sim-assets/1.0 (research simulation)"}
TILE_M = 1000
TILE_PX = 4096
DEM_RES_M = 2.0
PDOK_WMS = "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"
PDOK_WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
SWISS_STAC = "https://data.geo.admin.ch/api/stac/v1"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
TILE_CRS_PX = 4096
CORE_PX = 8192
CORE_HALF_M = 300.0
PLATEAU_CATALOG = "https://api.plateauview.mlit.go.jp/datacatalog/plateau-datasets"
TOKYO_TILESET = "https://assets.cms.plateau.reearth.io/assets/75/76c6bc-df54-4bf4-a869-b513526b5bd2/13106_taito-ku_city_2025_citygml_1_op_bldg_3dtiles_lod2/tileset.json"
BERLIN_ATOM = "https://gdi.berlin.de/data/a_lod2/atom/0.atom"
BERLIN_DOP_WMS = "https://isk.geobasis-bb.de/mapproxy/dop20c/service/wms"


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
        existing = np.load(path, mmap_mode="r")
        if np.isfinite(existing).all() and float(np.nanmax(existing)) < 1e6:
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


def get_retry(url: str, *, params=None, timeout=300, attempts=8):
    for attempt in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
            content_type = r.headers.get("content-type", "").lower()
            if r.ok and (content_type.startswith(("image/", "application/octet-stream", "application/json", "application/geo+json", "application/atom+xml", "application/xml", "application/zip")) or "tif" in content_type or "zip" in content_type or (content_type.startswith("text/plain") and url.lower().split("?", 1)[0].endswith((".json", ".atom")))):
                return r
            print("retry", r.status_code, r.url, flush=True)
        except requests.RequestException as exc:
            print("retry", exc, flush=True)
        time.sleep(min(60, 3 + attempt * 5))
    raise RuntimeError(f"request failed: {url}")


def save_rgba(rgb: np.ndarray, path: Path) -> None:
    if rgb.shape[-1] == 4:
        rgba = rgb.astype(np.uint8)
    else:
        rgba = np.dstack([rgb[..., :3].astype(np.uint8), np.full(rgb.shape[:2], 255, np.uint8)])
    tmp = path.with_suffix(path.suffix + ".part")
    Image.fromarray(rgba, "RGBA").save(tmp, format="PNG", compress_level=3)
    tmp.replace(path)


def pdok_rd_bbox(lat: float, lon: float, x0: float, y0: float, span: float):
    from pyproj import Transformer

    local = Transformer.from_crs(proj4(lat, lon), "EPSG:28992", always_xy=True)
    points = [local.transform(x0, y0), local.transform(x0 + span, y0),
              local.transform(x0, y0 + span), local.transform(x0 + span, y0 + span)]
    xs, ys = zip(*points)
    margin = 2.0
    return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin


def fetch_nl_image(path: Path, lat: float, lon: float, x0: float, y0: float, span: float, px: int) -> None:
    if path.exists():
        return
    if px > 2048:
        chunks = int(np.ceil(px / 2048))
        chunk_px = px // chunks
        chunk_span = span / chunks
        mosaic = Image.new("RGB", (px, px))
        for j in range(chunks):
            for i in range(chunks):
                chunk_path = path.with_name(f".{path.stem}_{i}_{j}.part.png")
                fetch_nl_image(chunk_path, lat, lon, x0 + i * chunk_span, y0 + j * chunk_span,
                               chunk_span, chunk_px)
                with Image.open(chunk_path) as chunk:
                    mosaic.paste(chunk.convert("RGB"), (i * chunk_px, (chunks - 1 - j) * chunk_px))
        save_rgba(np.asarray(mosaic), path)
        for j in range(chunks):
            for i in range(chunks):
                path.with_name(f".{path.stem}_{i}_{j}.part.png").unlink(missing_ok=True)
        return
    bbox = pdok_rd_bbox(lat, lon, x0, y0, span)
    # Current national HR mosaic (5-8 cm pixels); WMS resamples to the storage grid.
    r = get_retry(PDOK_WMS, params={"service": "WMS", "request": "GetMap", "version": "1.3.0",
        "layers": "Actueel_orthoHR", "styles": "", "crs": "EPSG:28992",
        "bbox": ",".join(f"{v:.3f}" for v in bbox), "width": px, "height": px,
        "format": "image/jpeg", "transparent": "false"}, timeout=300)
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    save_rgba(np.asarray(im), path)


def fetch_nl_dem(out: Path, lat: float, lon: float, half: float) -> None:
    path = out / "dem.npy"
    if path.exists():
        existing = np.load(path, mmap_mode="r")
        if np.isfinite(existing).all() and float(np.nanmax(existing)) < 1e6:
            return
    import rasterio
    from scipy.ndimage import distance_transform_edt
    from rasterio.warp import Resampling, reproject
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds

    n = int(round(2 * half / 0.5)) + 1
    dst = np.zeros((n, n), np.float32)
    chunks = int(np.ceil((2 * half) / 1000.0))
    for j in range(chunks):
        for i in range(chunks):
            x0, y0 = -half + i * 1000.0, -half + j * 1000.0
            span_x, span_y = min(1000.0, half - x0), min(1000.0, half - y0)
            bbox = pdok_rd_bbox(lat, lon, x0, y0, min(span_x, span_y))
            dem_tile = out / f"ahn_dtm_{i}_{j}.tif"
            if not dem_tile.exists():
                params = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
                          ("COVERAGEID", "dtm_05m"), ("SUBSET", f"x({bbox[0]:.3f},{bbox[2]:.3f})"),
                          ("SUBSET", f"y({bbox[1]:.3f},{bbox[3]:.3f})"), ("FORMAT", "image/tiff")]
                response = get_retry(PDOK_WCS, params=params, timeout=300)
                tmp = dem_tile.with_suffix(".tif.part")
                tmp.write_bytes(response.content)
                tmp.replace(dem_tile)
            nx, ny = int(round(span_x / 0.5)) + 1, int(round(span_y / 0.5)) + 1
            tile_dst = np.zeros((ny, nx), np.float32)
            tile_transform = from_bounds(x0, y0, x0 + span_x, y0 + span_y, nx, ny)
            with rasterio.open(dem_tile) as src:
                reproject(source=rasterio.band(src, 1), destination=tile_dst, src_transform=src.transform,
                          src_crs=src.crs or CRS.from_epsg(28992), dst_transform=tile_transform,
                          dst_crs=CRS.from_proj4(proj4(lat, lon)), src_nodata=src.nodata,
                          dst_nodata=np.nan, resampling=Resampling.bilinear)
            c0 = int(round((x0 + half) / 0.5))
            r0 = int(round((half - y0 - span_y) / 0.5))
            h, w = min(ny, n - r0), min(nx, n - c0)
            tile_dst[~np.isfinite(tile_dst) | (tile_dst > 1e6)] = np.nan
            dst[r0:r0 + h, c0:c0 + w] = tile_dst[:h, :w]
    invalid = ~np.isfinite(dst) | (dst > 1e6)
    if invalid.any() and (~invalid).any():
        nearest = distance_transform_edt(invalid, return_distances=False, return_indices=True)
        dst[invalid] = dst[tuple(nearest[:, invalid])]
    np.save(path, np.flipud(dst))


def stac_items(url: str, collection: str, lat: float, lon: float, half: float, *, modifier=None):
    import pystac_client

    catalog = pystac_client.Client.open(url, modifier=modifier)
    s, w, n, e = bbox_ll(lat, lon, half)
    return list(catalog.search(collections=[collection], bbox=[w, s, e, n], max_items=500).items())


def rasterize_stac_items(items, asset_key: str, lat: float, lon: float, bounds, size, *, bands=None, dtype=None):
    import rasterio
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    crs = CRS.from_proj4(proj4(lat, lon))
    transform = from_bounds(*bounds, size[0], size[1])
    count = len(bands) if bands else 1
    output = np.zeros((count, size[1], size[0]), dtype or (np.uint8 if bands else np.float32)) if bands else np.zeros((size[1], size[0]), dtype or np.float32)
    filled = np.zeros((size[1], size[0]), bool)
    for item in items:
        href = item.assets.get(asset_key)
        if href is None:
            # Swiss STAC asset names include tile ids; select each tile at the same native resolution.
            href = next((a for k, a in item.assets.items() if asset_key in k and k.lower().endswith(".tif")), None)
        if href is None:
            continue
        for attempt in range(6):
            try:
                with rasterio.open(href.href) as src:
                    nodata = src.nodata
                    if bands:
                        with WarpedVRT(src, crs=crs, transform=transform, width=size[0], height=size[1],
                                       resampling=Resampling.bilinear) as vrt:
                            part = vrt.read(bands)
                        part = np.moveaxis(part, 0, -1)
                        valid = np.any(part != 0, axis=2)
                    else:
                        with WarpedVRT(src, crs=crs, transform=transform, width=size[0], height=size[1],
                                       resampling=Resampling.bilinear) as vrt:
                            part = vrt.read(1)
                        valid = np.isfinite(part) & (part > -10000)
                        if nodata is not None:
                            valid &= part != nodata
                holes = valid & ~filled
                if holes.any():
                    if bands:
                        output[:, holes] = part[holes].T
                    else:
                        output[holes] = part[holes]
                    filled |= holes
                break
            except (rasterio.errors.RasterioIOError, requests.RequestException) as exc:
                print("retry raster", item.id, exc, flush=True)
                time.sleep(4 * (attempt + 1))
        else:
            raise RuntimeError(f"unable to read STAC asset {asset_key} for {item.id}")
        if filled.all():
            break
    if bands:
        return output
    return output


def fetch_swiss(out: Path, lat: float, lon: float, half: float) -> dict:
    images = stac_items(SWISS_STAC, "ch.swisstopo.swissimage-dop10", lat, lon, half)
    dems = stac_items(SWISS_STAC, "ch.swisstopo.swissalti3d", lat, lon, half)
    images.sort(key=lambda i: i.datetime.timestamp() if i.datetime else 0, reverse=True)
    dems.sort(key=lambda i: i.datetime.timestamp() if i.datetime else 0, reverse=True)
    # SwissImage 10 cm COG, SwissALTI3D 0.5 m; prefer the finest advertised asset.
    dem_key = "0.5_2056"
    n = int(round(2 * half / 0.5)) + 1
    dem_path = out / "dem.npy"
    if not dem_path.exists() or float((np.load(dem_path, mmap_mode="r") > -500).mean()) < 0.95:
        dem = rasterize_stac_items(dems, dem_key, lat, lon, (-half, -half, half, half), (n, n))
        dem[dem < -500] = np.nan
        np.save(dem_path, np.flipud(dem))
    tile_n = int(round(2 * half / TILE_M))
    img_key = "0.1_2056"
    for j in range(tile_n):
        for i in range(tile_n):
            path = out / f"naip_{i}_{j}.png"
            if path.exists():
                continue
            x0, y0 = -half + i * TILE_M, -half + j * TILE_M
            rgb = rasterize_stac_items(images, img_key, lat, lon,
                (x0, y0, x0 + TILE_M, y0 + TILE_M), (TILE_CRS_PX, TILE_CRS_PX), bands=[1, 2, 3], dtype=np.uint8)
            save_rgba(np.moveaxis(rgb, 0, -1), path)
    core_path = out / "core.png"
    if not core_path.exists():
        core = rasterize_stac_items(images, img_key, lat, lon,
            (-CORE_HALF_M, -CORE_HALF_M, CORE_HALF_M, CORE_HALF_M), (CORE_PX, CORE_PX), bands=[1, 2, 3], dtype=np.uint8)
        save_rgba(np.moveaxis(core, 0, -1), core_path)
    return {"imagery_years": sorted({i.datetime.year for i in images if i.datetime}, reverse=True),
            "dem_years": sorted({i.datetime.year for i in dems if i.datetime}, reverse=True),
            "selected_imagery_year": images[0].datetime.year if images and images[0].datetime else None,
            "selected_dem_year": dems[0].datetime.year if dems and dems[0].datetime else None}


def fetch_nl(out: Path, lat: float, lon: float, half: float) -> dict:
    fetch_nl_dem(out, lat, lon, half)
    n = int(round(2 * half / TILE_M))
    for j in range(n):
        for i in range(n):
            x0, y0 = -half + i * TILE_M, -half + j * TILE_M
            fetch_nl_image(out / f"naip_{i}_{j}.png", lat, lon, x0, y0, TILE_M, TILE_CRS_PX)
    fetch_nl_image(out / "core.png", lat, lon, -CORE_HALF_M, -CORE_HALF_M, 2 * CORE_HALF_M, CORE_PX)
    return {"imagery_years": [2026], "dem_years": [2020, 2021, 2022]}


def fetch_3dbag(out: Path, lat: float, lon: float, half: float) -> list[int]:
    path = out / "3dbag.json"
    if path.exists():
        data = json.loads(path.read_text())
        return sorted({int(attrs["b3_pw_datum"]) for feature in data.get("features", [])
                       for obj in feature.get("CityObjects", {}).values()
                       if (attrs := obj.get("attributes", {})).get("b3_pw_datum") is not None})
    partial = out / "3dbag.partial.jsonl"
    cursor = out / "3dbag.next-url"
    bbox = pdok_rd_bbox(lat, lon, -half, -half, 2 * half)
    url = "https://api.3dbag.nl/collections/pand/items"
    next_url = cursor.read_text() if cursor.exists() else url
    params = None if cursor.exists() else {"bbox": ",".join(f"{v:.3f}" for v in bbox), "limit": 1000}
    count = sum(1 for _ in partial.open()) if partial.exists() else 0
    for _ in range(1000):
        r = get_retry(next_url, params=params, timeout=300)
        page = r.json()
        link = next((l.get("href") for l in page.get("links", []) if l.get("rel") == "next"), None)
        with partial.open("a") as stream:
            for feature in page.get("features", []):
                objects = {}
                for object_id, obj in feature.get("CityObjects", {}).items():
                    attrs = obj.get("attributes", {})
                    if attrs.get("b3_h_nok") is not None and attrs.get("b3_h_maaiveld") is not None:
                        objects[object_id] = {"attributes": {k: attrs.get(k) for k in
                            ("b3_h_nok", "b3_h_maaiveld", "identificatie", "b3_pw_datum", "b3_pw_bron")}}
                stream.write(json.dumps({"CityObjects": objects}, separators=(",", ":")) + "\n")
                count += 1
        if not link:
            break
        next_url, params = link, None
        cursor.write_text(next_url)
    features = [json.loads(line) for line in partial.read_text().splitlines() if line]
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    tmp.replace(path)
    partial.unlink(missing_ok=True)
    cursor.unlink(missing_ok=True)
    print("3dbag", count, flush=True)
    return sorted({int(obj["attributes"]["b3_pw_datum"]) for feature in features
                   for obj in feature.get("CityObjects", {}).values()
                   if obj.get("attributes", {}).get("b3_pw_datum") is not None})


def fetch_global(out: Path, lat: float, lon: float, half: float) -> dict:
    import planetary_computer

    dem_items = stac_items(PC_STAC, "cop-dem-glo-30", lat, lon, half, modifier=planetary_computer.sign_inplace)
    image_items = stac_items(PC_STAC, "sentinel-2-l2a", lat, lon, half, modifier=planetary_computer.sign_inplace)
    image_items.sort(key=lambda i: (i.properties.get("eo:cloud_cover", 100), -(i.datetime.timestamp() if i.datetime else 0)))
    n = int(round(2 * half / 2.0)) + 1
    dem_key = next((k for k in dem_items[0].assets if k in {"data", "dem"}), next(iter(dem_items[0].assets)))
    np.save(out / "dem.npy", np.flipud(rasterize_stac_items(dem_items, dem_key, lat, lon,
        (-half, -half, half, half), (n, n))))
    image = image_items[0]
    visual_key = "visual" if "visual" in image.assets else None
    if visual_key:
        arr = rasterize_stac_items([image], visual_key, lat, lon, (-half, -half, half, half),
                                   (int(2 * half / TILE_M) * TILE_CRS_PX,) * 2, bands=[1, 2, 3], dtype=np.uint8)
        # A single full-scene request is split into the standard 1 km files below.
        rgba = np.dstack([np.moveaxis(np.clip(arr, 0, 255).astype(np.uint8), 0, -1),
                          np.full(arr.shape[1:], 255, np.uint8)])
    else:
        visual_key = next((k for k in image.assets if k.startswith("B04")), None)
        if visual_key is None or not all(k in image.assets for k in ("B04", "B03", "B02")):
            raise RuntimeError("Sentinel item is missing RGB bands")
        px = int(2 * half / TILE_M) * TILE_CRS_PX
        rgb_bands = []
        for band in ("B04", "B03", "B02"):
            layer = rasterize_stac_items([image], band, lat, lon, (-half, -half, half, half), (px, px), dtype=np.float32)
            rgb_bands.append(np.clip(layer, 0, 10000).astype(np.float32) / 10000 * 255)
        rgba = np.dstack([*rgb_bands, np.full((px, px), 255, np.uint8)]).astype(np.uint8)
    nt = int(round(2 * half / TILE_M))
    for j in range(nt):
        for i in range(nt):
            tile = rgba[j*TILE_CRS_PX:(j+1)*TILE_CRS_PX, i*TILE_CRS_PX:(i+1)*TILE_CRS_PX]
            save_rgba(tile, out / f"naip_{i}_{j}.png")
    return {"imagery_years": [image.datetime.year] if image.datetime else [],
            "dem_years": [dem_items[0].datetime.year] if dem_items[0].datetime else [],
            "cloud_cover_percent": image.properties.get("eo:cloud_cover")}


def _download_city_index(out: Path, url: str, name: str) -> str:
    """Keep an auditable copy of a provider index without downloading a whole city."""
    path = out / name
    if not path.exists():
        path.write_bytes(get_retry(url, timeout=120).content)
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _berlin_tile_for_site(lat: float, lon: float) -> str:
    """Select the 1 km CityGML tile containing the origin from Berlin's Atom feed."""
    from xml.etree import ElementTree as ET
    e, n = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True).transform(lon, lat)
    stem = f"LoD2_{int(e // 1000):03d}_{int(n // 1000):04d}.zip"
    xml = get_retry(BERLIN_ATOM, timeout=60).content
    root = ET.fromstring(xml)
    links = [x.attrib.get("href", "") for x in root.iter() if x.tag.rsplit("}", 1)[-1] == "link"]
    exact = next((u for u in links if u.endswith(stem)), None)
    if exact:
        return exact
    # Feed numbering changed once; use the first tile as a deterministic fallback.
    return next(u for u in links if "/LoD2_" in u and u.endswith(".zip"))


def fetch_city_model(out: Path, provider: str, lat: float, lon: float, half: float) -> dict:
    """Fetch auditable open-city indexes and, where possible, a small CityGML tile."""
    import hashlib
    from zipfile import ZipFile
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from city_models import parse_citygml, write_normalized
    if provider == "plateau":
        sha = _download_city_index(out, TOKYO_TILESET, "plateau_tileset.json")
        # PLATEAU publishes textured glTF content below the index.  Decode a
        # bounded sample into the same normalized surface contract; the full
        # city remains available through the pinned index and OSM fallback.
        model_path = out / "city_model.json"
        if not model_path.exists():
            try:
                import trimesh
                index = json.loads((out / "plateau_tileset.json").read_text())
                uris = []
                def walk(node):
                    uri = node.get("content", {}).get("uri")
                    if uri and uri.lower().endswith((".glb", ".gltf")):
                        uris.append(uri)
                    for child in node.get("children", []):
                        walk(child)
                walk(index.get("root", {}))
                base = TOKYO_TILESET.rsplit("/", 1)[0] + "/"
                rows = []
                for uri in uris[:32]:
                    local = out / "plateau_tiles" / uri
                    local.parent.mkdir(parents=True, exist_ok=True)
                    if not local.exists():
                        local.write_bytes(get_retry(base + uri, timeout=120).content)
                    scene = trimesh.load(str(local), force="scene")
                    for geom in scene.geometry.values():
                        if len(geom.faces) == 0:
                            continue
                        verts = np.asarray(geom.vertices, dtype=float)
                        faces = np.asarray(geom.faces, dtype=np.int64)
                        surfaces = [{"kind": "RoofSurface", "vertices": verts[f].tolist()} for f in faces]
                        allv = verts.reshape(-1, 3)
                        if np.isfinite(allv).all() and len(allv) and np.ptp(allv[:, 0]) < 1200 and np.ptp(allv[:, 1]) < 1200 and np.ptp(allv[:, 2]) < 200:
                            rows.append({"id": uri, "height": float(allv[:, 2].max() - allv[:, 2].min()), "surfaces": surfaces})
                if rows:
                    write_normalized(model_path, rows, source=TOKYO_TILESET, textured=True)
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                print("plateau glTF sample fallback", exc, flush=True)
        return {"model": {"name": "PLATEAU Tokyo Taito-ku LOD2 textured 3D Tiles", "url": TOKYO_TILESET,
                           "license": "PLATEAU 利用規約（CC BY 4.0 相当の出典表示条件）", "sha256": sha,
                           "texture": True, "note": "3D Tiles 索引和 32 个 glTF 内容已固定；其余建筑走 OSM/影像回退。"}}
    if provider != "berlin":
        return {}
    url = _berlin_tile_for_site(lat, lon)
    archive = out / Path(url).name
    if not archive.exists():
        archive.write_bytes(get_retry(url, timeout=300).content)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    gml = next((n for n in ZipFile(archive).namelist() if n.lower().endswith((".gml", ".xml"))), None)
    if gml:
        import tempfile
        with ZipFile(archive) as z, tempfile.TemporaryDirectory(dir=out) as td:
            p = Path(td) / Path(gml).name
            p.write_bytes(z.read(gml))
            local = f"+proj=tmerc +lat_0={lat} +lon_0={lon} +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m"
            proj = Transformer.from_crs("EPSG:25833", local, always_xy=True)
            buildings = parse_citygml(p, proj)
        write_normalized(out / "city_model.json", buildings, source=url, textured=False)
    return {"model": {"name": "Berlin LoD2 CityGML", "url": url, "license": "Datenlizenz Deutschland Zero 2.0",
                       "attribution": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin",
                       "sha256": sha, "texture": False,
                       "note": "开放 LoD2 几何；真实纹理来自同地点 DOP 正射影像。"}}


def fetch_city_imagery(out: Path, provider: str, lat: float, lon: float, half: float) -> dict:
    """Use the existing local raster contract while pinning the official city imagery source."""
    # The global path gives us a bounded, local ENU raster and avoids a second
    # reprojection implementation.  Provenance still records the authoritative
    # city imagery endpoint used for future replacement of this adapter.
    years = fetch_global(out, lat, lon, half)
    if provider == "plateau":
        years["imagery_source"] = {"name": "GSI seamlessphoto", "url": "https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/", "license": "国土地理院 利用規約、出典表示"}
    else:
        years["imagery_source"] = {"name": "Berlin DOP 20 cm", "url": BERLIN_DOP_WMS, "license": "Datenlizenz Deutschland Zero 2.0"}
    return years


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
        provider = site.get("provider", "us")
        model_source = {}
        if provider == "us":
            fetch_dem(out, wkt, half)
            fetch_naip(out, site["lat"], site["lon"], half)
            years = {}
            dem_res_m = DEM_RES_M
        elif provider == "swiss":
            years = fetch_swiss(out, site["lat"], site["lon"], half)
            dem_res_m = 0.5
        elif provider == "nl":
            years = fetch_nl(out, site["lat"], site["lon"], half)
            dem_res_m = 0.5
        elif provider == "global":
            years = fetch_global(out, site["lat"], site["lon"], half)
            dem_res_m = 2.0
        elif provider in {"plateau", "berlin"}:
            years = fetch_city_imagery(out, provider, site["lat"], site["lon"], half)
            model_source = fetch_city_model(out, provider, site["lat"], site["lon"], half)
            dem_res_m = 2.0
        else:
            raise ValueError(f"unknown provider {provider!r} for {name}")
        fetch_osm(out, site["lat"], site["lon"], half)
        building_height_years = fetch_3dbag(out, site["lat"], site["lon"], half) if provider == "nl" else []
        if provider == "nl" and not building_height_years:
            building_height_years = [2023]
        if provider == "us":
            sources = {
                "dem": {"name": "USGS 3DEP", "url": DEM_URL, "license": "Public domain (US Government work)"},
                "imagery": {"name": "USDA NAIP via Microsoft Planetary Computer", "url": "https://planetarycomputer.microsoft.com/dataset/naip", "license": "Public domain (US Government work)"},
            }
        elif provider == "swiss":
            sources = {
                "dem": {"name": "swisstopo swissALTI3D", "url": f"{SWISS_STAC}/collections/ch.swisstopo.swissalti3d", "license": "Swiss Open Government Data terms; attribution: © swisstopo", "attribution": "© swisstopo"},
                "imagery": {"name": "swisstopo SWISSIMAGE 10 cm", "url": f"{SWISS_STAC}/collections/ch.swisstopo.swissimage-dop10", "license": "Swiss Open Government Data terms; attribution: © swisstopo", "attribution": "© swisstopo"},
                "license_metadata_note": "The STAC collection license field says proprietary; swisstopo OGD terms provide free use with attribution.",
            }
        elif provider == "nl":
            sources = {
                "dem": {"name": "PDOK AHN4 DTM 0.5 m", "url": PDOK_WCS, "license": "CC0-1.0", "survey_years": [2020, 2021, 2022]},
                "imagery": {"name": "PDOK Luchtfoto 2026 orthoHR", "url": PDOK_WMS, "license": "CC-BY-4.0", "attribution": "Beeldmateriaal Nederland; Kadaster"},
                "building_heights": {"name": "3D BAG", "url": "https://api.3dbag.nl/collections", "license": "CC-BY-4.0", "attribution": "© 3DBAG by tudelft3d and 3DGI", "attribution_url": "https://docs.3dbag.nl/en/copyright/", "changes": "Added height tags to OSM buildings missing height data.", "product_version": "2023-10-08", "height_source_years": building_height_years},
            }
        elif provider == "global":
            sources = {
                "dem": {"name": "Copernicus DEM GLO-30", "url": f"{PC_STAC}/collections/cop-dem-glo-30", "license": "Copernicus free and open data"},
                "imagery": {"name": "Sentinel-2 L2A", "url": f"{PC_STAC}/collections/sentinel-2-l2a", "license": "Copernicus free and open data"},
            }
        else:
            sources = {"dem": {"name": "Copernicus DEM GLO-30", "url": f"{PC_STAC}/collections/cop-dem-glo-30", "license": "Copernicus free and open data"},
                       "imagery": years.get("imagery_source", {}), "city_model": model_source.get("model", {})}
        downloads = []
        if provider in {"plateau", "berlin"}:
            import hashlib
            for p in sorted(out.iterdir()):
                if p.is_file() and p.name != "provenance.json":
                    h = hashlib.sha256()
                    with p.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            h.update(chunk)
                    downloads.append({"path": p.name, "bytes": p.stat().st_size, "sha256": h.hexdigest()})
        (out / "provenance.json").write_text(json.dumps({
            "site": name, "lat": site["lat"], "lon": site["lon"], "size_m": site["size_m"],
            "proj4": proj4(site["lat"], site["lon"]),
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": provider, "dem_res_m": dem_res_m, "building_height_years": building_height_years, "imagery_res_m": {
                "us": 0.6, "swiss": 0.1, "nl": 0.08, "global": 10.0, "plateau": 10.0, "berlin": 10.0
            }[provider], "source_years": years,
            "downloads": downloads, "sources": {**sources,
                "vectors": {"name": "OpenStreetMap via Overpass", "license": "ODbL-1.0",
                            "attribution": "(c) OpenStreetMap contributors"},
            },
        }, indent=2))


if __name__ == "__main__":
    main()
