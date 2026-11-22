#!/usr/bin/env python3
"""Download terrain, aerial imagery and OSM vectors for one real site.

Everything lands in <asset_root>/sites/<site>/raw with a provenance.json. Rasters use a
transverse Mercator centred on the site origin, so +x is east, +y is north and metres
match the Gazebo ENU frame directly.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import numpy as np
import requests
from PIL import Image

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
FETCH_LOCK_ENV = "MD_X1_FETCH_LOCK"
X1_EXTENT_M = 6000
SENTINEL_PROVIDERS = {"global", "berlin", "plateau"}
BAG_MAX_PAGES = 20_000


def _checkpoint() -> None:
    lock = os.environ.get(FETCH_LOCK_ENV)
    if lock:
        from scripts.realworld.x1_lock import flight_requested

        if flight_requested(Path(lock)):
            raise SystemExit(75)


def _retry_sleep(seconds: float) -> None:
    if not os.environ.get(FETCH_LOCK_ENV):
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        _checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _iter_stac_items(search):
    if not os.environ.get(FETCH_LOCK_ENV):
        yield from search.items()
        return
    # Advancing the page iterator can issue another request. Some clients
    # expose only items(), in which case check between those items instead.
    pages = getattr(search, "pages", None)
    _checkpoint()
    if callable(pages):
        for page in pages():
            _checkpoint()
            yield from page
            _checkpoint()
    else:
        for item in search.items():
            _checkpoint()
            yield item
            _checkpoint()
    _checkpoint()


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
    import tifffile

    sr = json.dumps({"wkt": wkt})
    params = dict(bbox=",".join(f"{v:.3f}" for v in bbox), bboxSR=sr, imageSR=sr,
                  size=f"{size[0]},{size[1]}", f="image", format="tiff", **extra)
    for attempt in range(30):
        _checkpoint()
        try:
            r = requests.get(url, params=params, timeout=300, headers=UA)
            if r.status_code == 200 and "tif" in r.headers.get("content-type", ""):
                _checkpoint()
                dem = tifffile.imread(io.BytesIO(r.content))
                _checkpoint()
                return dem
            print("retry", r.status_code, flush=True)
        except requests.RequestException as exc:
            print("retry", exc, flush=True)
        _retry_sleep(min(60, 3 + attempt * 4))
    raise RuntimeError(f"export failed {url} {bbox}")


def publish_bytes(path: Path, data: bytes) -> None:
    _checkpoint()
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    _checkpoint()


def save_npy(path: Path, data: np.ndarray) -> None:
    _checkpoint()
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("wb") as stream:
        np.save(stream, data)
    tmp.replace(path)
    _checkpoint()


def sha256_file(path: Path) -> str:
    _checkpoint()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            _checkpoint()
    return digest.hexdigest()


def fetch_dem(out: Path, wkt: str, half: float) -> None:
    _checkpoint()
    path = out / "dem.npy"
    if path.exists():
        existing = np.load(path, mmap_mode="r")
        if np.isfinite(existing).all() and float(np.nanmax(existing)) < 1e6:
            _checkpoint()
            return
    # 3DEP caps exports near 8000 px; 2 m over at most 4 km is well inside that.
    n = int(2 * half / DEM_RES_M) + 1
    pad = DEM_RES_M / 2
    dem = export(DEM_URL, wkt, (-half - pad, -half - pad, half + pad, half + pad), (n, n),
                 pixelType="F32", interpolation="RSP_BilinearInterpolation")
    save_npy(path, np.flipud(dem.astype(np.float32)))  # row 0 = south


def fetch_naip(out: Path, lat: float, lon: float, half: float) -> None:
    """NAIP cloud-optimised GeoTIFFs from the Planetary Computer mirror, warped to the local frame.

    The USGS image server fails on most large requests; this mirror takes seconds per tile.
    Newest scenes win; older ones only fill gaps at scene edges.
    """
    _checkpoint()
    import planetary_computer
    import pystac_client
    import rasterio
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    _checkpoint()
    catalog = pystac_client.Client.open(PC_STAC, modifier=planetary_computer.sign_inplace)
    _checkpoint()
    s, w, n_, e = bbox_ll(lat, lon, half)
    items = list(_iter_stac_items(catalog.search(collections=["naip"], bbox=[w, s, e, n_],
        sortby=[{"field": "datetime", "direction": "desc"}], max_items=120)))
    years = sorted({i.datetime.year for i in items}, reverse=True)
    print("naip scenes", len(items), "years", years, flush=True)
    crs = CRS.from_proj4(proj4(lat, lon))
    n = int(round(2 * half / TILE_M))
    used = set()
    for j in range(n):
        for i in range(n):
            _checkpoint()
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
                    _checkpoint()
                    try:
                        with rasterio.open(item.assets["image"].href) as src, WarpedVRT(
                            src, crs=crs, resampling=Resampling.bilinear, transform=transform, width=TILE_PX, height=TILE_PX
                        ) as vrt:
                            part = vrt.read()
                        _checkpoint()
                        break
                    except rasterio.errors.RasterioIOError as exc:
                        print("retry", exc, flush=True)
                        _retry_sleep(5 * (attempt + 1))
                        if attempt == 4:
                            item = next(_iter_stac_items(catalog.search(ids=[item.id], collections=["naip"])))
                else:
                    continue
                hole = (img[0] == 0) & (part[0] > 0)
                if hole.any():
                    used.add(item.id)
                    img[:, hole] = part[:, hole]
            save_rgba(np.moveaxis(img, 0, -1), path)
            print("naip", i, j, f"empty={(img[0] == 0).mean():.4f}", flush=True)
    publish_bytes(out / "naip_scenes.json", json.dumps(sorted(used)).encode())


def bbox_ll(lat: float, lon: float, half: float):
    from pyproj import Transformer
    t = Transformer.from_crs(proj4(lat, lon), "EPSG:4326", always_xy=True)
    xs, ys = t.transform([-half, half, -half, half], [-half, -half, half, half])
    return min(ys), min(xs), max(ys), max(xs)


def fetch_osm(out: Path, lat: float, lon: float, half: float) -> None:
    _checkpoint()
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
        _checkpoint()
        url = OVERPASS[attempt % 2]
        try:
            r = requests.post(url, data={"data": query}, timeout=700, headers=UA)
            if r.status_code == 200:
                _checkpoint()
                data = r.json()
                if isinstance(data.get("elements"), list) and not data.get("remark"):
                    publish_bytes(path, r.content)
                    print("osm", len(r.content), flush=True)
                    return
                print("osm incomplete response", url, flush=True)
            print("osm retry", url, r.status_code, flush=True)
        except requests.RequestException as exc:
            print("osm retry", exc, flush=True)
        _retry_sleep(15 * (attempt + 1))
    raise RuntimeError("overpass failed")


def get_retry(url: str, *, params=None, timeout=300, attempts=8):
    for attempt in range(attempts):
        _checkpoint()
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
            content_type = r.headers.get("content-type", "").lower()
            if r.ok and (content_type.startswith(("image/", "application/octet-stream", "application/json", "application/geo+json", "application/atom+xml", "application/xml", "application/zip")) or "tif" in content_type or "zip" in content_type or (content_type.startswith("text/plain") and url.lower().split("?", 1)[0].endswith((".json", ".atom")))):
                _checkpoint()
                return r
            print("retry", r.status_code, r.url, flush=True)
        except requests.RequestException as exc:
            print("retry", exc, flush=True)
        _retry_sleep(min(60, 3 + attempt * 5))
    raise RuntimeError(f"request failed: {url}")


def save_rgba(rgb: np.ndarray, path: Path) -> None:
    _checkpoint()
    if rgb.shape[-1] == 4:
        rgba = rgb.astype(np.uint8)
    else:
        rgba = np.dstack([rgb[..., :3].astype(np.uint8), np.full(rgb.shape[:2], 255, np.uint8)])
    tmp = path.with_suffix(path.suffix + ".part")
    Image.fromarray(rgba, "RGBA").save(tmp, format="PNG", compress_level=3)
    tmp.replace(path)
    _checkpoint()


def pdok_rd_bbox(lat: float, lon: float, x0: float, y0: float, span: float):
    from pyproj import Transformer

    local = Transformer.from_crs(proj4(lat, lon), "EPSG:28992", always_xy=True)
    points = [local.transform(x0, y0), local.transform(x0 + span, y0),
              local.transform(x0, y0 + span), local.transform(x0 + span, y0 + span)]
    xs, ys = zip(*points)
    margin = 2.0
    return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin


def fetch_nl_image(path: Path, lat: float, lon: float, x0: float, y0: float, span: float, px: int) -> None:
    _checkpoint()
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
    _checkpoint()
    path = out / "dem.npy"
    if path.exists():
        existing = np.load(path, mmap_mode="r")
        if np.isfinite(existing).all() and float(np.nanmax(existing)) < 1e6:
            _checkpoint()
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
            _checkpoint()
            x0, y0 = -half + i * 1000.0, -half + j * 1000.0
            span_x, span_y = min(1000.0, half - x0), min(1000.0, half - y0)
            bbox = pdok_rd_bbox(lat, lon, x0, y0, min(span_x, span_y))
            dem_tile = out / f"ahn_dtm_{i}_{j}.tif"
            if not dem_tile.exists():
                params = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
                          ("COVERAGEID", "dtm_05m"), ("SUBSET", f"x({bbox[0]:.3f},{bbox[2]:.3f})"),
                          ("SUBSET", f"y({bbox[1]:.3f},{bbox[3]:.3f})"), ("FORMAT", "image/tiff")]
                response = get_retry(PDOK_WCS, params=params, timeout=300)
                publish_bytes(dem_tile, response.content)
            nx, ny = int(round(span_x / 0.5)) + 1, int(round(span_y / 0.5)) + 1
            tile_dst = np.zeros((ny, nx), np.float32)
            tile_transform = from_bounds(x0, y0, x0 + span_x, y0 + span_y, nx, ny)
            with rasterio.open(dem_tile) as src:
                reproject(source=rasterio.band(src, 1), destination=tile_dst, src_transform=src.transform,
                          src_crs=src.crs or CRS.from_epsg(28992), dst_transform=tile_transform,
                          dst_crs=CRS.from_proj4(proj4(lat, lon)), src_nodata=src.nodata,
                          dst_nodata=np.nan, resampling=Resampling.bilinear)
            _checkpoint()
            c0 = int(round((x0 + half) / 0.5))
            r0 = int(round((half - y0 - span_y) / 0.5))
            h, w = min(ny, n - r0), min(nx, n - c0)
            tile_dst[~np.isfinite(tile_dst) | (tile_dst > 1e6)] = np.nan
            dst[r0:r0 + h, c0:c0 + w] = tile_dst[:h, :w]
    invalid = ~np.isfinite(dst) | (dst > 1e6)
    if invalid.any() and (~invalid).any():
        _checkpoint()
        nearest = distance_transform_edt(invalid, return_distances=False, return_indices=True)
        _checkpoint()
        dst[invalid] = dst[tuple(nearest[:, invalid])]
    save_npy(path, np.flipud(dst))


def stac_items(url: str, collection: str, lat: float, lon: float, half: float, *, modifier=None):
    import pystac_client

    _checkpoint()
    catalog = pystac_client.Client.open(url, modifier=modifier)
    _checkpoint()
    s, w, n, e = bbox_ll(lat, lon, half)
    return list(_iter_stac_items(catalog.search(collections=[collection], bbox=[w, s, e, n], max_items=500)))


def public_asset_url(href: str) -> str:
    """Keep provenance reproducible without persisting temporary signed query tokens."""
    parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def rasterize_stac_items(items, asset_key: str, lat: float, lon: float, bounds, size, *, bands=None, dtype=None,
                         masked_dem=False):
    _checkpoint()
    import rasterio
    from rasterio.crs import CRS
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    crs = CRS.from_proj4(proj4(lat, lon))
    transform = from_bounds(*bounds, size[0], size[1])
    count = len(bands) if bands else 1
    output = np.zeros((count, size[1], size[0]), dtype or (np.uint8 if bands else np.float32)) if bands else np.zeros((size[1], size[0]), dtype or np.float32)
    if masked_dem:
        output.fill(np.nan)
    filled = np.zeros((size[1], size[0]), bool)
    for item in items:
        _checkpoint()
        href = item.assets.get(asset_key)
        if href is None:
            # Swiss STAC asset names include tile ids; select each tile at the same native resolution.
            href = next((a for k, a in item.assets.items() if asset_key in k and k.lower().endswith(".tif")), None)
        if href is None:
            continue
        for attempt in range(6):
            _checkpoint()
            try:
                with rasterio.open(href.href) as src:
                    nodata = src.nodata
                    if bands:
                        with WarpedVRT(src, crs=crs, transform=transform, width=size[0], height=size[1],
                                       resampling=Resampling.bilinear) as vrt:
                            part = vrt.read(bands)
                        _checkpoint()
                        part = np.moveaxis(part, 0, -1)
                        valid = np.any(part != 0, axis=2)
                    else:
                        options = {"dtype": "float32", "nodata": np.nan} if masked_dem else {}
                        with WarpedVRT(src, crs=crs, transform=transform, width=size[0], height=size[1],
                                       resampling=Resampling.bilinear, **options) as vrt:
                            warped = vrt.read(1, masked=masked_dem)
                        _checkpoint()
                        part = warped.filled(np.nan) if masked_dem else warped
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
                _retry_sleep(4 * (attempt + 1))
        else:
            raise RuntimeError(f"unable to read STAC asset {asset_key} for {item.id}")
        if filled.all():
            break
    _checkpoint()
    if bands:
        return output
    return output


def fetch_swiss(out: Path, lat: float, lon: float, half: float) -> dict:
    _checkpoint()
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
        save_npy(dem_path, np.flipud(dem))
    tile_n = int(round(2 * half / TILE_M))
    img_key = "0.1_2056"
    for j in range(tile_n):
        for i in range(tile_n):
            _checkpoint()
            path = out / f"naip_{i}_{j}.png"
            if path.exists():
                continue
            x0, y0 = -half + i * TILE_M, -half + j * TILE_M
            rgb = rasterize_stac_items(images, img_key, lat, lon,
                (x0, y0, x0 + TILE_M, y0 + TILE_M), (TILE_CRS_PX, TILE_CRS_PX), bands=[1, 2, 3], dtype=np.uint8)
            save_rgba(np.moveaxis(rgb, 0, -1), path)
    core_path = out / "core.png"
    _checkpoint()
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
    _checkpoint()
    path = out / "3dbag.json"
    pages = out / ".3dbag-pages"
    if not path.exists():
        pages.mkdir(exist_ok=True)
        bbox = pdok_rd_bbox(lat, lon, -half, -half, 2 * half)
        next_url = "https://api.3dbag.nl/collections/pand/items"
        params = {"bbox": ",".join(f"{v:.3f}" for v in bbox), "limit": 1000}
        features = []
        seen = set()
        for index in range(BAG_MAX_PAGES):
            _checkpoint()
            if next_url in seen:
                raise RuntimeError(f"3DBAG pagination repeated cursor: {next_url}")
            seen.add(next_url)
            cached = pages / f"{index:04d}.json"
            if cached.exists():
                page = json.loads(cached.read_text())
                if page["url"] != next_url:
                    raise ValueError(f"3DBAG page cursor mismatch: {cached}")
                if index == 0:
                    # Legacy checkpoints did not store the initial request params.
                    legacy_bbox = parse_qs(urlsplit(page["next"] or "").query).get("bbox")
                    if (("params" in page and page["params"] != params)
                            or (legacy_bbox and legacy_bbox != [params["bbox"]])):
                        raise ValueError(f"3DBAG page bbox mismatch: {cached}")
            else:
                response = get_retry(next_url, params=params, timeout=300).json()
                rows = []
                for feature in response["features"]:
                    objects = {}
                    for object_id, obj in feature.get("CityObjects", {}).items():
                        attrs = obj.get("attributes", {})
                        if attrs.get("b3_h_nok") is not None and attrs.get("b3_h_maaiveld") is not None:
                            objects[object_id] = {"attributes": {k: attrs.get(k) for k in
                                ("b3_h_nok", "b3_h_maaiveld", "identificatie", "b3_pw_datum", "b3_pw_bron")}}
                    rows.append({"CityObjects": objects})
                link = next((item["href"] for item in response.get("links", []) if item.get("rel") == "next"), None)
                page = {"url": next_url, "next": urljoin(next_url, link) if link else None,
                        "features": rows}
                if params is not None:
                    page["params"] = params
                publish_bytes(cached, json.dumps(page, separators=(",", ":")).encode())
            features.extend(page["features"])
            next_url, params = page["next"], None
            if not next_url:
                break
        else:
            raise RuntimeError(f"3DBAG pagination exceeded {BAG_MAX_PAGES} pages")
        publish_bytes(path, json.dumps({"type": "FeatureCollection", "features": features}).encode())
    else:
        features = json.loads(path.read_text())["features"]
    if pages.exists():
        shutil.rmtree(pages)
    (out / "3dbag.partial.jsonl").unlink(missing_ok=True)
    (out / "3dbag.next-url").unlink(missing_ok=True)
    _checkpoint()
    print("3dbag", len(features), flush=True)
    return sorted({int(obj["attributes"]["b3_pw_datum"]) for feature in features
                   for obj in feature.get("CityObjects", {}).values()
                   if obj.get("attributes", {}).get("b3_pw_datum") is not None})


def _stac_source_identity(item) -> dict:
    return {"id": item.id, "updated": item.properties.get("updated"),
            "assets": [(key, public_asset_url(asset.href)) for key, asset in item.assets.items()]}


def x1_dem_coverage(dem: np.ndarray) -> dict:
    covered = np.isfinite(dem)
    if not covered.any():
        raise ValueError("X1 DEM has no valid coverage in the 6 km window")
    if not np.any(covered & (dem != 0)):
        raise ValueError("X1 DEM is all zero across valid samples in the 6 km window")
    return {"dem_coverage_fraction": float(covered.mean()),
            "dem_origin_covered": bool(covered[dem.shape[0] // 2, dem.shape[1] // 2]),
            "dem_uncovered_value": "NaN"}


def fetch_global(out: Path, lat: float, lon: float, half: float) -> dict:
    _checkpoint()
    import planetary_computer

    dem_items = stac_items(PC_STAC, "cop-dem-glo-30", lat, lon, half, modifier=planetary_computer.sign_inplace)
    image_items = stac_items(PC_STAC, "sentinel-2-l2a", lat, lon, half, modifier=planetary_computer.sign_inplace)
    _checkpoint()
    image_items.sort(key=lambda i: (i.properties.get("eo:cloud_cover", 100), -(i.datetime.timestamp() if i.datetime else 0)))
    n = int(round(2 * half / 2.0)) + 1
    dem_key = next((k for k in dem_items[0].assets if k in {"data", "dem"}), next(iter(dem_items[0].assets)))
    image = image_items[0]
    nt = int(round(2 * half / TILE_M))
    tiles = [(i, j, out / f"naip_{i}_{j}.png") for j in range(nt) for i in range(nt)]
    dem_path = out / "dem.npy"
    masked_dem = 2 * half == X1_EXTENT_M
    cache_identity = out / ".x1-global-raster-sources.json"
    identity = None
    reuse = False
    if os.environ.get(FETCH_LOCK_ENV):
        identity = json.dumps({
            "version": 2 if masked_dem else 1, "grid": [lat, lon, half, n, TILE_M, TILE_CRS_PX],
            "dem_key": dem_key, "dem": [_stac_source_identity(item) for item in dem_items],
            "imagery": _stac_source_identity(image),
        }, sort_keys=True).encode()
        reuse = cache_identity.exists() and cache_identity.read_bytes() == identity
    _checkpoint()
    if not reuse:
        # With no matching identity, the existing rasters may be mixed after an
        # interrupted refresh. Rewrite all of them until a refresh completes.
        cache_identity.unlink(missing_ok=True)
        if identity is not None and not dem_path.exists() and not any(path.exists() for _, _, path in tiles):
            publish_bytes(cache_identity, identity)
            reuse = True
    if reuse:
        tiles = [(i, j, path) for i, j, path in tiles if not path.exists()]
    needs_dem = not reuse or not dem_path.exists()
    if identity is not None and (needs_dem or tiles):
        (out / "provenance.json").unlink(missing_ok=True)
    coverage = {}
    if needs_dem:
        dem = rasterize_stac_items(dem_items, dem_key, lat, lon, (-half, -half, half, half), (n, n),
                                   **({"masked_dem": True} if masked_dem else {}))
        if masked_dem:
            coverage = x1_dem_coverage(dem)
        save_npy(dem_path, np.flipud(dem))
    elif masked_dem:
        coverage = x1_dem_coverage(np.load(dem_path, mmap_mode="r"))
    if tiles or not reuse:
        # Keep the existing full-scene warp and publish only the tiles needed.
        _checkpoint()
        px = int(2 * half / TILE_M) * TILE_CRS_PX
        if "visual" in image.assets:
            arr = rasterize_stac_items([image], "visual", lat, lon, (-half, -half, half, half),
                                       (px, px), bands=[1, 2, 3], dtype=np.uint8)
            _checkpoint()
            rgba = np.dstack([np.moveaxis(np.clip(arr, 0, 255).astype(np.uint8), 0, -1),
                              np.full(arr.shape[1:], 255, np.uint8)])
        else:
            rgb_bands = []
            for band in ("B04", "B03", "B02"):
                _checkpoint()
                if band not in image.assets:
                    raise RuntimeError("Sentinel item is missing RGB bands")
                layer = rasterize_stac_items([image], band, lat, lon, (-half, -half, half, half), (px, px), dtype=np.float32)
                _checkpoint()
                rgb_bands.append(np.clip(layer, 0, 10000).astype(np.float32) / 10000 * 255)
            rgba = np.dstack([*rgb_bands, np.full((px, px), 255, np.uint8)]).astype(np.uint8)
        for i, j, path in tiles:
            _checkpoint()
            tile = rgba[j*TILE_CRS_PX:(j+1)*TILE_CRS_PX, i*TILE_CRS_PX:(i+1)*TILE_CRS_PX]
            save_rgba(tile, path)
    if identity is not None and not reuse:
        publish_bytes(cache_identity, identity)
    _checkpoint()
    dem_item = dem_items[0]
    return {"imagery_years": [image.datetime.year] if image.datetime else [],
            "dem_years": [dem_item.datetime.year] if dem_item.datetime else [],
            "cloud_cover_percent": image.properties.get("eo:cloud_cover"),
            "imagery_item_id": image.id, "imagery_datetime": image.datetime.isoformat() if image.datetime else None,
            "imagery_gsd_m": image.properties.get("gsd", 10.0),
            "imagery_asset_urls": {k: public_asset_url(v.href) for k, v in image.assets.items() if k in {"visual", "B02", "B03", "B04"}},
            "dem_item_id": dem_item.id, "dem_datetime": dem_item.datetime.isoformat() if dem_item.datetime else None,
            "dem_asset_url": public_asset_url(dem_item.assets[dem_key].href),
            "raster_grid_res_m": 2 * half / max(n - 1, 1), **coverage}


def _download_city_index(out: Path, url: str, name: str) -> str:
    """Keep an auditable copy of a provider index without downloading a whole city."""
    path = out / name
    if not path.exists():
        publish_bytes(path, get_retry(url, timeout=120).content)
    return sha256_file(path)


def _berlin_tile_for_site(lat: float, lon: float) -> str:
    """Select the 1 km CityGML tile containing the origin from Berlin's Atom feed."""
    from xml.etree import ElementTree as ET

    from pyproj import Transformer

    e, n = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True).transform(lon, lat)
    stem = f"LoD2_{int(e // 1000):03d}_{int(n // 1000):04d}.zip"
    xml = get_retry(BERLIN_ATOM, timeout=60).content
    root = ET.fromstring(xml)
    links = [x.attrib.get("href", "") for x in root.iter() if x.tag.rsplit("}", 1)[-1] == "link"]
    exact = next((u for u in links if u.endswith(stem)), None)
    if exact:
        return exact
    raise ValueError(f"Berlin feed has no tile covering the requested origin: {stem}")


