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
