import json
import shutil
import subprocess

import pytest

from multidetect.polaris_sim.gateway.doctor import (
    _check_firewall,
    _parse_memory_bytes,
    classify_container_memory_usage,
    doctor_checks,
)


def test_doctor_memory_thresholds_and_docker_usage_parser():
    gib = 1024**3
    assert classify_container_memory_usage(10 * gib) == "PASS"
    assert classify_container_memory_usage(10 * gib + 1) == "WARN"
    assert classify_container_memory_usage(14 * gib) == "WARN"
    assert classify_container_memory_usage(14 * gib + 1) == "FAIL"
    assert _parse_memory_bytes("14.5GiB") == int(14.5 * gib)


def test_doctor_reports_high_memory_with_restart_suggestion(monkeypatch):
    class Result:
        returncode = 0
        stdout = "14.5GiB / 16GiB"
        stderr = ""

    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.socket.create_connection",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor._udp_listener_ports", lambda: set())
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor._check_firewall", lambda _add: None)
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor._check_lan_ips", lambda _add: None)
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor._lan_ip_list", lambda: [])
    monkeypatch.setattr(
        "multidetect.polaris_sim.gateway.doctor.subprocess.run",
        lambda *args, **kwargs: Result(),
    )
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.shutil.which", lambda _name: None)
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.urllib.request.urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))

    report = doctor_checks(
        vehicles=[{"slot": 5, "sysid": 1, "container": "md-polaris-sim-slot-5"}]
    )
    memory = next(row for row in report["checks"] if row["name"] == "sitl:memory:sysid-1")
    assert memory["status"] == "FAIL"
    assert "重启仿真" in memory["suggestion"]


def test_doctor_reports_vehicle_gps_clock_and_video_states(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "unavailable"

    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.socket.create_connection",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.subprocess.run", lambda *args, **kwargs: Result())
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.urllib.request.urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))
    report = doctor_checks(
        vehicles=[{"sysid": 1, "last_heartbeat_age_s": 0.2, "gps_fix_type": 3, "rtf": 0.9,
                   "airframe": "x500_gimbal"}],
        discovery={"host": "127.0.0.1"},
    )
    statuses = {row["name"]: row["status"] for row in report["checks"]}
    assert statuses["sitl:heartbeat:sysid-1"] == "PASS"
    assert statuses["sitl:gps:sysid-1"] == "PASS"
    assert statuses["sitl:clock:sysid-1"] == "PASS"
    assert statuses["video:stream"] == "WARN"
    assert json.loads(json.dumps(report))["status"] in {"FAIL", "WARN"}


def test_doctor_invokes_ffprobe_for_each_camera_stream(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = '{"streams":[{"codec_name":"h264"}]}'
        stderr = ""

    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.socket.create_connection",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")))
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.subprocess.run",
                        lambda args, **kwargs: (calls.append(args) or Result()))
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.shutil.which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor.urllib.request.urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("not running")))
    report = doctor_checks(vehicles=[{"sysid": 1, "airframe": "x500_gimbal"}], discovery={"host": "10.0.0.2"})
    assert any(call[0] == "/usr/bin/ffprobe" for call in calls)
    assert any(row["name"].startswith("video:sysid-1:") and row["status"] == "PASS" for row in report["checks"])


def test_doctor_only_passes_firewall_rules_scoped_to_local_lan(monkeypatch):
    ports = ("5770/tcp", "14550/udp", "8554/tcp", "8780/tcp", "8790/tcp")
    monkeypatch.setattr("multidetect.polaris_sim.gateway.doctor._lan_cidrs", lambda: ["192.168.1.0/24"])

    class Result:
        returncode = 0
        stderr = ""

    def check_firewall(output):
        result = Result()
        result.stdout = output
        monkeypatch.setattr(
            "multidetect.polaris_sim.gateway.doctor.subprocess.run",
            lambda command, **kwargs: result if command == ["ufw", "status"] else pytest.fail(command),
        )
        checks = []
        _check_firewall(lambda name, status, detail, suggestion="": checks.append(
            {"name": name, "status": status, "detail": detail, "suggestion": suggestion}
        ))
        return checks[-1]

    lan_rules = "Status: active\nTo Action From\n-- ------ ----\n" + "\n".join(
        f"{port} ALLOW IN 192.168.1.0/24" for port in ports
    )
    assert check_firewall(lan_rules)["status"] == "PASS"

    broad_and_denied = "Status: active\nTo Action From\n-- ------ ----\n" + "\n".join(
        f"{port} {'DENY' if port == '5770/tcp' else 'ALLOW'} IN Anywhere" for port in ports
    )
    result = check_firewall(broad_and_denied)
    assert result["status"] == "WARN"
    assert all(port in result["detail"] for port in ports)

    denied_before_allow = (
        "Status: active\nTo Action From\n-- ------ ----\n5770/tcp DENY IN Anywhere\n"
        "5770/tcp ALLOW IN 192.168.1.0/24\n"
        + "\n".join(f"{port} ALLOW IN 192.168.1.0/24" for port in ports[1:])
    )
    result = check_firewall(denied_before_allow)
    assert result["status"] == "WARN"
    assert "5770/tcp" in result["detail"]


def test_ffprobe_reads_local_cpu_encoded_h264_without_polaris(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe are unavailable")
    stream = tmp_path / "camera.h264"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=black:s=64x64:r=1:d=1", "-frames:v", "1", "-c:v", "libx264",
         "-preset", "ultrafast", "-tune", "zerolatency", "-f", "h264", str(stream)],
        check=True, timeout=10,
    )
    result = subprocess.run(
        [ffprobe, "-v", "error", "-f", "h264", "-i", str(stream),
         "-show_entries", "stream=codec_name", "-of", "json"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    assert json.loads(result.stdout)["streams"][0]["codec_name"] == "h264"
