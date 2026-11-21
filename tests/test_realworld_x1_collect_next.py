from pathlib import Path

import pytest

from scripts.realworld import x1_collect_next as collect


IMAGE = "sha256:" + "d" * 64
SITE = "rw_canal_amsterdam"
NAME = f"{SITE}_w6km__clear_noon"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "OUTPUT", tmp_path / "nas")
    asset = tmp_path / f"{SITE}_w6km" / "v1"
    for folder in ("worlds", "x1", "models"):
        (asset / folder).mkdir(parents=True)
    paths = {"world": asset / "worlds" / f"{NAME}.sdf",
             "scenario": asset / "x1" / f"{NAME}.scenario.json",
             "sidecar": asset / "x1" / f"{NAME}.long_range.json",
             "map": asset / "x1" / f"{NAME}.geojson",
             "dem": asset / "x1" / f"{NAME}.dem.json"}
    for path in paths.values():
        path.write_text(path.name)
    paths["sidecar"].write_text(collect.batch.dump({
        "route_preflight": {"metrics": {"flight_margin_revision": collect.policy.FLIGHT_MARGIN_REVISION}}}))
    receipt = {"passed": True, "scenario": paths["scenario"].name,
               **{f"{key}_sha256": collect.batch.file_hash(p) for key, p in paths.items()}}
    (asset / "x1/preflight.json").write_text(collect.batch.dump(receipt))
    return tmp_path, paths


def test_site_plan_uses_exclusive_gpu_slots_identity_and_in_repo_overrides(world):
    root, _ = world
    for index, slot in enumerate((5, 6, 7, 5, 6, 7)):
        plan, assets = collect.site_plan(root, SITE, index, IMAGE)
        job = plan["jobs"][0]
        assert plan["gpu"] == 3 and plan["slots"] == [slot] and plan["share_gpu"] is False
        assert plan["start_load_max"] == 60 and job["matrix"]["minimum_rtf"] == 0.9
        assert job["inputs"]["gimbal_bridge_stabilize"] and job["inputs"]["image"] == IMAGE
        assert {folder for _, folder, _ in assets} == {
            Path(job["matrix"][f"variant_{kind}_root"]) for kind in ("world", "map", "terrain")}
        assert all(folder.is_relative_to(collect.ROOT / ".tmp/x1-worlds") for _, folder, _ in assets)
    a, _ = collect.site_plan(root, SITE, 0, IMAGE)
    b, _ = collect.site_plan(root, SITE, 0, "sha256:" + "f" * 64)
    assert a["output_root"] != b["output_root"]


def test_hash_verified_legacy_route_is_never_dispatched(world):
    root, paths = world
    paths["sidecar"].write_text('{}')
    receipt_path = paths["sidecar"].with_name("preflight.json")
    receipt = collect.batch.read_object(receipt_path)
    receipt["sidecar_sha256"] = collect.batch.file_hash(paths["sidecar"])
    receipt_path.write_text(collect.batch.dump(receipt))
    with pytest.raises(ValueError, match="stale X1 route"):
        collect.site_plan(root, SITE, 0, IMAGE)


def test_altered_asset_is_never_dispatched(world):
    root, paths = world
    paths["dem"].write_text("changed after CPU preflight")
    spec = {"asset_root": str(root), "sites": {SITE: "raw"}}
    pending, done, unavailable = collect.plans(spec, IMAGE)
    assert not pending and not done and "dem identity mismatch" in unavailable[SITE]


def test_collection_requires_horizon_rtf_bridge_identity_and_route(world):
    plan, _ = collect.site_plan(world[0], SITE, 0, IMAGE)
    job = plan["jobs"][0]
    record = {"job_id": job["job_id"], "valid_round": True, "accepted": True,
              "image_id": IMAGE, "slot": 5, "gpu": 3, "rtf_window_median": 0.9,
              "max_distance_from_spawn_m": 4000,
              "route_completed": True, "horizon_acceptance": {"passed": True},
              "bridge_stabilize": True, "bridge_stabilize_check": {"passed": True, "env_enabled": True}}
    assert collect.verdict(record, job)
    assert collect.verdict({**record, "gpu": 2, "gpu_index": 2,
                            "gpu_uuid": "GPU-00000002-0000-0000-0000-000000000000",
                            "gpu_reason": "fallback_gpu_idle"}, job)
    for failure in ({"gpu": 2}, {"slot": 4}, {"rtf_window_median": 0.899}, {"route_completed": False},
                    {"horizon_acceptance": {"passed": False}}, {"bridge_stabilize": False},
                    {"max_distance_from_spawn_m": 3999.99}):
        assert not collect.verdict({**record, **failure}, job)


