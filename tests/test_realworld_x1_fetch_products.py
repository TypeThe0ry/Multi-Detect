import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from scripts.realworld import fetch_site, range_profile, x1_world_pipeline


BAG_URL = "https://api.3dbag.nl/collections/pand/items"
BBOX = "0.000,0.000,6000.000,6000.000"


def bag_url(index):
    return f"{BAG_URL}?bbox={BBOX}&offset={index * 100 + 1}&limit=100" if index else BAG_URL


def test_3dbag_extends_legacy_cache_and_resumes_beyond_1000(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_site, "pdok_rd_bbox", lambda *_: (0, 0, 6000, 6000))
    pages = tmp_path / ".3dbag-pages"
    pages.mkdir()
    for index in range(1000):
        (pages / f"{index:04d}.json").write_text(json.dumps({
            "url": bag_url(index), "next": bag_url(index + 1), "features": [{"CityObjects": {}}],
        }))

    calls = []
    interrupted = True

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["params"] is None
        assert url in {bag_url(1000), bag_url(1001)}
        if interrupted and url == bag_url(1001):
            raise InterruptedError
        attrs = {"b3_h_nok": 12, "b3_h_maaiveld": 2, "b3_pw_datum": 2024}
        body = {"features": [{"CityObjects": {url: {"attributes": attrs}}}],
                "links": [{"rel": "next", "href": bag_url(1001)}] if url == bag_url(1000) else []}
        return SimpleNamespace(json=lambda: body)

    monkeypatch.setattr(fetch_site, "get_retry", get)
    with pytest.raises(InterruptedError):
        fetch_site.fetch_3dbag(tmp_path, 52, 4, 3000)
    assert calls == [bag_url(1000), bag_url(1001)]
    assert (pages / "1000.json").is_file()
    assert not (tmp_path / "3dbag.json").exists()

    interrupted = False
    calls.clear()
    assert fetch_site.fetch_3dbag(tmp_path, 52, 4, 3000) == [2024]
    assert calls == [bag_url(1001)]
    assert len(json.loads((tmp_path / "3dbag.json").read_text())["features"]) == 1002
    assert not pages.exists()
    assert fetch_site.fetch_3dbag(tmp_path, 52, 4, 3000) == [2024]
    assert calls == [bag_url(1001)]


def test_3dbag_rejects_cursor_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_site, "pdok_rd_bbox", lambda *_: (0, 0, 6000, 6000))
    calls = []

    def get(url, **_):
        calls.append(url)
        body = {"features": [], "links": [{"rel": "next", "href": bag_url(1)}]}
        return SimpleNamespace(json=lambda: body)

    monkeypatch.setattr(fetch_site, "get_retry", get)
    with pytest.raises(RuntimeError, match="repeated cursor"):
        fetch_site.fetch_3dbag(tmp_path, 52, 4, 3000)
    assert calls == [BAG_URL, bag_url(1)]
    assert not (tmp_path / "3dbag.json").exists()


@pytest.fixture
def fetch_cli(tmp_path, monkeypatch):
    monkeypatch.delenv(fetch_site.FETCH_LOCK_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "range_profile", range_profile)
    monkeypatch.setattr(fetch_site, "TILE_CRS_PX", 10)
    sites_file = tmp_path / "sites.json"
    monkeypatch.setattr(fetch_site, "SITES", sites_file)
    site = {"provider": "global", "lat": 1.35, "lon": 103.82, "size_m": 6000}
    name = "rw_test_w6km"
    asset = x1_world_pipeline.Asset.__new__(x1_world_pipeline.Asset)
    asset.name = name
    asset.root = tmp_path / "v1"
    asset.raw = asset.root / "raw" / name / "raw"
    asset.site = site
    asset.raw.mkdir(parents=True)

    def run(*args):
        sites_file.write_text(json.dumps({"sites": {name: site}}))
        monkeypatch.setattr(sys, "argv", ["fetch_site.py", "--raw-root", str(asset.raw.parent.parent), *args])
        fetch_site.main()

    return asset, run


def write_rasters(out):
    (out / "dem.npy").write_bytes(b"mock dem")
    for j in range(6):
        for i in range(6):
            tile = Image.new("RGBA", (10, 10))
            tile.putdata([(i, j, 10 * y + x, 255) for y in range(10) for x in range(10)])
            tile.save(out / f"naip_{i}_{j}.png")


