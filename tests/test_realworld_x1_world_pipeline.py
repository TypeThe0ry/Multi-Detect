from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.realworld import x1_world_pipeline as pipeline


@pytest.fixture
def asset(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "ASSET_ROOT", tmp_path / "assets")
    monkeypatch.setattr(pipeline, "LOCK", tmp_path / "x1.lock")
    return pipeline.Asset(pipeline.SITES[0])


def stage_raw(asset):
    asset.raw.mkdir(parents=True)
    paths = {"osm.json", "dem.npy", "core.png"} | {f"naip_{i}_{j}.png" for i in range(6) for j in range(6)}
    for path in paths:
        (asset.raw / path).write_bytes(b"raw")
    np = pytest.importorskip("numpy")
    np.save(asset.raw / "dem.npy", np.full((5, 5), 500.0))
    pipeline.atomic_json(asset.raw / "provenance.json", {
        "site": asset.name, "size_m": 6000,
        **{key: asset.site[key] for key in ("lat", "lon", "provider")},
        "downloads": [{"path": path, "bytes": (asset.raw / path).stat().st_size} for path in sorted(paths)],
    })


def stage_build(asset):
    pipeline.atomic_json(asset.scene, {"site": asset.name, "size_m": 6000, "elevation": 500, "raw_dir": str(asset.raw),
                                      **{key: asset.site[key] for key in ("lat", "lon", "kind")},
                                      "objects": [], "routes": [], "footprints": []})
    asset.terrain.parent.mkdir(parents=True)
    (asset.terrain.parent / "terrain.obj").write_text("v 0 0 0\n")
    asset.terrain.write_text(f'<sdf><model name="{asset.name}"><link name="terrain"><visual name="v">'
                             f'<geometry><mesh><uri>model://{asset.name}/terrain.obj</uri></mesh></geometry>'
                             '</visual></link></model></sdf>')
    pipeline.atomic_json(asset.state / "build.json", asset.build_pin(asset.fetch_provenance()))


def stage_route(asset):
    root = asset.root / "x1"
    channels = {}
    for key in ("runtime_input", "truth", "expectations"):
        path = root / f"{asset.world.stem}.{key}.json"
        pipeline.atomic_json(path, {})
        channels[key] = {"path": path.name, "sha256": hashlib.sha256(b"{}").hexdigest()}
    names = {"scenario": "scenario.json", "sidecar": "long_range.json", "map": "geojson", "dem": "dem.json"}
    receipt = {"passed": True, "world_sha256": pipeline.digest(asset.world)}
    policy = pipeline.scenario_policy
    scenario_class = policy.configured_class(asset.world.stem, pipeline.CONFIG)
    route_policy = {"scenario_class": scenario_class, "threshold_m": policy.distance_gate(asset.world.stem, scenario_class)}
    if scenario_class == policy.MOUNTAIN_SHORT:
        route_policy["wide_area_unreachable"] = {
            "threshold_m": 4000, "maximum_possible_distance_m": {"30": 3598, "40": 3584}}
    receipt.update(route_policy)
    for kind, suffix in names.items():
        path = root / f"{asset.world.stem}.{suffix}"
        body = {"channels": channels} if kind == "scenario" else {}
        if kind == "sidecar":
            body.update(route_policy, minimum_max_distance_m=route_policy["threshold_m"],
                        route_preflight={"metrics": {"flight_margin_revision": policy.FLIGHT_MARGIN_REVISION}})
        pipeline.atomic_json(path, body)
        receipt[f"{kind}_sha256"] = pipeline.digest(path)
        if kind == "scenario":
            receipt[kind] = path.name
    pipeline.atomic_json(root / "preflight.json", receipt)


def test_same_device_bind_mount_counts(monkeypatch, tmp_path):
    target = tmp_path / "inclusive-bind"
    target.mkdir()
    monkeypatch.setattr(pipeline, "DATA", tmp_path)
    mount = {"target": str(target), "options": "rw,nosuid", "fstype": "ext4"}
    monkeypatch.setattr(pipeline, "command", lambda _: json.dumps({"filesystems": [mount]}))
    pipeline.check_mount(target)
    mount["options"] = "ro"
    with pytest.raises(pipeline.Pending, match="writable"):
        pipeline.check_mount(target)


