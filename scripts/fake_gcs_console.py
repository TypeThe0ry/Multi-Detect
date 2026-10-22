#!/usr/bin/env python3
"""Small signed-TUNNEL console for exercising onboard prompt missions."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_operator_protocol import document_chunks  # noqa: E402
from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.operator_mavlink import (  # noqa: E402
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import OperatorTunnelCodec  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14580)
    parser.add_argument("--hmac-key", required=True, help="32-byte HMAC key, hex")
    parser.add_argument("--mavlink-key", required=True, help="32-byte MAVLink signing key, hex")
    args = parser.parse_args(argv)
    raw = args.package.read_bytes()
    package = load_mission_package(raw)
    codec = OperatorTunnelCodec(hmac_key=bytes.fromhex(args.hmac_key), geometries=())
    endpoint = OperatorMavlinkEndpoint(255, 190, 1, 191)
    link = OperatorMavlinkTunnelAdapter(
        codec,
        endpoint,
        signing_key=bytes.fromhex(args.mavlink_key),
        signing_link_id=1,
        initial_signing_timestamp=max(1, int(time.time() * 100_000)),
        receive_signing_link_id=1,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1.0)
        sequence = 1
        for chunk in document_chunks(raw, 1):
            payload = codec.encode_mission(28, chunk.body, sequence=sequence, sent_at_s=time.time())
            sock.sendto(link.wrap_authenticated_operator_payload(payload), (args.host, args.port))
            sequence += 1
        print(f"uploaded {package.mission_id}; press Enter to approve")
        while True:
            try:
                frame, _ = sock.recvfrom(4096)
            except TimeoutError:
                continue
            packet = codec.decode(link.decode_authenticated_datagram(frame).operator_payload)
            if packet.message_type == 35:
                body = packet.message.body
                try:
                    from multidetect.autonomy.mission_operator_protocol import CHUNK

                    document = json.loads(body[CHUNK.size :].decode())
                    print(json.dumps(document, ensure_ascii=False))
                    if document.get("kind") == "plan":
                        input("approve with Enter, or Ctrl-C to leave waiting: ")
                        command = json.dumps(
                            {
                                "mission_id": package.mission_id,
                                "type": "approve_plan",
                                "plan_sha256": document["plan_sha256"],
                            },
                            separators=(",", ":"),
                        ).encode()
                        for chunk in document_chunks(command, 2, message_type=37):
                            payload = codec.encode_mission(
                                37, chunk.body, sequence=sequence, sent_at_s=time.time()
                            )
                            sock.sendto(
                                link.wrap_authenticated_operator_payload(payload),
                                (args.host, args.port),
                            )
                            sequence += 1
                except (ValueError, KeyError, json.JSONDecodeError):
                    continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
