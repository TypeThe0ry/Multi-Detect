from __future__ import annotations

import json
import socket
import threading
import time

import pytest

import multidetect.field_link_preflight as field_link_preflight
from multidetect.field_link_preflight import (
    EndpointProbe,
    build_field_link_report,
    discover_field_link_hosts,
    main,
    probe_local_route,
    probe_mavlink_tcp,
    probe_rtsp_options,
    probe_ssh_banner,
    probe_udp_bind,
    verify_field_link_discovery,
    verify_field_link_interface_diagnostic,
    verify_field_link_report,
    verify_main,
    watch_field_link,
)


def _serve_once(
    response: bytes,
    *,
    read_request: bool = False,
    request_sink: list[bytes] | None = None,
) -> tuple[socket.socket, threading.Thread]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        listener.settimeout(1.0)
        try:
            connection, _ = listener.accept()
            with connection:
                if read_request:
                    request = connection.recv(4096)
                    if request_sink is not None:
                        request_sink.append(request)
                if response:
                    connection.sendall(response)
                elif request_sink is not None:
                    connection.settimeout(0.1)
                    try:
                        request = connection.recv(4096)
                    except TimeoutError:
                        request = b""
                    if request:
                        request_sink.append(request)
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread


def _serve_twice(
    response: bytes,
    *,
    read_request: bool = False,
) -> tuple[socket.socket, threading.Thread]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def serve() -> None:
        listener.settimeout(1.5)
        try:
            for _ in range(2):
                connection, _ = listener.accept()
                with connection:
                    if read_request:
                        connection.recv(4096)
                    if response:
                        connection.sendall(response)
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread


