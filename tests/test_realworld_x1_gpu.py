import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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
    rows = {i: {"index": i, "uuid": gpu_uuid(i), "used_mib": 128, "util_pct": 0,
                "compute": []} for i in x1_gpu.ORDER}
    registry = tmp_path / "registry.tsv"
    registry.write_text("cx-altair-rejectdata\tcodex-lane\t-\t-\t-\tAltair\tyes\t1\n"
                        "train-fixed\ttrain-unit\ttraining_unit train-fixed.service\t-\t-\tP\tno\t0\n")
    monkeypatch.setattr(x1_gpu, "KEEPALIVE", tmp_path)

    def publish(*, age=0, entries=()):
        (tmp_path / "heartbeat.json").write_text(json.dumps({
            "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
            "gpu": list(rows.values()), "entries": entries}))

    def read(args, *, timeout):
        if args[:2] == ["systemctl", "list-units"]:
            return "\n".join(training)
        if args[:2] == ["systemctl", "show"]:
            return training[args[-1]]
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

    publish()
    monkeypatch.setattr(x1_gpu, "run_read", read)
    return SimpleNamespace(live=live, training=training, rows=rows, publish=publish,
                           status=tmp_path / "GPU-STATUS.json")


def test_prefers_gpu3_when_idle(host):
    assert x1_gpu.select(host.status) == {"gpu_index": 3, "gpu_uuid": gpu_uuid(3),
                                         "gpu_reason": "preferred_gpu3_idle", "gpu_shared_processes": []}
    assert json.loads(host.status.read_text())["blocked_gpus"] == {}


@pytest.mark.parametrize("training_on_1,expected", [(False, 1), (True, 2)])
def test_prefers_idle_over_shared_and_skips_training_gpu(host, training_on_1, expected):
    host.live[3]["processes"] = [(123, "G", "/opt/google/chrome/chrome", 96)]
    if training_on_1:
        host.training["md-sched-example.service"] = f"SCHED_GPU={gpu_uuid(1)}"
    assert x1_gpu.select(host.status) == {"gpu_index": expected, "gpu_uuid": gpu_uuid(expected),
                                         "gpu_reason": "fallback_gpu_idle", "gpu_shared_processes": []}
    blocked = json.loads(host.status.read_text())["blocked_gpus"]
    assert blocked == ({"1": ["keepalive_training_reservation"]} if training_on_1 else {})


def test_shared_desktop_with_matching_compute_row_and_training_reservation(host):
    host.live[3]["processes"] = [(999, "C", "/usr/bin/python", 64)]
    host.live[0].update(used_mib=384, util_pct=21, processes=[
        (123, "G", "/usr/bin/Xorg", 128), (456, "C+G", "/usr/bin/sunshine", 64)])
    host.rows[0]["compute"] = [{"pid": 456, "process": "/opt/bin/sunshine"}]
    host.rows[2]["compute"] = [{"pid": 999, "process": "training"}]
    current = host.status.parent / "state/cx-altair-rejectdata/current"
    meta = host.status.parent / "queue/altair/jobs.tsv"
    current.parent.mkdir(parents=True)
    meta.parent.mkdir(parents=True)
    current.write_text("fixture.md\n")
    meta.write_text("fixture.md\t60\t1\t/data/receipt\ttrue\n")
    host.publish()
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
        "1": ["keepalive_training_reservation"],
        "2": ["heartbeat_compute_present_or_unknown"],
    }


def test_all_busy_records_wait(host):
    host.live[3]["processes"] = [(123, "G", "/usr/bin/blender", 32)]
    host.live[0]["processes"] = [(456, "C", "/opt/google/chrome/chrome", 64)]
    host.rows[1]["used_mib"] = 2048
    host.rows[2]["util_pct"] = 5
    host.publish()
    with pytest.raises(x1_gpu.GPUUnavailable, match=x1_gpu.UNAVAILABLE):
        x1_gpu.select(host.status)
    report = json.loads(host.status.read_text())
    assert report["state"] == "WAITING" and report["gpu_index"] is report["gpu_uuid"] is None
    assert report["gpu_reason"] == x1_gpu.UNAVAILABLE and report["gpu_shared_processes"] == []
    assert report["blocked_gpus"] == {
        "3": ["gpu_process:123:G:blender"],
        "1": ["heartbeat_memory_mib:2048"],
        "2": ["heartbeat_util_pct:5"],
        "0": ["gpu_process:456:C:chrome"],
    }


