from __future__ import annotations

import io
import json
import threading
from dataclasses import replace

import pytest

from multidetect.alerts import (
    AlertAcknowledgementError,
    AlertAuthenticationError,
    AlertDeliveryReceipt,
    AuthenticatedUdpAlertReceiver,
    JsonLineAlertPublisher,
    LoopbackAcknowledgedAlertTransport,
    RecordingAlertPublisher,
    RetryingAcknowledgedAlertPublisher,
    SqliteAlertDeduplicationStore,
    SqliteAlertOutbox,
    UdpAcknowledgedAlertTransport,
    alert_document,
)
from multidetect.domain import BoundingBox, FireAlert


def fire_alert() -> FireAlert:
    return FireAlert(
        alert_id="alert-1",
        mission_id="mission-1",
        target_id="track-1",
        target_revision=4,
        frame_id="frame-4",
        label="flame",
        confidence=0.93,
        bbox=BoundingBox(0.1, 0.2, 0.3, 0.4),
        observed_at_s=12.5,
        aircraft_latitude_deg=31.123456,
        aircraft_longitude_deg=121.654321,
        aircraft_altitude_agl_m=48.5,
    )


def test_recording_alert_publisher_preserves_alert() -> None:
    publisher = RecordingAlertPublisher()
    alert = fire_alert()

    publisher.publish(alert)

    assert publisher.alerts() == (alert,)


def test_json_line_alert_publisher_emits_data_link_envelope() -> None:
    stream = io.StringIO()
    publisher = JsonLineAlertPublisher(stream)

    publisher.publish(fire_alert())

    document = json.loads(stream.getvalue())
    assert document["event"] == "fire_alert"
    assert document["target_id"] == "track-1"
    assert document["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert document["aircraft_position"] == {
        "latitude_deg": 31.123456,
        "longitude_deg": 121.654321,
        "altitude_agl_m": 48.5,
    }
    assert document["hardware_control_enabled"] is False


def test_sqlite_outbox_persists_pending_alert_until_delivery(tmp_path) -> None:
    path = tmp_path / "alerts.sqlite3"
    alert = fire_alert()
    outbox = SqliteAlertOutbox(path)
    outbox.enqueue(alert)
    outbox.mark_failed(alert.alert_id, error_type="TimeoutError")
    outbox.close()

    reopened = SqliteAlertOutbox(path)
    (pending,) = reopened.pending_alerts()
    assert pending.alert_id == alert.alert_id
    assert pending.bbox == alert.bbox
    assert reopened.attempt_count(alert.alert_id) == 1

    reopened.mark_delivered(alert.alert_id, delivered_at_s=13.0)

    assert reopened.pending_alerts() == ()
    assert reopened.attempt_count(alert.alert_id) == 2
    reopened.close()


def test_acknowledged_publisher_retries_with_bounded_backoff() -> None:
    delays: list[float] = []
    transport = LoopbackAcknowledgedAlertTransport(fail_first_attempts=2)
    publisher = RetryingAcknowledgedAlertPublisher(
        transport,
        maximum_attempts=3,
        initial_backoff_seconds=0.1,
        backoff_multiplier=2.0,
        sleep_fn=delays.append,
    )

    publisher.publish(fire_alert())

    assert publisher.last_attempt_count == 3
    assert transport.attempt_count == 3
    assert delays == [0.1, 0.2]


def test_acknowledged_publisher_rejects_mismatched_receipt() -> None:
    class _WrongReceiptTransport:
        def send(self, _document) -> AlertDeliveryReceipt:
            return AlertDeliveryReceipt("wrong-alert", "receiver", 10.0)

    publisher = RetryingAcknowledgedAlertPublisher(
        _WrongReceiptTransport(),
        maximum_attempts=2,
        initial_backoff_seconds=0,
    )

    with pytest.raises(AlertAcknowledgementError, match="after 2 attempts"):
        publisher.publish(fire_alert())

    assert publisher.last_attempt_count == 2


def test_acknowledged_publisher_caps_backoff_delay() -> None:
    delays: list[float] = []
    publisher = RetryingAcknowledgedAlertPublisher(
        LoopbackAcknowledgedAlertTransport(fail_first_attempts=3),
        maximum_attempts=4,
        initial_backoff_seconds=1.0,
        backoff_multiplier=10.0,
        maximum_backoff_seconds=2.0,
        sleep_fn=delays.append,
    )

    publisher.publish(fire_alert())

    assert delays == [1.0, 2.0, 2.0]


