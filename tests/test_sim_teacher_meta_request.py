"""CPU checks of the pixel-free GETMETA request (MDGZMET1) and the 10 Hz X1 track capture."""

import io
import os
import queue
import struct
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.gimbal_recording import X1TrackCapture
from multidetect.sim_teacher.streams import SensorStreamReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts.gazebo_meta_wire import (  # noqa: E402
    META_WIRE_MAGIC,
    build_meta_wire_record,
    read_meta_wire_frame,
)
from scripts.sim_teacher_frame_server import (  # noqa: E402
    Candidate,
    LatestCandidate,
    encode_candidate,
    encode_meta_candidate,
    serve_requests,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mdgzsim3_get_record.hex"
MS = 1_000_000

JOINTS = {
    "timestamp_ns": 100 * MS, "source_timestamp_ns": 99 * MS, "source_skew_ns": MS,
    "propeller_joint_state": None,
    "gimbal_joint_state": {
        "timestamp_ns": 100 * MS,
        "position_rad": {"pitch": -0.5, "yaw": 0.7, "roll": 0.9},
        "velocity_rad_s": {"pitch": 0.1, "yaw": 0.2, "roll": 0.3},
        "source": "gazebo_joint_state", "fresh": True,
    },
}


def _joints_at(stamp):
    return {**JOINTS, "timestamp_ns": stamp, "source_timestamp_ns": stamp - MS,
            "gimbal_joint_state": {**JOINTS["gimbal_joint_state"], "timestamp_ns": stamp}}


def _candidate(stamp=100 * MS, joints=None):
    return Candidate(
        stamp, 2, 1, b"\x01\x02\x03\x04\x05\x06", stamp,
        {"stamp_ns": stamp, "latitude_deg": 1.0, "longitude_deg": 2.0, "altitude_m": 3.0},
        {"stamp_ns": stamp, "position_east_m": 0, "position_north_m": 0,
         "position_up_m": 3, "orientation_x": 0, "orientation_y": 0,
         "orientation_z": 0, "orientation_w": 1, "velocity_forward_mps": 0,
         "velocity_left_mps": 0, "velocity_up_mps": 0,
         "attitude_covariance_enu_rad2": (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0)},
        stamp, struct.pack("<ff", 1, 2), 2, 1, joint_state=joints,
    )


def _contract():
    return {"source_topic": "/depth", "calibration_id": "depth-v1",
            "calibration_sha256": "a" * 64, "registration_mode": "registered_to_rgb",
            "coordinate_space": "rgb_pixel_aligned", "distance_semantics": "optical_axis_z_m",
            "minimum_depth_m": 0.1, "maximum_depth_m": 100, "fx_px": 1,
            "fy_px": 1, "cx_px": 1, "cy_px": 0.5}


def _pixels(candidate, sequence):
    return encode_candidate(candidate, sequence, _contract(), (2, 1), (2, 1), "FLOAT32_M")


class _Lines:
    """Blocking line source standing in for the request pipe."""

    def __init__(self):
        self._queue = queue.Queue()

    def send(self, text):
        self._queue.put(text.encode("ascii") + b"\n")

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item


class _Out:
    def __init__(self):
        self.buffer = io.BytesIO()
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return self.buffer.write(data)

    def flush(self):
        pass


def _serve(latest, lines, out, pixels=_pixels, **kwargs):
    kwargs.setdefault("request_timeout_s", 2.0)
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(serve_requests(lines, out, latest, pixels, **kwargs)),
        daemon=True,
    )
    thread.start()
    return thread, result


# --- wire ---------------------------------------------------------------------------------

def test_old_get_record_bytes_are_pinned_by_fixture():
    expected = bytes.fromhex(FIXTURE.read_text().strip())
    record = encode_candidate(
        replace(_candidate(), joint_state=JOINTS), 7, _contract(), (2, 1), (2, 1), "UINT16_MM"
    )
    assert record == expected
    assert record.startswith(b"MDGZSIM3") and META_WIRE_MAGIC not in record[:8]


def test_get_reply_stream_is_byte_identical_with_and_without_the_meta_worker():
    latest = LatestCandidate()
    latest.put(_candidate(100 * MS, JOINTS))
    lines, out = _Lines(), _Out()
    thread, result = _serve(latest, lines, out)
    lines.send("GET 100000000")
    deadline = time.monotonic() + 2
    while not out.writes and time.monotonic() < deadline:
        time.sleep(0.005)
    lines.close()
    thread.join(2)
    assert out.buffer.getvalue() == _pixels(_candidate(100 * MS, JOINTS), 0)
    assert result == {"GET": 1, "GETMETA": 0}


