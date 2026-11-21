import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.realworld import collection_guard as guard
from scripts.realworld import x1_gpu


def gpu_uuid(index):
    return f"GPU-{index:08d}-0000-0000-0000-000000000000"


@pytest.fixture
def host(tmp_path, monkeypatch):
    processes = {}
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
        index = int(args[2])
        process_xml = "".join(f"<process_info><pid>123</pid><type>{kind}</type>"
                              f"<process_name>{name}</process_name></process_info>"
                              for kind, name in processes.get(index, []))
        return (f"<nvidia_smi_log><gpu><uuid>{gpu_uuid(index)}</uuid>"
                f"<fb_memory_usage><used>128 MiB</used></fb_memory_usage>"
                f"<utilization><gpu_util>0 %</gpu_util></utilization>"
                f"<processes>{process_xml or 'None'}</processes></gpu></nvidia_smi_log>")

    publish()
    monkeypatch.setattr(x1_gpu, "run_read", read)
    return processes, training, rows, publish, tmp_path / "GPU-STATUS.json"


def test_prefers_gpu3_when_idle(host):
    *_, status = host
    assert x1_gpu.select(status) == {"gpu_index": 3, "gpu_uuid": gpu_uuid(3),
                                    "gpu_reason": "preferred_gpu3_idle"}
    assert json.loads(status.read_text())["blocked_gpus"] == {}


@pytest.mark.parametrize("training_on_1,expected", [(False, 1), (True, 2)])
def test_chrome_on_gpu3_falls_back_without_using_training_gpu(host, training_on_1, expected):
    processes, training, _, _, status = host
    processes[3] = [("G", "/opt/google/chrome/chrome")]
    if training_on_1:
        training["md-sched-example.service"] = f"SCHED_GPU={gpu_uuid(1)}"
    assert x1_gpu.select(status)["gpu_index"] == expected
    blocked = json.loads(status.read_text())["blocked_gpus"]
    assert "gpu_process:123:G:chrome" in blocked["3"]
    if training_on_1:
        assert "keepalive_training_reservation" in blocked["1"]


def test_heartbeat_busy_altair_reservation_and_gpu0_desktop(host):
    processes, _, rows, publish, status = host
    processes.update({3: [("C+G", "chrome")], 0: [("G", "/usr/bin/Xorg")]})
    rows[2]["compute"] = [{"pid": 999, "process": "training"}]
    current = status.parent / "state/cx-altair-rejectdata/current"
    meta = status.parent / "queue/altair/jobs.tsv"
    current.parent.mkdir(parents=True)
    meta.parent.mkdir(parents=True)
    current.write_text("fixture.md\n")
    meta.write_text("fixture.md\t60\t1\t/data/receipt\ttrue\n")
    publish()
    assert x1_gpu.select(status)["gpu_index"] == 0
    blocked = json.loads(status.read_text())["blocked_gpus"]
    assert blocked["1"] == ["keepalive_training_reservation"]
    assert blocked["2"] == ["heartbeat_compute_present_or_unknown"]


def test_all_busy_records_wait_and_rejects_two_gib_or_five_percent(host):
    processes, _, rows, publish, status = host
    processes[3] = [("G", "chrome")]
    processes[0] = [("C+G", "/usr/bin/sunshine")]
    rows[1]["used_mib"] = 2048
    rows[2]["util_pct"] = 5
    publish()
    with pytest.raises(x1_gpu.GPUUnavailable):
        x1_gpu.select(status)
    report = json.loads(status.read_text())
    assert report["state"] == "WAITING" and report["gpu_index"] is report["gpu_uuid"] is None
    assert set(report["blocked_gpus"]) == set("0123")
    assert report["blocked_gpus"]["1"] == ["heartbeat_memory_mib:2048"]
    assert report["blocked_gpus"]["2"] == ["heartbeat_util_pct:5"]


def test_stale_heartbeat_and_late_uuid_mismatch_fail_closed(host, monkeypatch):
    processes, _, _, publish, status = host
    processes[3] = [("G", "chrome")]
    publish(age=x1_gpu.HEARTBEAT_MAX_AGE_S + 1)
    with pytest.raises(x1_gpu.GPUUnavailable, match="stale"):
        x1_gpu.select(status)
    publish()
    monkeypatch.setenv("RTF_MATRIX_X1_GPU_UUID", gpu_uuid(2))
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    assert guard.main(["gate", "--gpu", "1", "--out", str(status.parent)]) == 75
    assert json.loads(status.read_text())["blocked_gpus"] == {"1": ["selected_gpu_uuid_changed"]}
    assert json.loads((status.parent / "guard.json").read_text()) == {"voided_reason": x1_gpu.UNAVAILABLE}


@pytest.mark.parametrize("entry", ["resume", "collect"])
def test_busy_flight_launch_defers_and_writes_preflight_status(host, monkeypatch, entry):
    from scripts.realworld import x1_collect_next as collect
    from scripts.realworld import x1_resume as resume

    processes, _, _, _, status = host
    processes.update({index: [("G", "chrome")] for index in x1_gpu.ORDER})
    module = resume if entry == "resume" else collect
    monkeypatch.setattr(module, "OUTPUT", status.parent / entry)
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
    assert module.main(["launch"]) == 75
    report = json.loads((module.OUTPUT / "GPU-PREFLIGHT.json").read_text())
    assert report["state"] == "WAITING" and set(report["blocked_gpus"]) == set("0123")
