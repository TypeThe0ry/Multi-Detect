from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.realworld import fetch_site


@pytest.fixture
def flight(monkeypatch, tmp_path):
    lock = tmp_path / "shared.lock"
    request = SimpleNamespace(active=False)

    def flight_requested(path):
        assert path == lock
        return request.active

    monkeypatch.setenv("MD_X1_FETCH_LOCK", str(lock))
    monkeypatch.setitem(sys.modules, "scripts.realworld.x1_lock",
                        SimpleNamespace(flight_requested=flight_requested))
    return request


def test_checkpoint_is_opt_in_and_precedes_request(monkeypatch, flight):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(ok=True, headers={"content-type": "application/json"})

    monkeypatch.setattr(fetch_site.requests, "get", get)
    flight.active = True
    with pytest.raises(SystemExit) as stopped:
        fetch_site.get_retry("offline")
    assert stopped.value.code == 75
    assert calls == []

    monkeypatch.delenv("MD_X1_FETCH_LOCK")
    fetch_site.get_retry("offline")
    assert calls == ["offline"]


def test_request_during_retry_wait_exits_before_next_attempt(monkeypatch, flight):
    calls = []
    sleeps = []

    def get(url, **kwargs):
        calls.append(url)
        raise fetch_site.requests.ConnectionError("offline")

    def sleep(seconds):
        sleeps.append(seconds)
        flight.active = True

    monkeypatch.setattr(fetch_site.requests, "get", get)
    monkeypatch.setattr(fetch_site.time, "sleep", sleep)
    with pytest.raises(SystemExit) as stopped:
        fetch_site.get_retry("offline")
    assert stopped.value.code == 75
    assert calls == ["offline"]
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1


@pytest.mark.parametrize("paged", [True, False])
def test_stac_checks_before_advancing(monkeypatch, flight, paged):
    advances = []

    def results():
        advances.append(1)
        yield ["first", "still first page"] if paged else "first"
        advances.append(2)
        yield ["second"] if paged else "second"

    search = SimpleNamespace(**{"pages" if paged else "items": results})
    client = SimpleNamespace(search=lambda **_: search)
    monkeypatch.setitem(sys.modules, "pystac_client", SimpleNamespace(Client=SimpleNamespace(open=lambda *_, **__: client)))
    monkeypatch.setattr(fetch_site, "bbox_ll", lambda *_: (0, 0, 1, 1))
    flight.active = True
    with pytest.raises(SystemExit) as stopped:
        fetch_site.stac_items("offline", "example", 0, 0, 1000)
    assert stopped.value.code == 75
    assert advances == []

    flight.active = False
    items = fetch_site._iter_stac_items(search)
    assert next(items) == "first"
    flight.active = True
    if paged:
        assert next(items) == "still first page"
    with pytest.raises(SystemExit) as stopped:
        next(items)
    assert stopped.value.code == 75
    assert advances == [1]


def test_3dbag_reuses_page_published_before_yield(monkeypatch, tmp_path, flight):
    monkeypatch.setattr(fetch_site, "pdok_rd_bbox", lambda *_: (0, 0, 1, 1))
    first_url = "https://api.3dbag.nl/collections/pand/items"
    second_url = "https://example.test/second"
    calls = []
    replace = Path.replace

    def get(url, **_):
        calls.append(url)
        links = [{"rel": "next", "href": second_url}] if url == first_url else []
        return SimpleNamespace(json=lambda: {"features": [], "links": links})

    def request_after_replace(path, target):
        result = replace(path, target)
        if target.name == "0000.json":
            flight.active = True
        return result

    monkeypatch.setattr(fetch_site, "get_retry", get)
    monkeypatch.setattr(Path, "replace", request_after_replace)
    with pytest.raises(SystemExit) as stopped:
        fetch_site.fetch_3dbag(tmp_path, 0, 0, 1000)
    assert stopped.value.code == 75
    assert calls == [first_url]
    assert json.loads((tmp_path / ".3dbag-pages/0000.json").read_text())["next"] == second_url
    assert not (tmp_path / "3dbag.json").exists()

    flight.active = False
    fetch_site.fetch_3dbag(tmp_path, 0, 0, 1000)
    assert calls == [first_url, second_url]
    assert (tmp_path / "3dbag.json").is_file()
    assert not (tmp_path / ".3dbag-pages").exists()