@pytest.mark.parametrize("field,reason", [
    pytest.param(None, None, id="below-all-limits"),
    pytest.param("live-memory", "gpu_memory_mib:1024", id="live-memory-at-limit"),
    pytest.param("live-util", "gpu_util_pct:60", id="live-util-at-limit"),
    pytest.param("process-memory", "gpu_process_memory_mib:123:1024", id="process-memory-at-limit"),
    pytest.param("heartbeat-memory", "heartbeat_memory_mib:2048", id="heartbeat-memory-at-limit"),
    pytest.param("heartbeat-util", "heartbeat_util_pct:60", id="heartbeat-util-at-limit"),
])
def test_shared_gpu_thresholds(host, field, reason):
    host.live[1].update(used_mib=1024 if field == "live-memory" else 1023,
                        util_pct=60 if field == "live-util" else 59,
                        processes=[(123, "G", "firefox", 1024 if field == "process-memory" else 1023)])
    host.rows[1].update(used_mib=2048 if field == "heartbeat-memory" else 2047,
                        util_pct=60 if field == "heartbeat-util" else 59)
    host.publish()
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
        assert x1_gpu.select(host.status, indices=(3,))["gpu_reason"] == "preferred_gpu3_idle"
    else:
        with pytest.raises(x1_gpu.GPUUnavailable):
            x1_gpu.select(host.status, indices=(3,))
        assert json.loads(host.status.read_text())["blocked_gpus"] == {"3": [reason]}


@pytest.mark.parametrize("kind,pid,process", [
    pytest.param("C+G", 456, "chrome", id="different-pid"),
    pytest.param("C+G", 123, "firefox", id="different-executable"),
    pytest.param("G", 123, "chrome", id="graphics-only-is-not-compute"),
])
def test_heartbeat_rejects_unconfirmed_compute_even_with_shared_graphics(host, kind, pid, process):
    host.live[1]["processes"] = [(123, kind, "/opt/google/chrome/chrome", 64)]
    host.rows[1]["compute"] = [{"pid": pid, "process": process}]
    host.publish()
    with pytest.raises(x1_gpu.GPUUnavailable):
        x1_gpu.select(host.status, indices=(1,))
    assert json.loads(host.status.read_text())["blocked_gpus"] == {"1": ["heartbeat_compute_present_or_unknown"]}


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


def test_heartbeat_reservation_excludes_lightest_shared_gpu(host):
    host.live[3]["processes"] = [(123, "G", "chrome", 64)]
    host.live[1].update(used_mib=256, processes=[(456, "G", "firefox", 64)])
    host.publish(entries=[{"name": "cx-altair-rejectdata", "status": "START_REQUESTED",
                           "gpu_requirement": "3"}])
    assert x1_gpu.select(host.status, indices=(3, 1))["gpu_index"] == 1
    assert json.loads(host.status.read_text())["blocked_gpus"] == {"3": ["keepalive_training_reservation"]}


def test_stale_heartbeat_blocks_fallback_gpus(host):
    host.live[3]["processes"] = [(123, "C", "training", 64)]
    host.publish(age=x1_gpu.HEARTBEAT_MAX_AGE_S + 1)
    with pytest.raises(x1_gpu.GPUUnavailable, match="stale"):
        x1_gpu.select(host.status)
    blocked = json.loads(host.status.read_text())["blocked_gpus"]
    assert set(blocked) == set("0123")
    assert all(blocked[str(index)][0].startswith("heartbeat_unavailable:") for index in (1, 2, 0))


@pytest.mark.parametrize("change,reason", [
    ("none", None), ("uuid", "selected_gpu_uuid_changed"), ("compute", "gpu_process:456:C:python"),
])
def test_gate_rechecks_shared_gpu_lease(host, monkeypatch, change, reason):
    host.live[1]["processes"] = [(123, "C+G", "/opt/google/chrome/chrome", 64)]
    host.rows[1]["compute"] = [{"pid": 123, "process": "chrome"}]
    host.publish()
    lease = x1_gpu.select(host.status, indices=(1,))
    if change == "uuid":
        host.live[1]["uuid"] = host.rows[1]["uuid"] = gpu_uuid(99)
        host.publish()
    elif change == "compute":
        host.live[1]["processes"].append((456, "C", "/usr/bin/python", 32))
    monkeypatch.setenv("RTF_MATRIX_X1_GPU_UUID", lease["gpu_uuid"])
    monkeypatch.setattr(guard, "host_disk_reason", lambda: None)
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    out = host.status.parent
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
