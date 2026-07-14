from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from pymavlink import mavutil

EXPECTED_CONTAINER_NAME = "multidetect-px4-auto-mission-acceptance"
EXPECTED_PURPOSE_LABEL = "px4-sitl-auto-mission-acceptance"
DATALINK_CONTAINER_NAME = "multidetect-px4-datalink-loss-acceptance"
DATALINK_PURPOSE_LABEL = "px4-sitl-datalink-loss-acceptance"
DATALINK_GCS_INPUT_PORT = 18570
PINNED_IMAGE_REFERENCE = (
    "px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216"
)
SITL_HOST_PORT = 14652
PROTECTED_GROUND_STATION_PORT = 14550
SITL_ENDPOINT = f"udpin:0.0.0.0:{SITL_HOST_PORT}"
SOURCE_SYSTEM_ID = 250
SOURCE_COMPONENT_ID = 190
_ALLOWED_DISARMED_SYSTEM_STATUSES = frozenset(
    {
        mavutil.mavlink.MAV_STATE_STANDBY,
        mavutil.mavlink.MAV_STATE_ACTIVE,
    }
)


class SitlMissionUploadError(RuntimeError):
    """Raised when the disposable SITL mission boundary cannot be proven."""


@dataclass(frozen=True, slots=True)
class MissionItem:
    sequence: int
    command: int
    command_name: str
    param1: float
    param2: float
    param3: float
    param4: float
    latitude_e7: int
    longitude_e7: int
    relative_altitude_m: float


@dataclass(frozen=True, slots=True)
class MissionUploadResult:
    acknowledged: bool
    acknowledgement_code: int
    acknowledgement_name: str
    request_sequences: tuple[int, ...]
    request_message_types: tuple[str, ...]
    transmitted_message_count: int
    retry_count: int


