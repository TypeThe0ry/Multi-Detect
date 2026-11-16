import asyncio
import json
from pathlib import Path

from multidetect.polaris_sim.gateway.protocol import MavlinkFrameDecoder, build_mavlink2_frame
from multidetect.polaris_sim.gateway.server import (
    GatewayConfig,
    PolarisSimGateway,
    Vehicle,
    vehicle_for_slot,
)


class _FakeWriter:
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("192.168.1.20", 50000) if name == "peername" else None

    def write(self, data):
        self.output.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _FakeHttpReader:
    def __init__(self, request):
        self.header, _, self.body = request.partition(b"\r\n\r\n")
        self.header += b"\r\n\r\n"

    async def readuntil(self, separator):
        return self.header

    async def readexactly(self, length):
        assert length == len(self.body)
        return self.body


class _FakeTcpClient:
    def __init__(self):
        self.registered = True
        self.queue = asyncio.Queue(maxsize=8)
        self.writer = _FakeWriter()


async def _run_route_smoke():
    vehicles = [vehicle_for_slot(slot, "rw_test", "x500_gimbal") for slot in (5, 6)]
    gateway = PolarisSimGateway(vehicles, GatewayConfig(record_dir=Path("/tmp/polaris-sim-test")))
    try:
        try:
            from pymavlink.dialects.v20 import common as mavlink

            gcs = mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
            heartbeat = mavlink.MAVLink_heartbeat_message(
                mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, mavlink.MAV_STATE_ACTIVE, 3
            ).pack(gcs)
            command = mavlink.MAVLink_command_long_message(
                2, 1, 1000, 0, -25, 0, float("nan"), float("nan"), 44, 0, 0
            ).pack(gcs)
            telemetry = [
                mavlink.MAVLink_heartbeat_message(
                    mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_PX4, 0, 0, mavlink.MAV_STATE_ACTIVE, 3
                ).pack(mavlink.MAVLink(None, srcSystem=sysid, srcComponent=1))
                for sysid in (1, 2)
            ]
        except ImportError:
            heartbeat = build_mavlink2_frame(0, bytes(9), system_id=255)
            command_payload = bytearray(33)
            command_payload[30] = 2
            command_payload[28:30] = (1000).to_bytes(2, "little")
            command = build_mavlink2_frame(76, command_payload)
            telemetry = [build_mavlink2_frame(0, bytes(9), system_id=sysid, component_id=1) for sysid in (1, 2)]

        sends = []
        frames = MavlinkFrameDecoder().feed(heartbeat + command)
        gateway._route_up(heartbeat + command, frames, lambda vehicle, packet: sends.append((vehicle.sysid, packet)))
        await asyncio.gather(*tuple(gateway._tasks))
        sent_by_sysid = {sysid: MavlinkFrameDecoder().feed(packet) for sysid, packet in sends}
        assert [frame.message_id for frame in sent_by_sysid[1]] == [0]
        assert [frame.message_id for frame in sent_by_sysid[2]] == [0, 76]

        client = _FakeTcpClient()
        gateway._clients.add(client)
        await gateway._deliver_down(telemetry[0], vehicles[0])
        await gateway._deliver_down(telemetry[1], vehicles[1])
        assert await client.queue.get() == telemetry[0]
        assert await client.queue.get() == telemetry[1]

        status, health = await gateway._dispatch_http("GET", "/health", {}, {}, "192.168.1.20")
        assert status == 200 and health["vehicles"] == 2
    finally:
        if gateway._tasks:
            await asyncio.gather(*tuple(gateway._tasks), return_exceptions=True)


def test_fake_gcs_and_two_fake_sitl_vehicles_share_tcp_without_cross_routing():
    asyncio.run(_run_route_smoke())


def test_http_controls_enforce_token_and_record_round_trip(tmp_path, monkeypatch):
    async def exercise():
        gateway = PolarisSimGateway(
            [Vehicle(5, 1, "rw_test", "x500_gimbal")],
            GatewayConfig(record_dir=tmp_path),
        )
        monkeypatch.setenv("POLARIS_SIM_TOKEN", "secret")
        status, _ = await gateway._dispatch_http(
            "PUT", "/v1/netem", {"direction": "up", "delay_ms": 10}, {}, "192.168.1.20"
        )
        assert status == 403
        status, data = await gateway._dispatch_http(
            "PUT", "/v1/netem", {"direction": "up", "delay_ms": 10},
            {"authorization": "Bearer secret"}, "192.168.1.20"
        )
        assert status == 200 and data["up"]["delay_ms"] == 10
        started = await gateway.start_recording()
        assert started["status"] == "recording"
        gateway._record("down", 1, b"sample")
        stopped = await gateway.stop_recording()
        assert stopped["packets"] == 1
        replay = await gateway.replay_recording(Path(started["path"]).name)
        assert replay["status"] == "replaying"
        if gateway._replay_task:
            await gateway._replay_task

    asyncio.run(exercise())


def test_http_client_request_reaches_health_handler():
    async def exercise():
        gateway = PolarisSimGateway([vehicle_for_slot(5, "rw_test", "x500_mono_cam")])
        request = b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        writer = _FakeWriter()
        await gateway._handle_http(_FakeHttpReader(request), writer)
        status_line, body = bytes(writer.output).split(b"\r\n\r\n", 1)
        assert b"200 OK" in status_line
        assert json.loads(body)["service"] == "polaris-sim-gateway"
        assert writer.closed

    asyncio.run(exercise())


def test_discovery_contract_contains_gateway_endpoints():
    gateway = PolarisSimGateway([vehicle_for_slot(5, "rw_test", "x500_mono_cam")])
    payload = gateway.discovery()
    assert payload["service"] == "polaris-sim"
    assert payload["mavlink_tcp"] == 5770
    assert payload["mavlink_udp"] == 14550
    assert payload["vehicles"][0]["sysid"] == 1


def test_relay_queue_is_bounded_and_keeps_newest_packet():
    async def exercise():
        vehicle = vehicle_for_slot(5, "rw_test", "x500_mono_cam")
        gateway = PolarisSimGateway([vehicle], GatewayConfig(relay_queue_size=1))
        sends = []
        packets = []
        for sequence in (1, 2):
            payload = bytearray(33)
            payload[30] = 1
            packets.append(build_mavlink2_frame(76, payload, sequence=sequence))
        for packet in packets:
            frames = MavlinkFrameDecoder().feed(packet)
            gateway._route_up(packet, frames, lambda target, body: sends.append(body))
        await asyncio.gather(*tuple(gateway._tasks))
        assert sends == [packets[1]]
        assert vehicle.dropped_packets == 1

    asyncio.run(exercise())


def test_gps_raw_int_uses_fix_type_wire_offset():
    vehicle = vehicle_for_slot(5, "rw_test", "x500_mono_cam")
    gateway = PolarisSimGateway([vehicle])
    payload = bytearray(30)
    payload[24] = 1
    payload[28] = 3
    frame = MavlinkFrameDecoder().feed(build_mavlink2_frame(24, payload))[0]
    gateway._observe_vehicle_frame(vehicle, frame)
    assert vehicle.gps_fix_type == 3
