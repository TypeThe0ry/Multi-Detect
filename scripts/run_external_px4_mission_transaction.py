#!/usr/bin/env python3
"""Run the shared Mission transaction inside an owned Docker PX4 runtime.

This is an explicit bridge between ``run_simulator_docker_acceptance`` and
``ExternalPx4MissionExecutionAdapter``.  It consumes a hash-bound
``PlannedMissionCompilation`` produced before the runtime starts; it does not
select a task, create a second policy, arm, change mode, or start flight.
Without ``--execute`` the command is only the normal Docker preflight.  With
``--execute`` it opens a MAVLink connection only after ownership and
heartbeat/GPS qualification, performs upload/readback/commit/permit, and then
lets the parent acceptance helper stop and remove the disposable container.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy import ContextSnapshot, normalize_vehicle_class
from multidetect.autonomy.mission_adapter import build_external_px4_adapter
from multidetect.autonomy.serialization import strict_json_loads
from multidetect.geolocation_accuracy import wgs84_distance_m
from multidetect.mission_planning import (
    PlannedMissionCompilation,
    parse_mission_compile_runtime,
)
from multidetect.px4_external_mission import (
    ExternalPx4MissionError,
    attest_owned_px4_container,
)

try:
    from scripts.run_simulator_docker_acceptance import (
        DEFAULT_CONTROL_PORT,
        DEFAULT_EVIDENCE,
        DEFAULT_SCENARIO,
        DEFAULT_TELEMETRY_PORT,
        PINNED_IMAGE,
        run_acceptance,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from run_simulator_docker_acceptance import (  # type: ignore[no-redef]
        DEFAULT_CONTROL_PORT,
        DEFAULT_EVIDENCE,
        DEFAULT_SCENARIO,
        DEFAULT_TELEMETRY_PORT,
        PINNED_IMAGE,
        run_acceptance,
    )


class ExternalMissionCampaignError(RuntimeError):
    """Raised when the external Mission bridge cannot prove its inputs."""


def _verify_live_gps_context(
    telemetry: Mapping[str, object], context: ContextSnapshot
) -> dict[str, object]:
    """Reject a precompiled Mission whose origin is not the observed PX4 GPS."""

    gps = telemetry.get("gps")
    if not isinstance(gps, Mapping) or telemetry.get("qualified") is not True:
        raise ExternalMissionCampaignError("qualified live GPS is required")
    try:
        fix_type = int(gps["fix_type"])
        latitude_deg = int(gps["lat"]) / 10_000_000.0
        longitude_deg = int(gps["lon"]) / 10_000_000.0
        distance_m = wgs84_distance_m(
            latitude1_deg=context.aircraft_position.latitude_deg,
            longitude1_deg=context.aircraft_position.longitude_deg,
            latitude2_deg=latitude_deg,
            longitude2_deg=longitude_deg,
        )
        home_distance_m = wgs84_distance_m(
            latitude1_deg=context.home_position.latitude_deg,
            longitude1_deg=context.home_position.longitude_deg,
            latitude2_deg=latitude_deg,
            longitude2_deg=longitude_deg,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalMissionCampaignError("live GPS position is invalid") from exc
    tolerance_m = max(5.0, 3.0 * context.gps_horizontal_accuracy_m)
    if fix_type < 3 or context.gps_fix_type < 3:
        raise ExternalMissionCampaignError("live/context GPS fix is insufficient")
    if distance_m > tolerance_m or home_distance_m > tolerance_m:
        raise ExternalMissionCampaignError(
            "precompiled context does not match live PX4 GPS: "
            f"aircraft_delta_m={distance_m:.3f} home_delta_m={home_distance_m:.3f} "
            f"tolerance_m={tolerance_m:.3f}"
        )
    return {
        "qualified": True,
        "live_fix_type": fix_type,
        "aircraft_delta_m": round(distance_m, 6),
        "home_delta_m": round(home_distance_m, 6),
        "tolerance_m": tolerance_m,
        "source": "PX4_GPS_RAW_INT",
    }


def _load(path: str | Path, *, label: str) -> object:
    source = Path(path).expanduser().resolve()
    try:
        return strict_json_loads(source.read_bytes(), name=label)
    except (OSError, TypeError, ValueError) as exc:
        raise ExternalMissionCampaignError(f"cannot load {label}: {source}") from exc


def build_mission_transaction_runner(
    *,
    compiled_path: str | Path,
    context_path: str | Path,
    runtime_path: str | Path,
    vehicle_class: str,
    journal_directory: str | Path,
    transaction_id: str,
    mavutil_module: object | None = None,
) -> Any:
    """Prepare a strict acceptance hook around one compiled Mission artifact."""

    normalized = normalize_vehicle_class(vehicle_class)
    context_raw = _load(context_path, label="ContextSnapshot")
    context = ContextSnapshot.from_mapping(context_raw)
    compilation_raw = _load(compiled_path, label="PlannedMissionCompilation")
    compilation = PlannedMissionCompilation.from_mapping(compilation_raw)
    if compilation.vehicle_class != normalized:
        raise ExternalMissionCampaignError("compiled Mission vehicle class does not match")
    runtime_raw = _load(runtime_path, label="MissionCompileRuntime")
    _geometry, policy = parse_mission_compile_runtime(runtime_raw, normalized)
    journal_root = Path(journal_directory).expanduser().resolve()
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise ExternalMissionCampaignError("transaction_id must be non-empty")
    if any(token in transaction_id for token in ("/", "\\", "..")):
        raise ExternalMissionCampaignError("transaction_id contains an unsafe path token")
    if mavutil_module is None:
        try:
            from pymavlink import mavutil as mavutil_module  # type: ignore[assignment]
        except ImportError as exc:  # pragma: no cover - host dependency
            raise ExternalMissionCampaignError(
                "pymavlink is required for --execute Mission transport"
            ) from exc
    connector = getattr(mavutil_module, "mavlink_connection", None)
    if not callable(connector):
        raise ExternalMissionCampaignError("MAVLink module has no mavlink_connection")

    def run_hook(**kwargs: object) -> Mapping[str, object]:
        plan = kwargs.get("plan")
        inspect_record = kwargs.get("inspect_record")
        telemetry = kwargs.get("telemetry")
        telemetry_endpoint = kwargs.get("telemetry_endpoint")
        if not hasattr(plan, "vehicle_model") or not hasattr(plan, "image"):
            raise ExternalMissionCampaignError("acceptance hook did not provide a Docker plan")
        if not isinstance(inspect_record, Mapping):
            raise ExternalMissionCampaignError("acceptance hook did not provide inspect data")
        if not isinstance(telemetry, Mapping) or telemetry.get("qualified") is not True:
            raise ExternalMissionCampaignError("Mission transport requires qualified telemetry")
        gps_binding = _verify_live_gps_context(telemetry, context)
        container_id = inspect_record.get("Id")
        if not isinstance(container_id, str):
            raise ExternalMissionCampaignError("Docker inspect record has no container Id")
        try:
            attestation = attest_owned_px4_container(
                inspect_record,
                expected_container_id=container_id,
                expected_image=str(plan.image),
                vehicle_class=normalized,
                scenario_id=str(plan.scenario_id),
                scenario_manifest_sha256=str(plan.scenario_manifest_hash),
                expected_model=str(dict(plan.environment).get("PX4_SIM_MODEL", "")),
                expected_purpose_label="simulator-docker",
                control_port=int(plan.host_control_port),
                telemetry_qualified=True,
                expected_readonly_mounts={
                    "/opt/multidetect-gazebo-models": str(plan.model_root),
                    "/opt/px4-gazebo/share/gz/worlds/default.sdf": str(plan.world_path),
                    "/workspace/scripts": str(plan.scripts_root),
                },
            )
        except (ExternalPx4MissionError, TypeError, ValueError) as exc:
            raise ExternalMissionCampaignError("Docker ownership attestation failed") from exc
        if context.airframe_profile_id != str(plan.airframe_profile_id):
            raise ExternalMissionCampaignError("context and Docker airframe profiles differ")
        if not isinstance(telemetry_endpoint, str) or not telemetry_endpoint:
            raise ExternalMissionCampaignError(
                "acceptance hook did not provide telemetry MAVLink endpoint"
            )
        connection = connector(
            telemetry_endpoint,
            source_system=250,
            source_component=191,
            autoreconnect=False,
        )
        journal_path = journal_root / f"{transaction_id}.jsonl"
        try:
            adapter = build_external_px4_adapter(
                normalized,
                connection,
                airframe_profile_id=context.airframe_profile_id,
                runtime_attestation=attestation,
            )
            result = adapter.execute(
                compilation,
                context=context,
                policy=policy,
                journal_path=journal_path,
                now_utc=datetime.now(timezone.utc),
                transaction_id=transaction_id,
            )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        return {
            **result,
            "live_gps_context_binding": gps_binding,
            "external_runtime_attested": True,
            "flight_started": False,
            "physical_output_enabled": False,
        }

    return run_hook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", "--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--container-name", default="multidetect-external-mission")
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--image", default=PINNED_IMAGE)
    parser.add_argument("--compiled-mission", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--vehicle-class", required=True)
    parser.add_argument("--journal-directory", type=Path, default=Path("artifacts/evaluation"))
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        hook = None
        if args.execute:
            required = {
                "--compiled-mission": args.compiled_mission,
                "--context": args.context,
                "--runtime": args.runtime,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ExternalMissionCampaignError(
                    "--execute requires " + ", ".join(missing)
                )
            hook = build_mission_transaction_runner(
                compiled_path=args.compiled_mission,
                context_path=args.context,
                runtime_path=args.runtime,
                vehicle_class=args.vehicle_class,
                journal_directory=args.journal_directory,
                transaction_id=args.transaction_id,
            )
        payload = run_acceptance(
            args.scenario_manifest,
            args.evidence,
            execute=args.execute,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            image=args.image,
            mission_transaction_runner=hook,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("gate_passed") is True else 2
    except (ExternalMissionCampaignError, OSError, TypeError, ValueError) as exc:
        print(f"external-px4-mission: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ExternalMissionCampaignError",
    "build_mission_transaction_runner",
    "build_parser",
    "main",
]