def validate_disarmed_upload_heartbeat(heartbeat: Any) -> dict[str, object]:
    """Require an explicitly disarmed, operational SITL heartbeat."""

    base_mode = int(heartbeat.base_mode)
    system_status = int(heartbeat.system_status)
    armed = bool(base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
    if armed:
        raise SitlMissionUploadError("SITL mission upload refuses an armed vehicle")
    if system_status not in _ALLOWED_DISARMED_SYSTEM_STATUSES:
        state = mavutil.mavlink.enums["MAV_STATE"].get(system_status)
        state_name = state.name if state is not None else f"MAV_STATE_{system_status}"
        raise SitlMissionUploadError(
            f"SITL mission upload requires a disarmed Standby/Active state; observed {state_name}"
        )
    state_name = mavutil.mavlink.enums["MAV_STATE"][system_status].name
    return {
        "armed": False,
        "base_mode": base_mode,
        "system_status_id": system_status,
        "system_status": state_name,
    }


def build_hil_patrol_mission(
    *, home_latitude_e7: int, home_longitude_e7: int
) -> tuple[MissionItem, ...]:
    """Return a deliberately low-altitude lifecycle-only fixed-wing mission."""

    return (
        MissionItem(
            sequence=0,
            command=mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            command_name="MAV_CMD_NAV_TAKEOFF",
            param1=10.0,
            param2=0.0,
            param3=0.0,
            param4=math.nan,
            latitude_e7=home_latitude_e7 + 7_200,
            longitude_e7=home_longitude_e7,
            relative_altitude_m=3.0,
        ),
        MissionItem(
            sequence=1,
            command=mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
            command_name="MAV_CMD_NAV_WAYPOINT",
            param1=0.0,
            param2=25.0,
            param3=0.0,
            param4=math.nan,
            latitude_e7=home_latitude_e7 + 10_800,
            longitude_e7=home_longitude_e7,
            relative_altitude_m=5.0,
        ),
        MissionItem(
            sequence=2,
            command=mavutil.mavlink.MAV_CMD_NAV_LOITER_TIME,
            command_name="MAV_CMD_NAV_LOITER_TIME",
            param1=60.0,
            param2=0.0,
            param3=80.0,
            param4=math.nan,
            latitude_e7=home_latitude_e7 + 10_800,
            longitude_e7=home_longitude_e7 + 13_200,
            relative_altitude_m=5.0,
        ),
    )


def inspect_owned_disposable_container(
    *,
    container_name: str,
    expected_container_id: str,
    ownership_profile: str = "auto_mission",
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    profiles = {
        "auto_mission": {
            "container_name": EXPECTED_CONTAINER_NAME,
            "purpose_label": EXPECTED_PURPOSE_LABEL,
            "port_bindings": {},
        },
        "datalink_loss": {
            "container_name": DATALINK_CONTAINER_NAME,
            "purpose_label": DATALINK_PURPOSE_LABEL,
            "port_bindings": {
                f"{DATALINK_GCS_INPUT_PORT}/udp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": str(DATALINK_GCS_INPUT_PORT),
                    }
                ]
            },
        },
    }
    profile = profiles.get(ownership_profile)
    if profile is None:
        raise SitlMissionUploadError("unsupported disposable SITL ownership profile")
    if container_name != profile["container_name"]:
        raise SitlMissionUploadError("the fixed disposable SITL container name is required")
    if not expected_container_id or len(expected_container_id) < 12:
        raise SitlMissionUploadError("a full or unambiguous owned container ID is required")

    result = run_command(
        ["docker", "container", "inspect", container_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise SitlMissionUploadError("the owned disposable SITL container is unavailable")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SitlMissionUploadError("docker inspect did not return valid JSON") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise SitlMissionUploadError("docker inspect returned an unexpected container set")

    record = records[0]
    actual_id = str(record.get("Id", ""))
    if not actual_id.startswith(expected_container_id) and not expected_container_id.startswith(
        actual_id
    ):
        raise SitlMissionUploadError("container identity changed before mission upload")
    config = record.get("Config") or {}
    host_config = record.get("HostConfig") or {}
    state = record.get("State") or {}
    labels = config.get("Labels") or {}
    environment = config.get("Env") or []
    command = " ".join(str(item) for item in (config.get("Cmd") or []))

    actual_port_bindings = host_config.get("PortBindings") or {}
    checks = {
        "running": state.get("Running") is True,
        "pinned_image": config.get("Image") == PINNED_IMAGE_REFERENCE,
        "purpose_label": labels.get("multidetect.purpose") == profile["purpose_label"],
        "sih_fixed_wing_model": "PX4_SIM_MODEL=sihsim_airplane" in environment,
        "isolated_destination_port": f"-o {SITL_HOST_PORT}" in command,
        "protected_port_not_targeted": f"-o {PROTECTED_GROUND_STATION_PORT}" not in command,
        "bridge_network": host_config.get("NetworkMode") == "bridge",
        "not_privileged": host_config.get("Privileged") is False,
        "no_device_mapping": not host_config.get("Devices"),
        "exact_host_port_boundary": actual_port_bindings == profile["port_bindings"],
        "no_host_mounts": not record.get("Mounts"),
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    if failed:
        raise SitlMissionUploadError(
            "disposable SITL ownership/isolation checks failed: " + ", ".join(failed)
        )
    return {
        "container_id": actual_id,
        "container_name": container_name,
        "ownership_profile": ownership_profile,
        "checks": checks,
    }


def _send_item(
    connection: Any,
    *,
    target_system: int,
    target_component: int,
    item: MissionItem,
    request_type: str,
) -> None:
    if request_type == "MISSION_REQUEST_INT":
        connection.mav.mission_item_int_send(
            target_system,
            target_component,
            item.sequence,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            item.command,
            1 if item.sequence == 0 else 0,
            1,
            item.param1,
            item.param2,
            item.param3,
            item.param4,
            item.latitude_e7,
            item.longitude_e7,
            item.relative_altitude_m,
        )
        return
    if request_type == "MISSION_REQUEST":
        connection.mav.mission_item_send(
            target_system,
            target_component,
            item.sequence,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
            item.command,
            1 if item.sequence == 0 else 0,
            1,
            item.param1,
            item.param2,
            item.param3,
            item.param4,
            item.latitude_e7 / 10_000_000.0,
            item.longitude_e7 / 10_000_000.0,
            item.relative_altitude_m,
        )
        return
    raise SitlMissionUploadError(f"unsupported mission request type: {request_type}")


def upload_mission(
    connection: Any,
    *,
    target_system: int,
    target_component: int,
    items: Sequence[MissionItem],
    message_timeout_s: float = 1.5,
    maximum_retries: int = 5,
    maximum_duration_s: float = 20.0,
) -> MissionUploadResult:
    if not items:
        raise SitlMissionUploadError("the SITL patrol mission cannot be empty")
    expected_sequences = tuple(range(len(items)))
    actual_sequences = tuple(item.sequence for item in items)
    if actual_sequences != expected_sequences:
        raise SitlMissionUploadError("mission item sequences must be contiguous and zero-based")

    connection.mav.mission_count_send(target_system, target_component, len(items))
    transmitted_count = 1
    retry_count = 0
    request_sequences: list[int] = []
    request_types: list[str] = []
    last_request: tuple[str, int] | None = None
    deadline = time.monotonic() + maximum_duration_s

    while time.monotonic() < deadline:
        message = connection.recv_match(
            type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"],
            blocking=True,
            timeout=message_timeout_s,
        )
        if message is None:
            retry_count += 1
            if retry_count > maximum_retries:
                raise SitlMissionUploadError("mission upload timed out after bounded retries")
            if last_request is None:
                connection.mav.mission_count_send(target_system, target_component, len(items))
            else:
                request_type, sequence = last_request
                _send_item(
                    connection,
                    target_system=target_system,
                    target_component=target_component,
                    item=items[sequence],
                    request_type=request_type,
                )
            transmitted_count += 1
            continue

        message_type = str(message.get_type())
        if message_type == "MISSION_ACK":
            acknowledgement = int(message.type)
            result_name = mavutil.mavlink.enums["MAV_MISSION_RESULT"][acknowledgement].name
            accepted = acknowledgement == mavutil.mavlink.MAV_MISSION_ACCEPTED
            if not accepted:
                raise SitlMissionUploadError(
                    f"PX4 rejected the SITL mission: {result_name} ({acknowledgement})"
                )
            return MissionUploadResult(
                acknowledged=True,
                acknowledgement_code=acknowledgement,
                acknowledgement_name=result_name,
                request_sequences=tuple(request_sequences),
                request_message_types=tuple(request_types),
                transmitted_message_count=transmitted_count,
                retry_count=retry_count,
            )

        sequence = int(message.seq)
        if sequence not in expected_sequences:
            raise SitlMissionUploadError(f"PX4 requested invalid mission sequence {sequence}")
        request_sequences.append(sequence)
        request_types.append(message_type)
        last_request = (message_type, sequence)
        _send_item(
            connection,
            target_system=target_system,
            target_component=target_component,
            item=items[sequence],
            request_type=message_type,
        )
        transmitted_count += 1

    raise SitlMissionUploadError("mission upload exceeded its total duration limit")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Upload one fixed lifecycle-only mission to the exact owned disposable PX4 SITL "
            "container; this helper cannot arm, change mode, set parameters or address hardware."
        )
    )
    parser.add_argument("--container-name", default=EXPECTED_CONTAINER_NAME)
    parser.add_argument("--container-id", required=True)
    parser.add_argument(
        "--ownership-profile",
        choices=("auto_mission", "datalink_loss"),
        default="auto_mission",
    )
    parser.add_argument("--acknowledge-owned-disposable-sitl", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.acknowledge_owned_disposable_sitl:
        raise SitlMissionUploadError(
            "--acknowledge-owned-disposable-sitl is required before any network socket is opened"
        )
    container = inspect_owned_disposable_container(
        container_name=args.container_name,
        expected_container_id=args.container_id,
        ownership_profile=args.ownership_profile,
    )

    connection = mavutil.mavlink_connection(
        SITL_ENDPOINT,
        source_system=SOURCE_SYSTEM_ID,
        source_component=SOURCE_COMPONENT_ID,
        autoreconnect=False,
    )
    try:
        heartbeat = connection.wait_heartbeat(timeout=10)
        if heartbeat is None:
            raise SitlMissionUploadError("no heartbeat arrived from the isolated SITL port")
        if int(heartbeat.get_srcSystem()) != 1 or int(heartbeat.get_srcComponent()) != 1:
            raise SitlMissionUploadError("unexpected MAVLink source identity on the SITL port")
        if int(heartbeat.autopilot) != mavutil.mavlink.MAV_AUTOPILOT_PX4:
            raise SitlMissionUploadError("isolated sender is not PX4")
        if int(heartbeat.type) != mavutil.mavlink.MAV_TYPE_FIXED_WING:
            raise SitlMissionUploadError("isolated sender is not a fixed-wing vehicle")
        upload_state = validate_disarmed_upload_heartbeat(heartbeat)

        position = connection.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
        if position is None or int(position.get_srcSystem()) != 1:
            raise SitlMissionUploadError("no qualified global position arrived from SITL")
        home_latitude_e7 = int(position.lat)
        home_longitude_e7 = int(position.lon)
        mission = build_hil_patrol_mission(
            home_latitude_e7=home_latitude_e7,
            home_longitude_e7=home_longitude_e7,
        )
        result = upload_mission(
            connection,
            target_system=1,
            target_component=1,
            items=mission,
        )
    finally:
        connection.close()

    payload = {
        "schema_version": 1,
        "event": "px4_sitl_mission_upload_finished",
        "simulation_only": True,
        "container": container,
        "endpoint": SITL_ENDPOINT,
        "protected_ground_station_port": PROTECTED_GROUND_STATION_PORT,
        "heartbeat_identity": {
            "system_id": int(heartbeat.get_srcSystem()),
            "component_id": int(heartbeat.get_srcComponent()),
            "autopilot": "MAV_AUTOPILOT_PX4",
            "vehicle_type": "MAV_TYPE_FIXED_WING",
            **upload_state,
        },
        "home": {
            "latitude_e7": home_latitude_e7,
            "longitude_e7": home_longitude_e7,
        },
        "mission": [
            {
                **asdict(item),
                "param4": None if math.isnan(item.param4) else item.param4,
            }
            for item in mission
        ],
        "protocol": asdict(result),
        "allowed_transmitted_message_types": [
            "MISSION_COUNT",
            "MISSION_ITEM_INT",
            "MISSION_ITEM",
        ],
        "simulator_mission_control_enabled": True,
        "arming_supported": False,
        "mode_change_supported": False,
        "parameter_access_supported": False,
        "actuator_control_supported": False,
        "payload_control_supported": False,
        "real_v6x_contacted": False,
        "real_hardware_control_enabled": False,
    }
    print(json.dumps(payload, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "event": "px4_sitl_mission_upload_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "simulation_only": True,
                    "real_v6x_contacted": False,
                    "real_hardware_control_enabled": False,
                },
                separators=(",", ":"),
            )
        )
        raise SystemExit(1) from None
