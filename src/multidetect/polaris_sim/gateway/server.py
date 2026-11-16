"""MAVLink, control HTTP and discovery services for a Polaris simulation."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .netem import NetemConfig, NetemShaper
from .protocol import MavlinkFrame, MavlinkFrameDecoder, parse_datagram, route_frames

DISCOVERY_GROUP = "239.255.77.77"
DEFAULT_TCP_PORT = 5770
DEFAULT_UDP_PORT = 14550
DEFAULT_HTTP_PORT = 8780
DEFAULT_DISCOVERY_PORT = 8781
DEFAULT_RTSP_PORT = 8554
DEFAULT_AI_PORT = 8790


@dataclass(slots=True)
class Vehicle:
    slot: int
    sysid: int
    world: str
    airframe: str
    control_port: int | None = None
    telemetry_port: int | None = None
    status: str = "starting"
    last_heartbeat: float | None = None
    gps_fix_type: int | None = None
    last_sim_time_ms: int | None = None
    last_sim_sample: float | None = None
    rtf: float | None = None
    rx_packets: int = 0
    tx_packets: int = 0
    dropped_packets: int = 0
    tcp_queue_drops: int = 0
    messages: dict[str, int] = field(default_factory=dict)

    def to_mapping(self, *, now: float | None = None) -> dict[str, object]:
        clock = time.monotonic() if now is None else now
        heartbeat_age = None if self.last_heartbeat is None else max(0.0, clock - self.last_heartbeat)
        return {
            "slot": self.slot,
            "sysid": self.sysid,
            "world": self.world,
            "airframe": self.airframe,
            "status": self.status if heartbeat_age is None or heartbeat_age < 5.0 else "disconnected",
            "last_heartbeat_age_s": heartbeat_age,
            "gps_fix_type": self.gps_fix_type,
            "rtf": self.rtf,
            "ports": {"control": self.control_port, "telemetry": self.telemetry_port},
            "rx_packets": self.rx_packets,
            "tx_packets": self.tx_packets,
            "dropped_packets": self.dropped_packets,
            "tcp_queue_drops": self.tcp_queue_drops,
            "messages": dict(self.messages),
        }


@dataclass(frozen=True, slots=True)
class VideoSourceConfig:
    vehicle_index: int
    camera: str
    container: str
    world: str
    model: str
    topic: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    bind_host: str = "0.0.0.0"
    tcp_port: int = DEFAULT_TCP_PORT
    udp_port: int = DEFAULT_UDP_PORT
    http_port: int = DEFAULT_HTTP_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    rtsp_port: int = DEFAULT_RTSP_PORT
    ai_port: int = DEFAULT_AI_PORT
    record_dir: Path = Path("/data/scratch/polaris-sim/recordings")
    state_file: Path = Path("/data/scratch/polaris-sim/state.json")
    netem_seed: int = 1
    host_address: str | None = None
    tcp_queue_size: int = 64
    relay_queue_size: int = 64
    video_sources: tuple[VideoSourceConfig, ...] = ()


@dataclass(slots=True)
class _TcpClient:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    queue: asyncio.Queue[bytes]
    connected_at: float
    registered: bool = False
    overflow_count: int = 0
    writer_task: asyncio.Task[None] | None = None

    @property
    def peer(self) -> str:
        info = self.writer.get_extra_info("peername")
        return str(info[0]) if info else "unknown"


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, callback) -> None:
        self.callback = callback
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.callback(data, addr)

    def error_received(self, exc) -> None:
        return None


class PolarisSimGateway:
    """Run a sysid-aware bidirectional MAVLink gateway with bounded client queues."""

    def __init__(self, vehicles: list[Vehicle], config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()
        self.vehicles = {vehicle.sysid: vehicle for vehicle in vehicles}
        if len(self.vehicles) != len(vehicles) or not vehicles:
            raise ValueError("vehicles must be non-empty with unique sysids")
        if any(vehicle.sysid != vehicle.slot - 4 for vehicle in vehicles):
            raise ValueError("slot 5/6/7 must map to sysid 1/2/3")
        if self.config.tcp_queue_size <= 0 or self.config.relay_queue_size <= 0:
            raise ValueError("TCP and relay queue sizes must be positive")
        if any(vehicle.control_port is None for vehicle in vehicles):
            for vehicle in vehicles:
                vehicle.control_port = 14560 + 10 * vehicle.slot
                vehicle.telemetry_port = vehicle.control_port + 1
        self.netem = NetemShaper(seed=self.config.netem_seed)
        self.tcp_server: asyncio.AbstractServer | None = None
        self.http_server: asyncio.AbstractServer | None = None
        self._udp_transports: list[asyncio.DatagramTransport] = []
        self._clients: set[_TcpClient] = set()
        self._udp_gcs: dict[tuple[str, int], float] = {}
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._discovery_transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._relay_queues: dict[tuple[str, int], asyncio.Queue[bytes]] = {}
        self._relay_workers: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._started_at = time.monotonic()
        self._record_stream = None
        self._record_path: Path | None = None
        self._record_lock = asyncio.Lock()
        self._record_packets = 0
        self._replay_task: asyncio.Task[None] | None = None
        self._closing = False
        self.video_service = None

    async def start(self) -> None:
        self.tcp_server = await asyncio.start_server(self._handle_tcp, self.config.bind_host, self.config.tcp_port)
        self.http_server = await asyncio.start_server(self._handle_http, self.config.bind_host, self.config.http_port)
        loop = asyncio.get_running_loop()
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self._from_gcs_udp),
            local_addr=(self.config.bind_host, self.config.udp_port),
        )
        self._udp_transports.append(self._udp_transport)
        for vehicle in self.vehicles.values():
            transport, _ = await loop.create_datagram_endpoint(
                lambda sysid=vehicle.sysid: _UdpProtocol(lambda data, addr: self._from_vehicle(sysid, data, addr)),
                local_addr=(self.config.bind_host, int(vehicle.telemetry_port)),
            )
            self._udp_transports.append(transport)
        self._discovery_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
        if self.config.video_sources:
            from ..video.pipeline import VideoService

            self.video_service = VideoService(
                self.config.bind_host,
                self.config.rtsp_port,
                shaper=self.netem,
            )
            for source in self.config.video_sources:
                self.video_service.add_gazebo_camera(
                    source.vehicle_index,
                    source.camera,
                    source.container,
                    source.world,
                    source.model,
                    topic=source.topic,
                )
            self.video_service.start()
        self._track(asyncio.create_task(self._publish_discovery(), name="polaris-discovery"))

    async def close(self) -> None:
        self._closing = True
        if self.video_service is not None:
            self.video_service.stop()
            self.video_service = None
        if self._replay_task is not None:
            self._replay_task.cancel()
        for server in (self.tcp_server, self.http_server):
            if server is not None:
                server.close()
        await asyncio.gather(
            *(server.wait_closed() for server in (self.tcp_server, self.http_server) if server is not None),
            return_exceptions=True,
        )
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for client in tuple(self._clients):
            client.writer.close()
        for transport in self._udp_transports:
            transport.close()
        if self._discovery_transport is not None:
            self._discovery_transport.close()
        await self.stop_recording()

    def health(self) -> dict[str, object]:
        return {
            "service": "polaris-sim-gateway",
            "status": "closing" if self._closing else "ok",
            "uptime_s": max(0.0, time.monotonic() - self._started_at),
            "vehicles": len(self.vehicles),
            "tcp_clients": len(self._clients),
            "udp_gcs": len(self._udp_gcs),
            "recording": self._record_path is not None,
            "recording_path": None if self._record_path is None else str(self._record_path),
            "video_streams": 0 if self.video_service is None else len(self.video_service.server.streams),
        }

    def vehicle_rows(self) -> list[dict[str, object]]:
        return [self.vehicles[sysid].to_mapping() for sysid in sorted(self.vehicles)]

    def discovery(self) -> dict[str, object]:
        host = self.config.host_address or discover_lan_address()
        return {
            "service": "polaris-sim",
            "version": 1,
            "host": host,
            "mavlink_tcp": self.config.tcp_port,
            "mavlink_udp": self.config.udp_port,
            "http": self.config.http_port,
            "rtsp": self.config.rtsp_port,
            "ai": self.config.ai_port,
            "vehicles": self.vehicle_rows(),
        }

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _from_gcs_udp(self, data: bytes, addr: tuple[str, int]) -> None:
        if not _is_private_peer(addr[0]):
            return
        frames = parse_datagram(data)
        if any(frame.is_heartbeat for frame in frames):
            self._udp_gcs[addr] = time.monotonic()
        if addr not in self._udp_gcs:
            return
        self._route_up(data, frames, lambda vehicle, packet: self._send_to_vehicle(vehicle, packet))

    def _from_vehicle(self, expected_sysid: int, data: bytes, addr: tuple[str, int]) -> None:
        if not _is_private_peer(addr[0]):
            return
        vehicle = self.vehicles.get(expected_sysid)
        if vehicle is None:
            return
        frames = parse_datagram(data)
        if not frames:
            return
        vehicle.rx_packets += len(frames)
        accepted: list[bytes] = []
        for frame in frames:
            if frame.system_id == expected_sysid:
                accepted.append(frame.raw)
                self._observe_vehicle_frame(vehicle, frame)
                self._record("down", expected_sysid, frame.raw)
            else:
                vehicle.messages["unexpected_sysid"] = vehicle.messages.get("unexpected_sysid", 0) + 1
        if accepted:
            self._enqueue_relay("down", b"".join(accepted), vehicle)

    def _route_up(self, data: bytes, frames: list[MavlinkFrame], send) -> None:
        packets = route_frames(frames, set(self.vehicles))
        for frame in frames:
            if not frame.is_heartbeat and frame.target_system not in {0, *self.vehicles}:
                vehicle = next(iter(self.vehicles.values()))
                vehicle.messages["unroutable"] = vehicle.messages.get("unroutable", 0) + 1
        for sysid, packet in packets.items():
            vehicle = self.vehicles[sysid]
            vehicle.tx_packets += len(parse_datagram(packet))
            self._record("up", sysid, packet)
            self._enqueue_relay("up", packet, vehicle, lambda body, v=vehicle: send(v, body))

    def _enqueue_relay(self, direction: str, packet: bytes, vehicle: Vehicle, sender=None) -> None:
        key = (direction, vehicle.sysid)
        queue = self._relay_queues.setdefault(key, asyncio.Queue(maxsize=self.config.relay_queue_size))
        if queue.full():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                pass
            vehicle.dropped_packets += 1
        queue.put_nowait(packet)
        worker = self._relay_workers.get(key)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._drain_relay_queue(direction, vehicle, queue, sender),
                name=f"polaris-relay-{direction}-sysid-{vehicle.sysid}",
            )
            self._relay_workers[key] = worker
            self._track(worker)

    async def _drain_relay_queue(
        self, direction: str, vehicle: Vehicle, queue: asyncio.Queue[bytes], sender=None
    ) -> None:
        key = (direction, vehicle.sysid)
        current = asyncio.current_task()
        try:
            while not queue.empty():
                packet = queue.get_nowait()
                try:
                    if direction == "down":
                        callback = lambda body: self._fanout_down(body, vehicle)
                    else:
                        callback = sender or (lambda body: self._send_to_vehicle(vehicle, body))
                    await self._shape_and_send(direction, packet, callback, vehicle)
                finally:
                    queue.task_done()
        finally:
            if self._relay_workers.get(key) is current:
                self._relay_workers.pop(key, None)

    def _send_to_vehicle(self, vehicle: Vehicle, packet: bytes) -> None:
        if self._udp_transport is not None and vehicle.control_port is not None:
            self._udp_transport.sendto(packet, ("127.0.0.1", vehicle.control_port))

    async def _shape_and_send(self, direction: str, packet: bytes, sender, vehicle: Vehicle) -> None:
        delay = self.netem.shape(direction, packet)  # type: ignore[arg-type]
        if delay is None:
            vehicle.dropped_packets += 1
            return
        if delay:
            await asyncio.sleep(delay)
        sender(packet)

    async def _deliver_down(self, packet: bytes, vehicle: Vehicle) -> None:
        await self._shape_and_send("down", packet, lambda body: self._fanout_down(body, vehicle), vehicle)

    def _fanout_down(self, packet: bytes, vehicle: Vehicle) -> None:
        now = time.monotonic()
        expired = [peer for peer, seen in self._udp_gcs.items() if now - seen > 15.0]
        for peer in expired:
            self._udp_gcs.pop(peer, None)
        for peer in tuple(self._udp_gcs):
            if self._udp_transport is not None:
                self._udp_transport.sendto(packet, peer)
        for client in tuple(self._clients):
            if not client.registered:
                continue
            self._queue_tcp(client, packet, vehicle)

    def _queue_tcp(self, client: _TcpClient, packet: bytes, vehicle: Vehicle) -> None:
        if client.writer.is_closing():
            return
        if client.queue.full():
            try:
                client.queue.get_nowait()
                client.queue.task_done()
            except asyncio.QueueEmpty:
                pass
            client.overflow_count += 1
            vehicle.tcp_queue_drops += 1
        try:
            client.queue.put_nowait(packet)
        except asyncio.QueueFull:
            client.overflow_count += 1

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or not _is_private_peer(peer[0]):
            writer.close()
            await writer.wait_closed()
            return
        client = _TcpClient(reader, writer, asyncio.Queue(maxsize=self.config.tcp_queue_size), time.monotonic())
        client.registered = True
        self._clients.add(client)
        client.writer_task = asyncio.create_task(self._tcp_writer(client), name=f"polaris-tcp-writer-{client.peer}")
        decoder = MavlinkFrameDecoder()
        try:
            while data := await reader.read(65536):
                frames = decoder.feed(data)
                if not frames:
                    continue
                if any(frame.is_heartbeat for frame in frames):
                    client.registered = True
                packet = b"".join(frame.raw for frame in frames)
                self._route_up(packet, frames, lambda vehicle, body: self._send_to_vehicle(vehicle, body))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(client)
            if client.writer_task is not None:
                client.writer_task.cancel()
                await asyncio.gather(client.writer_task, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _tcp_writer(self, client: _TcpClient) -> None:
        try:
            while True:
                packet = await client.queue.get()
                client.writer.write(packet)
                await asyncio.wait_for(client.writer.drain(), timeout=1.0)
                client.queue.task_done()
        except (ConnectionError, asyncio.TimeoutError, asyncio.CancelledError):
            client.writer.close()

    def _observe_vehicle_frame(self, vehicle: Vehicle, frame: MavlinkFrame) -> None:
        vehicle.messages[str(frame.message_id)] = vehicle.messages.get(str(frame.message_id), 0) + 1
        now = time.monotonic()
        if frame.is_heartbeat:
            vehicle.last_heartbeat = now
            vehicle.status = "connected"
        if frame.message_id == 24 and len(frame.payload) > 28:
            vehicle.gps_fix_type = frame.payload[28]
        if frame.message_id == 2 and len(frame.payload) >= 12:  # SYSTEM_TIME time_boot_ms
            sim_ms = int.from_bytes(frame.payload[8:12], "little")
            if vehicle.last_sim_time_ms is not None and vehicle.last_sim_sample is not None:
                wall_delta = now - vehicle.last_sim_sample
                sim_delta = (sim_ms - vehicle.last_sim_time_ms) / 1000.0
                if wall_delta > 0 and sim_delta >= 0:
                    vehicle.rtf = sim_delta / wall_delta
            vehicle.last_sim_time_ms = sim_ms
            vehicle.last_sim_sample = now

    def _record(self, direction: str, sysid: int, packet: bytes) -> None:
        if self._record_stream is None:
            return
        entry = {
            "wall_time": time.time(),
            "direction": direction,
            "sysid": sysid,
            "data_b64": base64.b64encode(packet).decode("ascii"),
        }
        try:
            self._record_stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._record_packets += 1
        except OSError:
            self._record_stream = None

    async def start_recording(self) -> dict[str, object]:
        async with self._record_lock:
            if self._record_stream is not None:
                return {"status": "recording", "path": str(self._record_path), "packets": self._record_packets}
            directory = self.config.record_dir
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self._record_path = directory / f"polaris-sim-{stamp}-{os.getpid()}.jsonl"
            self._record_stream = self._record_path.open("x", encoding="utf-8", buffering=1)
            self._record_packets = 0
            return {"status": "recording", "path": str(self._record_path), "packets": 0}

    async def stop_recording(self) -> dict[str, object]:
        async with self._record_lock:
            stream = self._record_stream
            path = self._record_path
            self._record_stream = None
            if stream is not None:
                stream.flush()
                stream.close()
            return {"status": "stopped", "path": None if path is None else str(path), "packets": self._record_packets}

    async def replay_recording(self, recording_id: str, speed: float = 1.0) -> dict[str, object]:
        if self._replay_task is not None and not self._replay_task.done():
            raise ValueError("a recording replay is already active")
        if not recording_id or Path(recording_id).name != recording_id or not recording_id.endswith(".jsonl"):
            raise ValueError("recording_id must be a .jsonl basename")
        if not 0.1 <= speed <= 8.0:
            raise ValueError("speed must be within 0.1..8")
        path = self.config.record_dir / recording_id
        if not path.is_file():
            raise FileNotFoundError(recording_id)
        self._replay_task = asyncio.create_task(self._replay(path, speed), name="polaris-recording-replay")
        return {"status": "replaying", "recording_id": recording_id, "speed": speed}

    async def _replay(self, path: Path, speed: float) -> None:
        previous: float | None = None
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    if entry.get("direction") != "down":
                        continue
                    stamp = float(entry["wall_time"])
                    packet = base64.b64decode(entry["data_b64"], validate=True)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if previous is not None:
                    await asyncio.sleep(min(1.0, max(0.0, stamp - previous) / speed))
                previous = stamp
                await self._deliver_down(packet, self.vehicles.get(int(entry.get("sysid", 0)), next(iter(self.vehicles.values()))))

    async def _publish_discovery(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            if self._discovery_transport is None:
                continue
            payload = json.dumps(self.discovery(), separators=(",", ":")).encode("utf-8")
            try:
                self._discovery_transport.sendto(payload, (DISCOVERY_GROUP, self.config.discovery_port))
            except OSError:
                continue

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or not _is_private_peer(peer[0]):
            await _write_http(writer, 403, {"error": "private LAN access required"})
            return
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
            if len(header) > 16_384:
                await _write_http(writer, 413, {"error": "request header too large"})
                return
            lines = header.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if not 0 <= length <= 1_048_576:
                await _write_http(writer, 413, {"error": "request body too large"})
                return
            body = await reader.readexactly(length) if length else b""
            payload = json.loads(body) if body else {}
            status, result = await self._dispatch_http(method.upper(), urlsplit(path).path, payload, headers, peer[0])
            await _write_http(writer, status, result)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            await _write_http(writer, 400, {"error": "invalid HTTP request"})
        except Exception as exc:
            await _write_http(writer, 500, {"error": str(exc)})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _dispatch_http(self, method: str, path: str, payload: object, headers: dict[str, str], peer: str):
        if method == "GET" and path == "/health":
            return 200, self.health()
        if method == "GET" and path == "/v1/vehicles":
            return 200, {"vehicles": self.vehicle_rows()}
        if method == "GET" and path == "/v1/discovery":
            return 200, self.discovery()
        if method == "GET" and path == "/v1/netem":
            return 200, self.netem.snapshot()
        if method == "PUT" and path == "/v1/netem":
            if not self._authorized(headers, peer):
                return 403, {"error": "POLARIS_SIM_TOKEN or a private LAN address is required"}
            try:
                self._set_netem(payload)
            except (TypeError, ValueError, AttributeError) as exc:
                return 400, {"error": str(exc)}
            return 200, self.netem.snapshot()
        if method == "POST" and path in {"/v1/record/start", "/v1/record/stop", "/v1/record/replay"}:
            if not self._authorized(headers, peer):
                return 403, {"error": "POLARIS_SIM_TOKEN or a private LAN address is required"}
            if path.endswith("/start"):
                return 200, await self.start_recording()
            if path.endswith("/stop"):
                return 200, await self.stop_recording()
            if not isinstance(payload, dict):
                return 400, {"error": "JSON object required"}
            try:
                return 202, await self.replay_recording(str(payload.get("recording_id", "")), float(payload.get("speed", 1.0)))
            except (ValueError, FileNotFoundError) as exc:
                return 400, {"error": str(exc)}
        if method == "GET" and path == "/v1/doctor":
            return 200, await self._doctor_snapshot()
        return 404, {"error": "not found"}

    def _authorized(self, headers: dict[str, str], peer: str) -> bool:
        token = os.environ.get("POLARIS_SIM_TOKEN", "")
        provided = headers.get("authorization", "").removeprefix("Bearer ")
        if token:
            return provided == token
        return _is_private_peer(peer)

    def _set_netem(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        seed = payload.get("seed")
        if "direction" in payload:
            direction = payload["direction"]
            if direction not in {"up", "down"}:
                raise ValueError("direction must be 'up' or 'down'")
            config = NetemConfig.from_mapping({key: value for key, value in payload.items() if key != "direction" and key != "seed"})
            self.netem.configure(direction, config, reseed=None if seed is None else int(seed))
            return
        for direction in ("up", "down"):
            if direction in payload:
                config = NetemConfig.from_mapping(payload[direction])
                self.netem.configure(direction, config, reseed=None if seed is None else int(seed))

    async def _doctor_snapshot(self) -> dict[str, object]:
        from .doctor import doctor_checks

        return await asyncio.to_thread(
            doctor_checks,
            vehicles=self.vehicle_rows(),
            tcp_port=self.config.tcp_port,
            udp_port=self.config.udp_port,
            http_port=self.config.http_port,
            rtsp_port=self.config.rtsp_port,
            ai_port=self.config.ai_port,
            discovery=self.discovery(),
        )


async def _write_http(writer: asyncio.StreamWriter, status: int, payload: object) -> None:
    reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large", 500: "Internal Server Error"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    response = (
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    writer.write(response)
    try:
        await asyncio.wait_for(writer.drain(), timeout=1.0)
    except (asyncio.TimeoutError, ConnectionError):
        return


def _is_private_peer(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_private or address.is_loopback
    except ValueError:
        return False


def discover_lan_address() -> str:
    candidates: list[ipaddress.IPv4Address] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            address = ipaddress.ip_address(item[4][0])
            if address.is_private and not address.is_loopback:
                candidates.append(address)
    except OSError:
        pass
    for address in candidates:
        if str(address).startswith("192.168."):
            return str(address)
    return str(candidates[0]) if candidates else "127.0.0.1"


def vehicle_for_slot(slot: int, world: str, airframe: str) -> Vehicle:
    if slot not in {5, 6, 7}:
        raise ValueError("Polaris SIM slots are 5, 6 and 7")
    control = 14560 + 10 * slot
    return Vehicle(slot, slot - 4, world, airframe, control, control + 1)


__all__ = [
    "GatewayConfig",
    "PolarisSimGateway",
    "Vehicle",
    "VideoSourceConfig",
    "discover_lan_address",
    "vehicle_for_slot",
]