def _write_interface_diagnostic(path, *, status: str = "missing", aliases=None) -> None:
    candidates = [] if aliases is None else list(aliases)
    payload = {
        "schema_version": 1,
        "read_only": True,
        "expected_source_cidr": "192.168.144.0/24",
        "normalized_source_cidr": "192.168.144.0/24",
        "adapters": [],
        "target_routes": [],
        "selection": {
            "status": status,
            "candidate_count": len(candidates),
            "candidate_aliases": candidates,
        },
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_field_link_report_checks_tcp_and_local_udp() -> None:
    listeners: list[socket.socket] = []
    ports: list[int] = []
    for _ in range(3):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listeners.append(listener)
        ports.append(listener.getsockname()[1])

    def accept_once(listener: socket.socket) -> None:
        listener.settimeout(1.0)
        try:
            connection, _ = listener.accept()
            connection.close()
        except OSError:
            pass

    threads = [
        threading.Thread(target=accept_once, args=(listener,), daemon=True)
        for listener in listeners
    ]
    for thread in threads:
        thread.start()
    try:
        report = build_field_link_report(
            jetson_host="127.0.0.1",
            gr01_host="127.0.0.1",
            camera_host="127.0.0.1",
            jetson_port=ports[0],
            gr01_port=ports[1],
            rtsp_port=ports[2],
            metadata_port=0,
            qgc_port=0,
            timeout_s=0.5,
        )
    finally:
        for listener in listeners:
            listener.close()
        for thread in threads:
            thread.join(timeout=1.0)

    assert report["schema_version"] == 1
    assert report["summary"]["local_udp_ports_available"] is True
    assert report["summary"]["tcp_reachable"] is True
    assert len(report["probes"]) == 5


def test_discovery_is_bounded_read_only_and_returns_only_responsive_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe(name: str, host: str, port: int, timeout_s: float) -> EndpointProbe:
        service_port = {"ssh": 22, "mavlink": 5760, "rtsp": 554}[name]
        ok = host == "127.0.0.2" and port == service_port
        return EndpointProbe(
            name=name,
            transport="tcp",
            host=host,
            port=port,
            ok=ok,
            elapsed_ms=0.1,
            detail="fake",
        )

    monkeypatch.setattr(field_link_preflight, "probe_tcp", fake_probe)
    monkeypatch.setattr(field_link_preflight, "probe_ssh_banner", fake_probe)
    monkeypatch.setattr(field_link_preflight, "probe_mavlink_tcp", fake_probe)
    monkeypatch.setattr(field_link_preflight, "probe_rtsp_options", fake_probe)

    result = discover_field_link_hosts(
        cidr="127.0.0.0/30",
        timeout_s=0.1,
        protocol_checks=True,
        max_hosts=4,
        workers=8,
    )

    assert result["schema_version"] == 1
    assert result["cidr"] == "127.0.0.0/30"
    assert result["scanned_hosts"] == 2
    assert result["responsive_hosts"] == 1
    assert result["service_hits"] == {"ssh": 1, "mavlink": 1, "rtsp": 1}
    assert result["service_candidates"] == {
        "ssh": ["127.0.0.2"],
        "mavlink": ["127.0.0.2"],
        "rtsp": ["127.0.0.2"],
    }
    assert [item["host"] for item in result["hosts"]] == ["127.0.0.2"]
    assert result["hosts"][0]["services"]["ssh"]["transport"] == "tcp"


def test_discovery_rejects_unbounded_or_non_ipv4_scope() -> None:
    with pytest.raises(ValueError, match="reduce the scope"):
        discover_field_link_hosts(cidr="127.0.0.0/24", max_hosts=2)
    with pytest.raises(ValueError, match="IPv4 CIDR only"):
        discover_field_link_hosts(cidr="::1/128")
    with pytest.raises(ValueError, match="discover_rtsp_port"):
        discover_field_link_hosts(cidr="127.0.0.0/30", rtsp_port=0)


def _fake_discovery_snapshot(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Create a deterministic discovery object without opening sockets."""

    def fake_probe(name: str, host: str, port: int, timeout_s: float) -> EndpointProbe:
        service_port = {"ssh": 22, "mavlink": 5760, "rtsp": 554}[name]
        ok = host == "127.0.0.2" and port == service_port
        return EndpointProbe(
            name=name,
            transport="tcp",
            host=host,
            port=port,
            ok=ok,
            elapsed_ms=0.1,
            detail="fake",
        )

    monkeypatch.setattr(field_link_preflight, "probe_tcp", fake_probe)
    return discover_field_link_hosts(
        cidr="127.0.0.0/30",
        timeout_s=0.1,
        protocol_checks=False,
        max_hosts=4,
        workers=2,
    )


def test_verify_field_link_discovery_recomputes_scope_and_service_aggregates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    discovery = _fake_discovery_snapshot(monkeypatch)

    verified = verify_field_link_discovery(discovery)

    assert verified["gate_passed"] is True
    assert verified["checks"]["scanned_scope"] is True
    assert verified["checks"]["aggregates"] is True

    payload = {
        "schema_version": 1,
        "probes": [{"name": "jetson", "ok": True}],
        "summary": {"all_passed": True, "hardware_link_ready": True},
        "discovery": discovery,
    }
    path = tmp_path / "discovery-bound.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report_result = verify_field_link_report(path)
    assert report_result["gate_passed"] is True
    assert report_result["checks"]["discovery"]["gate_passed"] is True


def test_verify_field_link_discovery_keeps_legacy_v1_snapshot_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _fake_discovery_snapshot(monkeypatch)
    discovery.pop("service_candidates")

    result = verify_field_link_discovery(discovery)

    assert result["gate_passed"] is True
    assert result["checks"]["candidate_lists_present"] is False
    assert result["warnings"]


@pytest.mark.parametrize(
    ("tamper", "expected_reason"),
    [
        (
            lambda discovery: discovery["service_hits"].update({"ssh": 0}),
            "service_hits/service_candidates do not match host services",
        ),
        (
            lambda discovery: discovery["service_candidates"].update({"ssh": ["10.0.0.5"]}),
            "service_hits/service_candidates do not match host services",
        ),
        (
            lambda discovery: discovery.update({"responsive_hosts": 2}),
            "responsive_hosts does not match the hosts record count",
        ),
    ],
)
def test_verify_field_link_discovery_rejects_tampered_aggregates_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    tamper,
    expected_reason: str,
) -> None:
    discovery = _fake_discovery_snapshot(monkeypatch)
    tamper(discovery)

    result = verify_field_link_discovery(discovery)

    assert result["gate_passed"] is False
    assert expected_reason in " ".join(result["reasons"])


def test_verify_field_link_discovery_rejects_out_of_scope_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _fake_discovery_snapshot(monkeypatch)
    discovery["hosts"][0]["host"] = "10.0.0.5"

    result = verify_field_link_discovery(discovery)

    assert result["gate_passed"] is False
    assert "outside the explicit CIDR" in " ".join(result["reasons"])


def test_verify_field_link_discovery_rejects_nonresponsive_record_in_filtered_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _fake_discovery_snapshot(monkeypatch)
    discovery["hosts"][0]["responsive"] = False
    for service in discovery["hosts"][0]["services"].values():
        service["ok"] = False
    discovery["responsive_hosts"] = 1

    result = verify_field_link_discovery(discovery)

    assert result["gate_passed"] is False
    assert "responsive must be true" in " ".join(result["reasons"])


def test_verify_field_link_discovery_rejects_huge_numeric_fields_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _fake_discovery_snapshot(monkeypatch)
    discovery["timeout_s"] = 10**1000

    result = verify_field_link_discovery(discovery)

    assert result["gate_passed"] is False
    assert "timeout_s must be finite" in " ".join(result["reasons"])


def test_udp_bind_reports_port_collision() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    occupied.bind(("127.0.0.1", 0))
    try:
        port = occupied.getsockname()[1]
        probe = probe_udp_bind("collision", "127.0.0.1", port)
    finally:
        occupied.close()
    assert probe.ok is False
    assert "Address already in use" in probe.detail or "WinError 10048" in probe.detail


def test_local_route_probe_is_passive_and_can_enforce_source_network() -> None:
    probe = probe_local_route(
        "loopback-route",
        "127.0.0.1",
        5760,
        expected_source_cidr="127.0.0.0/8",
    )
    assert probe.ok is True
    assert probe.transport == "route"
    assert probe.source_address == "127.0.0.1"
    assert "within expected 127.0.0.0/8" in probe.detail


def test_local_route_probe_fails_closed_when_source_network_does_not_match() -> None:
    probe = probe_local_route(
        "loopback-route",
        "127.0.0.1",
        5760,
        expected_source_cidr="10.0.0.0/8",
    )
    assert probe.ok is False
    assert probe.source_address == "127.0.0.1"
    assert "outside expected 10.0.0.0/8" in probe.detail


def test_field_link_report_can_require_aircraft_source_route() -> None:
    report = build_field_link_report(
        jetson_host="127.0.0.1",
        gr01_host="127.0.0.1",
        camera_host="127.0.0.1",
        jetson_port=22,
        gr01_port=5760,
        rtsp_port=554,
        metadata_port=14580,
        qgc_port=14581,
        timeout_s=0.01,
        expected_source_cidr="10.0.0.0/8",
        require_route=True,
    )
    assert report["summary"]["route_checks_requested"] is True
    assert report["summary"]["route_checks_passed"] is False
    assert report["summary"]["route_source_addresses"] == {
        probe["name"]: probe["source_address"] for probe in report["route_probes"]
    }
    assert len(report["route_probes"]) == 3
    assert report["summary"]["hardware_link_ready"] is False


def test_field_link_cli_forwards_route_gate_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_report(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"summary": {"all_passed": True}, "notes": []}

    monkeypatch.setattr(field_link_preflight, "build_field_link_report", fake_report)
    output = tmp_path / "route-cli.json"
    assert (
        field_link_preflight.main(
            [
                "--expected-source-cidr",
                "192.168.144.0/24",
                "--require-route",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    assert captured["expected_source_cidr"] == "192.168.144.0/24"
    assert captured["require_route"] is True
    assert output.is_file()


def test_field_link_verifier_rejects_tampered_route_summary(tmp_path) -> None:
    path = tmp_path / "tampered-route.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"name": "tcp", "ok": True}],
                "route_probes": [
                    {
                        "name": "route",
                        "transport": "route",
                        "host": "127.0.0.1",
                        "port": 5760,
                        "ok": False,
                        "source_address": "127.0.0.1",
                    }
                ],
                "summary": {
                    "all_passed": True,
                    "hardware_link_ready": True,
                    "configuration_valid": True,
                    "route_checks_requested": True,
                    "route_checks_passed": True,
                },
            }
        ),
        encoding="utf-8",
    )
    result = verify_field_link_report(path)
    assert result["gate_passed"] is False
    assert "summary does not match probes" in " ".join(result["reasons"])


def test_field_link_verifier_rejects_tampered_route_source_projection(tmp_path) -> None:
    path = tmp_path / "tampered-route-source.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"name": "tcp", "ok": True}],
                "route_probes": [
                    {
                        "name": "jetson-route",
                        "transport": "route",
                        "host": "192.168.144.20",
                        "port": 22,
                        "ok": True,
                        "source_address": "192.168.144.250",
                    }
                ],
                "summary": {
                    "all_passed": True,
                    "hardware_link_ready": True,
                    "configuration_valid": True,
                    "route_checks_requested": True,
                    "route_checks_passed": True,
                    "route_source_addresses": {
                        "jetson-route": "192.168.1.13",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = verify_field_link_report(path)

    assert result["gate_passed"] is False
    assert "summary does not match probes" in " ".join(result["reasons"])


def test_ssh_banner_probe_is_passive_and_validates_identification() -> None:
    outbound: list[bytes] = []
    listener, thread = _serve_once(
        b"device pre-banner\r\nSSH-2.0-test-device\r\n",
        request_sink=outbound,
    )
    try:
        probe = probe_ssh_banner("jetson", "127.0.0.1", listener.getsockname()[1], 0.5)
    finally:
        listener.close()
        thread.join(timeout=1.0)

    assert probe.ok is True
    assert probe.transport == "ssh"
    assert "SSH-2.0-test-device" in probe.detail
    assert outbound == []


def test_mavlink_probe_accepts_a_complete_v1_frame_without_sending() -> None:
    # MAVLink v1, zero-byte payload: magic + len/seq/sys/comp/msgid + CRC.
    outbound: list[bytes] = []
    listener, thread = _serve_once(
        b"\xfe\x00\x01\x01\x01\x00\x00\x00",
        request_sink=outbound,
    )
    try:
        probe = probe_mavlink_tcp("gr01", "127.0.0.1", listener.getsockname()[1], 0.5)
    finally:
        listener.close()
        thread.join(timeout=1.0)

    assert probe.ok is True
    assert probe.transport == "mavlink"
    assert "MAVLink v1" in probe.detail
    assert outbound == []


def test_mavlink_probe_accepts_a_complete_v2_frame() -> None:
    # MAVLink v2, zero-byte payload: magic + 9-byte header + CRC.
    listener, thread = _serve_once(b"\xfd\x00\x00\x00\x00\x01\x01\x00\x00\x00\x00\x00")
    try:
        probe = probe_mavlink_tcp("gr01", "127.0.0.1", listener.getsockname()[1], 0.5)
    finally:
        listener.close()
        thread.join(timeout=1.0)

    assert probe.ok is True
    assert "MAVLink v2" in probe.detail


def test_rtsp_options_probe_sends_read_only_request_and_accepts_auth_required() -> None:
    requests: list[bytes] = []
    listener, thread = _serve_once(
        b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n",
        read_request=True,
        request_sink=requests,
    )
    try:
        probe = probe_rtsp_options("camera", "127.0.0.1", listener.getsockname()[1], 0.5)
    finally:
        listener.close()
        thread.join(timeout=1.0)

    assert probe.ok is True
    assert probe.transport == "rtsp"
    assert "authentication required" in probe.detail
    assert len(requests) == 1
    assert requests[0].startswith(b"OPTIONS * RTSP/1.0\r\n")
    assert b"DESCRIBE" not in requests[0]
    assert b"SETUP" not in requests[0]
    assert b"PLAY" not in requests[0]


def test_protocol_checks_extend_field_report_and_gate_readiness() -> None:
    ssh_listener, ssh_thread = _serve_twice(b"SSH-2.0-test-device\r\n")
    mav_listener, mav_thread = _serve_twice(b"\xfe\x00\x01\x01\x01\x00\x00\x00")
    rtsp_listener, rtsp_thread = _serve_twice(
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n",
        read_request=True,
    )
    try:
        report = build_field_link_report(
            jetson_host="127.0.0.1",
            gr01_host="127.0.0.1",
            camera_host="127.0.0.1",
            jetson_port=ssh_listener.getsockname()[1],
            gr01_port=mav_listener.getsockname()[1],
            rtsp_port=rtsp_listener.getsockname()[1],
            metadata_port=0,
            qgc_port=0,
            timeout_s=0.5,
            protocol_checks=True,
        )
    finally:
        ssh_listener.close()
        mav_listener.close()
        rtsp_listener.close()
        ssh_thread.join(timeout=1.0)
        mav_thread.join(timeout=1.0)
        rtsp_thread.join(timeout=1.0)

    assert report["summary"]["protocol_checks_requested"] is True
    assert report["summary"]["protocol_checks_passed"] is True
    assert report["summary"]["all_passed"] is True
    assert report["summary"]["hardware_link_ready"] is True
    assert len(report["probes"]) == 8
    assert {item["transport"] for item in report["probes"]} >= {"ssh", "mavlink", "rtsp"}


def test_field_link_report_rejects_metadata_port_collision() -> None:
    report = build_field_link_report(
        jetson_host="127.0.0.1",
        gr01_host="127.0.0.1",
        camera_host="127.0.0.1",
        jetson_port=1,
        gr01_port=2,
        rtsp_port=3,
        metadata_port=14581,
        qgc_port=14581,
        timeout_s=0.01,
    )
    assert report["summary"]["configuration_valid"] is False
    assert report["summary"]["all_passed"] is False
    assert report["summary"]["hardware_link_ready"] is False
    assert report["summary"]["configuration_errors"] == [
        "metadata_port and qgc_port must differ (both are 14581)"
    ]


def test_field_link_report_rejects_invalid_port_range() -> None:
    report = build_field_link_report(
        jetson_host="127.0.0.1",
        gr01_host="127.0.0.1",
        camera_host="127.0.0.1",
        jetson_port=70000,
        gr01_port=2,
        rtsp_port=3,
        metadata_port=14580,
        qgc_port=14581,
        timeout_s=0.01,
    )
    assert report["summary"]["configuration_valid"] is False
    assert (
        "jetson_port must be an integer in [0, 65535]" in report["summary"]["configuration_errors"]
    )


def test_interface_diagnostic_verifier_accepts_missing_selection_and_binds_hash(tmp_path) -> None:
    path = tmp_path / "interface-diagnostic.json"
    _write_interface_diagnostic(path)

    result = verify_field_link_interface_diagnostic(
        path,
        expected_source_cidr="192.168.144.0/24",
        expected_interface_alias="Ethernet",
    )

    assert result["gate_passed"] is True
    assert result["read_only"] is True
    assert result["selection"] == {
        "status": "missing",
        "candidate_count": 0,
        "candidate_aliases": [],
    }
    assert result["report_sha256"]
    assert result["report_bytes"] == path.stat().st_size


def test_interface_diagnostic_verifier_rejects_alias_and_cidr_drift(tmp_path) -> None:
    path = tmp_path / "interface-diagnostic-ready.json"
    _write_interface_diagnostic(path, status="ready", aliases=["Ethernet"])

    result = verify_field_link_interface_diagnostic(
        path,
        expected_source_cidr="10.0.0.0/24",
        expected_interface_alias="Wi-Fi",
    )

    assert result["gate_passed"] is False
    joined = " ".join(result["reasons"])
    assert "CIDR does not match field-link preflight" in joined
    assert "not one of the diagnostic candidates" in joined


def test_interface_diagnostic_verifier_rejects_duplicate_keys_and_readiness_claim(
    tmp_path,
) -> None:
    path = tmp_path / "interface-diagnostic-tampered.json"
    path.write_text(
        '{"schema_version":1,"read_only":true,"read_only":false,"hardware_link_ready":true}\n',
        encoding="utf-8",
    )

    result = verify_field_link_interface_diagnostic(path)

    assert result["gate_passed"] is False
    joined = " ".join(result["reasons"])
    assert "duplicate JSON key" in joined


def test_field_link_report_rechecks_hash_bound_interface_diagnostic(tmp_path) -> None:
    diagnostic = tmp_path / "interface-diagnostic.json"
    _write_interface_diagnostic(diagnostic)
    diagnostic_result = verify_field_link_interface_diagnostic(diagnostic)
    binding = {
        "required": True,
        "report_path": diagnostic_result["report_path"],
        "report_sha256": diagnostic_result["report_sha256"],
        "report_bytes": diagnostic_result["report_bytes"],
        "schema_version": diagnostic_result["diagnostic_schema_version"],
        "read_only": True,
        "expected_source_cidr": diagnostic_result["expected_source_cidr"],
        "normalized_source_cidr": diagnostic_result["normalized_source_cidr"],
        "selection_status": diagnostic_result["selection"]["status"],
        "selection_candidate_count": diagnostic_result["selection"]["candidate_count"],
        "candidate_aliases": diagnostic_result["selection"]["candidate_aliases"],
        "interface_alias": "Ethernet",
        "verified": True,
    }
    report_path = tmp_path / "field-link.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": {"expected_source_cidr": "192.168.144.0/24"},
                "probes": [{"name": "jetson", "ok": True}],
                "summary": {"all_passed": True, "hardware_link_ready": True},
                "interface_diagnostic": binding,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    verified = verify_field_link_report(report_path)
    assert verified["gate_passed"] is True
    assert verified["checks"]["interface_diagnostic"]["gate_passed"] is True

    diagnostic.write_text(diagnostic.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    tampered = verify_field_link_report(report_path)
    assert tampered["gate_passed"] is False
    assert "interface diagnostic is unavailable" not in " ".join(tampered["reasons"])
    assert "report_sha256 does not match source" in " ".join(tampered["reasons"])


def test_field_link_cli_attaches_interface_diagnostic_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = tmp_path / "interface-diagnostic.json"
    _write_interface_diagnostic(diagnostic)
    output = tmp_path / "field-link.json"
    monkeypatch.setattr(
        field_link_preflight,
        "build_field_link_report",
        lambda **_: {
            "schema_version": 1,
            "probes": [{"name": "jetson", "ok": True}],
            "summary": {"all_passed": True, "hardware_link_ready": True},
        },
    )

    assert (
        main(
            [
                "--interface-diagnostic",
                str(diagnostic),
                "--interface-alias",
                "Ethernet",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["interface_diagnostic"]["verified"] is True
    assert verify_field_link_report(output)["gate_passed"] is True


def test_verify_field_link_report_binds_hash_and_readiness(tmp_path) -> None:
    path = tmp_path / "field-link.json"
    payload = {
        "schema_version": 1,
        "probes": [{"name": "jetson", "ok": True}],
        "summary": {"all_passed": True, "hardware_link_ready": True},
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    verified = verify_field_link_report(path)

    assert verified["gate_passed"] is True
    assert verified["hardware_link_ready"] is True
    assert verified["report_sha256"]
    assert verified["report_bytes"] == path.stat().st_size
    assert verified["summary"] == payload["summary"]

    payload["summary"]["hardware_link_ready"] = False
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    changed = verify_field_link_report(path)
    assert changed["gate_passed"] is False
    assert "hardware_link_ready is false" in " ".join(changed["reasons"])


def test_verify_field_link_report_accepts_utf8_bom_without_changing_hash_binding(
    tmp_path,
) -> None:
    path = tmp_path / "field-link-bom.json"
    payload = {
        "schema_version": 1,
        "probes": [{"name": "jetson", "ok": True}],
        "summary": {"all_passed": True, "hardware_link_ready": True},
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8-sig")

    verified = verify_field_link_report(path)

    assert verified["gate_passed"] is True
    assert verified["report_sha256"]
    assert verified["report_bytes"] == path.stat().st_size


def test_verify_field_link_report_rejects_duplicate_keys_and_nonfinite_json(
    tmp_path,
) -> None:
    duplicate = tmp_path / "duplicate-field-link.json"
    duplicate.write_text(
        '{"schema_version":1,"probes":[{"ok":true}],'
        '"summary":{"all_passed":true,"hardware_link_ready":true},'
        '"summary":{"all_passed":false,"hardware_link_ready":false}}\n',
        encoding="utf-8",
    )
    duplicate_result = verify_field_link_report(duplicate, require_ready=False)
    assert duplicate_result["gate_passed"] is False
    assert "duplicate JSON key" in " ".join(duplicate_result["reasons"])

    nonfinite = tmp_path / "nonfinite-field-link.json"
    nonfinite.write_text(
        '{"schema_version":1,"probes":[{"ok":true}],'
        '"summary":{"all_passed":true,"hardware_link_ready":true},'
        '"elapsed_s":NaN}\n',
        encoding="utf-8",
    )
    nonfinite_result = verify_field_link_report(nonfinite, require_ready=False)
    assert nonfinite_result["gate_passed"] is False
    assert "invalid JSON constant NaN" in " ".join(nonfinite_result["reasons"])


def test_verify_field_link_report_rejects_summary_upgraded_over_failed_probe(tmp_path) -> None:
    path = tmp_path / "tampered-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"name": "gr01-mavlink", "ok": False}],
                "summary": {"all_passed": True, "hardware_link_ready": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = verify_field_link_report(path)

    assert result["gate_passed"] is False
    assert "summary does not match probes" in " ".join(result["reasons"])


def test_verify_field_link_report_rejects_inconsistent_summary_and_watch(tmp_path) -> None:
    report = {
        "schema_version": 1,
        "mode": "watch",
        "configuration": {"samples_requested": 2},
        "samples": [
            {
                "probes": [{"ok": True}],
                "summary": {"all_passed": True, "hardware_link_ready": True},
            },
        ],
        "summary": {
            "samples_requested": 2,
            "samples_collected": 1,
            "all_passed": True,
            "hardware_link_ready": True,
        },
        "probes": [],
    }
    path = tmp_path / "inconsistent-watch.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    result = verify_field_link_report(path)
    assert result["gate_passed"] is False
    assert "watch hardware_link_ready does not match collected samples" in " ".join(
        result["reasons"]
    )


def test_verify_field_link_report_accepts_partial_watch_route_as_unknown(tmp_path) -> None:
    """A fail-fast watch keeps an unobserved aggregate route result as null."""

    report = {
        "schema_version": 1,
        "mode": "watch",
        "configuration": {"samples_requested": 3},
        "samples": [
            {
                "sample_index": 1,
                "probes": [{"name": "jetson", "ok": False}],
                "route_probes": [
                    {
                        "name": "jetson-route",
                        "transport": "route",
                        "host": "192.168.144.20",
                        "port": 22,
                        "ok": False,
                        "source_address": "192.168.1.13",
                    }
                ],
                "summary": {
                    "all_passed": False,
                    "hardware_link_ready": False,
                    "configuration_valid": True,
                    "route_checks_requested": True,
                    "route_checks_passed": False,
                },
            },
        ],
        # ``None`` means the remaining requested samples were never observed;
        # the aggregate remains fail-closed through all_passed=false.
        "summary": {
            "samples_requested": 3,
            "samples_collected": 1,
            "route_checks_requested": True,
            "route_checks_passed": None,
            "route_source_cidr": "192.168.144.0/24",
            "all_samples_passed": False,
            "all_passed": False,
            "hardware_link_ready": False,
        },
    }
    path = tmp_path / "partial-watch-route.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    result = verify_field_link_report(path, require_ready=False)

    assert result["gate_passed"] is True
    assert result["hardware_link_ready"] is False
    assert result["reasons"] == []


def test_verify_field_link_report_rejects_watch_all_passed_drift(tmp_path) -> None:
    report = {
        "schema_version": 1,
        "mode": "watch",
        "configuration": {"samples_requested": 1},
        "samples": [
            {
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            },
        ],
        "summary": {
            "samples_requested": 1,
            "samples_collected": 1,
            "all_passed": True,
            "all_samples_passed": False,
            "hardware_link_ready": False,
        },
        "probes": [],
    }
    path = tmp_path / "watch-all-passed-drift.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    result = verify_field_link_report(path, require_ready=False)

    assert result["gate_passed"] is False
    assert "watch all_passed does not match collected samples" in " ".join(result["reasons"])


def test_verify_field_link_report_allows_structural_diagnostic_when_not_required(tmp_path) -> None:
    path = tmp_path / "failed-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )
    result = verify_field_link_report(path, require_ready=False)
    assert result["gate_passed"] is True
    assert result["hardware_link_ready"] is False


def test_verify_field_link_report_enforces_optional_freshness_window(tmp_path) -> None:
    path = tmp_path / "fresh-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at_epoch_s": time.time(),
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )

    result = verify_field_link_report(path, require_ready=False, max_age_s=30.0)

    assert result["gate_passed"] is True
    assert result["checks"]["freshness"]["passed"] is True
    assert result["report_timestamp_epoch_s"] is not None
    assert result["report_age_s"] is not None

    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["generated_at_epoch_s"] = time.time() - 31.0
    path.write_text(json.dumps(stale), encoding="utf-8")
    stale_result = verify_field_link_report(path, require_ready=False, max_age_s=30.0)

    assert stale_result["gate_passed"] is False
    assert stale_result["checks"]["freshness"]["passed"] is False
    assert "field-link preflight report is stale" in " ".join(stale_result["reasons"])


def test_verify_field_link_report_rejects_future_timestamp_when_freshness_required(
    tmp_path,
) -> None:
    path = tmp_path / "future-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at_epoch_s": time.time() + 30.0,
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )

    result = verify_field_link_report(path, require_ready=False, max_age_s=60.0)

    assert result["gate_passed"] is False
    assert "timestamp is in the future" in " ".join(result["reasons"])


def test_verify_field_link_report_requires_timestamp_when_freshness_requested(tmp_path) -> None:
    path = tmp_path / "untimestamped-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )

    result = verify_field_link_report(path, require_ready=False, max_age_s=60.0)

    assert result["gate_passed"] is False
    assert result["checks"]["freshness"]["passed"] is False
    assert "timestamp is missing or invalid" in " ".join(result["reasons"])


def test_verify_field_link_cli_accepts_freshness_window_for_diagnostics(tmp_path) -> None:
    path = tmp_path / "fresh-cli-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at_epoch_s": time.time(),
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )

    assert verify_main([str(path), "--allow-unready", "--max-age-s", "30"]) == 0


def test_verify_field_link_cli_preserves_fail_closed_default_and_diagnostic_override(
    tmp_path,
) -> None:
    path = tmp_path / "failed-field-link.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        ),
        encoding="utf-8",
    )

    assert verify_main([str(path)]) == 1
    assert verify_main([str(path), "--allow-unready"]) == 0


def test_watch_field_link_requires_every_sample_and_reports_flapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            {"summary": {"all_passed": True, "hardware_link_ready": True}},
            {"summary": {"all_passed": False, "hardware_link_ready": False}},
            {"summary": {"all_passed": True, "hardware_link_ready": True}},
        )
    )
    calls: list[dict[str, object]] = []

    def fake_report(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return next(responses)

    timestamps = [100.0]
    sleeps: list[float] = []

    def clock() -> float:
        timestamps[0] += 0.1
        return timestamps[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        timestamps[0] += seconds

    monkeypatch.setattr(field_link_preflight, "build_field_link_report", fake_report)
    report = watch_field_link(
        jetson_host="jetson",
        gr01_host="gr01",
        camera_host="camera",
        timeout_s=0.25,
        protocol_checks=True,
        samples=3,
        interval_s=0.5,
        clock=clock,
        sleep=sleep,
    )

    assert len(calls) == 3
    assert all(call["protocol_checks"] is True for call in calls)
    assert report["mode"] == "watch"
    assert report["summary"] == {
        "samples_requested": 3,
        "samples_collected": 3,
        "stop_reason": "samples_complete",
        "first_failure_index": 2,
        "passed_samples": 2,
        "failed_samples": 1,
        "stability_ratio": round(2 / 3, 6),
        "max_consecutive_failures": 1,
        "protocol_checks_requested": True,
        "route_checks_requested": False,
        "route_checks_passed": None,
        "route_source_cidr": None,
        "all_samples_passed": False,
        "hardware_link_ready": False,
        "all_passed": False,
    }
    assert [sample["sample_index"] for sample in report["samples"]] == [1, 2, 3]
    assert sleeps == [0.5, 0.5]


def test_watch_field_link_can_stop_at_first_failure_and_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            {"summary": {"all_passed": True, "hardware_link_ready": True}},
            {"summary": {"all_passed": False, "hardware_link_ready": False}},
            {"summary": {"all_passed": True, "hardware_link_ready": True}},
        )
    )
    monkeypatch.setattr(
        field_link_preflight,
        "build_field_link_report",
        lambda **_: next(responses),
    )
    sleeps: list[float] = []
    report = watch_field_link(
        jetson_host="jetson",
        gr01_host="gr01",
        camera_host="camera",
        samples=3,
        interval_s=0.25,
        stop_on_failure=True,
        sleep=sleeps.append,
    )

    assert report["summary"]["samples_collected"] == 2
    assert report["summary"]["stop_reason"] == "failure"
    assert report["summary"]["first_failure_index"] == 2
    assert report["summary"]["hardware_link_ready"] is False
    assert report["summary"]["all_passed"] is False
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"samples": 0}, "samples must be a positive integer"),
        ({"samples": True}, "samples must be a positive integer"),
        ({"samples": 2, "interval_s": -1.0}, "interval_s must be finite and non-negative"),
        ({"samples": 2, "stop_on_failure": 1}, "stop_on_failure must be a boolean"),
    ],
)
def test_watch_field_link_rejects_invalid_configuration(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        watch_field_link(
            jetson_host="jetson",
            gr01_host="gr01",
            camera_host="camera",
            **kwargs,
        )