def test_outbox_prunes_only_old_delivered_alerts(tmp_path) -> None:
    outbox = SqliteAlertOutbox(tmp_path / "alerts.sqlite3")
    alerts = tuple(
        replace(fire_alert(), alert_id=f"alert-{index}", observed_at_s=float(index))
        for index in range(3)
    )
    for index, alert in enumerate(alerts):
        outbox.enqueue(alert)
        outbox.mark_delivered(alert.alert_id, delivered_at_s=float(index))

    deleted = outbox.prune_delivered(keep_latest=1)

    assert deleted == 2
    assert outbox.attempt_count("alert-2") == 1
    with pytest.raises(KeyError):
        outbox.attempt_count("alert-0")
    outbox.close()


def test_authenticated_udp_alert_round_trip_returns_correlated_ack() -> None:
    key = b"test-only-alert-key-material-32-bytes-minimum"
    received = []
    with AuthenticatedUdpAlertReceiver(
        bind_host="127.0.0.1",
        port=0,
        hmac_key=key,
        receiver_id="ground-1",
        expected_sender_id="aircraft-1",
        receive_timeout_seconds=2.0,
    ) as receiver:
        worker = threading.Thread(target=lambda: received.append(receiver.receive()))
        worker.start()
        transport = UdpAcknowledgedAlertTransport(
            host="127.0.0.1",
            port=receiver.local_address[1],
            hmac_key=key,
            sender_id="aircraft-1",
            receiver_id="ground-1",
            acknowledgement_timeout_seconds=2.0,
        )

        receipt = transport.send(alert_document(fire_alert()))
        worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert receipt.alert_id == "alert-1"
    assert receipt.receiver_id == "ground-1"
    assert len(received) == 1
    assert received[0].document["target_id"] == "track-1"
    assert received[0].duplicate is False


def test_authenticated_udp_receiver_deduplicates_retransmitted_alert_id() -> None:
    key = b"test-only-alert-key-material-32-bytes-minimum"
    received = []
    with AuthenticatedUdpAlertReceiver(
        bind_host="127.0.0.1",
        port=0,
        hmac_key=key,
        receiver_id="ground-1",
        expected_sender_id="aircraft-1",
        receive_timeout_seconds=2.0,
    ) as receiver:
        worker = threading.Thread(
            target=lambda: received.extend((receiver.receive(), receiver.receive()))
        )
        worker.start()
        transport = UdpAcknowledgedAlertTransport(
            host="127.0.0.1",
            port=receiver.local_address[1],
            hmac_key=key,
            sender_id="aircraft-1",
            receiver_id="ground-1",
            acknowledgement_timeout_seconds=2.0,
        )

        transport.send(alert_document(fire_alert()))
        transport.send(alert_document(fire_alert()))
        worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert [item.duplicate for item in received] == [False, True]


def test_authenticated_udp_receiver_rejects_wrong_shared_key() -> None:
    receiver_key = b"receiver-test-key-material-of-at-least-32-bytes"
    sender_key = b"different-sender-key-material-at-least-32-bytes"
    errors = []
    with AuthenticatedUdpAlertReceiver(
        bind_host="127.0.0.1",
        port=0,
        hmac_key=receiver_key,
        receiver_id="ground-1",
        expected_sender_id="aircraft-1",
        receive_timeout_seconds=1.0,
    ) as receiver:

        def receive_once() -> None:
            try:
                receiver.receive()
            except AlertAuthenticationError as exc:
                errors.append(exc)

        worker = threading.Thread(target=receive_once)
        worker.start()
        transport = UdpAcknowledgedAlertTransport(
            host="127.0.0.1",
            port=receiver.local_address[1],
            hmac_key=sender_key,
            sender_id="aircraft-1",
            receiver_id="ground-1",
            acknowledgement_timeout_seconds=0.2,
        )

        with pytest.raises(TimeoutError):
            transport.send(alert_document(fire_alert()))
        worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], AlertAuthenticationError)


def test_authenticated_udp_transport_rejects_fragmentation_sized_alert() -> None:
    transport = UdpAcknowledgedAlertTransport(
        host="127.0.0.1",
        port=14_600,
        hmac_key=b"test-only-alert-key-material-32-bytes-minimum",
        sender_id="aircraft-1",
        receiver_id="ground-1",
    )
    oversized = replace(fire_alert(), mission_id="m" * 2_000)

    with pytest.raises(ValueError, match="packet limit"):
        transport.send(alert_document(oversized))


def test_ground_receiver_deduplication_persists_across_restart(tmp_path) -> None:
    path = tmp_path / "received-alerts.sqlite3"
    payload_hash = "a" * 64
    store = SqliteAlertDeduplicationStore(path)

    assert store.check_and_record("alert-1", payload_hash, received_at_unix_s=100.0) is False
    store.close()

    reopened = SqliteAlertDeduplicationStore(path)
    assert reopened.check_and_record("alert-1", payload_hash, received_at_unix_s=101.0) is True
    with pytest.raises(AlertAuthenticationError, match="different content"):
        reopened.check_and_record("alert-1", "b" * 64, received_at_unix_s=102.0)
    reopened.close()
