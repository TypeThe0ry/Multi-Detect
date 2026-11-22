import errno
import fcntl
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from scripts.realworld import collection_guard as guard
from scripts.realworld import x1_gpu


def gpu_uuid(index):
    return f"GPU-{index:08d}-0000-0000-0000-000000000000"


@pytest.fixture
def host(tmp_path, monkeypatch):
    live = {i: {"uuid": gpu_uuid(i), "used_mib": 128, "util_pct": 0, "processes": []}
            for i in x1_gpu.ORDER}
    training = {}
    compute = []
    registry = tmp_path / "registry.tsv"
    registry.write_text("cx-altair-rejectdata\tcodex-lane\t-\t-\t-\tAltair\tyes\t1\n"
                        "train-fixed\ttrain-unit\ttraining_unit train-fixed.service\t-\t-\tP\tno\t0\n")
    monkeypatch.setattr(x1_gpu, "KEEPALIVE", tmp_path)
    monkeypatch.setattr(x1_gpu, "CGROUP_ROOT", tmp_path / "cgroups")
    monkeypatch.setattr(x1_gpu, "GPU_LOCK_ROOT", tmp_path / "gpu-locks")
    monkeypatch.delenv("RTF_MATRIX_GPU_LOCK_ROOT", raising=False)
    monkeypatch.setattr(x1_gpu, "SUNSHINE_COMPUTE_MARKER", tmp_path / "allow-sunshine")

    def read(args, *, timeout):
        if args[:2] == ["systemctl", "list-units"]:
            assert "train-fixed.service" in args
            return "\n".join(training)
        if args[:2] == ["systemctl", "show"]:
            return training[args[-1]].get(args[3], "")
        if args[:2] == ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid"]:
            return "\n".join(compute)
        assert args[:2] == ["nvidia-smi", "-i"]
        assert args[3:] == ["-q", "-x"]
        card = live[int(args[2])]
        xml = ET.Element("nvidia_smi_log")
        gpu = ET.SubElement(xml, "gpu")
        ET.SubElement(gpu, "uuid").text = card["uuid"]
        fb = ET.SubElement(gpu, "fb_memory_usage")
        ET.SubElement(fb, "used").text = f"{card['used_mib']} MiB"
        util = ET.SubElement(gpu, "utilization")
        ET.SubElement(util, "gpu_util").text = f"{card['util_pct']} %"
        if card["processes"] is None:
            return ET.tostring(xml, encoding="unicode")
        processes = ET.SubElement(gpu, "processes")
        if not card["processes"]:
            processes.text = "None"
        for pid, kind, name, memory in card["processes"]:
            process = ET.SubElement(processes, "process_info")
            ET.SubElement(process, "pid").text = str(pid)
            ET.SubElement(process, "type").text = kind
            ET.SubElement(process, "process_name").text = name
            ET.SubElement(process, "used_memory").text = f"{memory} MiB"
        return ET.tostring(xml, encoding="unicode")

    monkeypatch.setattr(x1_gpu, "run_read", read)
    return SimpleNamespace(live=live, training=training, compute=compute,
                           status=tmp_path / "GPU-STATUS.json")


def test_prefers_gpu0_when_idle(host):
    assert x1_gpu.select(host.status) == {"gpu_index": 0, "gpu_uuid": gpu_uuid(0),
                                         "gpu_reason": "primary_gpu_idle", "gpu_shared_processes": []}
    assert json.loads(host.status.read_text())["blocked_gpus"] == {}


