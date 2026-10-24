#!/usr/bin/env python3
"""Small signed-TUNNEL console for exercising onboard prompt missions.

Uploads a signed mission package (a prompt-only package is compiled onboard),
prints every plan/status/finding/final-summary document the aircraft sends
back, and approves the plan when ``a``/Enter is typed (``r`` rejects), or at
once with ``--auto-approve``.  Keyboard input is read on its own thread, so the
receive loop keeps sending heartbeats and telemetry ACKs while it waits.
``--link`` reads the keys and port that ``scripts/run_sim_session.py
--operator-link`` writes for a session; its session package key signs an
unsigned package file before upload (onboard refuses unsigned packages).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from multidetect.autonomy.mission_security import (  # noqa: E402
    AuthenticationKey,
    load_key,
    sign_document,
)
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
        self.first_received: dict[tuple[int, bytes], tuple[float, float]] = {}

    def add(self, body: bytes) -> bytes | None:
        token, index, count, length, digest = CHUNK.unpack_from(body)
        data = body[CHUNK.size : CHUNK.size + CHUNK_DATA_BYTES]
        key = (token, digest)
        self.first_received.setdefault(key, (time.time(), time.monotonic()))
        parts = self._parts.setdefault(key, {})
        parts[index] = data
        if len(parts) < count:
            return None
        document = b"".join(parts[i] for i in range(count))[:length]
        del self._parts[key]
        if hashlib.sha256(document).digest() != digest:
            return None
        return document

    def take_first_received(self, body: bytes) -> tuple[float, float] | None:
        token, _index, _count, _length, digest = CHUNK.unpack_from(body)
        return self.first_received.pop((token, digest), None)


def initial_sequence(now_s: float | None = None) -> int:
    """Seed from Unix milliseconds, as the GCS does, so a restarted console is
    ahead of the onboard sequence high-water instead of "expired" at 1."""
    now_s = time.time() if now_s is None else now_s
    return (int(now_s * 1000) & 0xFFFFFFFF) or 1


def load_command_sequence(path: Path | None, default: int) -> int:
    if path is None:
        return default
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default
    return max(default, value + 1)


def persist_command_sequence(path: Path | None, sequence: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(str(sequence), encoding="utf-8")
    os.replace(temporary, path)


def new_token() -> int:
    """Fresh non-zero transfer token; onboard caches completed transfers by token."""
    return secrets.randbits(62) | 1


class PlanDecisions:
    """Approve/reject decisions for onboard plans, never blocking the receive loop."""

    def __init__(
        self,
        mission_id: str,
        send_command: Callable[[dict[str, Any]], None],
        *,
        auto_approve: bool = False,
        command_key: AuthenticationKey | None = None,
        command_sequence: int = 1,
        clock: Callable[[], float] = time.time,
        prompt: Callable[[str], None] = lambda text: print(text, flush=True),
        persist_sequence: Callable[[int], None] | None = None,
    ) -> None:
        self.mission_id = mission_id
        self.send_command = send_command
        self.auto_approve = auto_approve
        self.command_key = command_key
        self.command_sequence = command_sequence
        self.clock = clock
        self.prompt = prompt
        self.persist_sequence = persist_sequence
        self.pending: str | None = None
        self.decided: set[str] = set()
        self.inputs: queue.Queue[str] = queue.Queue()

    def on_plan(self, document: dict[str, Any]) -> None:
        if document.get("mission_id") != self.mission_id:
            self.prompt(f"ignored plan for another mission: {document.get('mission_id')!r}")
            return
        digest = document.get("plan_sha256")
        if not isinstance(digest, str) or digest in self.decided:
            return
        self.pending = digest
        if self.auto_approve:
            self.decide("approve")
        else:
            self.prompt("plan received: type a (or Enter) to approve, r to reject")

    def poll(self) -> None:
        """Apply typed decisions; called from the receive loop."""
        while True:
            try:
                line = self.inputs.get_nowait().strip().lower()
            except queue.Empty:
                return
            if line in {"", "a", "y", "approve"}:
                self.decide("approve")
            elif line in {"r", "n", "reject"}:
                self.decide("reject")

    def decide(self, decision: str) -> None:
        digest = self.pending
        if digest is None:
            return
        self.pending = None
        self.decided.add(digest)
        command: dict[str, Any] = {
            "mission_id": self.mission_id,
            "type": "approve_plan" if decision == "approve" else "reject_plan",
            "plan_sha256": digest,
        }
        if self.command_key is not None:
            # Signed ground commands carry their own replay protection.
            command.update(
                sequence=self.command_sequence,
                timestamp=self.clock(),
                timestamp_version=1,
                timestamp_domain="unix",
            )
            if self.persist_sequence is not None:
                self.persist_sequence(self.command_sequence)
            self.command_sequence += 1
            command = sign_document(command, self.command_key)
        self.send_command(command)
        self.prompt(f"{decision}d plan {digest[:12]}")


def _package_key(args, link_config: dict[str, Any]) -> AuthenticationKey | None:
    hex_key = args.package_key_hex or link_config.get("operator_package_key_hex")
    if hex_key:
        return AuthenticationKey("hmac-sha256", bytes.fromhex(hex_key), "default")
    return load_key(args.package_key) if args.package_key else None


def signed_package_bytes(raw: bytes, key: AuthenticationKey | None) -> tuple[bytes, str]:
    """Return upload bytes and mission id; sign an unsigned package when a key is given."""
    document = json.loads(raw)
    if "signature" in document:
        return raw, str(document["mission_id"])
    load_mission_package(document)  # refuse an invalid package before signing it
    if key is None:
        raise ValueError("package is unsigned: pass --package-key/--package-key-hex or --link")
    signed = sign_document(document, key)
    return json.dumps(signed, ensure_ascii=False, separators=(",", ":")).encode(), str(
        document["mission_id"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--link", type=Path, help="operator-link.json written by a sim session")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14580)
    parser.add_argument("--hmac-key", help="32-byte HMAC key, hex")
    parser.add_argument("--mavlink-key", help="32-byte MAVLink signing key, hex")
    parser.add_argument("--package-key", type=Path, help="package signing key file")
    parser.add_argument("--package-key-hex", help="32-byte HMAC package signing key, hex")
    parser.add_argument("--command-key", type=Path, help="ground command signing key file")
    parser.add_argument("--command-sequence", type=int, default=1)
    parser.add_argument("--state", type=Path, help="signed command sequence state file")
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="0 runs until Ctrl-C")
    parser.add_argument("--log", type=Path, help="append every received document as JSON lines")
    args = parser.parse_args(argv)
    link_config: dict[str, Any] = {}
    if args.link:
        link_config = json.loads(args.link.read_text(encoding="utf-8"))
        args.port = int(link_config["operator_port"])
        args.host = str(link_config.get("operator_bind_host", args.host))
        args.hmac_key = link_config["operator_hmac_key_hex"]
        args.mavlink_key = link_config["operator_mavlink_signing_key_hex"]
    if not args.hmac_key or not args.mavlink_key:
        parser.error("--hmac-key and --mavlink-key (or --link) are required")
    try:
        raw, mission_id = signed_package_bytes(
            args.package.read_bytes(), _package_key(args, link_config)
        )
    except ValueError as error:
        parser.error(str(error))
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
    deadline = time.monotonic() + args.max_seconds if args.max_seconds > 0 else None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.2)
        sequence = initial_sequence()

        def frame(message_type: int, body: bytes) -> bytes:
            nonlocal sequence
            payload = codec.encode_mission(
                message_type, body, sequence=sequence, sent_at_s=time.time()
            )
            sequence = (sequence + 1) & 0xFFFFFFFF or 1
            return link.wrap_authenticated_operator_payload(payload)

        def send_chunks(chunks, message_type: int = 28) -> None:
            for chunk in chunks:
                sock.sendto(frame(message_type, chunk.body), (args.host, args.port))
                # Onboard rate-limits each message type to a burst of 20 and 20/s
                # (operator_udp.DEFAULT_COMMAND_*) and drops excess chunks without
                # an ACK; a ~1 KB prompt package is ~25 chunks, so pace below that.
                time.sleep(0.06)

        def send(message_type: int, document: bytes, token: int) -> None:
            send_chunks(document_chunks(document, token, message_type=message_type), message_type)

        def send_command(command: dict[str, Any]) -> None:
            encoded = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode()
            send(37, encoded, new_token())

        command_state = args.state
        if command_state is None and args.link and args.command_key:
            command_state = args.link.parent / "command-sequence-state.txt"
        command_sequence = (
            load_command_sequence(command_state, args.command_sequence)
            if args.command_key
            else args.command_sequence
        )
        decisions = PlanDecisions(
            mission_id,
            send_command,
            auto_approve=args.auto_approve,
            command_key=load_key(args.command_key) if args.command_key else None,
            command_sequence=command_sequence,
            persist_sequence=(lambda value: persist_command_sequence(command_state, value))
            if args.command_key
            else None,
        )
        if not args.auto_approve:

            def read_keyboard() -> None:
                for line in sys.stdin:
                    decisions.inputs.put(line)

            threading.Thread(target=read_keyboard, name="gcs-keyboard", daemon=True).start()

        upload_token = new_token()
        upload_chunks = list(document_chunks(raw, upload_token, message_type=28))
        beat = CONTROL.pack(mission_token(mission_id), 1)
        sock.sendto(frame(34, beat), (args.host, args.port))
        sock.sendto(frame(28, upload_chunks[0].body), (args.host, args.port))
        upload_tail_sent = False
        last_upload = time.monotonic()
        heard = False
        last_heartbeat = 0.0
        heartbeat_count = 0
        print(f"uploaded {mission_id}", flush=True)
        while deadline is None or time.monotonic() < deadline:
            # The aircraft may start listening after the console: resend the
            # package until anything comes back (a resend after admission would
            # replace the mission that is waiting for approval).
            # Ground heartbeats (type 34) keep the onboard link "up"; log records
            # such as the plan are only downlinked while it is.
            if time.monotonic() - last_heartbeat >= 1.0:
                heartbeat_count += 1
                beat = CONTROL.pack(mission_token(mission_id), heartbeat_count)
                sock.sendto(frame(34, beat), (args.host, args.port))
                last_heartbeat = time.monotonic()
            if not upload_tail_sent and time.monotonic() - last_upload >= 1.0:
                sock.sendto(frame(28, upload_chunks[0].body), (args.host, args.port))
                last_upload = time.monotonic()
            decisions.poll()
            try:
                received, _ = sock.recvfrom(4096)
                packet = codec.decode(link.decode_authenticated_datagram(received).operator_payload)
            except (TimeoutError, OSError):
                continue
            except ValueError as error:
                print(f"rejected packet: {error}", flush=True)
                continue
            if packet.message_type == 29:
                # Onboard verdict on an upload/command chunk, with a reason text.
                _token, kind, index, accepted = ACK.unpack_from(packet.message.body)
                detail = packet.message.body[ACK.size :].decode("utf-8", "replace")
                if not accepted or index == 0 or os.environ.get("GCS_ALL_ACKS"):
                    print(
                        f"onboard ack kind={kind} index={index} accepted={bool(accepted)} {detail}",
                        flush=True,
                    )
                if kind == 28 and not accepted and "必须从首个分块开始上传" in detail:
                    upload_token = new_token()
                    upload_chunks = list(document_chunks(raw, upload_token, message_type=28))
                    sock.sendto(frame(28, upload_chunks[0].body), (args.host, args.port))
                    upload_tail_sent = False
                    last_upload = time.monotonic()
                elif kind == 28 and accepted and index == 0 and not upload_tail_sent:
                    send_chunks(upload_chunks[1:])
                    upload_tail_sent = True
                continue
            if packet.message_type != 35:
                continue
            heard = True
            # Onboard telemetry is stop-and-wait: each chunk is resent every second
            # until the ground acknowledges (token, index) with a type-36 message.
            token, index = CHUNK.unpack_from(packet.message.body)[:2]
            received_wall = time.time()
            received_mono = time.monotonic()
            sock.sendto(frame(36, TELEMETRY_ACK.pack(token, index)), (args.host, args.port))
            document_bytes = assembler.add(packet.message.body)
            if document_bytes is None:
                continue
            try:
                document = json.loads(document_bytes.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            documents = [document]
            first_received = assembler.take_first_received(packet.message.body)
            document["ground_received_wall_time"] = received_wall
            document["received_at_wall"] = received_wall
            document["ground_received_wall_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(received_wall))
            document["ground_received_monotonic_s"] = received_mono
            document["received_at_monotonic"] = received_mono
            if first_received is not None:
                document["first_chunk_received_wall_time"] = first_received[0]
                document["first_chunk_received_monotonic_s"] = first_received[1]
                document["first_chunk_received_at_wall"] = first_received[0]
                document["first_chunk_received_at_monotonic"] = first_received[1]
            document["document_completed_wall_time"] = received_wall
            document["document_completed_monotonic_s"] = received_mono
            document["completed_at_wall"] = received_wall
            document["completed_at_monotonic"] = received_mono
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
                if item is not document:
                    for key in (
                        "ground_received_wall_time", "received_at_wall", "ground_received_wall_iso",
                        "ground_received_monotonic_s", "received_at_monotonic",
                        "first_chunk_received_wall_time", "first_chunk_received_monotonic_s",
                        "first_chunk_received_at_wall", "first_chunk_received_at_monotonic",
                        "document_completed_wall_time", "document_completed_monotonic_s",
                        "completed_at_wall", "completed_at_monotonic",
                    ):
                        if key in document:
                            item[key] = document[key]
                print(json.dumps(item, ensure_ascii=False), flush=True)
                if args.log:
                    with args.log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            if document.get("kind") == "plan":
                decisions.on_plan(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
