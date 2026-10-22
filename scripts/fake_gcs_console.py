#!/usr/bin/env python3
"""Small signed-TUNNEL console for exercising onboard prompt missions.

Uploads a mission package (a prompt-only package is compiled onboard), prints
every plan/status/finding/final-summary document the aircraft sends back, and
approves the plan on Enter (or at once with ``--auto-approve``).  ``--link``
reads the keys and port that ``scripts/run_sim_session.py --operator-link``
writes for a session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_operator_protocol import (  # noqa: E402
    CHUNK,
    CHUNK_DATA_BYTES,
    document_chunks,
)
from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.operator_mavlink import (  # noqa: E402
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import OperatorTunnelCodec  # noqa: E402


class DocumentAssembler:
    """Reassemble chunked onboard documents; a document is complete only when every
    chunk arrived and the bytes match the digest carried in each chunk header."""

    def __init__(self) -> None:
        self._parts: dict[tuple[int, bytes], dict[int, bytes]] = {}

    def add(self, body: bytes) -> bytes | None:
        token, index, count, length, digest = CHUNK.unpack_from(body)
        data = body[CHUNK.size : CHUNK.size + CHUNK_DATA_BYTES]
        parts = self._parts.setdefault((token, digest), {})
        parts[index] = data
        if len(parts) < count:
            return None
        document = b"".join(parts[i] for i in range(count))[:length]
        del self._parts[(token, digest)]
        if hashlib.sha256(document).digest() != digest:
            return None
        return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--link", type=Path, help="operator-link.json written by a sim session")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14580)
    parser.add_argument("--hmac-key", help="32-byte HMAC key, hex")
    parser.add_argument("--mavlink-key", help="32-byte MAVLink signing key, hex")
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="0 runs until Ctrl-C")
    parser.add_argument("--log", type=Path, help="append every received document as JSON lines")
    args = parser.parse_args(argv)
    if args.link:
        link_config = json.loads(args.link.read_text(encoding="utf-8"))
        args.port = int(link_config["operator_port"])
        args.host = str(link_config.get("operator_bind_host", args.host))
        args.hmac_key = link_config["operator_hmac_key_hex"]
        args.mavlink_key = link_config["operator_mavlink_signing_key_hex"]
    if not args.hmac_key or not args.mavlink_key:
        parser.error("--hmac-key and --mavlink-key (or --link) are required")
    raw = args.package.read_bytes()
    package = load_mission_package(json.loads(raw))
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
    assembler = DocumentAssembler()
    approved: set[str] = set()
    deadline = time.monotonic() + args.max_seconds if args.max_seconds > 0 else None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1.0)
        sequence = 1

        def send(message_type: int, document: bytes, token: int) -> None:
            nonlocal sequence
            for chunk in document_chunks(document, token, message_type=message_type):
                payload = codec.encode_mission(
                    message_type, chunk.body, sequence=sequence, sent_at_s=time.time()
                )
                frame = link.wrap_authenticated_operator_payload(payload)
                sock.sendto(frame, (args.host, args.port))
                sequence += 1

        send(28, raw, 1)
        last_upload = time.monotonic()
        print(f"uploaded {package.mission_id}", flush=True)
        while deadline is None or time.monotonic() < deadline:
            # The aircraft may start listening after the console: resend the
            # package until its plan comes back.
            if not approved and time.monotonic() - last_upload > 5.0:
                send(28, raw, 1)
                last_upload = time.monotonic()
            try:
                frame, _ = sock.recvfrom(4096)
                packet = codec.decode(link.decode_authenticated_datagram(frame).operator_payload)
            except (TimeoutError, OSError):
                continue
            except ValueError as error:
                print(f"rejected packet: {error}", flush=True)
                continue
            if packet.message_type != 35:
                continue
            document_bytes = assembler.add(packet.message.body)
            if document_bytes is None:
                continue
            try:
                document = json.loads(document_bytes.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            print(json.dumps(document, ensure_ascii=False), flush=True)
            if args.log:
                with args.log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(document, ensure_ascii=False) + "\n")
            if document.get("kind") != "plan" or document.get("plan_sha256") in approved:
                continue
            if not args.auto_approve:
                input("approve with Enter, or Ctrl-C to leave waiting: ")
            approved.add(document["plan_sha256"])
            command = json.dumps(
                {
                    "mission_id": package.mission_id,
                    "type": "approve_plan",
                    "plan_sha256": document["plan_sha256"],
                },
                separators=(",", ":"),
            ).encode()
            send(37, command, 2)
            print(f"approved plan {document['plan_sha256'][:12]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
