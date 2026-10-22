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
import secrets
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_operator_protocol import (  # noqa: E402
    ACK,
    CHUNK,
    CHUNK_DATA_BYTES,
    CONTROL,
    TELEMETRY_ACK,
    document_chunks,
    mission_token,
)
from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.operator_link import VideoGeometry  # noqa: E402
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
    # Same registered stream as the onboard endpoint (scripts/onboard_main.py).
    codec = OperatorTunnelCodec(
        hmac_key=bytes.fromhex(args.hmac_key),
        geometries=(VideoGeometry("camera-main", 1280, 720),),
    )
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
    summary_pages: dict[str, dict[int, str]] = {}
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
                # Onboard rate-limits each message type to a burst of 20 and 20/s
                # (operator_udp.DEFAULT_COMMAND_*) and drops excess chunks without
                # an ACK; a ~1 KB prompt package is ~25 chunks, so pace below that.
                time.sleep(0.06)

        # A fresh token per console run: onboard remembers completed transfers by
        # token, so reusing one makes every later upload "expired or conflicting".
        upload_token = secrets.randbits(62) | 1
        send(28, raw, upload_token)
        last_upload = time.monotonic()
        heard = False
        last_heartbeat = 0.0
        heartbeat_count = 0
        print(f"uploaded {package.mission_id}", flush=True)
        while deadline is None or time.monotonic() < deadline:
            # The aircraft may start listening after the console: resend the
            # package until anything comes back (a resend after admission would
            # replace the mission that is waiting for approval).
            # Ground heartbeats (type 34) keep the onboard link "up"; log records
            # such as the plan are only downlinked while it is.
            if time.monotonic() - last_heartbeat >= 1.0:
                heartbeat_count += 1
                beat = codec.encode_mission(
                    34,
                    CONTROL.pack(mission_token(package.mission_id), heartbeat_count),
                    sequence=sequence,
                    sent_at_s=time.time(),
                )
                sock.sendto(link.wrap_authenticated_operator_payload(beat), (args.host, args.port))
                sequence += 1
                last_heartbeat = time.monotonic()
            if not heard and time.monotonic() - last_upload > 5.0:
                send(28, raw, upload_token)
                last_upload = time.monotonic()
            try:
                frame, _ = sock.recvfrom(4096)
                packet = codec.decode(link.decode_authenticated_datagram(frame).operator_payload)
            except (TimeoutError, OSError):
                continue
            except ValueError as error:
                print(f"rejected packet: {error}", flush=True)
                continue
            if packet.message_type == 29:
                # Onboard verdict on an upload/command chunk, with a reason text.
                _token, kind, index, accepted = ACK.unpack_from(packet.message.body)
                detail = packet.message.body[ACK.size :].decode("utf-8", "replace")
                if not accepted or index == 0 or __import__("os").environ.get("GCS_ALL_ACKS"):
                    print(
                        f"onboard ack kind={kind} index={index} accepted={bool(accepted)} {detail}",
                        flush=True,
                    )
                continue
            if packet.message_type != 35:
                continue
            heard = True
            # Onboard telemetry is stop-and-wait: each chunk is resent every second
            # until the ground acknowledges (token, index) with a type-36 message.
            token, index = CHUNK.unpack_from(packet.message.body)[:2]
            ack = codec.encode_mission(
                36, TELEMETRY_ACK.pack(token, index), sequence=sequence, sent_at_s=time.time()
            )
            sock.sendto(link.wrap_authenticated_operator_payload(ack), (args.host, args.port))
            sequence += 1
            document_bytes = assembler.add(packet.message.body)
            if document_bytes is None:
                continue
            try:
                document = json.loads(document_bytes.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            documents = [document]
            if document.get("kind") == "final_summary_page":
                # A large final summary arrives as ordered pages of JSON text;
                # show it once every page is here.
                pages = summary_pages.setdefault(str(document.get("mission_id")), {})
                pages[int(document["page"])] = str(document["body"])
                if len(pages) == int(document["pages"]):
                    try:
                        summary = json.loads("".join(pages[i] for i in sorted(pages)))
                    except json.JSONDecodeError:
                        summary = None
                    if summary is not None:
                        documents.append(
                            {
                                "kind": "final_summary",
                                "mission_id": document.get("mission_id"),
                                "summary": summary,
                                "assembled_pages": len(pages),
                            }
                        )
            for item in documents:
                print(json.dumps(item, ensure_ascii=False), flush=True)
                if args.log:
                    with args.log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
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
