"""Host orchestration + real UDP test-peer replies; not aircraft acceptance.

The original Mission, VLM output and clearance remain explicit software
fixtures. Post-pause mode/navigation messages cross actual kernel sockets and
are decoded from original MAVLink2 bytes, but the peer has no flight dynamics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from contextlib import ExitStack
from dataclasses import replace

import pytest
import test_onboard_vlm_admission as source_fixture
from test_onboard_continuation_admission import _admission_setup, _admit
from test_onboard_continuation_binding import SOURCE, TX, _fact
from test_onboard_vlm_admission import _clearance, _current
from test_px4_continuation_wire_loopback import _child_transaction, _loopback, _session

from multidetect.autonomy.onboard_continuation_binding import (
    RECEIVE_CONTRACT,
    ContinuationParentEvidence,
)
from multidetect.autonomy.onboard_continuation_host import (
    ContinuationHostError,
    OriginalParentContinuationHost,
)
from multidetect.autonomy.onboard_continuation_paused_admission import (
    admit_paused_continuation,
    prepare_paused_continuation,
)
from multidetect.autonomy.onboard_evidence_collector import (
    TELEMETRY_CONTRACT,
    OnboardTelemetryObservation,
)
from multidetect.autonomy.onboard_history import OnboardInferenceScope
from multidetect.autonomy.onboard_multimodal import OnboardMultimodalScope
from multidetect.px4_continuation_wire import ContinuationWireState


def _raw_packet(message):
    packet = bytes(message.get_msgbuf())
    return {
        "wire_bytes_base64": base64.b64encode(packet).decode("ascii"),
        "wire_bytes": len(packet),
        "wire_sha256": hashlib.sha256(packet).hexdigest(),
    }


def _admit_received_hold(original, attempt, received, clock):
    """Host test-only receiver join; every selected field has its original packet."""
    message, at_s = received["GLOBAL_POSITION_INT"]
    gps, _ = received["GPS_RAW_INT"]
    current = _current(
        original.anchor,
        north=-6,
        east=2,
        at_s=at_s,
        context_changes={
            "gps_fix_type": gps.fix_type,
            "gps_horizontal_accuracy_m": gps.h_acc / 1000,
        },
    )
    assert round(current.context.aircraft_position.latitude_deg * 1e7) == message.lat
    assert round(current.context.aircraft_position.longitude_deg * 1e7) == message.lon
    assert round(current.context.aircraft_position.altitude_m * 1000) == message.relative_alt
    scope = (
        OnboardMultimodalScope.from_inference_scope(current.scope)
        if isinstance(current.scope, OnboardInferenceScope)
        else current.scope
    )
    binding = original.parent_evidence.mission_binding
    telemetry = []
    for kind, names in (
        ("HEARTBEAT", ("base_mode", "custom_mode")),
        ("EXTENDED_SYS_STATE", ("landed_state",)),
        ("MISSION_CURRENT", ("seq",)),
    ):
        packet, stamp = received[kind]
        telemetry.append(
            _fact(
                {
                    "schema_version": 1,
                    "contract": TELEMETRY_CONTRACT,
                    "message_id": f"owned-kernel-{kind}",
                    "observed_at_s": stamp,
                    "clock_epoch": scope.clock_epoch,
                    "source_id": SOURCE,
                    "source_system_id": packet.get_srcSystem(),
                    "source_component_id": packet.get_srcComponent(),
                    "mission_sha256": binding.mission.canonical_hash,
                    "transaction_id": TX,
                    "message": {
                        "message_type": kind,
                        **{name: getattr(packet, name) for name in names},
                    },
                    **_raw_packet(packet),
                },
                OnboardTelemetryObservation,
            )
        )
    navigation = []
    for kind, names in (
        ("HOME_POSITION", ("latitude", "longitude", "altitude", "time_usec")),
        ("PARAM_VALUE", ("param_type", "param_count", "param_index")),
        ("GLOBAL_POSITION_INT", ("lat", "lon", "alt", "relative_alt", "time_boot_ms")),
    ):
        packet, stamp = received[kind]
        body = {"message_type": kind, **{name: getattr(packet, name) for name in names}}
        if kind == "PARAM_VALUE":
            wire_packet = bytes(packet.get_msgbuf())
            assert wire_packet[0] == 0xFD  # MAVLink2, ten-byte header.
            body.update(
                param_id=packet.param_id,
                parameter_encoding="bytewise",  # Explicit peer contract, not inferred from name.
                raw_value_hex=wire_packet[10:14].hex(),
            )
        navigation.append(
            _fact(
                {
                    "schema_version": 1,
                    "contract": RECEIVE_CONTRACT,
                    "message_id": f"owned-kernel-{kind}",
                    "scope_sha256": scope.canonical_hash,
                    "session_id": scope.session_id,
                    "aircraft_id": scope.aircraft_id,
                    "observed_at_s": stamp,
                    "clock_epoch": scope.clock_epoch,
                    "source_id": SOURCE,
                    "source_system_id": packet.get_srcSystem(),
                    "source_component_id": packet.get_srcComponent(),
                    "vehicle_identity_sha256": binding.vehicle_identity_sha256,
                    "mission_sha256": binding.mission.canonical_hash,
                    "transaction_id": TX,
                    "message": body,
                    **_raw_packet(packet),
                }
            )
        )
    proof = ContinuationParentEvidence(
        scope,
        binding,
        original.parent_evidence.journal_context,
        current.context,
        tuple(telemetry),
        tuple(navigation),
        original.parent_evidence.trusted_source_ids,
    )
    compilation = prepare_paused_continuation(
        attempt,
        current,
        hold_parent_evidence=proof,
        mission_id="kernel-after-pause-child",
        now_s=clock(),
    )
    # This clearance is test data. It is never described as a real 3-D review.
    current = replace(
        current,
        clearance=_clearance(
            current.context,
            current.event,
            original.anchor.target,
            wire_hash=compilation.wire_hash,
        ),
    )
    return admit_paused_continuation(
        attempt,
        current,
        hold_parent_evidence=proof,
        mission_id="kernel-after-pause-child",
        now_s=clock(),
    )


@pytest.mark.parametrize("case", ["resume", "cancel_after_admission", "foreign_geofence"])
def test_host_original_source_to_postpause_admission_to_actual_udp(
    tmp_path,
    record_property,
    case,
    monkeypatch,
):
    # Configure the ORIGINAL fixture request before its fake inference runs.
    # This does not extend a completed result's deadline or change defaults.
    original_source_factory = source_fixture._source

    def initial_source_with_protocol_budget(*args, **kwargs):
        request, *remaining = original_source_factory(*args, **kwargs)
        request = replace(request, model_input=replace(request.model_input, deadline_at_s=5.0))
        return request, *remaining

    monkeypatch.setattr(source_fixture, "_source", initial_source_with_protocol_budget)
    anchor, current, proof, policy, _ = _admission_setup(tmp_path / "original")
    original = _admit(anchor, current, proof, policy)
    drifted = _current(anchor, north=-6, east=2)
    started = time.monotonic()

    def clock():
        return 2.2 + time.monotonic() - started

    host = OriginalParentContinuationHost(
        original,
        attempt_id="owned-kernel-host-test",
        generation=0,
        host_clock=clock,
        current_gate=lambda phase, generation: True,
    )
    assert host.phase_guard("pause", 0) is False  # Not attached to a running owner.
    navigation = {
        "home": current.context.home_position,
        "hold_position": drifted.context.aircraft_position,
    }
    with ExitStack() as stack:
        gcs, peer, endpoint = stack.enter_context(_loopback(navigation=navigation))
        received = {}

        def receive(duration):
            packet = (
                gcs.recv_match(blocking=False)
                if duration <= 0
                else gcs.recv_match(blocking=True, timeout=duration)
            )
            if packet is not None:
                if packet.get_type() == "HEARTBEAT":
                    # Keep one actual ordered batch, not new HB mixed with a
                    # previous batch's EXT/CURRENT/navigation timestamps.
                    received.clear()
                received[packet.get_type()] = (packet, clock())
            return packet

        wire = _session(
            gcs,
            endpoint,
            phase_guard=host.phase_guard,
            host_clock=clock,
            clock_epoch=current.scope.clock_epoch,
            receive=receive,
        )
        admitted = []

        def admit_after_pause(attempt, pause):
            assert pause["state"] == "paused" and wire.state is ContinuationWireState.PAUSED
            # One sole receiver, on the executing thread; no competing recv loop.
            deadline = time.monotonic() + 0.5
            needed = {
                "HEARTBEAT",
                "EXTENDED_SYS_STATE",
                "MISSION_CURRENT",
                "HOME_POSITION",
                "PARAM_VALUE",
                "GPS_RAW_INT",
                "GLOBAL_POSITION_INT",
            }
            while not needed.issubset(received) and time.monotonic() < deadline:
                receive(0.01)
            assert needed.issubset(received)
            result = _admit_received_hold(original, attempt, received, clock)
            admitted.append(result)
            if case == "cancel_after_admission":
                host.cancel("test cancellation after real HOLD, before upload")
            return result

        def authorize_child(admission):
            # Explicit software grant fixture owned here, not manufactured by host.
            transaction, _ = stack.enter_context(
                _child_transaction(
                    tmp_path,
                    admission=admission,
                    bindings_changes={"geofence_hash": "0" * 64}
                    if case == "foreign_geofence"
                    else None,
                )
            )
            return transaction

        if case == "foreign_geofence":
            with pytest.raises(ContinuationHostError, match="exact admitted lineage"):
                host.execute(
                    wire, admit_after_pause=admit_after_pause, authorize_child=authorize_child
                )
            assert not any(kind == "MISSION_COUNT" for kind, _, _ in peer.received)
        elif case == "cancel_after_admission":
            with pytest.raises(ValueError, match="cancelled"):
                host.execute(
                    wire, admit_after_pause=admit_after_pause, authorize_child=authorize_child
                )
            assert not any(kind == "MISSION_COUNT" for kind, _, _ in peer.received)
        else:
            receipt = host.execute(
                wire, admit_after_pause=admit_after_pause, authorize_child=authorize_child
            )
            assert receipt["receiver_confirmed_resume"] is True
            assert receipt["actual_flight_verified"] is False
            assert admitted[0].compilation.wire_hash != original.compilation.wire_hash
            assert peer.items[0].x == round(drifted.context.aircraft_position.latitude_deg * 1e7)
            assert peer.items[1].x == round(original.anchor.target.latitude_deg * 1e7)
            assert original.anchor.canonical_hash == anchor.canonical_hash
            with pytest.raises(ContinuationHostError):
                host.execute(
                    wire, admit_after_pause=admit_after_pause, authorize_child=authorize_child
                )
        record_property("host_kernel_udp_session", json.dumps(host.to_mapping(), sort_keys=True))
        snapshot = host.to_mapping()
        original_event_state = snapshot["events"][0]["state"]
        original_hash = snapshot["receipt_sha256"]
        snapshot["events"][0]["state"] = "caller-fabricated-resumed"
        snapshot["wire"]["sends"][0]["args"][0] = 255
        reopened = host.to_mapping()
        assert reopened["events"][0]["state"] == original_event_state
        assert reopened["receipt_sha256"] == original_hash