@pytest.mark.parametrize("training_on_1,expected", [(False, 1), (True, 2)])
def test_prefers_idle_over_shared_and_skips_training_gpu(host, training_on_1, expected):
    host.live[0]["processes"] = [(123, "G", "/opt/google/chrome/chrome", 96)]
    if training_on_1:
        host.training["md-sched-example.service"] = {"Environment": f"SCHED_GPU={gpu_uuid(1)}"}
    assert x1_gpu.select(host.status) == {"gpu_index": expected, "gpu_uuid": gpu_uuid(expected),
                                         "gpu_reason": "fallback_gpu_idle" if training_on_1 else "primary_gpu_idle",
                                         "gpu_shared_processes": []}
    blocked = json.loads(host.status.read_text())["blocked_gpus"]
    assert blocked == ({"1": ["active_training_unit"]} if training_on_1 else {})


def test_shared_desktop_with_other_cards_busy(host):
    host.live[3]["processes"] = [(999, "C", "/usr/bin/python", 64)]
    host.live[0].update(used_mib=384, util_pct=21, processes=[
        (123, "G", "/usr/bin/Xorg", 128), (456, "C+G", "/usr/bin/sunshine", 64)])
    host.live[2]["processes"] = [(888, "C", "/usr/bin/python", 64)]
    host.training["md-lfm3-job.service"] = {"Environment": "SCHED_GPU=1"}
    selection = x1_gpu.select(host.status)
    assert selection == {
        "gpu_index": 0, "gpu_uuid": gpu_uuid(0), "gpu_reason": "shared_graphics_only",
        "gpu_shared_processes": [
            {"pid": 123, "type": "G", "name": "Xorg", "used_mib": 128},
            {"pid": 456, "type": "C+G", "name": "sunshine", "used_mib": 64},
        ],
    }
    report = json.loads(host.status.read_text())
    assert report["state"] == "READY"
    assert {key: report[key] for key in selection} == selection
    assert report["blocked_gpus"] == {
        "3": ["gpu_process:999:C:python"],
        "1": ["active_training_unit"],
        "2": ["gpu_process:888:C:python"],
    }


def test_all_busy_records_wait(host):
    host.live[3]["processes"] = [(123, "G", "/usr/bin/blender", 32)]
    host.live[0]["processes"] = [(456, "C", "/opt/google/chrome/chrome", 64)]
    host.live[1]["used_mib"] = 2048
    host.live[2]["util_pct"] = 5
    with pytest.raises(x1_gpu.GPUUnavailable, match=x1_gpu.UNAVAILABLE):
        x1_gpu.select(host.status)
    report = json.loads(host.status.read_text())
    assert report["state"] == "WAITING" and report["gpu_index"] is report["gpu_uuid"] is None
    assert report["gpu_reason"] == x1_gpu.UNAVAILABLE and report["gpu_shared_processes"] == []
    assert report["blocked_gpus"] == {
        "3": ["gpu_process:123:G:blender"],
        "1": ["gpu_memory_mib:2048"],
        "2": ["gpu_util_pct:5"],
        "0": ["gpu_process:456:C:chrome"],
    }


@pytest.mark.parametrize("field,reason", [
    pytest.param(None, None, id="below-all-limits"),
    pytest.param("live-memory", "gpu_memory_mib:1024", id="live-memory-at-limit"),
    pytest.param("live-util", "gpu_util_pct:60", id="live-util-at-limit"),
    pytest.param("process-memory", "gpu_process_memory_mib:123:1024", id="process-memory-at-limit"),
])
def test_shared_gpu_thresholds(host, field, reason):
    host.live[1].update(used_mib=1024 if field == "live-memory" else 1023,
                        util_pct=60 if field == "live-util" else 59,
                        processes=[(123, "G", "firefox", 1024 if field == "process-memory" else 1023)])
    if reason is None:
        assert x1_gpu.select(host.status, indices=(1,))["gpu_reason"] == "shared_graphics_only"
    else:
        with pytest.raises(x1_gpu.GPUUnavailable):
            x1_gpu.select(host.status, indices=(1,))
        assert json.loads(host.status.read_text())["blocked_gpus"] == {"1": [reason]}