def assert_core(out, provenance):
    # Global imagery tiles are north-up, named in north-to-south row order.
    with Image.open(out / "core.png") as core:
        assert core.size == (6, 6)
        assert [core.getpixel(p) for p in [(0, 0), (5, 0), (0, 5), (5, 5)]] == [
            (2, 2, 77, 255), (3, 2, 72, 255), (2, 3, 27, 255), (3, 3, 22, 255),
        ]
    assert provenance["derived_products"]["core.png"]["source_tiles"] == [
        "naip_2_2.png", "naip_3_2.png", "naip_2_3.png", "naip_3_3.png",
    ]
    core_row = next(row for row in provenance["downloads"] if row["path"] == "core.png")
    assert core_row == fetch_site.raw_product(out / "core.png", out)


@pytest.mark.parametrize("provider", ["global", "berlin", "plateau"])
def test_fresh_sentinel_x1_fetch_meets_pipeline_contract(fetch_cli, monkeypatch, provider):
    asset, run = fetch_cli
    asset.site["provider"] = provider
    seen = []

    def raster(out, lat, lon, half):
        seen.append((lat, lon, half))
        write_rasters(out)
        return {"imagery_gsd_m": 10}

    monkeypatch.setattr(fetch_site, "fetch_global", raster)
    monkeypatch.setattr(fetch_site, "fetch_city_model", lambda *_: {})
    monkeypatch.setattr(fetch_site, "fetch_osm", lambda out, *_: (out / "osm.json").write_text("{}"))
    run(asset.name, "--extent-m", "6000")
    assert seen == [(1.35, 103.82, 3000)]
    assert asset.fetch_provenance()
    provenance = json.loads((asset.raw / "provenance.json").read_text())
    assert_core(asset.raw, provenance)


@pytest.mark.parametrize("provider", ["berlin", "global"])
@pytest.mark.parametrize("damaged", [False, True])
def test_legacy_receipt_is_repaired_offline_without_repinning_inputs(fetch_cli, monkeypatch, provider, damaged):
    asset, run = fetch_cli
    asset.site["provider"] = provider
    write_rasters(asset.raw)
    (asset.raw / "osm.json").write_text("{}")
    if provider == "berlin":
        (asset.raw / "city_model.json").write_text("{}")
    downloads = [fetch_site.raw_product(path, asset.raw) for path in sorted(asset.raw.iterdir())]
    original = {**asset.site, "site": asset.name, "downloads": downloads, "fetched_utc": "original",
                "source_years": {"imagery_item_id": "pinned"}}
    receipt = asset.raw / "provenance.json"
    receipt.write_text(json.dumps(original))

    def offline(*_):
        pytest.fail("legacy repair must not access a provider")

    for name in ("fetch_global", "fetch_city_model", "fetch_osm"):
        monkeypatch.setattr(fetch_site, name, offline)
    if damaged:
        (asset.raw / "osm.json").write_text("[]")  # Same byte count, different hash.
        with pytest.raises(ValueError, match="cached raw product mismatch"):
            run(asset.name, "--extent-m", "6000")
        assert not (asset.raw / "core.png").exists()
        assert json.loads(receipt.read_text()) == original
        return

    run(asset.name, "--extent-m", "6000")
    assert asset.fetch_provenance()
    repaired = json.loads(receipt.read_text())
    assert_core(asset.raw, repaired)
    assert repaired["downloads"][:-1] == downloads
    assert repaired["source_years"] == original["source_years"]
    assert repaired["fetched_utc"] == original["fetched_utc"]
    pinned = receipt.read_bytes()
    run(asset.name, "--extent-m", "6000")
    assert receipt.read_bytes() == pinned


@pytest.mark.parametrize("sites,extent,coords", [
    ([], "6000", ("1", "2")), (["site", "site2"], "6000", ("1", "2")),
    (["site"], None, ("1", "2")), (["site"], "2000", ("1", "2")),
    (["site"], "6000", ("nan", "2")), (["site"], "6000", ("1", "inf")),
    (["site"], "6000", ("91", "2")), (["site"], "6000", ("1", "-181")),
])
def test_origin_requires_one_explicit_6km_site_and_valid_coordinates(fetch_cli, sites, extent, coords):
    _, run = fetch_cli
    with pytest.raises(SystemExit) as exc:
        run(*sites, *(["--extent-m", extent] if extent else []), "--origin", *coords)
    assert exc.value.code == 2


