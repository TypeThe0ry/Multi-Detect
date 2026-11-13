from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_sim_asset_catalog import build_resolved
from scripts.fetch_sim_assets import AssetFetchError, fetch_assets


def _fake_fuel_asset(root: Path) -> Path:
    model = root / "fuel.gazebosim.org/openrobotics/models/Synthetic/1"
    model.mkdir(parents=True)
    (model / "model.config").write_text(
        "<model><name>Synthetic</name><sdf version='1.7'>model.sdf</sdf></model>",
        encoding="utf-8",
    )
    (model / "model.sdf").write_text(
        "<sdf version='1.7'><model name='Synthetic'><link name='body'>"
        "<visual name='body'><geometry><box><size>1 1 1</size></box></geometry></visual>"
        "</link></model></sdf>",
        encoding="utf-8",
    )
    return model


def test_fetch_downloads_catalog_asset_verifies_and_builds_resolved(tmp_path):
    cache = tmp_path / "cache"
    source = _fake_fuel_asset(cache)
    raw_stage = tmp_path / "hash-input"
    raw_stage.mkdir()
    (raw_stage / "Synthetic").mkdir()
    for child in source.iterdir():
        (raw_stage / "Synthetic" / child.name).write_bytes(child.read_bytes())
    expected_root = build_resolved(raw_stage, tmp_path / "expected-resolved")
    expected_hash = hashlib.sha256((expected_root / "synthetic/model.sdf").read_bytes()).hexdigest()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "slug": "synthetic",
                        "source": "Synthetic",
                        "source_url": "https://fuel.gazebosim.org/1.0/OpenRobotics/models/Synthetic",
                        "model_sdf_sha256": expected_hash,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def downloader(_url, _version, cache_root):
        cache_root.mkdir(parents=True)
        found = _fake_fuel_asset(cache_root)
        assert (found / "model.sdf").is_file()

    raw_out, resolved_out = tmp_path / "OpenRobotics", tmp_path / "resolved"
    assert fetch_assets(catalog, raw_out, resolved_out, downloader=downloader) == ["synthetic"]
    assert (raw_out / "Synthetic/model.sdf").is_file()
    assert hashlib.sha256((resolved_out / "synthetic/model.sdf").read_bytes()).hexdigest() == expected_hash


def test_fetch_rejects_hash_mismatch_before_publishing_assets(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "slug": "synthetic",
                        "source": "Synthetic",
                        "source_url": "https://fuel.gazebosim.org/1.0/OpenRobotics/models/Synthetic",
                        "model_sdf_sha256": "0" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def downloader(_url, _version, cache_root):
        _fake_fuel_asset(cache_root)

    raw_out, resolved_out = tmp_path / "OpenRobotics", tmp_path / "resolved"
    with pytest.raises(AssetFetchError, match="SHA-256 mismatch"):
        fetch_assets(catalog, raw_out, resolved_out, downloader=downloader)
    assert not raw_out.exists()
    assert not resolved_out.exists()


def test_fuel_model_url_uses_spaces_for_multiword_models():
    from scripts.fetch_sim_assets import fuel_model_url

    base = "https://fuel.gazebosim.org/1.0/OpenRobotics/models/"
    assert fuel_model_url(base + "Cardboard_box") == base + "Cardboard%20box"
    assert fuel_model_url(base + "SUV") == base + "SUV"