@pytest.mark.parametrize("memory,util,reason", [
    (2047, 4, None), (2048, 4, "gpu_memory_mib:2048"), (2047, 5, "gpu_util_pct:5"),
])
def test_idle_gpu_live_thresholds(host, memory, util, reason):
    host.live[3].update(used_mib=memory, util_pct=util)
    if reason is None:
        assert x1_gpu.select(host.status, indices=(3,))["gpu_reason"] == "fallback_gpu_idle"
    else:
        with pytest.raises(x1_gpu.GPUUnavailable):
            x1_gpu.select(host.status, indices=(3,))
        assert json.loads(host.status.read_text())["blocked_gpus"] == {"3": [reason]}


@pytest.mark.parametrize("field,value", [
    ("uuid", "N/A"), ("used_mib", "N/A"), ("used_mib", -1), ("util_pct", "N/A"),
    ("util_pct", -1), ("processes", None), ("processes", [("N/A", "G", "chrome", 64)]),
    ("processes", [(123, "N/A", "chrome", 64)]), ("processes", [(123, "G", "chrome", "N/A")]),
])
def test_unknown_live_fields_reject_candidate(host, field, value):
    host.live[0][field] = value
    assert x1_gpu.select(host.status)["gpu_index"] == 1
    assert set(json.loads(host.status.read_text())["blocked_gpus"]) == {"0"}


@pytest.mark.parametrize("gpu3,gpu1,expected", [
    pytest.param((512, 1, 1), (256, 20, 2), 1, id="memory-first"),
    pytest.param((256, 20, 1), (256, 1, 2), 1, id="util-breaks-memory-tie"),
    pytest.param((256, 1, 2), (256, 1, 1), 1, id="process-count-breaks-util-tie"),
    pytest.param((256, 1, 1), (256, 1, 1), 3, id="probe-order-breaks-full-tie"),
])
def test_ranks_shared_gpus_by_live_load(host, gpu3, gpu1, expected):
    for index, (memory, util, count) in [(3, gpu3), (1, gpu1)]:
        host.live[index].update(used_mib=memory, util_pct=util,
                                processes=[(123 + i, "G", "chrome", 64) for i in range(count)])
    selection = x1_gpu.select(host.status, indices=(3, 1))
    assert (selection["gpu_index"], selection["gpu_reason"]) == (expected, "shared_graphics_only")
    assert json.loads(host.status.read_text())["blocked_gpus"] == {}


@pytest.mark.parametrize("state", ["ALIVE", "START_REQUESTED"])
def test_static_altair_queue_and_heartbeat_do_not_reserve_idle_gpus(host, state):
    current = host.status.parent / "state/cx-altair-rejectdata/current"
    queue = host.status.parent / "queue/altair/jobs.tsv"
    current.parent.mkdir(parents=True)
    queue.parent.mkdir(parents=True)
    current.write_text("fixture.md\n")
    queue.write_text("fixture.md\t60\t0,1,2,3\t/data/receipt\ttrue\n")
    (host.status.parent / "heartbeat.json").write_text(json.dumps({"entries": [
        {"name": "cx-altair-rejectdata", "status": state, "gpu_requirement": "0,1,2,3"},
        {"name": "train-fixed", "status": state, "gpu_requirement": "0"},
    ]}))
    assert x1_gpu.select(host.status)["gpu_index"] == 0
    assert json.loads(host.status.read_text())["blocked_gpus"] == {}


@pytest.mark.parametrize("unit", ["md-sched-vega-x1-wide.service", "train-fixed.service"])
def test_actual_training_gpu_uuids_override_declared_card(host, unit):
    group = f"system.slice/{unit}"
    host.training[unit] = {"ControlGroup": f"/{group}", "Environment": "SCHED_GPU=0"}
    path = x1_gpu.CGROUP_ROOT / group
    (path / "worker").mkdir(parents=True)
    (path / "cgroup.procs").write_text("123\n")
    (path / "worker" / "cgroup.procs").write_text("456\n")
    host.compute.extend(f"{gpu_uuid(index)}, {pid}" for index, pid in [(3, 456), (2, 456), (1, 999)])
    # Exercise the unit blocker independently of the later per-card XML process probe.
    assert x1_gpu.select(host.status, indices=(3, 2, 0))["gpu_index"] == 0
    assert json.loads(host.status.read_text())["blocked_gpus"] == {
        "3": ["active_training_unit"], "2": ["active_training_unit"],
    }


