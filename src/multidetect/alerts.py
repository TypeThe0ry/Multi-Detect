from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import socket
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Protocol, TextIO

from .domain import BoundingBox, FireAlert


class AlertPublisher(Protocol):
    """Software boundary for a future telemetry/data-link adapter."""

    def publish(self, alert: FireAlert) -> None: ...


class AlertDeduplicationStore(Protocol):
    def check_and_record(
        self, alert_id: str, payload_sha256: str, *, received_at_unix_s: float
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AlertDeliveryReceipt:
    alert_id: str
    receiver_id: str
    acknowledged_at_s: float


class AcknowledgedAlertTransport(Protocol):
    def send(self, document: dict[str, object]) -> AlertDeliveryReceipt: ...


class AlertAcknowledgementError(RuntimeError):
    """Raised when a bounded acknowledged alert delivery cannot be completed."""


class AlertAuthenticationError(RuntimeError):
    """Raised when an authenticated data-link packet fails validation."""


_UDP_PROTOCOL = "multi-detect-alert-udp"
_UDP_PROTOCOL_VERSION = 1
# Stay below common link MTUs so a normal alert does not depend on IP fragmentation.
_MAX_UDP_PACKET_BYTES = 1_200


def _canonical_json_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sign_document(document: dict[str, object], key: bytes) -> dict[str, object]:
    if len(key) < 32:
        raise ValueError("alert HMAC key must contain at least 32 bytes")
    signed = dict(document)
    signed["mac"] = hmac.new(key, _canonical_json_bytes(document), hashlib.sha256).hexdigest()
    return signed


def _verify_signed_document(document: dict[str, object], key: bytes) -> dict[str, object]:
    if len(key) < 32:
        raise ValueError("alert HMAC key must contain at least 32 bytes")
    mac = document.get("mac")
    if not isinstance(mac, str) or len(mac) != 64:
        raise AlertAuthenticationError("data-link packet signature is missing or invalid")
    unsigned = dict(document)
    del unsigned["mac"]
    expected = hmac.new(key, _canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise AlertAuthenticationError("data-link packet signature mismatch")
    return unsigned


def _decode_udp_document(packet: bytes) -> dict[str, object]:
    if not packet or len(packet) > _MAX_UDP_PACKET_BYTES:
        raise AlertAuthenticationError("data-link packet size is invalid")
    try:
        document = json.loads(packet.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlertAuthenticationError("data-link packet is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise AlertAuthenticationError("data-link packet must be a JSON object")
    return document


def _validate_packet_header(
    document: dict[str, object],
    *,
    message_type: str,
    expected_sender_id: str,
    expected_receiver_id: str,
    now_unix_s: float,
    maximum_clock_skew_seconds: float,
) -> None:
    if document.get("protocol") != _UDP_PROTOCOL or document.get("version") != 1:
        raise AlertAuthenticationError("unsupported data-link protocol or version")
    if document.get("message_type") != message_type:
        raise AlertAuthenticationError("unexpected data-link message type")
    if document.get("sender_id") != expected_sender_id:
        raise AlertAuthenticationError("unexpected data-link sender identity")
    if document.get("receiver_id") != expected_receiver_id:
        raise AlertAuthenticationError("unexpected data-link receiver identity")
    sent_at_unix_s = document.get("sent_at_unix_s")
    if not isinstance(sent_at_unix_s, int | float) or not math.isfinite(sent_at_unix_s):
        raise AlertAuthenticationError("data-link packet timestamp is invalid")
    if abs(now_unix_s - float(sent_at_unix_s)) > maximum_clock_skew_seconds:
        raise AlertAuthenticationError("data-link packet timestamp is outside the allowed window")


@dataclass(frozen=True, slots=True)
class ReceivedAuthenticatedAlert:
    document: dict[str, object]
    sender_id: str
    duplicate: bool
    peer: tuple[str, int]


class InMemoryAlertDeduplicationStore:
    def __init__(self, *, capacity: int = 10_000) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("deduplication capacity must be a positive integer")
        self.capacity = capacity
        self._delivered_hashes: OrderedDict[str, str] = OrderedDict()
        self._lock = RLock()

    def check_and_record(
        self, alert_id: str, payload_sha256: str, *, received_at_unix_s: float
    ) -> bool:
        del received_at_unix_s
        with self._lock:
            prior_hash = self._delivered_hashes.get(alert_id)
            if prior_hash is not None and prior_hash != payload_sha256:
                raise AlertAuthenticationError("alert identifier was reused with different content")
            duplicate = prior_hash is not None
            self._delivered_hashes[alert_id] = payload_sha256
            self._delivered_hashes.move_to_end(alert_id)
            while len(self._delivered_hashes) > self.capacity:
                self._delivered_hashes.popitem(last=False)
            return duplicate


class SqliteAlertDeduplicationStore:
    """Persistent ground-side alert-ID deduplication across receiver restarts."""

    def __init__(self, path: str | Path, *, capacity: int = 10_000) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("deduplication capacity must be a positive integer")
        self.path = Path(path)
        self.capacity = capacity
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS received_alerts (
                alert_id TEXT PRIMARY KEY,
                payload_sha256 TEXT NOT NULL,
                first_received_at_unix_s REAL NOT NULL,
                last_received_at_unix_s REAL NOT NULL,
                duplicate_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._prune_to_capacity_locked()
        self._connection.commit()
        self._lock = RLock()

    def check_and_record(
        self, alert_id: str, payload_sha256: str, *, received_at_unix_s: float
    ) -> bool:
        if not alert_id or len(payload_sha256) != 64:
            raise ValueError("alert ID or payload SHA-256 is invalid")
        if not math.isfinite(received_at_unix_s) or received_at_unix_s < 0:
            raise ValueError("received alert timestamp is invalid")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload_sha256 FROM received_alerts WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO received_alerts
                        (alert_id, payload_sha256, first_received_at_unix_s,
                         last_received_at_unix_s)
                    VALUES (?, ?, ?, ?)
                    """,
                    (alert_id, payload_sha256, received_at_unix_s, received_at_unix_s),
                )
                self._prune_to_capacity_locked()
                return False
            if str(row[0]) != payload_sha256:
                raise AlertAuthenticationError("alert identifier was reused with different content")
            self._connection.execute(
                """
                UPDATE received_alerts
                SET last_received_at_unix_s = ?, duplicate_count = duplicate_count + 1
                WHERE alert_id = ?
                """,
                (received_at_unix_s, alert_id),
            )
            return True

    @property
    def record_count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) FROM received_alerts").fetchone()
        if row is None:
            raise RuntimeError("failed to count received alert records")
        return int(row[0])

    def _prune_to_capacity_locked(self) -> int:
        cursor = self._connection.execute(
            """
            DELETE FROM received_alerts
            WHERE alert_id IN (
                SELECT alert_id FROM received_alerts
                ORDER BY last_received_at_unix_s DESC, alert_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.capacity,),
        )
        return max(0, cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class UdpAcknowledgedAlertTransport:
    """Authenticated UDP alert sender with correlated, signed receiver ACKs."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        hmac_key: bytes,
        sender_id: str,
        receiver_id: str,
        acknowledgement_timeout_seconds: float = 1.0,
        maximum_clock_skew_seconds: float = 30.0,
    ) -> None:
        if not host.strip() or not sender_id.strip() or not receiver_id.strip():
            raise ValueError("UDP host and data-link identities cannot be empty")
        if not 1 <= port <= 65_535:
            raise ValueError("UDP port must be between 1 and 65535")
        if len(hmac_key) < 32:
            raise ValueError("alert HMAC key must contain at least 32 bytes")
        if (
            not math.isfinite(acknowledgement_timeout_seconds)
            or acknowledgement_timeout_seconds <= 0
        ):
            raise ValueError("acknowledgement timeout must be finite and positive")
        if not math.isfinite(maximum_clock_skew_seconds) or maximum_clock_skew_seconds <= 0:
            raise ValueError("maximum clock skew must be finite and positive")
        self.host = host
        self.port = port
        self.hmac_key = hmac_key
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.acknowledgement_timeout_seconds = acknowledgement_timeout_seconds
        self.maximum_clock_skew_seconds = maximum_clock_skew_seconds

    def send(self, document: dict[str, object]) -> AlertDeliveryReceipt:
        alert_id = document.get("alert_id")
        if document.get("event") != "fire_alert" or not isinstance(alert_id, str) or not alert_id:
            raise ValueError("UDP transport accepts only identified fire-alert documents")
        if document.get("hardware_control_enabled") is not False:
            raise ValueError("UDP alert document must explicitly disable hardware control")
        nonce = secrets.token_hex(16)
        request = _sign_document(
            {
                "protocol": _UDP_PROTOCOL,
                "version": _UDP_PROTOCOL_VERSION,
                "message_type": "fire_alert",
                "sender_id": self.sender_id,
                "receiver_id": self.receiver_id,
                "alert_id": alert_id,
                "nonce": nonce,
                "sent_at_unix_s": time.time(),
                "payload": document,
            },
            self.hmac_key,
        )
        encoded = _canonical_json_bytes(request)
        if len(encoded) > _MAX_UDP_PACKET_BYTES:
            raise ValueError("encoded UDP alert exceeds the protocol packet limit")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            channel.settimeout(self.acknowledgement_timeout_seconds)
            channel.connect((self.host, self.port))
            channel.send(encoded)
            acknowledgement = _verify_signed_document(
                _decode_udp_document(channel.recv(_MAX_UDP_PACKET_BYTES + 1)), self.hmac_key
            )
        _validate_packet_header(
            acknowledgement,
            message_type="fire_alert_ack",
            expected_sender_id=self.receiver_id,
            expected_receiver_id=self.sender_id,
            now_unix_s=time.time(),
            maximum_clock_skew_seconds=self.maximum_clock_skew_seconds,
        )
        if acknowledgement.get("alert_id") != alert_id or acknowledgement.get("nonce") != nonce:
            raise AlertAuthenticationError("receiver acknowledgement correlation mismatch")
        return AlertDeliveryReceipt(
            alert_id=alert_id,
            receiver_id=self.receiver_id,
            acknowledged_at_s=time.monotonic(),
        )


class AuthenticatedUdpAlertReceiver:
    """Ground-side authenticated receiver; it only accepts alerts and sends signed ACKs."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        hmac_key: bytes,
        receiver_id: str,
        expected_sender_id: str,
        receive_timeout_seconds: float | None = None,
        maximum_clock_skew_seconds: float = 30.0,
        deduplication_capacity: int = 10_000,
        deduplication_store: AlertDeduplicationStore | None = None,
    ) -> None:
        if not bind_host.strip() or not receiver_id.strip() or not expected_sender_id.strip():
            raise ValueError("UDP bind host and data-link identities cannot be empty")
        if not 0 <= port <= 65_535:
            raise ValueError("UDP receiver port must be between 0 and 65535")
        if len(hmac_key) < 32:
            raise ValueError("alert HMAC key must contain at least 32 bytes")
        if receive_timeout_seconds is not None and (
            not math.isfinite(receive_timeout_seconds) or receive_timeout_seconds <= 0
        ):
            raise ValueError("receive timeout must be finite and positive")
        if not math.isfinite(maximum_clock_skew_seconds) or maximum_clock_skew_seconds <= 0:
            raise ValueError("maximum clock skew must be finite and positive")
        if (
            isinstance(deduplication_capacity, bool)
            or not isinstance(deduplication_capacity, int)
            or deduplication_capacity <= 0
        ):
            raise ValueError("deduplication capacity must be a positive integer")
        self.bind_host = bind_host
        self.port = port
        self.hmac_key = hmac_key
        self.receiver_id = receiver_id
        self.expected_sender_id = expected_sender_id
        self.receive_timeout_seconds = receive_timeout_seconds
        self.maximum_clock_skew_seconds = maximum_clock_skew_seconds
        self.deduplication_store = deduplication_store or InMemoryAlertDeduplicationStore(
            capacity=deduplication_capacity
        )
        self._channel: socket.socket | None = None

    @property
    def local_address(self) -> tuple[str, int]:
        if self._channel is None:
            raise RuntimeError("UDP alert receiver is not open")
        host, port = self._channel.getsockname()[:2]
        return str(host), int(port)

    def open(self) -> None:
        if self._channel is not None:
            return
        channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        channel.settimeout(self.receive_timeout_seconds)
        try:
            channel.bind((self.bind_host, self.port))
        except OSError:
            channel.close()
            raise
        self._channel = channel

    def receive(self) -> ReceivedAuthenticatedAlert:
        self.open()
        channel = self._channel
        if channel is None:  # Defensive guard for optimized Python and unusual subclasses.
            raise RuntimeError("UDP alert receiver failed to open")
        encoded, peer = channel.recvfrom(_MAX_UDP_PACKET_BYTES + 1)
        request = _verify_signed_document(_decode_udp_document(encoded), self.hmac_key)
        _validate_packet_header(
            request,
            message_type="fire_alert",
            expected_sender_id=self.expected_sender_id,
            expected_receiver_id=self.receiver_id,
            now_unix_s=time.time(),
            maximum_clock_skew_seconds=self.maximum_clock_skew_seconds,
        )
        alert_id = request.get("alert_id")
        nonce = request.get("nonce")
        payload = request.get("payload")
        if not isinstance(alert_id, str) or not alert_id or not isinstance(nonce, str) or not nonce:
            raise AlertAuthenticationError("alert correlation fields are invalid")
        if not isinstance(payload, dict) or payload.get("alert_id") != alert_id:
            raise AlertAuthenticationError("alert payload correlation mismatch")
        if (
            payload.get("event") != "fire_alert"
            or payload.get("hardware_control_enabled") is not False
        ):
            raise AlertAuthenticationError("data-link payload is not a non-control fire alert")
        payload_hash = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
        duplicate = self.deduplication_store.check_and_record(
            alert_id,
            payload_hash,
            received_at_unix_s=time.time(),
        )
        acknowledgement = _sign_document(
            {
                "protocol": _UDP_PROTOCOL,
                "version": _UDP_PROTOCOL_VERSION,
                "message_type": "fire_alert_ack",
                "sender_id": self.receiver_id,
                "receiver_id": self.expected_sender_id,
                "alert_id": alert_id,
                "nonce": nonce,
                "sent_at_unix_s": time.time(),
            },
            self.hmac_key,
        )
        channel.sendto(_canonical_json_bytes(acknowledgement), peer)
        return ReceivedAuthenticatedAlert(
            document=payload,
            sender_id=self.expected_sender_id,
            duplicate=duplicate,
            peer=(str(peer[0]), int(peer[1])),
        )

    def close(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            channel.close()

    def __enter__(self) -> AuthenticatedUdpAlertReceiver:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class RetryingAcknowledgedAlertPublisher:
    """Requires a correlated receiver ACK before publish is considered successful."""

    def __init__(
        self,
        transport: AcknowledgedAlertTransport,
        *,
        maximum_attempts: int = 3,
        initial_backoff_seconds: float = 0.1,
        backoff_multiplier: float = 2.0,
        maximum_backoff_seconds: float = 5.0,
        sleep_fn=time.sleep,
    ) -> None:
        if maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if not math.isfinite(initial_backoff_seconds) or initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must be finite and non-negative")
        if not math.isfinite(backoff_multiplier) or backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be finite and at least one")
        if not math.isfinite(maximum_backoff_seconds) or maximum_backoff_seconds < 0:
            raise ValueError("maximum_backoff_seconds must be finite and non-negative")
        self.transport = transport
        self.maximum_attempts = maximum_attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self.backoff_multiplier = backoff_multiplier
        self.maximum_backoff_seconds = maximum_backoff_seconds
        self.sleep_fn = sleep_fn
        self.last_attempt_count = 0

    def publish(self, alert: FireAlert) -> None:
        document = alert_document(alert)
        last_error: Exception | None = None
        self.last_attempt_count = 0
        delay = min(self.initial_backoff_seconds, self.maximum_backoff_seconds)
        for attempt in range(1, self.maximum_attempts + 1):
            self.last_attempt_count = attempt
            try:
                receipt = self.transport.send(document)
                _validate_receipt(receipt, expected_alert_id=alert.alert_id)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < self.maximum_attempts:
                    if delay > 0:
                        self.sleep_fn(delay)
                    delay = min(
                        self.maximum_backoff_seconds,
                        delay * self.backoff_multiplier,
                    )
                continue
            return
        error_type = type(last_error).__name__ if last_error is not None else "UnknownError"
        raise AlertAcknowledgementError(
            f"receiver acknowledgement failed after {self.maximum_attempts} attempts: {error_type}"
        ) from last_error


class LoopbackAcknowledgedAlertTransport:
    """Deterministic HIL receiver; it performs no network or radio I/O."""

    def __init__(self, *, receiver_id: str = "loopback-hil", fail_first_attempts: int = 0) -> None:
        if not receiver_id.strip():
            raise ValueError("receiver_id cannot be empty")
        if fail_first_attempts < 0:
            raise ValueError("fail_first_attempts cannot be negative")
        self.receiver_id = receiver_id
        self.fail_first_attempts = fail_first_attempts
        self.attempt_count = 0

    def send(self, document: dict[str, object]) -> AlertDeliveryReceipt:
        self.attempt_count += 1
        if self.attempt_count <= self.fail_first_attempts:
            raise TimeoutError("simulated receiver timeout")
        return AlertDeliveryReceipt(
            alert_id=str(document["alert_id"]),
            receiver_id=self.receiver_id,
            acknowledged_at_s=time.monotonic(),
        )


def alert_document(alert: FireAlert) -> dict[str, object]:
    return {
        "event": "fire_alert",
        "alert_id": alert.alert_id,
        "mission_id": alert.mission_id,
        "target_id": alert.target_id,
        "target_revision": alert.target_revision,
        "frame_id": alert.frame_id,
        "label": alert.label,
        "confidence": alert.confidence,
        "bbox": alert.bbox.rounded(),
        "observed_at_s": alert.observed_at_s,
        "aircraft_position": {
            "latitude_deg": _finite_or_none(alert.aircraft_latitude_deg),
            "longitude_deg": _finite_or_none(alert.aircraft_longitude_deg),
            "altitude_agl_m": _finite_or_none(alert.aircraft_altitude_agl_m),
        },
        "hardware_control_enabled": False,
    }


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _validate_receipt(receipt: AlertDeliveryReceipt, *, expected_alert_id: str) -> None:
    if receipt.alert_id != expected_alert_id:
        raise AlertAcknowledgementError("receiver acknowledgement alert_id mismatch")
    if not receipt.receiver_id.strip():
        raise AlertAcknowledgementError("receiver acknowledgement identity is empty")
    if not math.isfinite(receipt.acknowledged_at_s) or receipt.acknowledged_at_s < 0:
        raise AlertAcknowledgementError("receiver acknowledgement timestamp is invalid")


@dataclass(slots=True)
class RecordingAlertPublisher:
    """In-memory publisher for replay, tests and HIL orchestration."""

    _alerts: list[FireAlert] = field(default_factory=list, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)

    def publish(self, alert: FireAlert) -> None:
        with self._lock:
            self._alerts.append(alert)

    def alerts(self) -> tuple[FireAlert, ...]:
        with self._lock:
            return tuple(self._alerts)


class JsonLineAlertPublisher:
    """Real-time JSON Lines output; replace this port with the later data-link adapter."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = RLock()

    def publish(self, alert: FireAlert) -> None:
        document = alert_document(alert)
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._stream.write(encoded)
            self._stream.write("\n")
            self._stream.flush()


class SqliteAlertOutbox:
    """Durable at-least-once alert queue for a later acknowledged data link."""

    def __init__(
        self,
        path: str | Path,
        *,
        keep_latest_delivered: int | None = 10_000,
    ) -> None:
        if keep_latest_delivered is not None and (
            isinstance(keep_latest_delivered, bool)
            or not isinstance(keep_latest_delivered, int)
            or keep_latest_delivered < 0
        ):
            raise ValueError("keep_latest_delivered must be a non-negative integer or None")
        self.path = Path(path)
        self.keep_latest_delivered = keep_latest_delivered
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_outbox (
                alert_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'delivered')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_type TEXT,
                created_at_s REAL NOT NULL,
                delivered_at_s REAL
            )
            """
        )
        self._connection.commit()
        self._lock = RLock()

    def enqueue(self, alert: FireAlert) -> None:
        encoded = json.dumps(
            alert_document(alert),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO alert_outbox
                    (alert_id, payload_json, status, created_at_s)
                VALUES (?, ?, 'pending', ?)
                """,
                (alert.alert_id, encoded, alert.observed_at_s),
            )

    def mark_delivered(self, alert_id: str, *, delivered_at_s: float) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE alert_outbox
                SET status = 'delivered', attempt_count = attempt_count + 1,
                    last_error_type = NULL, delivered_at_s = ?
                WHERE alert_id = ? AND status = 'pending'
                """,
                (delivered_at_s, alert_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"pending alert not found: {alert_id}")
            if self.keep_latest_delivered is not None:
                self._prune_delivered_locked(self.keep_latest_delivered)

    def mark_failed(self, alert_id: str, *, error_type: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE alert_outbox
                SET attempt_count = attempt_count + 1, last_error_type = ?
                WHERE alert_id = ? AND status = 'pending'
                """,
                (error_type, alert_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"pending alert not found: {alert_id}")

    def pending_alerts(self, *, limit: int = 100) -> tuple[FireAlert, ...]:
        if limit <= 0:
            raise ValueError("outbox limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM alert_outbox
                WHERE status = 'pending'
                ORDER BY created_at_s, alert_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_alert_from_document(json.loads(row[0])) for row in rows)

    def attempt_count(self, alert_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT attempt_count FROM alert_outbox WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"alert not found: {alert_id}")
        return int(row[0])

    def prune_delivered(self, *, keep_latest: int = 10_000) -> int:
        if isinstance(keep_latest, bool) or not isinstance(keep_latest, int) or keep_latest < 0:
            raise ValueError("keep_latest must be a non-negative integer")
        with self._lock, self._connection:
            return self._prune_delivered_locked(keep_latest)

    def _prune_delivered_locked(self, keep_latest: int) -> int:
        cursor = self._connection.execute(
            """
            DELETE FROM alert_outbox
            WHERE alert_id IN (
                SELECT alert_id FROM alert_outbox
                WHERE status = 'delivered'
                ORDER BY delivered_at_s DESC, alert_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (keep_latest,),
        )
        return max(0, cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _alert_from_document(document: dict[str, object]) -> FireAlert:
    bbox = document["bbox"]
    position = document.get("aircraft_position", {})
    if not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(position, dict):
        raise ValueError("invalid persisted alert document")
    return FireAlert(
        alert_id=str(document["alert_id"]),
        mission_id=str(document["mission_id"]),
        target_id=str(document["target_id"]),
        target_revision=int(document["target_revision"]),
        frame_id=str(document["frame_id"]),
        label=str(document["label"]),
        confidence=float(document["confidence"]),
        bbox=BoundingBox(*(float(value) for value in bbox)),
        observed_at_s=float(document["observed_at_s"]),
        aircraft_latitude_deg=_optional_float(position.get("latitude_deg")),
        aircraft_longitude_deg=_optional_float(position.get("longitude_deg")),
        aircraft_altitude_agl_m=_optional_float(position.get("altitude_agl_m")),
    )


def _optional_float(value: object) -> float:
    return float(value) if value is not None else float("nan")


__all__ = [
    "AlertDeduplicationStore",
    "AlertAuthenticationError",
    "AlertPublisher",
    "AcknowledgedAlertTransport",
    "AlertAcknowledgementError",
    "AlertDeliveryReceipt",
    "AuthenticatedUdpAlertReceiver",
    "InMemoryAlertDeduplicationStore",
    "JsonLineAlertPublisher",
    "LoopbackAcknowledgedAlertTransport",
    "RecordingAlertPublisher",
    "ReceivedAuthenticatedAlert",
    "RetryingAcknowledgedAlertPublisher",
    "SqliteAlertOutbox",
    "SqliteAlertDeduplicationStore",
    "UdpAcknowledgedAlertTransport",
    "alert_document",
]