def test_pad_retry_replays_after_journal_commit(monkeypatch, tmp_path, asset):
    monkeypatch.setattr(pipeline, "NAS", tmp_path / "nas")
    asset.bind()
    stage_raw(asset)
    first_site = asset.site.copy()
    picked = {"lat": first_site["lat"] + 0.002, "lon": first_site["lon"]}
    phases = []

    def guarded(argv, env, log, report, *, requested):
        if "--raw-root" in argv:
            assert argv[-3:] == ["--origin", str(picked["lat"]), str(picked["lon"])]
            stage_raw(asset)
            phases.append("fetch")
        elif "--world" in argv:
            phases.append("route")
        else:
            phase = argv[-1]
            phases.append(phase)
            if phase == "check-6km" and asset.site == first_site:
                raise subprocess.CalledProcessError(pipeline.PAD_REJECTED, argv)
            if phase == "find-pad":
                pipeline.atomic_json(asset.state / "next-pad.json", picked)

    monkeypatch.setattr(pipeline, "guarded", guarded)
    monkeypatch.setattr(asset, "receipt", lambda: {"world_sha256": "hash"})
    monkeypatch.setattr(asset, "route_receipt", lambda _: None)
    install = asset.install_pad

    def interrupt(attempts):
        if attempts:
            raise pipeline.Yielded("interrupted after decision")
        install(attempts)

    with (tmp_path / "log").open("w") as log:
        progress = pipeline.Progress([asset.name], log)
        monkeypatch.setattr(asset, "install_pad", interrupt)
        with pytest.raises(pipeline.Yielded):
            pipeline.generate(asset, progress)
        assert pipeline.read_json(asset.state / "input.json") == asset.pin(first_site)
        monkeypatch.setattr(asset, "install_pad", install)
        pipeline.generate(asset, progress)
    assert phases == ["check-2km", "check-6km", "find-pad", "fetch", "check-6km", "build", "make", "route"]
    assert (asset.root.with_name("v1-rejected-pad-01") / ".pipeline/input.json").is_file()
    assert pipeline.read_json(asset.state / "input.json")["config"]["lat"] == picked["lat"]
    assert len(asset.load_pad_history()) == 1


def test_only_pad_rejections_spend_retry_budget(monkeypatch, tmp_path, asset):
    monkeypatch.setattr(pipeline, "NAS", tmp_path / "nas")
    monkeypatch.setitem(pipeline.CONFIG, "max_pad_retries", 2)
    scans = []

    def scan(active, _progress, phase):
        assert phase == "find-pad"
        scans.append(active.site["lat"])
        pipeline.atomic_json(active.state / "next-pad.json", {"lat": active.site["lat"] + 0.002, "lon": active.site["lon"]})

    def reject(*_, **__):
        raise pipeline.PadRejected("route rejected launch pad")

    def other_failure(*_, **__):
        raise subprocess.CalledProcessError(1, ["route"])

    monkeypatch.setattr(pipeline, "run_guarded_phase", scan)
    with (tmp_path / "log").open("w") as log:
        progress = pipeline.Progress([asset.name], log)
        monkeypatch.setattr(pipeline, "generate_at_origin", other_failure)
        with pytest.raises(subprocess.CalledProcessError):
            pipeline.generate(asset, progress)
        assert not scans and not asset.pad_journal.exists()
        monkeypatch.setattr(pipeline, "generate_at_origin", reject)
        for _ in range(2):
            with pytest.raises(ValueError, match="exhausted 2 pad retries"):
                pipeline.generate(asset, progress)
        assert len(scans) == len(asset.load_pad_history()) == 2


def test_legacy_retarget_only_migrates_known_unaccepted_site(asset):
    previous = dict(asset.site)
    previous.pop("origin_moved_for_pad")
    asset.bind()
    pipeline.atomic_json(asset.state / "input.json", asset.pin(previous))
    route = asset.root / "x1/preflight.json"
    pipeline.atomic_json(route, {"passed": True})
    with pytest.raises(ValueError, match="inputs changed"):
        asset.install_pad([])
    route.unlink()
    asset.install_pad([])
    assert pipeline.read_json(asset.state / "input.json") == asset.pin()
    assert (asset.root.with_name("v1-before-pad-retarget") / ".pipeline/input.json").is_file()