def test_zurich_collection_only_site_is_required_for_completion(world, monkeypatch):
    root, _ = world
    zurich = "rw_city_zurich"
    spec = {"asset_root": str(root), "sites": {SITE: "raw"}, "collection_only_sites": [zurich]}
    base_plan, _ = collect.site_plan(root, SITE, 0, IMAGE, spec)
    monkeypatch.setattr(collect, "accepted", lambda plan: plan == base_plan)
    normal_read = collect.batch.read_object
    monkeypatch.setattr(collect.batch, "read_object",
                        lambda path: spec if path == collect.MANIFEST else normal_read(path))
    monkeypatch.setattr(collect.resume, "mount_check", lambda **kwargs: None)
    monkeypatch.setattr(collect.resume, "image_check", lambda image: IMAGE)
    pending, done, unavailable = collect.plans(spec, IMAGE)
    assert not pending and done == [SITE] and set(unavailable) == {zurich}
    assert collect.main(["complete"]) == 1


@pytest.fixture
def variants(world, monkeypatch):
    root, base_paths = world
    asset = base_paths["world"].parents[1]
    config = root / "x1-variants.json"
    monkeypatch.setattr(collect, "VARIANT_MANIFEST", config)
    queue = {"schema_version": 1, "enabled": True, "sites": [SITE, collect.IOWA_SITE],
             "conditions": [{"time_of_day": "noon", "weather": "overcast"},
                            {"time_of_day": "dusk", "weather": "clear"}]}
    config.write_text(collect.batch.dump(queue))
    for preset in ("overcast", "dusk"):
        variant_dir = asset / "x1-variants" / preset
        paths = {}
        for key, base_path in base_paths.items():
            path = variant_dir / base_path.parent.name / base_path.name.replace("clear_noon", preset)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base_path.read_bytes())
            paths[key] = path
        body = collect.batch.read_object(paths["sidecar"])
        paths["sidecar"].write_text(collect.batch.dump({**body, "minimum_max_distance_m": 4000}))
        receipt = {"passed": True, "scenario": paths["scenario"].name,
                   **{f"{key}_sha256": collect.batch.file_hash(p) for key, p in paths.items()}}
        (paths["sidecar"].parent / "preflight.json").write_text(collect.batch.dump(receipt))
    return {"asset_root": str(root), "sites": {SITE: "raw"}}, config, queue


def test_variants_follow_completed_base_queue_and_use_same_gates(variants, monkeypatch):
    spec, config, queue = variants
    checked_images = []

    def iowa_complete(_spec, *, required_image_id):
        checked_images.append(required_image_id)
        return len(checked_images) > 1

    monkeypatch.setattr(collect.resume, "complete", iowa_complete)
    base, _, _ = collect.queue_plans(spec, IMAGE, {})
    assert [plan["jobs"][0]["weather_preset"] for plan, _ in base] == ["clear_noon"]
    assert not checked_images
    monkeypatch.setattr(collect, "accepted", lambda plan: plan == base[0][0])
    config.write_text(collect.batch.dump({**queue, "enabled": False}))
    assert collect.queue_plans(spec, IMAGE, {}) == ([], [SITE], {})
    assert not checked_images
    config.write_text(collect.batch.dump(queue))
    pending, done, unavailable = collect.queue_plans(spec, IMAGE, {})
    assert not pending and done == [SITE] and set(unavailable) == {collect.IOWA_SITE}
    pending, done, unavailable = collect.queue_plans(spec, IMAGE, {})
    assert checked_images == [IMAGE, IMAGE]
    assert set(unavailable) == {"rw_farm_iowa__overcast", "rw_farm_iowa__dusk"}
    assert len(pending) == 2 and done == [SITE]
    job_ids = {base[0][0]["jobs"][0]["job_id"]}
    for (plan, assets), preset, slot in zip(pending, ("overcast", "dusk"), (6, 7), strict=True):
        job, = plan["jobs"]
        assert job["weather_preset"] == preset and job["slot"] == slot
        assert job["matrix"]["minimum_rtf"] == 0.9
        assert collect.batch.distance_fields(job) == {"scenario_class": "wide_area", "minimum_max_distance_m": 4000}
        assert job["inputs"]["gimbal_bridge_stabilize"] is True
        assert f"{SITE}__{preset}" in plan["output_root"]
        assert all(preset in str(source) for source, _, _ in assets)
        job_ids.add(job["job_id"])
    assert len(job_ids) == 3


def test_variant_probe_requires_all_receipts_and_rejects_asset_substitution(variants, monkeypatch):
    spec, config, queue = variants
    config.write_text(collect.batch.dump({**queue, "sites": [SITE]}))
    normal_read = collect.batch.read_object
    monkeypatch.setattr(collect.batch, "read_object",
                        lambda path: spec if path == collect.MANIFEST else normal_read(path))
    monkeypatch.setattr(collect.resume, "mount_check", lambda **kwargs: None)
    monkeypatch.setattr(collect.resume, "image_check", lambda image: IMAGE)
    monkeypatch.setattr(collect, "accepted", lambda plan: plan["jobs"][0]["weather_preset"] != "dusk")
    assert collect.main(["complete"]) == 1
    monkeypatch.setattr(collect, "accepted", lambda plan: True)
    assert collect.main(["complete"]) == 0
    assets = Path(spec["asset_root"]) / f"{SITE}_w6km/v1/x1-variants/dusk/worlds"
    (assets / f"{SITE}_w6km__dusk.sdf").write_text("wrong SDF")
    assert collect.main(["complete"]) == 1