def test_origin_is_pinned_before_fetch_and_flows_into_provenance(fetch_cli, monkeypatch):
    asset, run = fetch_cli
    args = (asset.name, "--extent-m", "6000")
    monkeypatch.setattr(fetch_site, "fetch_osm", lambda out, *_: (out / "osm.json").write_text("{}"))
    seen = []

    def raster(out, lat, lon, half):
        seen.append((lat, lon, half))
        if len(seen) == 1:
            (out / "dem.npy").write_bytes(b"partial")
            raise InterruptedError
        write_rasters(out)
        return {}

    monkeypatch.setattr(fetch_site, "fetch_global", raster)
    with pytest.raises(InterruptedError):
        run(*args, "--origin", "1.4", "103.8")
    with pytest.raises(ValueError, match="different site origin"):
        run(*args, "--origin", "1.5", "103.8")
    with pytest.raises(ValueError, match="different site origin"):
        run(*args)
    run(*args, "--origin", "1.4", "103.8")
    assert seen == [(1.4, 103.8, 3000)] * 2
    provenance = json.loads((asset.raw / "provenance.json").read_text())
    assert (provenance["lat"], provenance["lon"]) == (1.4, 103.8)
    assert provenance["proj4"] == fetch_site.proj4(1.4, 103.8)
    assert asset.site["lat"] == 1.35


def test_origin_cannot_claim_an_unpinned_partial_cache(fetch_cli):
    asset, run = fetch_cli
    (asset.raw / "dem.npy").write_bytes(b"previous origin")
    with pytest.raises(ValueError, match="unpinned partial fetch"):
        run(asset.name, "--extent-m", "6000", "--origin", "1.4", "103.8")


@pytest.mark.parametrize("nodata", [None, -9999])
def test_masked_dem_distinguishes_zero_from_uncovered(tmp_path, nodata):
    import rasterio
    from rasterio.transform import from_bounds

    dem_path = tmp_path / "dem.tif"
    with rasterio.open(dem_path, "w", driver="GTiff", count=1, height=2, width=2,
                       crs=fetch_site.proj4(1, 2), transform=from_bounds(0, 0, 2, 2, 2, 2),
                       dtype="int16", nodata=nodata) as dst:
        dst.write(np.array([[0, 12], [5, nodata if nodata is not None else 8]], dtype=np.int16), 1)
    item = SimpleNamespace(id="local-dem", assets={"data": SimpleNamespace(href=str(dem_path))})
    raster = fetch_site.rasterize_stac_items([item], "data", 1, 2, (-1, 0, 2, 2), (3, 2), masked_dem=True)
    np.testing.assert_array_equal(raster, [[np.nan, 0, 12], [np.nan, 5, np.nan if nodata is not None else 8]])
    meta = fetch_site.x1_dem_coverage(raster)
    assert meta["dem_coverage_fraction"] == (3 if nodata is not None else 4) / 6
    assert meta["dem_origin_covered"] is True


@pytest.mark.parametrize("value,match", [(np.nan, "no valid coverage"), (0.0, "all zero")])
def test_fresh_global_x1_fetch_rejects_unusable_dem(tmp_path, monkeypatch, value, match):
    monkeypatch.delenv(fetch_site.FETCH_LOCK_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "planetary_computer", SimpleNamespace(sign_inplace=lambda _: None))
    common = {"datetime": None, "properties": {}}
    items = {
        "cop-dem-glo-30": SimpleNamespace(id="dem", assets={"data": None}, **common),
        "sentinel-2-l2a": SimpleNamespace(id="image", assets={"visual": None}, **common),
    }
    monkeypatch.setattr(fetch_site, "stac_items", lambda _, collection, *args, **kw: [items[collection]])

    def warp(_, key, lat, lon, bounds, size, *, masked_dem):
        assert (key, masked_dem) == ("data", True)
        return np.full((3, 3), value)

    monkeypatch.setattr(fetch_site, "rasterize_stac_items", warp)
    with pytest.raises(ValueError, match=match):
        fetch_site.fetch_global(tmp_path, 1, 2, 3000)
    assert not (tmp_path / "dem.npy").exists()
    assert not (tmp_path / "provenance.json").exists()
