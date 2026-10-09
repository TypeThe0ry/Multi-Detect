"""Shared fail-closed validation of Gazebo sensor fault recovery diagnostics."""

from __future__ import annotations

import json

from multidetect.autonomy.serialization import canonical_sha256


class SensorFaultDiagnosticError(RuntimeError):
    """A sensor fault timeline failed its identity or observed-recovery gate."""


def verify_sensor_fault_diagnostic(
    stderr_tail: str,
    *,
    fault_source: str,
    fault_start_image: int,
    fault_duration_images: int,
) -> dict[str, object]:
    """Accept only a matching helper-finished record with observed recovery."""

    finished = []
    for line in stderr_tail.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("event") == "gazebo_sensor_stream_finished":
            finished.append(record)
    if len(finished) != 1:
        raise SensorFaultDiagnosticError(
            "sensor fault exercise requires exactly one helper-finished diagnostic"
        )
    record = finished[0]
    fault = record.get("fault_injection")
    if not isinstance(fault, dict) or any(
        (
            fault.get("enabled") is not True,
            fault.get("source") != fault_source,
            fault.get("start_image") != fault_start_image,
            fault.get("duration_images") != fault_duration_images,
            fault.get("window_key") != "received_image_index",
            fault.get("dynamic_source_policy")
            != "reject_during_window_and_until_post_window_fresh_sample",
            fault.get("cache_boundary_policy")
            != "clear_source_history_on_first_dropped_callback_keep_stamp_order",
        )
    ):
        raise SensorFaultDiagnosticError("sensor fault helper diagnostic identity mismatch")
    dropped = fault.get("dropped_counts")
    selected_drops = (
        dropped.get(f"fault_dropped_{fault_source}")
        if isinstance(dropped, dict)
        else None
    )
    recovered = fault.get("recovered_frame_count")
    window_rejected = fault.get("window_rejected_frame_count")
    stale_rejected = fault.get("stale_source_rejection_count")
    emitted_during_window = fault.get("emitted_during_window_count")
    timeline = fault.get("timeline")
    timeline_overflow = fault.get("timeline_overflow")
    source_epoch = fault.get("source_epoch")
    timeline_events = (
        [item for item in timeline if isinstance(item, dict)]
        if isinstance(timeline, list)
        else []
    )
    if len(timeline_events) != len(timeline or ()):
        raise SensorFaultDiagnosticError("sensor fault timeline contains invalid records")
    window_end = fault_start_image + fault_duration_images
    received_indices = [item.get("received_image_index") for item in timeline_events]
    if any(type(index) is not int or index < 1 for index in received_indices):
        raise SensorFaultDiagnosticError("sensor fault timeline image index is invalid")
    if received_indices != sorted(received_indices):
        raise SensorFaultDiagnosticError("sensor fault timeline image index regressed")
    if any(
        type(item.get("fault_active")) is not bool
        or item["fault_active"] != (
            fault_start_image <= item["received_image_index"] < window_end
        )
        for item in timeline_events
    ):
        raise SensorFaultDiagnosticError("sensor fault timeline window boundary drift")
    dropped_stamps = [
        item.get("source_stamp_ns")
        for item in timeline_events
        if item.get("event") == "source_dropped"
    ]
    if any(type(stamp) is not int or stamp <= 0 for stamp in dropped_stamps):
        raise SensorFaultDiagnosticError("sensor fault dropped source timestamp is invalid")
    observed_drop_events = (
        len(dropped_stamps)
        if fault_source != "image"
        else sum(
            item.get("event") == "frame_rejected_fault_window"
            for item in timeline_events
        )
    )
    if selected_drops != observed_drop_events:
        raise SensorFaultDiagnosticError("sensor fault dropped source count drift")
    dropped_max = max(dropped_stamps, default=None)
    if fault_source != "image" and (type(source_epoch) is not int or source_epoch < 1):
        raise SensorFaultDiagnosticError(
            "dynamic fault did not expose a positive source epoch"
        )
    wire_events = [
        item
        for item in timeline_events
        if item.get("event") in {"wire_emitted", "wire_emitted_after_recovery"}
    ]
    emitted_wire_events = [
        item for item in wire_events if item.get("event") == "wire_emitted"
    ]
    wire_sequences = [item.get("wire_sequence") for item in emitted_wire_events]
    if any(type(sequence) is not int or sequence < 0 for sequence in wire_sequences):
        raise SensorFaultDiagnosticError("sensor fault wire sequence is invalid")
    if wire_sequences != list(range(len(wire_sequences))):
        raise SensorFaultDiagnosticError("sensor fault wire sequence is not contiguous")
    fresh_events = [
        item
        for item in timeline_events
        if item.get("event") == "source_fresh_after_window"
    ]
    if fault_source != "image" and (
        dropped_max is None
        or not any(
            type(item.get("source_stamp_ns")) is int
            and item["source_stamp_ns"] > dropped_max
            for item in fresh_events
        )
    ):
        raise SensorFaultDiagnosticError(
            "sensor fault source did not prove a fresh post-window sample"
        )
    for item in emitted_wire_events:
        if fault_source == "image":
            if item.get("source_stamp_ns") is not None:
                raise SensorFaultDiagnosticError(
                    "image fault wire carried a dynamic source stamp"
                )
        elif (
            type(item.get("source_stamp_ns")) is not int
            or item["source_stamp_ns"] <= 0
            or (
                item["received_image_index"] >= window_end
                and (dropped_max is None or item["source_stamp_ns"] <= dropped_max)
            )
        ):
            raise SensorFaultDiagnosticError("dynamic fault wire used a stale source")
    if fault_source != "image":
        epoch_events = {
            "source_dropped",
            "source_fresh_after_window",
            "frame_rejected_cached_source",
            "frame_rejected_waiting_for_fresh_source",
            "wire_emitted",
            "wire_emitted_after_recovery",
        }
        if any(
            item.get("event") in epoch_events
            and (
                type(item.get("source_epoch")) is not int
                or item["source_epoch"] != (
                    0 if item["received_image_index"] < fault_start_image else source_epoch
                )
            )
            for item in timeline_events
        ):
            raise SensorFaultDiagnosticError(
                "dynamic fault timeline source epoch drift"
            )
    recovery_wires = [
        item
        for item in timeline_events
        if item.get("event") == "wire_emitted_after_recovery"
    ]
    emitted_by_sequence = {item["wire_sequence"]: item for item in emitted_wire_events}
    recovery_keys = (
        "received_image_index", "source_stamp_ns", "source_epoch", "fault_active"
    )
    if len(recovery_wires) != 1 or any(
        item["received_image_index"] < window_end
        or type(item.get("wire_sequence")) is not int
        or item["wire_sequence"] not in emitted_by_sequence
        or any(
            item.get(key) != emitted_by_sequence[item["wire_sequence"]].get(key)
            for key in recovery_keys
        )
        for item in recovery_wires
    ):
        raise SensorFaultDiagnosticError(
            "sensor fault recovery wire is not bound to an emitted wire"
        )
    recovery_index = timeline_events.index(recovery_wires[0])
    matching_wire_index = next(
        index
        for index, item in enumerate(timeline_events)
        if item.get("event") == "wire_emitted"
        and item.get("wire_sequence") == recovery_wires[0].get("wire_sequence")
    )
    if matching_wire_index >= recovery_index:
        raise SensorFaultDiagnosticError(
            "sensor fault recovery marker precedes its emitted wire"
        )
    if fault_source != "image" and not any(
        index < recovery_index
        and item.get("event") == "source_fresh_after_window"
        and type(item.get("source_stamp_ns")) is int
        and dropped_max is not None
        and item["source_stamp_ns"] > dropped_max
        for index, item in enumerate(timeline_events)
    ):
        raise SensorFaultDiagnosticError(
            "sensor fault recovery wire precedes the fresh source callback"
        )
    wire_during_window = any(
        item.get("fault_active") is True and item.get("wire_sequence") is not None
        for item in timeline_events
    )
    recovered_wire = any(
        item.get("event") == "wire_emitted_after_recovery"
        and item.get("fault_active") is False
        and type(item.get("wire_sequence")) is int
        for item in timeline_events
    )
    if (
        record.get("gate_passed") is not True
        or type(selected_drops) is not int
        or selected_drops < 1
        or type(recovered) is not int
        or recovered < 1
        or type(window_rejected) is not int
        or window_rejected < 1
        or type(stale_rejected) is not int
        or (fault_source != "image" and stale_rejected < 1)
        or type(emitted_during_window) is not int
        or emitted_during_window != 0
        or not timeline_events
        or timeline_overflow is not False
        or (fault_source != "image" and fault.get("cache_cleared") is not True)
        or wire_during_window
        or not recovered_wire
        or fault.get("recovery_observed") is not True
    ):
        raise SensorFaultDiagnosticError(
            "sensor fault was not both observed and recovered before helper completion; "
            f"gate_passed={record.get('gate_passed')!r}, "
            f"dropped={selected_drops!r}, recovered={recovered!r}, "
            f"window_rejected={window_rejected!r}, stale_rejected={stale_rejected!r}, "
            f"emitted_during_window={emitted_during_window!r}, "
            f"timeline_events={[item.get('event') for item in timeline_events]!r}, "
            f"timeline_overflow={timeline_overflow!r}, "
            f"cache_cleared={fault.get('cache_cleared')!r}, "
            f"recovery_observed={fault.get('recovery_observed')!r}"
        )
    return {
        "enabled": True,
        "source": fault_source,
        "start_image": fault_start_image,
        "duration_images": fault_duration_images,
        "window_key": "received_image_index",
        "dynamic_source_policy": (
            "reject_during_window_and_until_post_window_fresh_sample"
        ),
        "cache_boundary_policy": (
            "clear_source_history_on_first_dropped_callback_keep_stamp_order"
        ),
        "source_epoch": source_epoch,
        "cache_cleared": fault.get("cache_cleared") is True,
        "dropped_source_messages": selected_drops,
        "window_rejected_frame_count": window_rejected,
        "stale_source_rejection_count": stale_rejected,
        "emitted_during_window_count": emitted_during_window,
        "timeline": timeline_events,
        "timeline_overflow": False,
        "recovered_frame_count": recovered,
        "recovery_observed": True,
        "helper_finished_sha256": canonical_sha256(record),
    }

