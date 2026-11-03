from __future__ import annotations

import random
import socket
import threading
import time
import zlib
from types import SimpleNamespace

import pytest

import multidetect.depth_grid_udp as depth_grid_udp
from multidetect.depth_grid_udp import DepthGridDatagramCodec, DepthGridUdpPublisher
from multidetect.metric_depth import MetricDepthGrid


def _grid() -> MetricDepthGrid:
    random_source = random.Random(42)
    raw = bytes(random_source.randrange(1, 256) for _ in range(160 * 90))
    return MetricDepthGrid(
        width=160,
        height=90,
        minimum_depth_m=1.25,
        maximum_depth_m=24.5,
        quantized_depth=raw,
    )


def test_depth_grid_codec_fragments_and_round_trips_authenticated_frame() -> None:
    codec = DepthGridDatagramCodec(b"d" * 32, maximum_datagram_bytes=400)
    grid = _grid()
    datagrams = codec.encode(grid, sequence=77, sent_at_ms=123_456)

    assert len(datagrams) > 1
    fragments = [codec.decode_fragment(datagram) for datagram in datagrams]
    assert all(len(datagram) <= 400 for datagram in datagrams)
    assert [fragment.fragment_index for fragment in fragments] == list(range(len(fragments)))
    compressed = b"".join(fragment.payload for fragment in fragments)
    assert int.from_bytes(compressed[:4], "big") == 160 * 90
    raw = zlib.decompress(compressed[4:])
    assert raw == grid.quantized_depth
    assert fragments[0].sequence == 77
    assert fragments[0].minimum_depth_mm == 1250
    assert fragments[0].maximum_depth_mm == 24_500
    assert fragments[0].logarithmic_encoding is True
    assert zlib.crc32(raw) & 0xFFFFFFFF == fragments[0].raw_crc32


def test_depth_grid_codec_rejects_tampering() -> None:
    codec = DepthGridDatagramCodec(b"a" * 32)
    datagram = bytearray(codec.encode(_grid(), sequence=1)[0])
    datagram[-17] ^= 0x20

    with pytest.raises(ValueError, match="authentication"):
        codec.decode_fragment(bytes(datagram))


@pytest.mark.usefixtures("require_loopback_sockets")
def test_depth_grid_publisher_uses_fixed_return_port_after_keepalive() -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(1.0)
    destination_port = receiver.getsockname()[1]
    local_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    local_probe.bind(("127.0.0.1", 0))
    local_port = local_probe.getsockname()[1]
    local_probe.close()
    publisher = DepthGridUdpPublisher(
        host="127.0.0.1",
        port=destination_port,
        local_port=local_port,
        hmac_key=b"p" * 32,
    )
    try:
        receiver.sendto(b"MDPD_HELLO_V1", ("127.0.0.1", local_port))
        assert publisher.publish(_grid()) >= 1
        _datagram, sender = receiver.recvfrom(65_535)
        assert sender[1] == local_port
    finally:
        publisher.close()
        receiver.close()


@pytest.mark.usefixtures("require_loopback_sockets")
def test_depth_grid_publisher_coalesces_display_frames_to_configured_rate() -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(0.25)
    local_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    local_probe.bind(("127.0.0.1", 0))
    local_port = local_probe.getsockname()[1]
    local_probe.close()
    publisher = DepthGridUdpPublisher(
        host="127.0.0.1",
        port=receiver.getsockname()[1],
        local_port=local_port,
        hmac_key=b"r" * 32,
        maximum_rate_hz=20.0,
    )
    try:
        assert publisher.publish(_grid()) > 0
        assert publisher.publish(_grid()) == 0
        assert publisher.suppressed_frames == 1
        time.sleep(0.06)
        assert publisher.publish(_grid()) > 0
        assert publisher.published_frames == 2
    finally:
        publisher.close()
        receiver.close()


