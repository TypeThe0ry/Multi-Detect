"""Optional signed target-geolocation projection over the existing TUNNEL protocol."""

from __future__ import annotations

import math
import os
import socket
import time
from collections.abc import Mapping, Sequence
from ipaddress import ip_address

from .inputs import current_frame, is_private_address

_SIGNING_EPOCH_UNIX_S = 1_420_070_400


def _sigma_from_covariance(
    covariance: Mapping[str, object],
) -> float | None:
    north = covariance.get("north_m2", covariance.get("nn_m2"))
    east = covariance.get("east_m2", covariance.get("ee_m2"))
    cross = covariance.get("north_east_m2", covariance.get("ne_m2", 0.0))
    matrix = covariance.get("matrix_m2", covariance.get("cov"))
    if (
        isinstance(matrix, Sequence)
        and len(matrix) == 2
        and all(isinstance(row, Sequence) for row in matrix)
    ):
        try:
            north = matrix[0][0]
            cross = matrix[0][1]
            east_cross = matrix[1][0]
            east = matrix[1][1]
            if isinstance(east_cross, (int, float)) and math.isfinite(east_cross):
                cross = (float(cross) + float(east_cross)) / 2.0
        except (IndexError, TypeError):
            pass
    valid = all(
        isinstance(value, (int, float)) and math.isfinite(value)
        for value in (north, east, cross)
    )
    if not valid:
        return None
    if float(north) < 0 or float(east) < 0:
        return None
    largest_eigenvalue = 0.5 * (
        float(north)
        + float(east)
        + math.sqrt((float(north) - float(east)) ** 2 + 4.0 * float(cross) ** 2)
    )
    return math.sqrt(largest_eigenvalue)


class SignedTunnelPublisher:
    """Maps only GPS-qualified locations to existing signed MAVLink TUNNEL type 22."""

    def __init__(self, config: Mapping[str, object]) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.reason = "disabled"
        self._sequence = 0
        self._socket: socket.socket | None = None
        self._adapter: object | None = None
        self._message_type: object | None = None
        self._host = str(config.get("host", ""))
        self._port = int(config.get("port", 14550))
        self._fix_type_min = int(config.get("min_gps_fix_type", 3))
        if not self.enabled:
            return
        try:
            private_ipv4 = ip_address(self._host).version == 4 and is_private_address(self._host)
        except ValueError:
            private_ipv4 = False
        if not private_ipv4:
            self.reason = "private_numeric_host_required"
            return
        key_hex = os.environ.get("POLARIS_SIM_MAVLINK_SIGNING_KEY", "")
        try:
            signing_key = bytes.fromhex(key_hex)
        except ValueError:
            self.reason = "invalid_signing_key"
            return
        if len(signing_key) != 32:
            self.reason = "signing_key_not_configured"
            return
        try:
            from multidetect.operator_link import TargetGeolocationStatusMessage, VideoGeometry
            from multidetect.operator_mavlink import (
                OperatorMavlinkEndpoint,
                OperatorMavlinkTunnelAdapter,
            )
            from multidetect.operator_protocol import OperatorTunnelCodec

            endpoint = OperatorMavlinkEndpoint(
                local_system_id=int(config["local_system_id"]),
                local_component_id=int(config["local_component_id"]),
                remote_system_id=int(config["remote_system_id"]),
                remote_component_id=int(config["remote_component_id"]),
            )
            hmac_hex = os.environ.get("POLARIS_SIM_OPERATOR_HMAC_KEY", "")
            hmac_key = bytes.fromhex(hmac_hex)
            if len(hmac_key) < 32:
                raise ValueError("operator HMAC key must contain at least 32 bytes")
            geometry = VideoGeometry(
                stream_id=str(config.get("stream_id", "polaris-sim")),
                width=int(config.get("video_width", 1280)),
                height=int(config.get("video_height", 720)),
            )
            codec = OperatorTunnelCodec(hmac_key=hmac_key, geometries=(geometry,))
            signing_epoch = max(1, int((time.time() - _SIGNING_EPOCH_UNIX_S) * 100_000))
            self._adapter = OperatorMavlinkTunnelAdapter(
                codec,
                endpoint,
                signing_key=signing_key,
                signing_link_id=int(config.get("signing_link_id", 0)),
                initial_signing_timestamp=signing_epoch,
            )
            self._message_type = TargetGeolocationStatusMessage
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.reason = "ready"
        except (ImportError, KeyError, TypeError, ValueError) as exc:
            self.reason = f"unavailable:{exc.__class__.__name__}"

    @property
    def active(self) -> bool:
        return self.enabled and self._adapter is not None and self._socket is not None

    def publish(self, destination: str, query: object, tracks: Sequence[object]) -> None:
        if destination != "polaris" or not self.active:
            return
        frame = current_frame()
        if (
            frame is None
            or not frame.telemetry.get("fresh", False)
            or not frame.telemetry.get("gps_fresh", False)
            or int(frame.telemetry.get("gps_fix_type", 0)) < self._fix_type_min
        ):
            return
        now = time.monotonic()
        for track in tracks:
            location = getattr(track, "location", None)
            if location is None or getattr(location, "validity", "") != "available":
                continue
            if "gps_qualified" not in getattr(location, "provenance", ()):
                continue
            values = getattr(location, "values", {})
            latitude = values.get("latitude_deg", values.get("lat"))
            longitude = values.get("longitude_deg", values.get("lon"))
            if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
                continue
            sigma = _sigma_from_covariance(
                getattr(location, "covariance", {}),
            )
            if sigma is None:
                continue
            try:
                self._sequence = (self._sequence + 1) & 0xFFFFFFFF
                message = self._message_type(
                    sequence=self._sequence,
                    target_id=str(getattr(track, "track_id", "")),
                    source_frame_id=frame.frame_id,
                    source_captured_at_s=frame.captured_at_s,
                    produced_at_s=now,
                    available=True,
                    reason="gps_qualified",
                    latitude_deg=float(latitude),
                    longitude_deg=float(longitude),
                    horizontal_sigma_m=sigma,
                )
                datagram = self._adapter.encode_target_geolocation_status(message)
                self._socket.sendto(datagram, (self._host, self._port))
            except (OSError, TypeError, ValueError):
                continue

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()

