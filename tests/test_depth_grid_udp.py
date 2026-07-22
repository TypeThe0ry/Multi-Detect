from __future__ import annotations

import random
import socket
import time
import zlib

import pytest

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
