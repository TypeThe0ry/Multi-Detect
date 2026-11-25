from types import SimpleNamespace

import pytest

from scripts.realworld import x1_repeat_eval as repeat

ORIGINAL = '{\n  "schema_version": 1,\n  "enabled": true,\n  "gimbal_eval": true\n}\n'
SITES = ["rw_farm_iowa__clear_noon", "rw_city_berlin__clear_noon"]


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


READY = result(
    stdout="ready: 21 world(s), GPU0 GPU-00000000-0000-0000-0000-000000000000, slot 5\n")
NOT_READY = result(
    returncode=75,
    stderr="x1-collect-next: no pending offline-qualified worlds (21 accepted)\n")
LOCKED = result(
    returncode=75,
    stderr="x1-collect-next: timed out after 120s waiting for X1 lock: "
           "/run/lock/md-x1-resume.lock\n")


class Clock:
    def __init__(self, step=60.0):
        self.value = 0.0
        self.step = step
        self.slept = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.value += seconds


class Runner:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.handler(args)

    def actions(self, action):
        return [call for call in self.calls if call[-1] == action]


class Lock:
    def __init__(self, busy_calls=0):
        self.busy_calls = busy_calls
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls <= self.busy_calls


def make_driver(tmp_path, *, handler=None, rounds=1, lock=None, clock=None,
                active=(), markers=None, dry_run=False, log=None, lock_timeout=0.0,
                unit_timeout=0.0, tags=("t1", "t2")):
    config = tmp_path / "configs/simulation/x1-gimbal-eval.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(ORIGINAL, encoding="utf-8")
    if markers is None:
        markers = [tmp_path / "markers/allow-training-share",
                   tmp_path / "markers/allow-fluidx3d-share",
                   tmp_path / "markers/allow-sunshine-compute"]
    runner = Runner(handler or (lambda args: READY if args[-1] == "preflight" else result()))
    clock = clock or Clock()
    return repeat.Driver(
        tags=list(tags), sites=SITES,
        config_path=config, collect=tmp_path / "scripts/realworld/x1_collect_next.py",
        markers=markers, rounds=rounds, poll_seconds=clock.step,
        lock=tmp_path / "md-x1-resume.lock", lock_timeout=lock_timeout,
        unit_timeout=unit_timeout, run=runner, sleep=clock.sleep,
        monotonic=clock.monotonic, lock_busy=lock or (lambda: False),
        unit_active=lambda lane: lane in active, log=log or (lambda message: None),
        dry_run=dry_run,
    ), runner, clock, config


def markers_of(tmp_path):
    return [tmp_path / "markers/allow-training-share",
            tmp_path / "markers/allow-fluidx3d-share",
            tmp_path / "markers/allow-sunshine-compute"]


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_config_and_markers_are_restored_when_a_lane_raises(tmp_path, error):
    def handler(_args):
        raise error("lane failed")

    preexisting = tmp_path / "markers/allow-fluidx3d-share"
    preexisting.parent.mkdir(parents=True)
    preexisting.write_text("shared by another tenant")
    driver, _, _, config = make_driver(tmp_path, handler=handler, markers=markers_of(tmp_path))

    with pytest.raises(error):
        driver.execute()

    assert config.read_text(encoding="utf-8") == ORIGINAL
    assert preexisting.read_text() == "shared by another tenant"
    assert not (tmp_path / "markers/allow-training-share").exists()
    assert not (tmp_path / "markers/allow-sunshine-compute").exists()


def test_waits_for_lock_before_launching_and_keeps_both_lanes(tmp_path):
    clock = Clock()
    launches_at = []

    def handler(args):
        if args[-1] == "launch":
            launches_at.append(len(clock.slept))
            return result()
        return READY

    driver, runner, _, _ = make_driver(
        tmp_path, handler=handler, clock=clock, lock=Lock(busy_calls=3), tags=["t1"])

    driver.execute()

    assert len(runner.actions("preflight")) == 2
    assert len(runner.actions("launch")) == 2
    assert clock.slept == [60.0, 60.0]
    assert launches_at == [2, 2]


def test_lock_timeout_restores_config_and_markers(tmp_path):
    clock = Clock()
    driver, _, _, config = make_driver(
        tmp_path, clock=clock, lock=Lock(busy_calls=10**6), lock_timeout=120.0,
        markers=markers_of(tmp_path))

    with pytest.raises(repeat.LockTimeout):
        driver.execute()

    assert clock.slept == [60.0, 60.0]
    assert config.read_text(encoding="utf-8") == ORIGINAL
    assert not any(path.exists() for path in markers_of(tmp_path))