def test_meta_record_roundtrip_and_integrity():
    record = encode_meta_candidate(_candidate(100 * MS, JOINTS), 3)
    assert record.startswith(META_WIRE_MAGIC)
    body = read_meta_wire_frame(io.BytesIO(record))
    assert body["sequence"] == 3 and body["sensor_stamp_ns"] == 100 * MS
    assert body["joint_state"] == JOINTS
    assert len(record) < 1024  # no pixels
    corrupt = bytearray(record)
    corrupt[20] ^= 0xFF
    with pytest.raises(ValueError):
        read_meta_wire_frame(io.BytesIO(bytes(corrupt)))
    with pytest.raises(ValueError):
        read_meta_wire_frame(io.BytesIO(record[:-1]))
    with pytest.raises(ValueError, match="finite"):
        read_meta_wire_frame(io.BytesIO(build_meta_wire_record({"sensor_stamp_ns": 1})))


# --- server -------------------------------------------------------------------------------

def test_getmeta_is_strictly_after_and_pixel_free():
    latest = LatestCandidate()
    latest.put(_candidate(100 * MS, JOINTS))
    lines, out = _Lines(), _Out()
    thread, result = _serve(latest, lines, out)
    lines.send("GETMETA 100000000")  # first frame at or after 100 ms -> the one we have
    lines.send("GETMETA 100000000")  # must now wait for a strictly newer frame
    deadline = time.monotonic() + 2
    while len(out.writes) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.05)
    assert len(out.writes) == 1  # second request is parked, not answered with the same frame
    latest.put(_candidate(200 * MS, JOINTS))
    while len(out.writes) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    lines.close()
    thread.join(2)
    stamps = []
    stream = io.BytesIO(out.buffer.getvalue())
    while stream.tell() < len(out.buffer.getvalue()):
        stamps.append(read_meta_wire_frame(stream)["sensor_stamp_ns"])
    assert stamps == [100 * MS, 200 * MS]
    assert result == {"GET": 0, "GETMETA": 2}


def test_get_and_getmeta_interleave_without_corrupting_the_stream():
    latest = LatestCandidate()
    stop = threading.Event()

    def feed():
        stamp = 100 * MS
        while not stop.is_set():
            latest.put(_candidate(stamp, _joints_at(stamp)))
            stamp += 100 * MS
            time.sleep(0.01)

    threading.Thread(target=feed, daemon=True).start()
    lines, out = _Lines(), _Out()
    thread, result = _serve(latest, lines, out)
    for _ in range(3):  # every reply must be strictly newer than that kind's previous reply
        lines.send("GETMETA 0")
        lines.send("GET 0")
    lines.close()
    thread.join(5)
    stop.set()
    assert latest.error is None, latest.error
    assert result == {"GET": 3, "GETMETA": 3}
    reader = SensorStreamReader(
        io.BytesIO(out.buffer.getvalue()), depth_binding=_contract()
    ).start()
    reader.done.wait(2)
    assert "ended early" in str(reader.error)  # clean EOF after the last whole record
    assert latest.error is None, latest.error
    assert reader.frames_received == 3 and reader.meta_received == 3
    assert reader.latest.rgb == b"\x01\x02\x03\x04\x05\x06"
    assert reader.latest_meta.rgb == b""


def test_getmeta_is_not_delayed_by_a_slow_pixel_encode():
    latest = LatestCandidate()
    latest.put(_candidate(100 * MS, JOINTS))

    def slow(candidate, sequence):
        time.sleep(0.4)
        return _pixels(candidate, sequence)

    lines, out = _Lines(), _Out()
    thread, _ = _serve(latest, lines, out, pixels=slow)
    lines.send("GET 100000000")
    time.sleep(0.05)
    started = time.monotonic()
    lines.send("GETMETA 100000000")
    while not out.writes and time.monotonic() - started < 2:
        time.sleep(0.001)
    assert time.monotonic() - started < 0.2
    assert out.writes[0].startswith(META_WIRE_MAGIC)
    lines.close()
    thread.join(3)
    assert out.writes[1].startswith(b"MDGZSIM3")


def test_unknown_verb_still_fails_closed():
    latest = LatestCandidate()
    lines, out = _Lines(), _Out()
    thread, _ = _serve(latest, lines, out)
    lines.send("GETMETAS 1")
    thread.join(2)
    assert not thread.is_alive() and "expected GET|GETMETA" in (latest.error or "")