def fetch_city_model(out: Path, provider: str, lat: float, lon: float, half: float) -> dict:
    """Fetch auditable open-city indexes and, where possible, a small CityGML tile."""
    _checkpoint()
    from zipfile import ZipFile
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from city_models import parse_citygml, write_normalized
    if provider == "plateau":
        sha = _download_city_index(out, TOKYO_TILESET, "plateau_tileset.json")
        # Index-only until the 3D Tiles hierarchy transforms and texture UVs
        # are preserved. Raw glTF coordinates are not local ENU coordinates.
        return {"model": {"name": "PLATEAU Tokyo Taito-ku 3D Tiles index", "url": TOKYO_TILESET,
                           "license": "PLATEAU 利用規約（出典表示条件）", "sha256": sha,
                           "texture": False, "note": "仅固定索引；未应用 3D Tiles 坐标变换，构建使用 OSM 回退。"}}
    if provider != "berlin":
        return {}
    from pyproj import Transformer

    url = _berlin_tile_for_site(lat, lon)
    archive = out / Path(url).name
    if not archive.exists():
        publish_bytes(archive, get_retry(url, timeout=300).content)
    sha = sha256_file(archive)
    gml = next((n for n in ZipFile(archive).namelist() if n.lower().endswith((".gml", ".xml"))), None)
    if gml and not (out / "city_model.json").exists():
        import tempfile
        _checkpoint()
        with ZipFile(archive) as z, tempfile.TemporaryDirectory(dir=out, prefix=".citygml-") as td:
            p = Path(td) / Path(gml).name
            p.write_bytes(z.read(gml))
            _checkpoint()
            local = f"+proj=tmerc +lat_0={lat} +lon_0={lon} +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m"
            proj = Transformer.from_crs("EPSG:25833", local, always_xy=True)
            buildings = parse_citygml(p, proj)
        _checkpoint()
        tmp = out / "city_model.json.part"
        write_normalized(tmp, buildings, source=url, textured=False)
        tmp.replace(out / "city_model.json")
    _checkpoint()
    return {"model": {"name": "Berlin LoD2 CityGML", "url": url, "license": "Datenlizenz Deutschland Zero 2.0",
                       "attribution": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin",
                       "sha256": sha, "texture": False,
                       "note": "开放 LoD2 几何；真实纹理来自同地点 DOP 正射影像。"}}