def test_marker_cleanup_removes_only_markers_created_by_this_run(tmp_path):
    preexisting = tmp_path / "markers/allow-training-share"
    preexisting.parent.mkdir(parents=True)
    preexisting.write_text("existing")
    driver, _, _, _ = make_driver(
        tmp_path, handler=lambda _args: NOT_READY, markers=markers_of(tmp_path), tags=["t1"])

    assert driver.execute() == 0

    assert preexisting.read_text() == "existing"
    assert not (tmp_path / "markers/allow-fluidx3d-share").exists()
    assert not (tmp_path / "markers/allow-sunshine-compute").exists()


def test_rounds_are_bounded_and_pending_lanes_are_retried(tmp_path):
    driver, runner, _, config = make_driver(tmp_path, rounds=3, tags=["t1"])

    assert driver.execute() == 1

    assert len(runner.actions("preflight")) == 6
    assert len(runner.actions("launch")) == 6
    assert config.read_text(encoding="utf-8") == ORIGINAL


def test_stops_after_one_round_when_no_lane_reports_ready(tmp_path):
    driver, runner, _, config = make_driver(
        tmp_path, handler=lambda _args: NOT_READY, rounds=5, tags=["t1"])

    assert driver.execute() == 0

    assert len(runner.actions("preflight")) == 2
    assert not runner.actions("launch")
    assert config.read_text(encoding="utf-8") == ORIGINAL


def test_active_lane_is_waited_on_without_relaunching_it(tmp_path):
    clock = Clock()
    calls = {"a": 0}

    def unit_active(lane):
        if lane != "a":
            return False
        calls["a"] += 1
        return calls["a"] <= 3

    driver, runner, _, _ = make_driver(tmp_path, rounds=1, clock=clock, tags=["t1"])
    driver.unit_active = unit_active

    driver.execute()

    assert clock.slept == [60.0]
    assert not [call for call in runner.actions("preflight") if call[-2] == "a"]


def test_dry_run_prints_plan_without_touching_config_markers_or_lanes(tmp_path):
    lines = []
    driver, runner, clock, config = make_driver(
        tmp_path, handler=lambda _args: pytest.fail("dry-run ran a lane"), dry_run=True,
        log=lines.append)

    assert driver.execute() == 0

    assert not runner.calls and not clock.slept
    assert config.read_text(encoding="utf-8") == ORIGINAL
    assert not any(path.exists() for path in markers_of(tmp_path))
    plan = "\n".join(lines)
    assert "tag t1 -> config tag t1" in plan
    assert "preflight" in plan and "launch" in plan
    assert str(config) in plan


def test_result_classification_distinguishes_ready_not_ready_and_lock():
    assert repeat.ready(READY)
    assert not repeat.ready(NOT_READY)
    assert not repeat.ready(LOCKED)
    assert repeat.lock_conflict(LOCKED)
    assert not repeat.lock_conflict(NOT_READY)


def test_build_config_merges_extra_keys_sites_and_tag_prefix():
    config = repeat.build_config(
        "s1", SITES, image="img:test", fw_legs=False, tag_prefix="fw-legs-short-",
        extra={"fw_min_leg_m": 50})

    assert config == {
        "schema_version": 1, "enabled": True, "gimbal_eval": True, "fw_legs": False,
        "image": "img:test", "fw_min_leg_m": 50, "tag": "fw-legs-short-s1", "only": SITES,
    }
    assert repeat.extra_mapping(["fw_min_leg_m=50", "note=plain"]) == {
        "fw_min_leg_m": 50, "note": "plain"}
    with pytest.raises(ValueError):
        repeat.parse_extra("missing-equals")
    with pytest.raises(ValueError):
        repeat.build_config("", SITES)


def test_waits_before_replacing_config_when_x1_lock_is_held(tmp_path):
    clock = Clock()
    observed = []
    config = tmp_path / "configs/simulation/x1-gimbal-eval.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(ORIGINAL, encoding="utf-8")

    def lock_busy():
        observed.append(config.read_text(encoding="utf-8"))
        return len(observed) <= 2

    driver, runner, _, config = make_driver(
        tmp_path, tags=["t1"], clock=clock, lock=lock_busy)

    driver.execute()

    assert observed[0] == ORIGINAL
    assert clock.slept == [60.0]
    assert len(runner.actions("launch")) == 2
    assert config.read_text(encoding="utf-8") == ORIGINAL


def test_partial_marker_setup_failure_cleans_up_created_markers(tmp_path, monkeypatch):
    first = tmp_path / "markers/first"
    second = tmp_path / "markers/second"
    original_touch = type(first).touch

    def fail_second(path, *args, **kwargs):
        if path == second:
            raise OSError("marker filesystem unavailable")
        return original_touch(path, *args, **kwargs)

    monkeypatch.setattr(type(first), "touch", fail_second)
    guard = repeat.MarkerGuard([first, second])

    with pytest.raises(OSError, match="marker filesystem unavailable"):
        guard.__enter__()

    assert not first.exists()
    assert not second.exists()