def test_guarded_global_fetch_reuses_committed_rasters(monkeypatch, tmp_path, flight):
    monkeypatch.setitem(sys.modules, "planetary_computer", SimpleNamespace(sign_inplace=lambda _: None))
    monkeypatch.setattr(fetch_site, "TILE_CRS_PX", 2)
    dem = SimpleNamespace(id="dem", datetime=None, properties={},
                          assets={"data": SimpleNamespace(href="https://example.test/dem")})
    image = SimpleNamespace(id="rgb", datetime=None, properties={},
                            assets={"visual": SimpleNamespace(href="https://example.test/rgb")})
    monkeypatch.setattr(fetch_site, "stac_items", lambda _, collection, *args, **kw:
                        [dem] if collection == "cop-dem-glo-30" else [image])
    reads = []
    published = []
    replace = Path.replace
    interrupt_on_tile = True

    def rasterize(items, asset, lat, lon, bounds, size, *, bands=None, dtype=None):
        reads.append(asset)
        return np.ones((3, *size) if bands else size, dtype=dtype or np.float32)

    def request_after_first_tile(path, target):
        result = replace(path, target)
        if target.suffix in {".npy", ".png"}:
            published.append(target.name)
        if interrupt_on_tile and target.name == "naip_0_0.png":
            flight.active = True
        return result

    monkeypatch.setattr(fetch_site, "rasterize_stac_items", rasterize)
    monkeypatch.setattr(Path, "replace", request_after_first_tile)
    with pytest.raises(SystemExit) as stopped:
        fetch_site.fetch_global(tmp_path, 0, 0, 1000)
    assert stopped.value.code == 75
    assert published == ["dem.npy", "naip_0_0.png"]
    assert reads == ["data", "visual"]

    flight.active = False
    (tmp_path / "naip_1_0.png.part").write_bytes(b"interrupted atomic write")
    image.assets["visual"].href += "?token=rotated"
    assert fetch_site.fetch_global(tmp_path, 0, 0, 1000)["imagery_item_id"] == "rgb"
    assert published == ["dem.npy", "naip_0_0.png", "naip_1_0.png", "naip_0_1.png", "naip_1_1.png"]
    assert reads == ["data", "visual", "visual"]
    assert not list(tmp_path.glob("*.part"))

    fetch_site.fetch_global(tmp_path, 0, 0, 1000)
    assert reads == ["data", "visual", "visual"]

    # A new selection invalidates the receipt before the first rewrite. An
    # interrupted refresh leaves no identity that could certify mixed rasters.
    identity = tmp_path / ".x1-global-raster-sources.json"
    assert json.loads(identity.read_text())["imagery"]["id"] == "rgb"
    (tmp_path / "provenance.json").write_text("{}")
    image.id = "new-rgb"
    published.clear()
    with pytest.raises(SystemExit) as stopped:
        fetch_site.fetch_global(tmp_path, 0, 0, 1000)
    assert stopped.value.code == 75
    assert published == ["dem.npy", "naip_0_0.png"]
    assert not identity.exists()
    assert not (tmp_path / "provenance.json").exists()

    flight.active = False
    interrupt_on_tile = False
    published.clear()
    assert fetch_site.fetch_global(tmp_path, 0, 0, 1000)["imagery_item_id"] == "new-rgb"
    assert published == ["dem.npy", "naip_0_0.png", "naip_1_0.png", "naip_0_1.png", "naip_1_1.png"]
    assert json.loads(identity.read_text())["imagery"]["id"] == "new-rgb"

    provenance = tmp_path / "provenance.json"
    provenance.write_text('{"existing": true}')
    monkeypatch.delenv("MD_X1_FETCH_LOCK")
    published.clear()
    fetch_site.fetch_global(tmp_path, 0, 0, 1000)
    assert published == ["dem.npy", "naip_0_0.png", "naip_1_0.png", "naip_0_1.png", "naip_1_1.png"]
    assert json.loads(provenance.read_text()) == {"existing": True}
    assert not identity.exists()
