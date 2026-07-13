from __future__ import annotations

import math
import socket
import time
from collections.abc import Callable

from .payload_confirmation_hil import (
    PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES,
    MissionPayloadConfirmationHilAdapter,
    PayloadConfirmationHilCodec,
    PayloadConfirmationHilError,
    PayloadConfirmationHilMessage,
    PayloadConfirmationHilReceipt,
)


class UdpPayloadConfirmationHilSender:
    """Send one signed independent-sensor HIL datagram; exposes no actuator API."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        codec: PayloadConfirmationHilCodec,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("confirmation HIL UDP host cannot be empty")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("confirmation HIL UDP port must be in [1, 65535]")
        self.remote_address = (socket.gethostbyname(host.strip()), port)
        self.codec = codec
        self.command_messages_sent = 0
        self.physical_release_enabled = False

    def send(self, message: PayloadConfirmationHilMessage) -> int:
        encoded = self.codec.encode(message)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sent = sender.sendto(encoded, self.remote_address)
        if sent != len(encoded):
            raise OSError("confirmation HIL UDP datagram was not fully sent")
        return sent


class UdpPayloadConfirmationHilReceiver:
    """Receive signed independent evidence until one valid confirmation or timeout."""

    def __init__(self, *, bind_host: str, port: int) -> None:
        if not isinstance(bind_host, str) or not bind_host.strip():
            raise ValueError("confirmation HIL UDP bind host cannot be empty")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("confirmation HIL UDP port must be in [0, 65535]")
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((bind_host.strip(), port))
        self.received_datagrams = 0
        self.rejected_datagrams = 0
        self.command_messages_sent = 0
        self.physical_release_enabled = False

    @property
    def local_address(self) -> tuple[str, int]:
        host, port = self._socket.getsockname()
        return str(host), int(port)

    def receive_until_confirmed(
        self,
        adapter: MissionPayloadConfirmationHilAdapter,
        *,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> PayloadConfirmationHilReceipt:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("confirmation HIL UDP timeout must be finite and positive")
        if not callable(clock):
            raise TypeError("confirmation HIL UDP clock must be callable")
        deadline_s = time.monotonic() + timeout_s
        while True:
            remaining_s = deadline_s - time.monotonic()
            if remaining_s <= 0:
                raise TimeoutError("no valid independent payload confirmation was received")
            self._socket.settimeout(remaining_s)
            try:
                encoded, _sender = self._socket.recvfrom(
                    PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES + 1
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "no valid independent payload confirmation was received"
                ) from exc
            self.received_datagrams += 1
            if len(encoded) > PAYLOAD_CONFIRMATION_HIL_MAX_MESSAGE_BYTES:
                self.rejected_datagrams += 1
                continue
            try:
                return adapter.accept(encoded, now_s=float(clock()))
            except (PayloadConfirmationHilError, TypeError, ValueError, OverflowError):
                self.rejected_datagrams += 1

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> UdpPayloadConfirmationHilReceiver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "UdpPayloadConfirmationHilReceiver",
    "UdpPayloadConfirmationHilSender",
]