def test_active_registered_train_unit_blocks_gpu_during_warmup(host):
    group = "system.slice/train-fixed.service"
    host.training["train-fixed.service"] = {"ControlGroup": f"/{group}"}
    path = x1_gpu.CGROUP_ROOT / group
    path.mkdir(parents=True)
    (path / "cgroup.procs").write_text("123\n")
    host.compute.append(f"{gpu_uuid(1)}, 999")  # An unrelated process does not override the declaration.
    assert x1_gpu.select(host.status)["gpu_index"] == 1
    assert json.loads(host.status.read_text())["blocked_gpus"] == {"0": ["active_training_unit"]}


@pytest.mark.parametrize("problem", ["unknown-binding", "bad-compute-row", "missing-cgroup"])
def test_unknown_training_occupancy_fails_closed(host, problem):
    host.training["md-sched-example.service"] = {"ControlGroup": "/system.slice/md-sched-example.service"}
    if problem == "bad-compute-row":
        host.compute.append(f"{gpu_uuid(0)}, N/A")
    elif problem == "missing-cgroup":
        host.compute.append(f"{gpu_uuid(0)}, 123")
    with pytest.raises(x1_gpu.GPUUnavailable, match="training_occupancy_unknown"):
        x1_gpu.select(host.status)


@pytest.mark.parametrize("timestamp", ["1970-01-01T00:00:00+00:00", "9999-01-01T00:00:00+00:00"])
def test_heartbeat_compute_and_load_do_not_block_any_live_idle_gpu(host, timestamp):
    (host.status.parent / "heartbeat.json").write_text(json.dumps({
        "timestamp": timestamp, "entries": [], "gpu": [
            {"index": i, "uuid": gpu_uuid(i), "used_mib": 4096, "util_pct": 100,
             "compute": [{"pid": 123, "process": "training"}]}
            for i in x1_gpu.ORDER
        ],
    }))
    for i in x1_gpu.ORDER:
        assert x1_gpu.select(host.status, indices=(i,))["gpu_index"] == i


def test_sunshine_compute_sharing_still_requires_marker(host):
    host.live[0]["processes"] = [(123, "C", "/usr/bin/sunshine", 64)]
    with pytest.raises(x1_gpu.GPUUnavailable):
        x1_gpu.select(host.status, indices=(0,))
    x1_gpu.SUNSHINE_COMPUTE_MARKER.touch()
    assert x1_gpu.select(host.status, indices=(0,))["gpu_reason"] == "shared_graphics_only"


def test_missing_lease_path_is_available_and_probe_creates_nothing(host):
    assert x1_gpu.select(host.status, skip_leased=True)["gpu_index"] == 0
    assert not x1_gpu.GPU_LOCK_ROOT.exists()


def test_lease_aware_selection_skips_exclusive_uuid_lock(host, monkeypatch):
    lock_root = host.status.parent / "custom-locks"
    monkeypatch.setenv("RTF_MATRIX_GPU_LOCK_ROOT", str(lock_root))
    lock_root.mkdir()
    lock = lock_root / f"{gpu_uuid(0)}.lock"
    lock.write_bytes(b"lease contents")
    with lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert x1_gpu.select(host.status, skip_leased=True)["gpu_index"] == 1
        assert json.loads(host.status.read_text())["blocked_gpus"] == {"0": ["gpu_lease_busy"]}
        assert x1_gpu.select(host.status, indices=(0,), expected_uuid=gpu_uuid(0))["gpu_index"] == 0
    assert x1_gpu.select(host.status, skip_leased=True)["gpu_index"] == 0
    with lock.open("rb") as held:
        # The selection probe drops its shared lock before returning.
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert lock.read_bytes() == b"lease contents"
    assert list(lock_root.iterdir()) == [lock]


