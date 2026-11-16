from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from multidetect.polaris_sim.ai.inputs import CameraFrameSource
from multidetect.polaris_sim.ai.service import AIService, create_server

SIRIUS_SOURCE = Path("/data/projects/Volatic/Sirius/wt/sirius-runtime/src")


class FakeCamera:
    fallback_video = None

    def capture(self, slot: int) -> tuple[None, str, None]:
        return None, "fixture", None


def test_component_toggle_and_gimbal_advice_without_http() -> None:
    service = AIService(_config(), start_inputs=False, camera_source=FakeCamera())
    try:
        result = service.set_components({"components": {"altair": True, "tracking": True}})
        assert result["components"]["altair"] is True
        assert service.status()["runtime"]["components"]["tracking"]["enabled"] is True
        suggestion = service.track(
            {
                "track_id": "manual-track",
                "target_bbox": {"x": 900, "y": 250, "width": 60, "height": 60},
                "image_size": {"width": 1280, "height": 720},
                "camera_fov_deg": {"horizontal": 90, "vertical": 60},
            },
            slot=7,
        )
        assert suggestion["vehicle_slot"] == 7
        assert suggestion["suggestion"]["yaw_delta_deg"] > 0
        assert suggestion["mavlink_sent"] is False
    finally:
        service.close()


def _config() -> dict[str, object]:
    return {
        "sirius_source": str(SIRIUS_SOURCE),
        "runtime": {
            "components": {
                "altair": False,
                "vega": False,
                "vlm": False,
                "mizar": False,
                "tracking": False,
                "publish_polaris": False,
            }
        },
        "inputs": {"camera": {}, "telemetry": {"enabled": False}},
        "target_push": {"enabled": False},
    }


@contextmanager
def running_server(service: AIService):
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        pytest.skip(f"sandbox disallows loopback sockets: {exc}")
    finally:
        if probe is not None:
            probe.close()
    server = create_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        service.close()


def _post(
    port: int,
    path: str,
    body: dict[str, object],
    token: str | None = None,
) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_status_components_query_and_track(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLARIS_SIM_TOKEN", raising=False)
    service = AIService(_config(), start_inputs=False, camera_source=FakeCamera())
    with running_server(service) as port:
        with urlopen(f"http://127.0.0.1:{port}/v1/status", timeout=3) as response:
            status = json.loads(response.read())
        assert status["service"] == "polaris-sim-ai"
        assert status["runtime"]["available"] is True
        code, components = _post(port, "/v1/components", {"components": {"altair": True}})
        assert code == 200
        assert components["components"]["altair"] is True
        code, result = _post(
            port,
            "/v1/query_target",
            {"request_id": "http-q", "query": {"schema_version": "1.0", "categories": ["vehicle"]}},
        )
        assert code == 200
        assert result["api_version"] == "1.0"
        assert result["result"]["name"] == "query_target"
        code, track = _post(
            port,
            "/v1/track",
            {
                "track_id": "track-1",
                "target_bbox": {"x": 900, "y": 250, "width": 60, "height": 60},
                "image_size": {"width": 1280, "height": 720},
                "camera_fov_deg": {"horizontal": 90, "vertical": 60},
            },
        )
        assert code == 200
        assert track["suggestion"]["yaw_delta_deg"] > 0
        assert track["advisory_only"] is True
        assert track["mavlink_sent"] is False


def test_http_requires_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLARIS_SIM_TOKEN", "test-secret")
    service = AIService(_config(), start_inputs=False, camera_source=FakeCamera())
    with running_server(service) as port:
        code, body = _post(port, "/v1/components", {"components": {"vega": True}})
        assert code == 401
        assert body["error"]["code"] == "unauthorized"
        code, _ = _post(port, "/v1/components", {"components": {"vega": True}}, "test-secret")
        assert code == 200


def _read_sse_event(response: http.client.HTTPResponse) -> tuple[int, str]:
    event_id = int(response.readline().decode().strip().removeprefix("id: "))
    event_name = response.readline().decode().strip().removeprefix("event: ")
    response.readline()
    response.readline()
    return event_id, event_name


def test_sse_reader_uses_event_field_not_payload_name() -> None:
    response = BytesIO(
        b'id: 1\nevent: query.parsed\ndata: {"name":"first"}\n\n'
    )

    assert _read_sse_event(response) == (1, "query.parsed")


def test_camera_source_uses_gateway_rtsp_url_for_vehicle_slot() -> None:
    source = CameraFrameSource(
        {
            "rtsp_template": "rtsp://127.0.0.1:8554/v{index}/cam",
            "rtsp_by_slot": {"5": "rtsp://127.0.0.1:8554/v0/gimbal"},
        }
    )
    requested: list[str] = []
    source._one_jpeg = lambda url, **_: (requested.append(url) or b"jpeg", None)

    assert source.capture(5) == (b"jpeg", "rtsp", None)
    assert requested == ["rtsp://127.0.0.1:8554/v0/gimbal"]


def test_sse_disconnect_reconnect_replays_after_last_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLARIS_SIM_TOKEN", raising=False)
    service = AIService(_config(), start_inputs=False, camera_source=FakeCamera())
    with running_server(service) as port:
        service.journal.publish("query.parsed", {"name": "first"})
        first = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        first.request("GET", "/v1/events", headers={"Last-Event-ID": "0"})
        response = first.getresponse()
        assert response.status == 200
        event_id, event_name = _read_sse_event(response)
        assert (event_id, event_name) == (1, "query.parsed")
        response.close()
        first.close()
        service.journal.publish("vega.detected", {"name": "second"})
        time.sleep(0.05)
        reconnect = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        reconnect.request("GET", "/v1/events", headers={"Last-Event-ID": str(event_id)})
        response = reconnect.getresponse()
        assert _read_sse_event(response) == (2, "vega.detected")
        response.close()
        reconnect.close()


def test_websocket_endpoint_streams_json_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLARIS_SIM_TOKEN", raising=False)
    service = AIService(_config(), start_inputs=False, camera_source=FakeCamera())
    with running_server(service) as port:
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        client.settimeout(3)
        key = base64.b64encode(b"test-websocket-key").decode()
        client.sendall(
            (
                "GET /v1/events/ws?after=0 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        handshake = b""
        while b"\r\n\r\n" not in handshake:
            handshake += client.recv(1024)
        assert b"101 Switching Protocols" in handshake
        service.journal.publish("published", {"request_id": "ws-1"})
        def read_exact(size: int) -> bytes:
            chunks = bytearray()
            while len(chunks) < size:
                chunks.extend(client.recv(size - len(chunks)))
            return bytes(chunks)

        first, second = read_exact(2)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(read_exact(8), "big")
        payload = read_exact(length)
        event = json.loads(payload)
        assert first & 0x0F == 1
        assert event["event"] == "published"
        client.close()
