from __future__ import annotations

import math
import socket
import time
from dataclasses import dataclass

from .payload_hil_protocol import (
    PAYLOAD_HIL_MAX_MESSAGE_BYTES,
    PayloadHilCodec,
    PayloadHilProtocolError,
    PayloadHilReleaseRequest,
    PayloadHilRequestGuard,
    PayloadHilResult,
    PayloadHilResultGuard,
    PayloadHilResultStatus,
)


@dataclass(frozen=True, slots=True)
class PayloadHilExchange:
    release_id: str
    attempts: int
    results: tuple[PayloadHilResult, ...]

    @property
    def terminal_result(self) -> PayloadHilResult | None:
        terminal = {
            PayloadHilResultStatus.REJECTED,
            PayloadHilResultStatus.EXECUTED,
            PayloadHilResultStatus.FAILED,
        }
        return next(
            (result for result in reversed(self.results) if result.status in terminal), None
        )


class UdpPayloadHilClient:
    """Bounded authenticated request/result exchange for an inert HIL controller."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        request_codec: PayloadHilCodec,
        result_codec: PayloadHilCodec,
        response_timeout_s: float = 0.5,
        maximum_attempts: int = 3,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("payload HIL UDP host cannot be empty")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("payload HIL UDP port must be in [1, 65535]")
        if not math.isfinite(response_timeout_s) or response_timeout_s <= 0:
            raise ValueError("payload HIL UDP timeout must be finite and positive")
        if (
            isinstance(maximum_attempts, bool)
            or not isinstance(maximum_attempts, int)
            or not 1 <= maximum_attempts <= 10
        ):
            raise ValueError("payload HIL UDP attempts must be in [1, 10]")
        self.remote_address = (socket.gethostbyname(host.strip()), port)
        self.request_codec = request_codec
        self.result_codec = result_codec
        self.response_timeout_s = response_timeout_s
        self.maximum_attempts = maximum_attempts

    def exchange(
        self,
        request: PayloadHilReleaseRequest,
        *,
        maximum_result_age_s: float,
    ) -> PayloadHilExchange:
        encoded_request = self.request_codec.encode_request(request)
        guard = PayloadHilResultGuard(
            request=request,
            maximum_age_s=maximum_result_age_s,
        )
        accepted_results: list[PayloadHilResult] = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(("127.0.0.1", 0))
            for attempt in range(1, self.maximum_attempts + 1):
                client.sendto(encoded_request, self.remote_address)
                deadline_s = time.monotonic() + self.response_timeout_s
                while True:
                    remaining_s = deadline_s - time.monotonic()
                    if remaining_s <= 0:
                        break
                    client.settimeout(remaining_s)
                    try:
                        encoded_result, sender = client.recvfrom(PAYLOAD_HIL_MAX_MESSAGE_BYTES + 1)
                    except TimeoutError:
                        break
                    if sender != self.remote_address:
                        continue
                    result = self.result_codec.decode_result(encoded_result)
                    verification = guard.verify(result, now_s=time.monotonic())
                    if not verification.valid:
                        raise PayloadHilProtocolError(
                            "payload HIL UDP result failed verification: "
                            + "; ".join(verification.reasons)
                        )
                    if not verification.idempotent_replay:
                        accepted_results.append(result)
                    if result.status in {
                        PayloadHilResultStatus.REJECTED,
                        PayloadHilResultStatus.EXECUTED,
                        PayloadHilResultStatus.FAILED,
                    }:
                        return PayloadHilExchange(
                            release_id=request.release_id,
                            attempts=attempt,
                            results=tuple(accepted_results),
                        )
        raise TimeoutError(
            "payload HIL controller produced no terminal result after "
            f"{self.maximum_attempts} attempts"
        )


class UdpInertPayloadHilController:
    """A telemetry-like inert controller simulator with no actuator integration."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        request_codec: PayloadHilCodec,
        result_codec: PayloadHilCodec,
        request_guard: PayloadHilRequestGuard,
    ) -> None:
        if not isinstance(bind_host, str) or not bind_host.strip():
            raise ValueError("payload HIL controller bind host cannot be empty")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("payload HIL controller port must be in [0, 65535]")
        self.request_codec = request_codec
        self.result_codec = result_codec
        self.request_guard = request_guard
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((bind_host.strip(), port))
        self._results_by_release_id: dict[str, tuple[bytes, ...]] = {}
        self._result_sequence = 0
        self.received_datagrams = 0
        self.rejected_datagrams = 0
        self.command_messages_sent = 0
        self.physical_release_enabled = False

    @property
    def local_address(self) -> tuple[str, int]:
        host, port = self._socket.getsockname()
        return str(host), int(port)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> UdpInertPayloadHilController:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def serve_once(
        self,
        *,
        receive_timeout_s: float = 2.0,
        simulate_inert_execution: bool = False,
        drop_first_response: bool = False,
    ) -> tuple[PayloadHilResult, ...]:
        if not math.isfinite(receive_timeout_s) or receive_timeout_s <= 0:
            raise ValueError("payload HIL controller timeout must be finite and positive")
        self._socket.settimeout(receive_timeout_s)
        encoded_request, sender = self._socket.recvfrom(PAYLOAD_HIL_MAX_MESSAGE_BYTES + 1)
        self.received_datagrams += 1
        try:
            request = self.request_codec.decode_request(encoded_request)
        except PayloadHilProtocolError:
            self.rejected_datagrams += 1
            return ()
        now_s = time.monotonic()
        verification = self.request_guard.verify(request, now_s=now_s)
        cached = self._results_by_release_id.get(request.release_id)
        if verification.idempotent_replay and cached is not None:
            for encoded in cached:
                self._socket.sendto(encoded, sender)
            return tuple(self.result_codec.decode_result(encoded) for encoded in cached)
        if not verification.valid:
            self.rejected_datagrams += 1
            rejected = self._new_result(
                request,
                status=PayloadHilResultStatus.REJECTED,
                observed_at_s=now_s,
                controller_healthy=True,
                interlock_healthy=False,
                reason="; ".join(verification.reasons),
            )
            self._socket.sendto(self.result_codec.encode_result(rejected), sender)
            return (rejected,)

        accepted = self._new_result(
            request,
            status=PayloadHilResultStatus.ACCEPTED,
            observed_at_s=now_s,
            controller_healthy=True,
            interlock_healthy=True,
        )
        results = [accepted]
        if simulate_inert_execution:
            results.append(
                self._new_result(
                    request,
                    status=PayloadHilResultStatus.EXECUTED,
                    observed_at_s=now_s,
                    controller_healthy=True,
                    interlock_healthy=True,
                )
            )
            self.request_guard.finish(release_id=request.release_id)
        encoded_results = tuple(self.result_codec.encode_result(result) for result in results)
        self._results_by_release_id[request.release_id] = encoded_results
        if not drop_first_response:
            for encoded in encoded_results:
                self._socket.sendto(encoded, sender)
        return tuple(results)

    def _new_result(
        self,
        request: PayloadHilReleaseRequest,
        *,
        status: PayloadHilResultStatus,
        observed_at_s: float,
        controller_healthy: bool,
        interlock_healthy: bool,
        reason: str | None = None,
    ) -> PayloadHilResult:
        self._result_sequence += 1
        return PayloadHilResult(
            mission_id=request.mission_id,
            module_id=request.module_id,
            release_id=request.release_id,
            payload_slot_id=request.payload_slot_id,
            status=status,
            observed_at_s=observed_at_s,
            sequence=self._result_sequence,
            key_id=self.result_codec.expected_key_id,
            controller_healthy=controller_healthy,
            interlock_healthy=interlock_healthy,
            reason=reason,
        )


__all__ = [
    "PayloadHilExchange",
    "UdpInertPayloadHilController",
    "UdpPayloadHilClient",
]
