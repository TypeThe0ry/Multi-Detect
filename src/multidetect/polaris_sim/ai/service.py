"""HTTP service for Sirius-backed Polaris simulation perception queries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .contracts import ContractError, validate_spica_request, validate_spica_response
from .events import EventJournal
from .inputs import FrameSample, SimulationInputs, bind_frame, is_private_address, unbind_frame
from .runtime import COMPONENTS, SiriusRuntimeBridge, SiriusUnavailable, load_sirius_bridge
from .tunnel import SignedTunnelPublisher

API_VERSION = "1.0"
MAX_BODY_BYTES = 1_048_576
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _private_peer(value: str) -> bool:
    try:
        return is_private_address(value)
    except (ValueError, TypeError):
        return False


class AIService:
    def __init__(
        self,
        config: Mapping[str, object],
        *,
        start_inputs: bool = True,
        camera_source: object | None = None,
    ) -> None:
        self.config = dict(config)
        self.journal = EventJournal(int(config.get("event_capacity", 2048)))
        input_config = config.get("inputs", {})
        self.inputs = SimulationInputs(input_config if isinstance(input_config, Mapping) else {})
        if camera_source is not None:
            self.inputs.camera = camera_source
        if start_inputs:
            self.inputs.start()
        target_push = config.get("target_push", {})
        self.publisher = SignedTunnelPublisher(
            target_push if isinstance(target_push, Mapping) else {}
        )
        runtime_config = config.get("runtime", {})
        runtime_config = runtime_config if isinstance(runtime_config, Mapping) else {}
        components = runtime_config.get("components", {})
        adapters = config.get("adapters", {})
        components = components if isinstance(components, Mapping) else {}
        adapters = adapters if isinstance(adapters, Mapping) else {}
        source = os.environ.get("SIRIUS_RUNTIME_SRC") or config.get("sirius_source")
        self.runtime: SiriusRuntimeBridge | None = None
        self.runtime_error: str | None = None
        try:
            self.runtime = load_sirius_bridge(
                source=source if isinstance(source, str) and source else None,
                components=components,
                adapters=adapters,
                journal=self.journal,
                publisher=self.publisher,
            )
        except (SiriusUnavailable, ImportError, ModuleNotFoundError, ValueError) as exc:
            self.runtime_error = str(exc)
        self.token = os.environ.get("POLARIS_SIM_TOKEN", "")
        self.max_concurrent_queries = max(1, min(4, int(config.get("max_concurrent_queries", 2))))
        self._query_slots = threading.BoundedSemaphore(self.max_concurrent_queries)
        self._track_lock = threading.Lock()
        self._tracks: dict[str, dict[str, object]] = {}

    def close(self) -> None:
        self.inputs.close()
        self.publisher.close()

    def status(self) -> dict[str, object]:
        components = self.runtime.status() if self.runtime is not None else {
            "available": False,
            "version": None,
            "components": {
                name: {
                    "enabled": False,
                    "available": False,
                    "stub": True,
                    "version": None,
                    "latency_ms": None,
                }
                for name in (*COMPONENTS[:-1], "publish_polaris")
            },
            "degradation_order": [
                "full",
                "locate_only",
                "detect_only",
                "review_only",
                "track_only",
                "abstain",
            ],
        }
        return {
            "api_version": API_VERSION,
            "service": "polaris-sim-ai",
            "runtime": components,
            "runtime_error": self.runtime_error,
            "inputs": {
                "camera": "ffmpeg_cpu",
                "telemetry": {
                    "endpoint": self.inputs.telemetry.endpoint,
                    "read_only": True,
                    "state": self.inputs.telemetry.status,
                },
                "fallback_video_configured": bool(self.inputs.camera.fallback_video),
            },
            "target_push": {
                "enabled": self.publisher.enabled,
                "active": self.publisher.active,
                "protocol": "signed-mavlink-tunnel-type-22",
                "reason": self.publisher.reason,
            },
            "tracking_sessions": len(self._tracks),
        }

    def query_target(self, body: Mapping[str, object], slot: int) -> dict[str, object]:
        normalized = self._normalize_query(body)
        request_id = normalized["request_id"]
        started = time.perf_counter()
        if not self._query_slots.acquire(timeout=0.1):
            raise ServiceError(503, "busy", "query capacity is temporarily full")
        sample: FrameSample | None = None
        token = None
        self.journal.bind(request_id=request_id, slot=slot, started_at=time.monotonic())
        if self.runtime is not None:
            self.runtime.event_sink.bind()
        try:
            sample = self.inputs.capture(slot)
            token = bind_frame(sample)
            if self.runtime is None:
                envelope = self._unavailable_envelope(
                    request_id,
                    self.runtime_error or "Sirius runtime unavailable",
                )
                runtime_info = {
                    "status": "disabled",
                    "degradation": "abstain",
                    "reason": self.runtime_error,
                }
            elif normalized["kind"] == "prompt":
                envelope, result = self.runtime.query_prompt(
                    str(normalized["prompt"]),
                    request_id=request_id,
                    outputs=normalized["outputs"],
                    watch=(
                        normalized["watch"].get("enabled", False)
                        if isinstance(normalized["watch"], Mapping)
                        else bool(normalized["watch"])
                    ),
                    frame=sample,
                )
                runtime_info = self._runtime_info(result)
            else:
                envelope, result = self.runtime.query_structured(
                    normalized["request"],
                    frame=sample,
                )
                runtime_info = self._runtime_info(result)
            if sample.warning:
                envelope["warnings"] = [*envelope.get("warnings", []), f"camera:{sample.warning}"]
            elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
            envelope["latency_ms"] = elapsed_ms
            validate_spica_response(envelope)
            return {
                "api_version": API_VERSION,
                "result": envelope,
                "runtime": runtime_info,
                "input": {
                    "vehicle_slot": slot,
                    "frame_id": sample.frame_id,
                    "frame_source": sample.source,
                    "telemetry_available": bool(sample.telemetry.get("fresh", False)),
                    "telemetry_age_ms": sample.telemetry.get("age_ms"),
                },
            }
        finally:
            if token is not None:
                unbind_frame(token)
            if self.runtime is not None:
                self.runtime.event_sink.unbind()
            self.journal.unbind()
            self._query_slots.release()

    @staticmethod
    def _runtime_info(result: object) -> dict[str, object]:
        status = getattr(getattr(result, "status", None), "value", "failed")
        stage = getattr(getattr(result, "stage", None), "value", "abstain")
        return {
            "status": status,
            "degradation": stage,
            "reason": getattr(result, "reason", None),
        }

    @staticmethod
    def _unavailable_envelope(request_id: str, reason: str) -> dict[str, object]:
        return {
            "id": request_id,
            "name": "query_target",
            "status": "unavailable",
            "latency_ms": 0.0,
            "data": None,
            "warnings": [reason],
        }

    @staticmethod
    def _normalize_query(body: Mapping[str, object]) -> dict[str, object]:
        request_id = body.get("request_id", uuid.uuid4().hex)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ServiceError(400, "invalid_request", "request_id must be a non-empty string")
        if "prompt" in body:
            allowed = {"request_id", "prompt", "outputs", "watch"}
            if set(body) - allowed:
                raise ServiceError(400, "invalid_request", "prompt request has unknown fields")
            prompt = body["prompt"]
            outputs = body.get("outputs")
            watch = body.get("watch", False)
            if not isinstance(prompt, str) or not prompt.strip():
                raise ServiceError(400, "invalid_request", "prompt must be a non-empty string")
            if outputs is not None and (
                not isinstance(outputs, list)
                or not all(isinstance(item, str) and item for item in outputs)
            ):
                raise ServiceError(400, "invalid_request", "outputs must be an array of names")
            if not isinstance(watch, (bool, Mapping)):
                raise ServiceError(400, "invalid_request", "watch must be boolean or object")
            return {
                "kind": "prompt",
                "request_id": request_id,
                "prompt": prompt.strip(),
                "outputs": outputs,
                "watch": watch,
            }
        if "query" not in body and "schema_version" in body:
            request = {"request_id": request_id, "query": dict(body)}
        else:
            request = dict(body)
        if "request_id" not in request:
            request["request_id"] = request_id
        try:
            validate_spica_request(request)
        except ContractError as exc:
            raise ServiceError(400, "invalid_spica_query", str(exc)) from exc
        return {"kind": "structured", "request_id": request_id, "request": request}

    def set_components(self, body: Mapping[str, object]) -> dict[str, object]:
        components = body.get("components")
        if not isinstance(components, Mapping):
            raise ServiceError(400, "invalid_request", "body must contain a components object")
        if self.runtime is None:
            raise ServiceError(
                503,
                "runtime_unavailable",
                self.runtime_error or "Sirius runtime unavailable",
            )
        try:
            updated = self.runtime.set_components(components)
        except ValueError as exc:
            raise ServiceError(400, "invalid_components", str(exc)) from exc
        return {"api_version": API_VERSION, "components": updated}

    def track(self, body: Mapping[str, object], slot: int) -> dict[str, object]:
        track_id = body.get("track_id")
        bbox = body.get("target_bbox")
        image_size = body.get("image_size")
        fov = body.get("camera_fov_deg")
        if not isinstance(track_id, str) or not track_id.strip():
            raise ServiceError(400, "invalid_request", "track_id is required")
        if (
            not isinstance(bbox, Mapping)
            or not isinstance(image_size, Mapping)
            or not isinstance(fov, Mapping)
        ):
            raise ServiceError(
                400,
                "invalid_request",
                "target_bbox, image_size and camera_fov_deg are required",
            )
        try:
            x, y, width, height = (float(bbox[key]) for key in ("x", "y", "width", "height"))
            image_width, image_height = (float(image_size[key]) for key in ("width", "height"))
            hfov, vfov = (float(fov[key]) for key in ("horizontal", "vertical"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError(
                400,
                "invalid_request",
                "pixel box, image size and FOV values must be numeric",
            ) from exc
        values = (x, y, width, height, image_width, image_height, hfov, vfov)
        if (
            not all(math.isfinite(value) for value in values)
            or width <= 0
            or height <= 0
            or image_width <= 0
            or image_height <= 0
        ):
            raise ServiceError(400, "invalid_request", "dimensions must be finite and positive")
        if not 1 <= hfov < 179 or not 1 <= vfov < 179:
            raise ServiceError(
                400,
                "invalid_request",
                "camera FOV must be between 1 and 179 degrees",
            )
        focal_x = (image_width / 2.0) / math.tan(math.radians(hfov / 2.0))
        focal_y = (image_height / 2.0) / math.tan(math.radians(vfov / 2.0))
        center_x, center_y = x + width / 2.0, y + height / 2.0
        yaw_delta = math.degrees(math.atan2(center_x - image_width / 2.0, focal_x))
        pitch_delta = math.degrees(math.atan2(image_height / 2.0 - center_y, focal_y))
        yaw_delta = max(-15.0, min(15.0, yaw_delta))
        pitch_delta = max(-15.0, min(15.0, pitch_delta))
        now = time.time()
        with self._track_lock:
            expired = [
                key
                for key, value in self._tracks.items()
                if now - float(value["updated_at"]) > 600.0
            ]
            for key in expired:
                self._tracks.pop(key, None)
            prior = self._tracks.get(track_id)
            state = "tracking" if prior and now - float(prior["updated_at"]) <= 2.0 else "acquired"
            self._tracks[track_id] = {"slot": slot, "updated_at": now, "state": state}
        return {
            "api_version": API_VERSION,
            "track_id": track_id,
            "vehicle_slot": slot,
            "state": state,
            "suggestion": {
                "yaw_delta_deg": yaw_delta,
                "pitch_delta_deg": pitch_delta,
                "frame": "camera_body_relative",
                "valid_for_ms": 500,
            },
            "advisory_only": True,
            "mavlink_sent": False,
            "flight_control_enabled": False,
        }


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def create_server(
    service: AIService,
    host: str = "0.0.0.0",
    port: int = 8790,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "polaris-sim-ai/1.0"
        protocol_version = "HTTP/1.1"

        @property
        def app(self) -> AIService:
            return self.server.ai_service  # type: ignore[attr-defined]

        def log_message(self, format_string: str, *args: object) -> None:
            if os.environ.get("POLARIS_SIM_AI_ACCESS_LOG") == "1":
                super().log_message(format_string, *args)

        def _authorized_peer(self, *, write: bool = False) -> bool:
            peer = self.client_address[0]
            if not _private_peer(peer):
                self._send_error(
                    403,
                    "private_network_required",
                    "service accepts private-network clients only",
                )
                return False
            if write and self.app.token:
                authorization = self.headers.get("Authorization", "")
                supplied = authorization.removeprefix("Bearer ")
                if not hmac.compare_digest(supplied, self.app.token):
                    self._send_error(401, "unauthorized", "a valid bearer token is required")
                    return False
            return True

        def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_error(self, status: int, code: str, message: str) -> None:
            self._send_json(
                status,
                {"api_version": API_VERSION, "error": {"code": code, "message": message}},
            )

        def _read_json(self) -> Mapping[str, object]:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ServiceError(411, "length_required", "Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ServiceError(
                    400,
                    "invalid_length",
                    "Content-Length must be an integer",
                ) from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise ServiceError(413, "body_too_large", "request body exceeds one MiB")
            if self.headers.get_content_type() != "application/json":
                raise ServiceError(
                    415,
                    "content_type_required",
                    "Content-Type must be application/json",
                )
            try:
                value = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ServiceError(400, "invalid_json", "request body is not valid JSON") from exc
            if not isinstance(value, Mapping):
                raise ServiceError(400, "invalid_request", "request body must be a JSON object")
            return value

        @staticmethod
        def _slot(headers: Mapping[str, str]) -> int:
            value = headers.get("X-Polaris-Slot", "5")
            try:
                slot = int(value)
            except ValueError as exc:
                raise ServiceError(
                    400,
                    "invalid_vehicle_slot",
                    "X-Polaris-Slot must be 5, 6 or 7",
                ) from exc
            if slot not in (5, 6, 7):
                raise ServiceError(400, "invalid_vehicle_slot", "X-Polaris-Slot must be 5, 6 or 7")
            return slot

        def do_GET(self) -> None:
            if not self._authorized_peer():
                return
            path = urlsplit(self.path).path
            if path in {"/health", "/v1/status"}:
                self._send_json(200, self.app.status())
            elif path == "/v1/events":
                self._serve_sse()
            elif path == "/v1/events/ws":
                self._serve_websocket()
            else:
                self._send_error(404, "not_found", "unknown endpoint")

        def do_POST(self) -> None:
            if not self._authorized_peer(write=True):
                return
            path = urlsplit(self.path).path
            try:
                body = self._read_json()
                if path == "/v1/query_target":
                    slot = self._slot(self.headers)
                    self._send_json(200, self.app.query_target(body, slot))
                elif path == "/v1/components":
                    self._send_json(200, self.app.set_components(body))
                elif path == "/v1/track":
                    slot = self._slot(self.headers)
                    self._send_json(200, self.app.track(body, slot))
                else:
                    self._send_error(404, "not_found", "unknown endpoint")
            except ServiceError as exc:
                self._send_error(exc.status, exc.code, str(exc))
            except ContractError as exc:
                self._send_error(400, "invalid_spica_query", str(exc))
            except Exception:
                self._send_error(500, "internal_error", "request failed")

        def _event_cursor(self) -> int:
            header = self.headers.get("Last-Event-ID")
            query = parse_qs(urlsplit(self.path).query)
            raw = (
                header
                if header is not None
                else query.get("after", [str(self.app.journal.latest_id())])[0]
            )
            try:
                cursor = int(raw)
            except ValueError as exc:
                raise ServiceError(
                    400,
                    "invalid_event_id",
                    "Last-Event-ID must be an integer",
                ) from exc
            if cursor < 0:
                raise ServiceError(400, "invalid_event_id", "event id cannot be negative") from None
            return cursor

        def _serve_sse(self) -> None:
            try:
                cursor = self._event_cursor()
            except ServiceError as exc:
                self._send_error(exc.status, exc.code, str(exc))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    event = self.app.journal.next_after(cursor, 15.0)
                    if event is None:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    cursor = int(event["id"])
                    self.wfile.write(self.app.journal.encode_sse(event))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _serve_websocket(self) -> None:
            try:
                cursor = self._event_cursor()
            except ServiceError as exc:
                self._send_error(exc.status, exc.code, str(exc))
                return
            key = self.headers.get("Sec-WebSocket-Key", "")
            if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                self._send_error(
                    400,
                    "websocket_handshake_required",
                    "WebSocket upgrade headers are required",
                )
                return
            digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode()).digest()
            accept = base64.b64encode(digest).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            closed = threading.Event()
            write_lock = threading.Lock()

            def send_frame(opcode: int, payload: bytes) -> None:
                size = len(payload)
                header = bytearray([0x80 | opcode])
                if size < 126:
                    header.append(size)
                elif size < 65_536:
                    header.extend([126, (size >> 8) & 0xFF, size & 0xFF])
                else:
                    header.append(127)
                    header.extend(size.to_bytes(8, "big"))
                with write_lock:
                    self.wfile.write(header + payload)
                    self.wfile.flush()

            def read_exact(size: int) -> bytes:
                data = self.rfile.read(size)
                if len(data) != size:
                    raise EOFError
                return data

            def read_client_frames() -> None:
                try:
                    while not closed.is_set():
                        first, second = read_exact(2)
                        opcode = first & 0x0F
                        length = second & 0x7F
                        if length == 126:
                            length = int.from_bytes(read_exact(2), "big")
                        elif length == 127:
                            length = int.from_bytes(read_exact(8), "big")
                        masked = bool(second & 0x80)
                        mask = read_exact(4) if masked else b""
                        if length > 65_536:
                            closed.set()
                            return
                        payload = read_exact(length)
                        if masked:
                            payload = bytes(
                                item ^ mask[index % 4]
                                for index, item in enumerate(payload)
                            )
                        if opcode == 0x8:
                            closed.set()
                            try:
                                send_frame(0x8, payload[:125])
                            except OSError:
                                pass
                            return
                        if opcode == 0x9:
                            send_frame(0xA, payload)
                except (EOFError, OSError, ValueError):
                    closed.set()

            threading.Thread(
                target=read_client_frames,
                name="polaris-sim-ai-ws-reader",
                daemon=True,
            ).start()
            try:
                while not closed.is_set():
                    event = self.app.journal.next_after(cursor, 15.0)
                    if event is None:
                        continue
                    cursor = int(event["id"])
                    payload = json.dumps(
                        event,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                    send_frame(0x1, payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                closed.set()

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.ai_service = service  # type: ignore[attr-defined]
    return server

