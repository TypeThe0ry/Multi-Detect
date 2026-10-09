"""Partial Gazebo sensor input for a development VLM, without flight authority.

Actual RGB is reversibly bound to the original wire-v3 BGR payload and header.
Only header-covered GPS/odometry enters model facts. Configured quality, DEM AGL,
depth metadata and simulator health defaults are retained provenance, not measured
navigation safety. Hashes do not authenticate a collecting host or a simulator.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields

from ..backends.gazebo_sim import (
    GazeboSimFrameProvenance,
    GazeboSimFrameTelemetryProvenance,
    GazeboSimTelemetryProvider,
    GazeboSimWireTelemetryProvenance,
    reconstruct_gazebo_sim_wire_frame,
)
from ..clock import validate_host_identity
from .onboard_multimodal import OnboardImageFrame, OnboardMultimodalError, OnboardMultimodalInput
from .onboard_vlm_runtime_contract import OnboardVlmRuntimeRequest, serialize_onboard_vlm_messages
from .serialization import (
    canonical_json,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

LIVE_INPUT_CONTRACT = "multidetect.onboard-live-partial-state-input-v1"
LIVE_REQUEST_CONTRACT = "multidetect.onboard-live-vlm-request-v1"
_SWAP = "bgr24_to_rgb8_channel_swap"
_FACT_FIELDS = frozenset(
    {"frame_id", "rgb_sha256", "clock_epoch", "source_provenance", "preprocessing"}
)
_PROVENANCE_FIELDS = frozenset(item.name for item in fields(GazeboSimFrameProvenance))
_PROVENANCE_REQUIRED = frozenset(
    item.name for item in fields(GazeboSimFrameProvenance) if item.default is MISSING
) | {"wire_unsigned_header_hex", "wire_telemetry"}
_UNKNOWN = (
    "home_position",
    "relative_home_altitude",
    "agl_altitude",
    "airframe_profile",
    "battery_remaining",
    "wind",
    "airspeed",
    "armed",
    "flight_mode",
    "geofence_health",
    "position_health",
    "link_health",
    "route_clearance",
    "gps_quality_measurement",
)


def gazebo_sensor_fact_json(
    frame: OnboardImageFrame, provenance: GazeboSimFrameProvenance | Mapping[str, object]
) -> str:
    """Build the exact envelope; input construction performs full wire validation."""
    if not isinstance(frame, OnboardImageFrame):
        raise TypeError("frame must be OnboardImageFrame")
    if isinstance(provenance, GazeboSimFrameProvenance):
        provenance = provenance.to_mapping()
    if not isinstance(provenance, Mapping):
        raise TypeError("source provenance must be a Gazebo mapping or typed provenance")
    return canonical_json(
        {
            "frame_id": frame.frame_id,
            "rgb_sha256": frame.image_sha256,
            "clock_epoch": frame.scope.clock_epoch,
            "source_provenance": dict(provenance),
            "preprocessing": {
                "method": _SWAP,
                "source_payload_sha256": provenance.get("payload_sha256"),
                "rgb_sha256": frame.image_sha256,
            },
        }
    )


def _frame_facts(frame: OnboardImageFrame, text: str, previous_stamp: int | None) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 64 * 1024:
        raise OnboardMultimodalError("sensor facts must be bounded canonical JSON text")
    fact = strict_json_loads(text, name="live sensor facts")
    strict_object(fact, name="live sensor facts", required=_FACT_FIELDS)
    if canonical_json(fact) != text:
        raise OnboardMultimodalError("sensor facts must use exact canonical JSON")
    if (fact["frame_id"], fact["rgb_sha256"], fact["clock_epoch"]) != (
        frame.frame_id,
        frame.image_sha256,
        frame.scope.clock_epoch,
    ):
        raise OnboardMultimodalError("sensor facts cross frame/hash/clock scope")
    if frame.encoding != "rgb8" or frame.context is not None:
        raise OnboardMultimodalError(
            "live partial facts require original RGB8 with full context unknown"
        )
    provenance = strict_object(
        fact["source_provenance"],
        name="Gazebo provenance",
        required=_PROVENANCE_REQUIRED,
        optional=_PROVENANCE_FIELDS - _PROVENANCE_REQUIRED,
    )
    preprocessing = strict_object(
        fact["preprocessing"],
        name="RGB preprocessing",
        required=frozenset({"method", "source_payload_sha256", "rgb_sha256"}),
    )
    if preprocessing != {
        "method": _SWAP,
        "source_payload_sha256": provenance["payload_sha256"],
        "rgb_sha256": frame.image_sha256,
    }:
        raise OnboardMultimodalError("RGB preprocessing does not bind the source channel swap")
    if (
        provenance["frame_id"] != frame.frame_id
        or provenance["captured_at_s"] != frame.captured_at_s
    ):
        raise OnboardMultimodalError("Gazebo provenance has a different frame or host receive time")
    require_real(provenance["captured_at_s"], "host receive time", minimum=0)
    require_int(provenance["sequence"], "source sequence", minimum=0)
    if provenance["captured_at_clock"] not in {"host_monotonic", "host_perf_counter"}:
        raise OnboardMultimodalError("unsupported source host clock domain")
    if provenance["captured_at_clock"] == "host_perf_counter":
        validate_host_identity(provenance.get("host_clock_identity"))
    elif "host_clock_identity" in provenance:
        raise OnboardMultimodalError("legacy monotonic provenance cannot invent a clock identity")
    for key in ("record_sha256", "payload_sha256"):
        if require_sha256(provenance[key], key) != provenance[key]:
            raise OnboardMultimodalError("source hashes must be canonical lowercase")
    for key in ("width", "height", "stride", "payload_bytes", "flags"):
        require_int(provenance[key], key, minimum=1)
    if (
        provenance["width"],
        provenance["height"],
        provenance["stride"],
        provenance["payload_bytes"],
        provenance["pixel_format"],
    ) != (frame.width, frame.height, frame.width * 3, len(frame.image_bytes), "BGR24"):
        raise OnboardMultimodalError("source RGB geometry/format differs")
    # RGB8 is lossless and unresized: reverse exactly the collecting callback's
    # BGR->RGB channel swap, then use the production reader for all wire rules.
    bgr = bytearray(frame.image_bytes)
    bgr[0::3], bgr[2::3] = frame.image_bytes[2::3], frame.image_bytes[0::3]
    if hashlib.sha256(bgr).hexdigest() != provenance["payload_sha256"]:
        raise OnboardMultimodalError("actual RGB does not reconstruct original BGR payload")
    wire = reconstruct_gazebo_sim_wire_frame(
        unsigned_header_hex=provenance["wire_unsigned_header_hex"],
        payload_bgr24=bgr,
        expected_sequence=provenance["sequence"],
        expected_width=frame.width,
        expected_height=frame.height,
        previous_sensor_stamp_ns=previous_stamp,
    )
    for name in (
        "record_sha256",
        "sensor_stamp_ns",
        "camera_info_stamp_ns",
        "navsat_stamp_ns",
        "odometry_stamp_ns",
        "flags",
    ):
        if provenance[name] != getattr(wire, name):
            raise OnboardMultimodalError("source provenance differs from original wire: " + name)
        if name.endswith("_ns"):
            require_int(provenance[name], name, minimum=1)
    for source in ("camera_info", "navsat", "odometry"):
        skew = provenance[source + "_source_skew_ns"]
        require_int(skew, source + " source skew", minimum=0)
        if skew != wire.sensor_stamp_ns - getattr(wire, source + "_stamp_ns"):
            raise OnboardMultimodalError("source skew is not the original timestamp difference")
    telemetry = strict_object(
        provenance["wire_telemetry"],
        name="wire telemetry",
        required=frozenset(item.name for item in fields(GazeboSimWireTelemetryProvenance)),
    )
    for name, value in telemetry.items():
        if name == "attitude_covariance_enu_rad2":
            if not isinstance(value, list) or len(value) != 9:
                raise OnboardMultimodalError("wire covariance requires nine JSON numbers")
            for item in value:
                require_real(item, "wire covariance element")
        else:
            require_real(value, "wire " + name)
    if telemetry != GazeboSimWireTelemetryProvenance.from_wire(wire).to_mapping():
        raise OnboardMultimodalError("GPS/odometry facts differ from byte-bound original wire")
    if "frame_telemetry" in provenance:
        derived = strict_object(
            provenance["frame_telemetry"],
            name="derived frame telemetry",
            required=frozenset(item.name for item in fields(GazeboSimFrameTelemetryProvenance)),
        )
        for name, value in derived.items():
            if name in {"altitude_reference", "attitude_uncertainty_source"}:
                if value is not None:
                    require_text(value, name)
            elif name in {"gps_fix_type", "satellites_visible"}:
                require_int(value, name, minimum=0, maximum=8 if name == "gps_fix_type" else 254)
            elif value is None and name in {"altitude_agl_m", "altitude_observed_at_s"}:
                continue
            else:
                require_real(value, "derived " + name)
        # Reuse the existing ENU/body-FLU conversion rather than introducing a
        # second orientation/velocity convention. Quality/floors/DEM stay out
        # of messages: they cannot be authenticated from this RGB wire alone.
        provider = GazeboSimTelemetryProvider()
        provider.update(wire, observed_at_s=frame.captured_at_s)
        baseline = GazeboSimFrameTelemetryProvenance.from_telemetry(
            provider.snapshot(now_s=frame.captured_at_s)
        ).to_mapping()
        # These values can be checked without DEM or configured quality floors.
        expected = {
            "latitude_deg": wire.latitude_deg,
            "longitude_deg": wire.longitude_deg,
            "gps_absolute_altitude_m": wire.altitude_m,
            "local_north_m": wire.position_north_m,
            "local_east_m": wire.position_east_m,
            "local_down_m": -wire.position_up_m,
            "gps_observed_at_s": frame.captured_at_s - provenance["navsat_source_skew_ns"] / 1e9,
            "attitude_observed_at_s": frame.captured_at_s
            - provenance["odometry_source_skew_ns"] / 1e9,
            "velocity_observed_at_s": frame.captured_at_s
            - provenance["odometry_source_skew_ns"] / 1e9,
            **{
                name: baseline[name]
                for name in (
                    "roll_deg",
                    "pitch_deg",
                    "heading_deg",
                    "velocity_north_mps",
                    "velocity_east_mps",
                    "attitude_uncertainty_source",
                )
            },
        }
        if any(derived[key] != value for key, value in expected.items()):
            raise OnboardMultimodalError("derived telemetry source or host-domain time changed")
    return fact


@dataclass(frozen=True, slots=True)
class OnboardLiveMultimodalInput(OnboardMultimodalInput):
    factual_sensor_state_json: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        OnboardMultimodalInput.__post_init__(self)
        self.validate_sensor_binding()

    def validate_sensor_binding(self) -> None:
        if not isinstance(self.factual_sensor_state_json, tuple) or len(
            self.factual_sensor_state_json
        ) != len(self.frames):
            raise OnboardMultimodalError(
                "exactly one immutable factual envelope per supplied frame is required"
            )
        previous_stamp = previous_sequence = previous_clock = None
        for frame, text in zip(self.frames, self.factual_sensor_state_json, strict=True):
            provenance = _frame_facts(frame, text, previous_stamp)["source_provenance"]
            clock = (provenance["captured_at_clock"], provenance.get("host_clock_identity"))
            if previous_clock is not None and clock != previous_clock:
                raise OnboardMultimodalError("sensor history crosses host clock identity")
            if previous_sequence is not None and provenance["sequence"] <= previous_sequence:
                raise OnboardMultimodalError("sensor sequence repeated or regressed")
            previous_stamp, previous_sequence, previous_clock = (
                provenance["sensor_stamp_ns"],
                provenance["sequence"],
                clock,
            )

    def to_mapping(self) -> dict[str, object]:
        result = OnboardMultimodalInput.to_mapping(self)
        result.update(
            {
                "contract": LIVE_INPUT_CONTRACT,
                "factual_sensor_state": [
                    strict_json_loads(text) for text in self.factual_sensor_state_json
                ],
                "partial_navigation_context_only": True,
                "source_authenticity_verified": False,
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class OnboardLiveVlmRequest(OnboardVlmRuntimeRequest):
    def __post_init__(self) -> None:
        if not isinstance(self.model_input, OnboardLiveMultimodalInput):
            raise TypeError("live request requires OnboardLiveMultimodalInput")
        OnboardVlmRuntimeRequest.__post_init__(self)

    def to_mapping(self) -> dict[str, object]:
        result = OnboardVlmRuntimeRequest.to_mapping(self)
        result.update(
            {
                "contract": LIVE_REQUEST_CONTRACT,
                "factual_sensor_state": self.model_input.to_mapping()["factual_sensor_state"],
            }
        )
        return result

    def validate_binding(self) -> None:
        OnboardVlmRuntimeRequest.validate_binding(self)
        self.model_input.validate_sensor_binding()


def serialize_onboard_live_vlm_messages(request: OnboardLiveVlmRequest) -> list[dict]:
    """Reuse actual RGB/verbatim Prompt and add only validated partial observations."""
    if not isinstance(request, OnboardLiveVlmRequest):
        raise TypeError("live serializer requires OnboardLiveVlmRequest")
    messages = serialize_onboard_vlm_messages(request)
    partial = []
    for text in request.model_input.factual_sensor_state_json:
        fact = strict_json_loads(text)
        provenance = fact["source_provenance"]
        partial.append(
            {
                "frame_id": fact["frame_id"],
                "host_received_at_s": provenance["captured_at_s"],
                "host_clock_domain": provenance["captured_at_clock"],
                "caller_clock_epoch": fact["clock_epoch"],
                "source_clock": "gazebo_sim_sensor_time_ns_not_utc",
                "source_timestamps_ns": {
                    key: provenance[key]
                    for key in (
                        "sensor_stamp_ns",
                        "camera_info_stamp_ns",
                        "navsat_stamp_ns",
                        "odometry_stamp_ns",
                    )
                },
                "measured_wire_gps_and_odometry": provenance["wire_telemetry"],
            }
        )
    metadata = {
        "contract": LIVE_INPUT_CONTRACT,
        "partial_factual_sensor_observations": partial,
        "navigation_safety_context": dict.fromkeys(_UNKNOWN),
        "source_authenticity": "collecting_host_responsibility_not_proven_by_hash",
        "retained_derived_quality_dem_and_depth_not_admitted_as_measurements": True,
        "position_up_is_not_agl_or_relative_home_altitude": True,
        "execution_authority": False,
    }
    messages[-1]["content"].append({"type": "text", "text": canonical_json(metadata)})
    return messages


__all__ = [
    "LIVE_INPUT_CONTRACT",
    "LIVE_REQUEST_CONTRACT",
    "OnboardLiveMultimodalInput",
    "OnboardLiveVlmRequest",
    "gazebo_sensor_fact_json",
    "serialize_onboard_live_vlm_messages",
]