def fetch_city_imagery(out: Path, provider: str, lat: float, lon: float, half: float) -> dict:
    """Fetch a Sentinel fallback and explicitly record its actual source."""
    years = fetch_global(out, lat, lon, half)
    years["imagery_source"] = {"name": "Sentinel-2 L2A", "url": f"{PC_STAC}/collections/sentinel-2-l2a", "license": "Copernicus free and open data", "resolution_m": 10.0}
    return years


def x1_raw_products() -> set[str]:
    n = X1_EXTENT_M // TILE_M
    return {"osm.json", "dem.npy", "core.png"} | {f"naip_{i}_{j}.png" for i in range(n) for j in range(n)}


def derive_x1_core(out: Path) -> dict:
    """Crop the existing north-up Sentinel mosaic; this does not add detail."""
    n = X1_EXTENT_M // TILE_M
    core_size = round(CORE_HALF_M / TILE_M * TILE_CRS_PX)
    core = Image.new("RGBA", (2 * core_size, 2 * core_size))
    sources = []
    # fetch_global slices the north-up raster in row order (north to south).
    for y, j in enumerate((n // 2 - 1, n // 2)):
        for x, i in enumerate((n // 2 - 1, n // 2)):
            _checkpoint()
            name = f"naip_{i}_{j}.png"
            with Image.open(out / name) as tile:
                if tile.size != (TILE_CRS_PX, TILE_CRS_PX):
                    raise ValueError(f"unexpected Sentinel tile dimensions: {out / name}")
                left = TILE_CRS_PX - core_size if x == 0 else 0
                top = TILE_CRS_PX - core_size if y == 0 else 0
                core.paste(tile.crop((left, top, left + core_size, top + core_size)),
                           (x * core_size, y * core_size))
            sources.append(name)
    save_rgba(np.asarray(core), out / "core.png")
    return {"method": "center crop of Sentinel-2 mosaic", "source_tiles": sources}


def raw_product(path: Path, out: Path) -> dict:
    return {"path": str(path.relative_to(out)), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def reuse_x1_products(out: Path, prior: dict | None, identity: dict) -> bool:
    """Accept a complete receipt, or repair the legacy core-only omission offline."""
    if not prior or any(prior.get(key) != value for key, value in identity.items()):
        return False
    downloads = {row["path"]: row for row in prior.get("downloads", [])}
    missing = x1_raw_products() - downloads.keys()
    if missing - {"core.png"}:
        return False
    # Do not reissue a receipt if a previously pinned input changed.
    for relative, row in downloads.items():
        path = out / relative
        if (not path.resolve().is_relative_to(out.resolve()) or not path.is_file()
                or row["bytes"] <= 0 or path.stat().st_size != row["bytes"]
                or sha256_file(path) != row["sha256"]):
            raise ValueError(f"cannot repair X1 core: cached raw product mismatch: {path}")
    if not missing:
        return True
    derived = derive_x1_core(out)
    prior.setdefault("derived_products", {})["core.png"] = derived
    prior["downloads"].append(raw_product(out / "core.png", out))
    publish_bytes(out / "provenance.json", json.dumps(prior, indent=2).encode())
    return True


def bind_fetch_origin(out: Path, prior: dict | None, identity: dict, *, override: bool) -> None:
    pin = out / ".x1-fetch-origin.json"
    if pin.exists():
        if json.loads(pin.read_text()) != identity:
            raise ValueError(f"{out}: cached fetch has a different site origin; use a new --raw-root")
    elif override:
        if prior is None and any(path.name != f"{pin.name}.part" for path in out.iterdir()):
            raise ValueError(f"{out}: unpinned partial fetch; use a new --raw-root for --origin")
        publish_bytes(pin, json.dumps(identity, sort_keys=True).encode())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sites", nargs="*")
    ap.add_argument("--raw-root", type=Path, help="原始数据站点目录，默认 <asset_root>/sites")
    ap.add_argument("--extent-m", type=float, help="正方形边长（米）；显式扩展时必须与 build_scene 使用同值")
    ap.add_argument("--origin", nargs=2, type=float, metavar=("LAT", "LON"),
                    help="override the origin of one explicitly selected --extent-m 6000 site")
    args = ap.parse_args()
    if args.origin is not None:
        if len(args.sites) != 1 or args.extent_m != X1_EXTENT_M:
            ap.error("--origin requires one explicitly selected site and --extent-m 6000")
        lat, lon = args.origin
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            ap.error("--origin requires finite latitude [-90, 90] and longitude [-180, 180]")
    _checkpoint()
    cfg = json.loads(SITES.read_text())
    for name in args.sites or cfg["sites"]:
        _checkpoint()
        site = cfg["sites"][name].copy()
        if args.origin is not None:
            site["lat"], site["lon"] = args.origin
        out = (args.raw_root or Path(cfg["asset_root"]) / "sites") / name / "raw"
        out.mkdir(parents=True, exist_ok=True)
        extent_m = site["size_m"] if args.extent_m is None else args.extent_m
        from range_profile import make_profile
        profile = make_profile(extent_m, name="fetched")
        provider = site.get("provider", "us")
        identity = {"site": name, "lat": site["lat"], "lon": site["lon"], "size_m": extent_m, "provider": provider}
        existing = out / "provenance.json"
        prior = None
        if existing.exists():
            prior = json.loads(existing.read_text())
            if abs(float(prior.get("size_m", extent_m)) - extent_m) > 1e-6:
                raise ValueError(f"{name}: existing raw extent {prior.get('size_m')} differs; use a new --raw-root")
            if extent_m == X1_EXTENT_M and any(prior.get(key) != identity[key] for key in ("lat", "lon")):
                raise ValueError(f"{name}: existing raw origin differs; use a new --raw-root")
        bind_fetch_origin(out, prior, identity, override=args.origin is not None)
        half = profile.half_extent_m
        wkt = local_wkt(site["lat"], site["lon"])
        print("==", name, flush=True)
        needs_x1_core = extent_m == X1_EXTENT_M and provider in SENTINEL_PROVIDERS
        if needs_x1_core and reuse_x1_products(out, prior, identity):
            print("reused X1 raw products, including derived core imagery", flush=True)
            continue
        model_source = {}
        if provider == "us":
            fetch_dem(out, wkt, half)
            _checkpoint()
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
            dem_res_m = 30.0
        elif provider in {"plateau", "berlin"}:
            years = fetch_city_imagery(out, provider, site["lat"], site["lon"], half)
            _checkpoint()
            model_source = fetch_city_model(out, provider, site["lat"], site["lon"], half)
            dem_res_m = 30.0
        else:
            raise ValueError(f"unknown provider {provider!r} for {name}")
        _checkpoint()
        fetch_osm(out, site["lat"], site["lon"], half)
        _checkpoint()
        building_height_years = fetch_3dbag(out, site["lat"], site["lon"], half) if provider == "nl" else []
        _checkpoint()
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
        derived_products = {}
        if needs_x1_core:
            for relative in sorted(x1_raw_products() - {"core.png"}):
                path = out / relative
                if not path.is_file() or path.stat().st_size <= 0:
                    raise ValueError(f"missing or empty X1 raw product: {path}")
            derived_products["core.png"] = derive_x1_core(out)
        downloads = []
        for p in sorted(out.rglob("*")):
            _checkpoint()
            if p.is_file() and p.name != "provenance.json" and not any(
                    part.startswith(".") or part.endswith(".part") for part in p.relative_to(out).parts):
                downloads.append(raw_product(p, out))
        publish_bytes(out / "provenance.json", json.dumps({
            **identity,
            "range_profile": profile.as_dict(),
            "proj4": proj4(site["lat"], site["lon"]),
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dem_res_m": dem_res_m, "building_height_years": building_height_years, "imagery_res_m": {
                "us": 0.6, "swiss": 0.1, "nl": 0.08, "global": 10.0, "plateau": 10.0, "berlin": 10.0
            }[provider], "source_years": years,
            "downloads": downloads, **({"derived_products": derived_products} if derived_products else {}), "sources": {**sources,
                "vectors": {"name": "OpenStreetMap via Overpass", "license": "ODbL-1.0",
                            "attribution": "(c) OpenStreetMap contributors"},
            },
        }, indent=2).encode())


if __name__ == "__main__":
    if os.environ.get(FETCH_LOCK_ENV) and str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    main()