def test_unknown_lease_probe_error_fails_closed(host, monkeypatch):
    lock_root = x1_gpu.GPU_LOCK_ROOT
    lock_root.mkdir()
    (lock_root / f"{gpu_uuid(0)}.lock").touch()

    def invalid_flock(*_):
        raise OSError(errno.EIO, "lease probe I/O failure")

    monkeypatch.setattr(x1_gpu.fcntl, "flock", invalid_flock)
    with pytest.raises(x1_gpu.GPUUnavailable, match="lease probe I/O failure"):
        x1_gpu.select(host.status, indices=(0,), skip_leased=True)


@pytest.mark.parametrize("change,reason", [
    ("none", None), ("uuid", "selected_gpu_uuid_changed"), ("compute", "gpu_process:456:C:python"),
])
def test_gate_rechecks_shared_gpu_lease(host, monkeypatch, change, reason):
    host.live[1]["processes"] = [(123, "C+G", "/opt/google/chrome/chrome", 64)]
    lease = x1_gpu.select(host.status, indices=(1,))
    if change == "uuid":
        host.live[1]["uuid"] = gpu_uuid(99)
    elif change == "compute":
        host.live[1]["processes"].append((456, "C", "/usr/bin/python", 32))
    monkeypatch.setenv("RTF_MATRIX_X1_GPU_UUID", lease["gpu_uuid"])
    monkeypatch.setattr(guard, "host_disk_reason", lambda: None)
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    out = host.status.parent
    x1_gpu.GPU_LOCK_ROOT.mkdir()
    with (x1_gpu.GPU_LOCK_ROOT / f"{lease['gpu_uuid']}.lock").open("wb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = guard.main(["gate", "--gpu", str(lease["gpu_index"]), "--out", str(out)])
    if reason is None:
        assert result == 0
        gate = json.loads((out / "gate.json").read_text())
        assert {key: gate[key] for key in lease} == lease
    else:
        assert result == 75
        assert json.loads(host.status.read_text())["blocked_gpus"] == {"1": [reason]}
        assert json.loads((out / "guard.json").read_text()) == {"voided_reason": x1_gpu.UNAVAILABLE}


@pytest.mark.parametrize("entry", ["resume", "collect"])
def test_busy_flight_launch_defers_and_writes_preflight_status(host, monkeypatch, entry):
    from scripts.realworld import x1_collect_next as collect
    from scripts.realworld import x1_resume as resume

    for card in host.live.values():
        card["processes"] = [(123, "C", "training", 64)]
    module = resume if entry == "resume" else collect
    monkeypatch.setattr(module, "OUTPUT", host.status.parent / entry)
    monkeypatch.setattr(resume, "mount_check", lambda **_: None)
    monkeypatch.setattr(resume, "image_check", lambda _: "fake-image")
    monkeypatch.setattr(resume, "flight_gate", lambda _: None)
    monkeypatch.setattr(resume, "complete", lambda _: False)
    monkeypatch.setattr(resume, "prepare_iowa", lambda _: None)
    monkeypatch.setattr(resume, "make_plan", lambda *_: None)
    monkeypatch.setattr(collect, "plans", lambda *_: (["pending"], [], {}))

    def forbidden(*_):
        pytest.fail("busy preflight must not acquire the flight lock or dispatch")

    monkeypatch.setattr(resume, "flight_lock", forbidden)
    monkeypatch.setattr(resume, "read_cmd", forbidden)
    assert module.main(["launch"]) == 75
    report = json.loads((module.OUTPUT / "GPU-PREFLIGHT.json").read_text())
    assert report["state"] == "WAITING" and set(report["blocked_gpus"]) == set("0123")