def test_check_6km_rejects_missing_dem_but_accepts_measured_zero_cells(tmp_path):
    from scripts.realworld.x1_world_origin import validate_dem

    np = pytest.importorskip("numpy")
    path = tmp_path / "dem.npy"
    dem = np.zeros((5, 5))
    np.save(path, dem)
    with pytest.raises(ValueError, match="all zero"):
        validate_dem(path)
    dem[0, 0] = np.nan
    np.save(path, dem)
    with pytest.raises(ValueError, match="uncovered"):
        validate_dem(path)
    dem[0, 0] = 1.2
    np.save(path, dem)
    validate_dem(path)


def test_find_pad_rejects_entirely_zero_dem_and_respects_clearance(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.realworld import x1_find_pad

    np = pytest.importorskip("numpy")
    monkeypatch.setattr(x1_find_pad, "Osm", lambda *_: SimpleNamespace(
        rel_polys=[], ways=[{"closed": True, "pts": [(-20, -20), (20, -20), (20, 20), (-20, 20), (-20, -20)],
                             "tags": {"building": "yes"}}]))
    pipeline.atomic_json(tmp_path / "provenance.json", {
        "size_m": 300, "proj4": "+proj=tmerc +lat_0=50 +lon_0=4 +datum=WGS84 +units=m",
    })
    dem = tmp_path / "dem.npy"
    np.save(dem, np.zeros((151, 151)))
    with pytest.raises(ValueError, match="all-zero DEM"):
        x1_find_pad.candidates(tmp_path, radius=100, step=25)
    np.save(dem, np.ones((151, 151)))
    rows = x1_find_pad.candidates(tmp_path, radius=100, step=25, min_building=60)
    assert rows and all(row["building_m"] >= 60 and row["offset_m"] > 60 for row in rows)


def test_launch_failures_consume_only_dispatch_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "LOCK", tmp_path / "lock")

    def blocked(_):
        raise pipeline.Pending("example prerequisite")

    monkeypatch.setattr(pipeline, "host_preflight", blocked)
    assert pipeline.main(["preflight"]) == 11
    assert pipeline.main(["launch"]) == 75
    monkeypatch.setattr(pipeline, "host_preflight", lambda _: None)

    def dispatch(argv):
        assert argv[0] == "systemd-run" and pipeline.UNIT in argv
        raise subprocess.CalledProcessError(1, argv, stderr="dispatch failed")

    monkeypatch.setattr(pipeline, "command", dispatch)
    assert pipeline.main(["launch"]) == 2


@pytest.mark.parametrize("abort", [False, True])
def test_in_flight_process_group_pause_resume_or_kill(monkeypatch, tmp_path, abort):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.01)
    child_ready = tmp_path / "child-ready"
    parent_script = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1.5)'])