# --- reader -------------------------------------------------------------------------------

def test_reader_decodes_meta_into_a_separate_slot_with_joint_state():
    pixel = _pixels(_candidate(100 * MS, JOINTS), 0)
    meta = encode_meta_candidate(_candidate(200 * MS, JOINTS), 0)
    reader = SensorStreamReader(
        io.BytesIO(pixel + meta), depth_binding=_contract(), camera_info={"fx": 5.0},
    ).start()
    reader.done.wait(2)
    assert "ended early" in str(reader.error)  # EOF only; both records were decoded whole
    assert reader.latest.sensor_stamp_ns == 100 * MS  # metadata never overwrites the pixel slot
    sample = reader.latest_meta
    assert sample.sensor_stamp_ns == 200 * MS and sample.rgb == b"" and sample.depth is None
    assert sample.joint_state == JOINTS
    assert sample.pose["local_enu_m"] == [0.0, 0.0, 3.0] and sample.camera_info["fx"] == 5.0
    assert reader.meta_received == 1 and reader.frames_received == 1


def test_request_meta_falls_back_to_get_when_unsupported():
    pipe = io.BytesIO()
    legacy = SensorStreamReader(io.BytesIO(), request_stream=pipe)
    assert legacy.request_meta(5) is False
    assert pipe.getvalue() == b"GET 5\n"
    pipe2 = io.BytesIO()
    modern = SensorStreamReader(io.BytesIO(), request_stream=pipe2, meta_requests=True)
    assert modern.request_meta(5) is True and modern.meta_supported
    modern.request(6)
    assert pipe2.getvalue() == b"GETMETA 5\nGET 6\n"
    assert SensorStreamReader(io.BytesIO(), meta_requests=True).meta_supported is False  # no pipe


def test_reader_and_server_end_to_end_meta_round_trip_is_fast_while_pixels_are_slow():
    latest = LatestCandidate()
    stop = threading.Event()

    def feed():
        stamp = 100 * MS
        while not stop.is_set():
            latest.put(_candidate(stamp, _joints_at(stamp)))
            stamp += 100 * MS
            time.sleep(0.02)

    def slow(candidate, sequence):
        time.sleep(0.33)
        return _pixels(candidate, sequence)

    req_r, req_w = os.pipe()
    out_r, out_w = os.pipe()
    stdin, stdout = os.fdopen(req_r, "rb"), os.fdopen(out_w, "wb", buffering=0)
    server = threading.Thread(
        target=lambda: serve_requests(stdin, stdout, latest, slow, request_timeout_s=5.0), daemon=True
    )
    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    server.start()
    reader = SensorStreamReader(
        os.fdopen(out_r, "rb"), request_stream=os.fdopen(req_w, "wb"),
        depth_binding=_contract(), meta_requests=True,
    ).start()
    try:
        reader.request(0)  # a pixel request is in flight for the whole test
        rtts = []
        for _ in range(8):
            before = reader.meta_received
            target = (reader.latest_meta.sensor_stamp_ns if reader.latest_meta else 0) + 1
            began = time.monotonic()
            reader.request_meta(target)
            while reader.meta_received == before and time.monotonic() - began < 2:
                time.sleep(0.0005)
            rtts.append(time.monotonic() - began)
            assert reader.latest_meta.sensor_stamp_ns >= target
        assert max(rtts) < 0.15, rtts
        assert reader.meta_received == 8
    finally:
        stop.set()
        reader.stop()


# --- capture ------------------------------------------------------------------------------