def test_depth_grid_publisher_starts_interval_after_slow_frame_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame that takes an interval to emit must still coalesce the next frame.

    The fake socket advances the injected monotonic clock from inside sendto,
    so this remains deterministic and catches regressions that record the
    limiter timestamp before encoding/transmitting the complete frame.
    """

    class _Clock:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

        @staticmethod
        def time() -> float:
            return 1_700_000_000.0

    class _Socket:
        def __init__(self, clock: _Clock) -> None:
            self._clock = clock
            self.sent: list[tuple[bytes, tuple[str, int]]] = []
            self.closed = False

        def bind(self, _address: tuple[str, int]) -> None:
            return None

        def setblocking(self, _enabled: bool) -> None:
            return None

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            raise BlockingIOError

        def sendto(self, datagram: bytes, destination: tuple[str, int]) -> int:
            self.sent.append((datagram, destination))
            # Model a fragmented-frame send that consumes more than the
            # configured 100 ms interval without sleeping the test process.
            self._clock.now += 0.2
            return len(datagram)

        def close(self) -> None:
            self.closed = True

    clock = _Clock()
    fake_socket = _Socket(clock)
    monkeypatch.setattr(
        depth_grid_udp,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, time=clock.time),
    )
    monkeypatch.setattr(
        depth_grid_udp,
        "socket",
        SimpleNamespace(
            AF_INET=socket.AF_INET,
            SOCK_DGRAM=socket.SOCK_DGRAM,
            gethostbyname=socket.gethostbyname,
            socket=lambda *_args: fake_socket,
        ),
    )
    publisher = DepthGridUdpPublisher(
        host="127.0.0.1",
        port=14540,
        local_port=14541,
        hmac_key=b"s" * 32,
        maximum_datagram_bytes=65_507,
        maximum_rate_hz=10.0,
    )
    try:
        assert publisher.publish(_grid()) == 1
        assert clock.now == pytest.approx(100.2)
        assert publisher.publish(_grid()) == 0
        assert publisher.suppressed_frames == 1
        assert publisher.published_frames == 1
        assert len(fake_socket.sent) == 1
    finally:
        publisher.close()


@pytest.mark.usefixtures("require_loopback_sockets")
def test_depth_grid_publisher_serializes_concurrent_publish_calls() -> None:
    """Concurrent producers cannot both pass the limiter before completion."""

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    local_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    local_probe.bind(("127.0.0.1", 0))
    local_port = local_probe.getsockname()[1]
    local_probe.close()
    publisher = DepthGridUdpPublisher(
        host="127.0.0.1",
        port=receiver.getsockname()[1],
        local_port=local_port,
        hmac_key=b"t" * 32,
        maximum_rate_hz=1.0,
    )
    try:
        first_encode_started = threading.Event()
        release_first_encode = threading.Event()
        second_encode_started = threading.Event()
        encode_lock = threading.Lock()
        encode_calls = 0
        results: list[int] = []
        errors: list[BaseException] = []

        def encode(
            _grid: MetricDepthGrid,
            *,
            sequence: int,
            sent_at_ms: int | None = None,
        ) -> tuple[bytes]:
            nonlocal encode_calls
            del sequence, sent_at_ms
            with encode_lock:
                encode_calls += 1
                call_index = encode_calls
            if call_index == 1:
                first_encode_started.set()
                release_first_encode.wait(timeout=2.0)
            else:
                second_encode_started.set()
            return (b"frame",)

        publisher._codec.encode = encode

        def publish() -> None:
            try:
                results.append(publisher.publish(_grid()))
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        first = threading.Thread(target=publish)
        second = threading.Thread(target=publish)
        first.start()
        assert first_encode_started.wait(timeout=1.0)
        second.start()
        # If publish() drops the state lock while encoding, the second call enters
        # encode while the first one is paused.  A lock-protected implementation
        # leaves it waiting until the first frame has recorded its completion.
        second_encode_started.wait(timeout=0.5)
        release_first_encode.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        assert not first.is_alive() and not second.is_alive()
        assert not errors
        assert sorted(results) == [0, 1]
        assert encode_calls == 1
        assert not second_encode_started.is_set()
        assert publisher.published_frames == 1
        assert publisher.suppressed_frames == 1
    finally:
        publisher.close()
        receiver.close()