open(sys.argv[1], 'w').write(str(child.pid))
child.wait()
"""
    checks = 0
    signals = []
    killpg = os.killpg

    def observe(pid, signo):
        signals.append(signo)
        killpg(pid, signo)

    def disk():
        nonlocal checks
        if child_ready.is_file() and child_ready.stat().st_size:
            checks += 1
            if checks == 3:
                status = Path(f"/proc/{child_ready.read_text()}/status").read_text()
                assert "\nState:\tT" in status
                if abort:
                    return 52 * pipeline.GIB
            if checks <= 3:
                return 69 * pipeline.GIB
        return 80 * pipeline.GIB

    monkeypatch.setattr(pipeline.os, "killpg", observe)
    monkeypatch.setattr(pipeline, "free_bytes", disk)
    states = []
    with (tmp_path / "log").open("w") as log:
        argv = [sys.executable, "-c", parent_script, str(child_ready)]
        progress = lambda state, free: states.append(state)
        if abort:
            with pytest.raises(pipeline.HardAbort):
                pipeline.guarded(argv, None, log, progress)
            assert states == ["running", "paused_disk"]
            assert signals == [signal.SIGSTOP, signal.SIGKILL]
        else:
            pipeline.guarded(argv, None, log, progress)
            assert states == ["running", "paused_disk", "running"]
            assert signals == [signal.SIGSTOP, signal.SIGCONT, signal.SIGKILL]


def test_make_accepts_gazebo_default_world_and_route_retry(monkeypatch, tmp_path, asset):
    monkeypatch.setenv("PYTHONPATH", str(pipeline.ROOT))
    monkeypatch.setenv("RW_SCENE_ROOT", str(asset.scene.parent.parent))
    monkeypatch.setenv("RW_RESOLVED_ROOT", str(asset.models))
    monkeypatch.setattr(pipeline, "NAS", tmp_path / "nas")
    if not os.access(pipeline.CONFIG["tools_python"], os.X_OK):
        pytest.skip("workstation asset tools are unavailable")
    monkeypatch.setattr(pipeline, "TOOLS", pipeline.CONFIG["tools_python"])
    seed = pipeline.ASSET_ROOT / pipeline.CONFIG["model_seed"]
    for relative in ("rw_sky/noon.jpg", "rw_sky/dome.obj", "rw_actor_walk/meshes/walk.dae", "rw_movers/meshes/rc_boat.dae"):
        path = seed / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed")
    monkeypatch.syspath_prepend(str(pipeline.HERE))
    asset.bind()
    stage_raw(asset)
    stage_build(asset)
    pipeline.run_phase(asset, "make")
    world_hash = asset.receipt()["world_sha256"]
    assert '<world name="default">' in asset.world.read_text()
    phases = []

    def run(argv, _env, _log, report, *, requested):
        report("running", 80 * pipeline.GIB)
        assert argv[-4:] == ["--asset", str(asset.root), "--world", str(asset.world)]
        phases.append("route")
        if len(phases) == 1:
            raise subprocess.CalledProcessError(1, argv)
        stage_route(asset)

    monkeypatch.setattr(pipeline, "guarded", run)
    with (tmp_path / "log").open("w") as log:
        progress = pipeline.Progress([asset.name], log)
        with pytest.raises(subprocess.CalledProcessError):
            pipeline.generate(asset, progress)
        assert asset.receipt()["world_sha256"] == world_hash
        pipeline.generate(asset, progress)
        pipeline.generate(asset, progress)
        assert phases == ["route", "route"]
        assert asset.route_receipt(world_hash)["passed"]
        assert pipeline.read_json(pipeline.NAS / "status.json")["state"] == "ready"
    (asset.root / "x1" / f"{asset.world.stem}.truth.json").write_text('{"changed": true}')
    with pytest.raises(ValueError, match="scenario channel changed"):
        asset.route_receipt(world_hash)


def test_3dbag_interruption_reuses_complete_pages(monkeypatch, tmp_path):
    from scripts.realworld import fetch_site

    monkeypatch.setattr(fetch_site, "pdok_rd_bbox", lambda *_: (0, 0, 6000, 6000))
    second = "https://example.test/second-page"
    calls = []

    def page(identifier, following=None):
        attrs = {"b3_h_nok": 10, "b3_h_maaiveld": 0, "b3_pw_datum": 2024}
        return {"features": [{"CityObjects": {identifier: {"attributes": attrs}}}],
                "links": [{"rel": "next", "href": following}] if following else []}

    class Response:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def interrupted(url, **_):
        calls.append(url)
        if url == second:
            raise InterruptedError
        return Response(page("first", second))

    monkeypatch.setattr(fetch_site, "get_retry", interrupted)
    with pytest.raises(InterruptedError):
        fetch_site.fetch_3dbag(tmp_path, 52.357, 4.882, 3000)
    assert not (tmp_path / "3dbag.json").exists()
    assert len(calls) == 2
    calls.clear()

    def resumed(url, **_):
        calls.append(url)
        return Response(page("second"))

    monkeypatch.setattr(fetch_site, "get_retry", resumed)
    assert fetch_site.fetch_3dbag(tmp_path, 52.357, 4.882, 3000) == [2024]
    assert calls == [second]
    rows = pipeline.read_json(tmp_path / "3dbag.json")["features"]
    assert [next(iter(row["CityObjects"])) for row in rows] == ["first", "second"]
    assert not (tmp_path / ".3dbag-pages").exists()


@pytest.mark.parametrize("preempt", [True, False])
def test_worker_yields_to_flight_and_replays_only_missing_phases(monkeypatch, tmp_path, asset, preempt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from scripts.realworld import x1_lock

    monkeypatch.setattr(pipeline, "NAS", tmp_path / "nas")
    pipeline.NAS.mkdir()
    monkeypatch.setattr(pipeline, "check_mount", lambda *_, **__: None)
    monkeypatch.setattr(pipeline, "host_preflight", lambda *_, **__: None)
    monkeypatch.setattr(pipeline, "free_bytes", lambda: 80 * pipeline.GIB)
    monkeypatch.setattr(pipeline, "SITES", (asset.name,))
    state = tmp_path / "keepalive"
    state.mkdir()
    attempts = state / "attempts"
    attempts.write_text("3\n")
    started, acquired, finish_flight = Event(), Event(), Event()
    phases = []

    def fly():
        assert started.wait(5)
        with x1_lock.flight_lock(pipeline.LOCK, wait=5):
            acquired.set()
            assert finish_flight.wait(5)

    def phase(argv, env, _log, report, *, requested):
        if "MD_X1_FETCH_LOCK" in env:
            name = "fetch"
        elif "--asset" in argv:
            name = "route"
        else:
            name = argv[-1]
        phases.append(name)
        report("running", 80 * pipeline.GIB)
        if name != "fetch":
            return
        stage_raw(asset)
        if preempt:
            started.set()
            for _ in range(500):
                if requested():
                    break
                # Let the other thread publish its request while this phase owns flock.
                time.sleep(0.01)
            assert requested()
            assert not acquired.is_set()

    monkeypatch.setattr(pipeline, "guarded", phase)
    monkeypatch.setattr(asset, "receipt", lambda: {"world_sha256": "digest"})
    monkeypatch.setattr(asset, "route_receipt", lambda _: None)
    if preempt:
        with ThreadPoolExecutor(max_workers=1) as pool:
            flight = pool.submit(fly)
            try:
                assert pipeline.worker([asset], state) == 0
                assert acquired.wait(5)
                status = pipeline.read_json(pipeline.NAS / "status.json")
                assert status["state"] == "yielded"
                assert phases == ["check-2km", "fetch"]
                assert pipeline.main(["complete"]) == 1
                assert pipeline.main(["keepalive-preflight", "--keepalive-state", str(state)]) == 11
                assert attempts.read_text() == "2\n"
                assert pipeline.main(["keepalive-preflight", "--keepalive-state", str(state)]) == 11
                assert attempts.read_text() == "2\n"
                with pytest.raises(x1_lock.Yielded):
                    with x1_lock.world_phase(pipeline.LOCK):
                        pytest.fail("world phase ran during flight")
            finally:
                finish_flight.set()
            flight.result(timeout=5)
        assert x1_lock.sidecar(pipeline.LOCK, "flight-request").is_file()
        assert not x1_lock.flight_requested(pipeline.LOCK)
    assert pipeline.worker([asset]) == 0
    prefix = ["check-2km", "fetch"] if preempt else []
    resumed = ["check-2km"] + ([] if preempt else ["fetch"])
    assert phases[:len(prefix) + len(resumed)] == prefix + resumed
    assert phases.count("fetch") == 1
    assert phases[-4:-1] == ["check-6km", "build", "make"]
    assert pipeline.read_json(pipeline.NAS / "status.json")["state"] == "complete"


def test_flight_timeout_drops_request(tmp_path):
    from scripts.realworld import x1_lock

    lock = tmp_path / "x1.lock"
    with x1_lock.world_owner(lock), x1_lock.world_phase(lock):
        with pytest.raises(x1_lock.LockBusy, match="timed out"):
            with x1_lock.flight_lock(lock, wait=0):
                pytest.fail("flight got a held world lock")
        assert not x1_lock.flight_requested(lock)
    with x1_lock.flight_lock(lock):
        with pytest.raises(x1_lock.LockBusy, match="X1 lock held"):
            with x1_lock.flight_lock(lock, wait=0):
                pytest.fail("two flights got the lock")


@pytest.mark.parametrize("yield_to_flight", [False, True])
def test_worker_preserves_site_failure_but_refunds_preemption(monkeypatch, tmp_path, yield_to_flight):
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "LOCK", tmp_path / "x1.lock")
    monkeypatch.setattr(pipeline, "NAS", tmp_path / "nas")
    pipeline.NAS.mkdir()
    monkeypatch.setattr(pipeline, "check_mount", lambda *_, **__: None)
    monkeypatch.setattr(pipeline, "host_preflight", lambda *_, **__: None)
    monkeypatch.setattr(pipeline, "free_bytes", lambda: 80 * pipeline.GIB)
    state = tmp_path / "keepalive"
    state.mkdir()
    attempts = state / "attempts"
    attempts.write_text("3\n")

    def generate(site, _progress):
        if site.name == "first":
            raise ValueError("invalid route")
        if yield_to_flight:
            raise pipeline.Yielded("flight requested the X1 lock")

    monkeypatch.setattr(pipeline, "generate", generate)
    assets = [SimpleNamespace(name=name) for name in ("first", "second")]
    assert pipeline.worker(assets, state) == (0 if yield_to_flight else 1)
    status = pipeline.read_json(pipeline.NAS / "status.json")
    assert status["state"] == ("yielded" if yield_to_flight else "failed")
    assert status["sites"]["first"]["state"] == "failed"
    assert status["sites"]["first"]["reason"] == "invalid route"
    assert attempts.read_text() == "3\n"
    assert pipeline.reconcile_keepalive_yield(state) is yield_to_flight
    assert not pipeline.reconcile_keepalive_yield(state)
    assert attempts.read_text() == ("2\n" if yield_to_flight else "3\n")


def test_stalled_stage_reaped_before_yield(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "free_bytes", lambda: 80 * pipeline.GIB)
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(pipeline, "YIELD_GRACE_SECONDS", 0)
    phases_started = []
    spawn = subprocess.Popen

    def start(*args, **kwargs):
        child = spawn(*args, **kwargs)
        phases_started.append(child)
        return child

    monkeypatch.setattr(pipeline.subprocess, "Popen", start)
    with (tmp_path / "log").open("w") as log:
        with pytest.raises(pipeline.Yielded):
            pipeline.guarded([sys.executable, "-c", "import time; time.sleep(30)"], None, log,
                             lambda *_: None, requested=lambda: bool(phases_started))
    assert phases_started[0].returncode == -signal.SIGKILL


@pytest.mark.parametrize("entry", ["resume", "collect", "eval-lane"])
def test_flight_entry_requests_world_handoff(monkeypatch, tmp_path, entry):
    from concurrent.futures import ThreadPoolExecutor

    from scripts.realworld import x1_collect_next as collect
    from scripts.realworld import x1_lock, x1_resume as resume

    lock = tmp_path / "x1.lock"
    monkeypatch.setattr(resume, "LOCK", lock)
    monkeypatch.setattr(resume, "complete", lambda _: False)
    monkeypatch.setattr(resume, "preflight", lambda _: ("fake-image", {}))
    monkeypatch.setattr(collect, "preflight", lambda *_, **__: ("fake-image", ["pending"], {}))
    monkeypatch.setattr(collect, "eval_config", lambda **_: {"gimbal_eval": True})

    def ran(*_, **__):
        with pytest.raises(x1_lock.Yielded):
            with x1_lock.world_phase(lock):
                pytest.fail("world phase ran while the flight worker held the lock")
        return 0

    monkeypatch.setattr(resume, "run", ran)
    monkeypatch.setattr(collect, "run", ran)
    with ThreadPoolExecutor(max_workers=1) as pool, x1_lock.world_owner(lock):
        with x1_lock.world_phase(lock):
            flags = ["--gimbal-eval", "--lane", "a"] if entry == "eval-lane" else []
            future = pool.submit((resume if entry == "resume" else collect).main,
                                 [*flags, "run", "--image-id", "fake-image"])
            deadline = time.monotonic() + 5
            while not x1_lock.flight_requested(lock) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert x1_lock.flight_requested(lock)
            assert not future.done()
        assert future.result(timeout=5) == 0