class _FakeServer:
    """Virtual-time request/response server: sim time == wall time, 10 Hz source frames."""

    def __init__(self, meta_rtt_s, pixel_rtt_s, *, meta_supported=True):
        self.meta_supported = meta_supported
        self.latest = None
        self.latest_meta = None
        self.meta_rtt_s, self.pixel_rtt_s = meta_rtt_s, pixel_rtt_s
        self.pending = []  # (arrival_wall_s, slot, stamp)
        self.now = 0.0
        self.start_ns = 0
        self.meta_requests = self.pixel_requests = 0

    def _frame_after(self, asked_ns):
        return max(((asked_ns // 100_000_000) + 1) * 100_000_000, asked_ns + 1)

    def _arrival(self, rtt, stamp):
        # a frame cannot be answered before simulation reaches it
        return max(self.now + rtt, (stamp - self.start_ns) / 1e9)

    def request(self, ns):
        self.pixel_requests += 1
        stamp = self._frame_after(ns)
        self.pending.append((self._arrival(self.pixel_rtt_s, stamp), "latest", stamp))

    def request_meta(self, ns):
        if not self.meta_supported:
            return self.request(ns)
        self.meta_requests += 1
        stamp = self._frame_after(ns)
        self.pending.append((self._arrival(self.meta_rtt_s, stamp), "latest_meta", stamp))
        return True

    def advance(self, wall):
        self.now = wall
        for item in [p for p in self.pending if p[0] <= wall]:
            self.pending.remove(item)
            setattr(self, item[1], SimpleNamespace(sensor_stamp_ns=item[2]))


def _run_capture(server, seconds=60):
    start = 100_000_000_000
    server.start_ns = start
    capture = X1TrackCapture(start)
    samples = []
    for i in range(seconds * 50):  # 50 Hz action ticks
        wall = i / 50.0
        server.advance(wall)
        sample = capture.tick(server, sim_ns=start + i * 20_000_000, wall_s=wall)
        if sample is not None:
            samples.append(sample.sensor_stamp_ns)
    return samples


def test_capture_reaches_ten_hz_with_20ms_meta_round_trips_over_330ms_pixels():
    server = _FakeServer(meta_rtt_s=0.02, pixel_rtt_s=0.33)
    samples = _run_capture(server)
    assert len(samples) / 60 >= 8, len(samples)
    assert server.pixel_requests == 0  # never touched the pixel path
    gaps = [(b - a) / 1e9 for a, b in zip(samples, samples[1:])]
    assert all(g >= 0.1 - 1e-9 for g in gaps) and max(gaps) < 0.5
    assert len(samples) < 620 and samples == sorted(set(samples))


def test_capture_falls_back_to_the_pixel_path_when_meta_is_unsupported():
    server = _FakeServer(meta_rtt_s=0.02, pixel_rtt_s=0.33, meta_supported=False)
    samples = _run_capture(server)
    assert server.meta_requests == 0 and server.pixel_requests > 100
    assert 120 <= len(samples) <= 220  # the legacy ~3 samples/s behaviour, unchanged
    spans = (samples[-1] - samples[0]) / 1e9
    assert spans >= 55


def test_capture_reasks_meta_when_an_answer_is_lost():
    server = _FakeServer(meta_rtt_s=0.02, pixel_rtt_s=0.33)
    original = server.request_meta
    dropped = []

    def lossy(ns):
        if not dropped:
            dropped.append(ns)
            return True  # swallow the first request
        return original(ns)

    server.request_meta = lossy
    samples = _run_capture(server, seconds=5)
    assert dropped and len(samples) >= 20


def test_x1_track_sample_accepts_a_metadata_sample_unchanged():
    from multidetect.sim_teacher.streams import MavlinkState
    from scripts import collect_sim_teacher_episodes as collector

    stamp = 2_000_000_000
    meta = SensorStreamReader(io.BytesIO(), meta_requests=True)._meta_sample(
        {"sensor_stamp_ns": stamp, "camera_info_stamp_ns": stamp, "width": 4, "height": 3,
         "latitude_deg": 1, "longitude_deg": 2, "altitude_m": 3, "position_east_m": 0,
         "position_north_m": 0, "position_up_m": 3, "orientation_x": 0, "orientation_y": 0,
         "orientation_z": 0, "orientation_w": 1, "velocity_forward_mps": 0,
         "velocity_left_mps": 0, "velocity_up_mps": 0,
         "joint_state": {"source_timestamp_ns": stamp - 2_000_000,
                         "gimbal_joint_state": {"position_rad": {"roll": 0.1, "pitch": 0., "yaw": 0.},
                                                "source": "gazebo_joint_state", "fresh": True}}})
    state = MavlinkState(messages={
        "LOCAL_POSITION_NED": {"x": 1, "time_boot_ms": 1990},
        "ATTITUDE": {"roll": 0., "pitch": 0., "yaw": 0., "time_boot_ms": 1995},
        "GLOBAL_POSITION_INT": {"lat": 1, "lon": 2, "alt": 3000, "time_boot_ms": 1998},
        "GPS_RAW_INT": {"fix_type": 3},
    })
    reader = SimpleNamespace(aligned_state=lambda *_a, **_k: state)
    row = collector._x1_track_sample(reader, meta, camera_mount=None, camera_geometry=None)
    assert row["sim_stamp_ns"] == stamp
