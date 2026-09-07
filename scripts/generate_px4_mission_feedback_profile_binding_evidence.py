"""Generate deterministic v185 profile-policy to PX4 feedback evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    source_root = Path(__file__).resolve().parents[1] / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.compat import UTC
from multidetect.pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.px4_mission import (
    PX4_FIXED_WING_ADAPTER_HASH,
    PX4_FIXED_WING_ADAPTER_ID,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
)
from multidetect.px4_mission_feedback import Px4MissionFeedbackConfig, Px4MissionFeedbackTracker
from multidetect.px4_mission_feedback_binding import (
    PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS,
    build_px4_mission_feedback_binding,
)
from multidetect.px4_mission_feedback_cli import observe_px4_mission_feedback
from multidetect.px4_mission_feedback_profile_binding import (
    verify_px4_mission_feedback_profile_binding,
)
from multidetect.runtime import RuntimeProfile
from multidetect.runtime_launcher import derive_endpoint_binding_hash, derive_vehicle_identity_hash
from multidetect.runtime_profile_binding_set import verify_runtime_profile_binding_set
from multidetect.runtime_profiles import sign_runtime_profile_document

KEY = b"multidetect-v185-profile-feedback-join-demo-key-material"
KEY_ID = "multidetect-v185-demo-key"
SIGNING_HASH = hashlib.sha256(b"multidetect-v185-demo-signing-key").hexdigest()
ADAPTERS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _source(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _mission(vehicle_class: str) -> ExecutableMission:
    adapter_id, adapter_hash = ADAPTERS[vehicle_class]
    return ExecutableMission(
        mission_id=f"v185-profile-feedback-{vehicle_class}",
        platform_plan_hash="b" * 64,
        airframe_profile_hash=hashlib.sha256(vehicle_class.encode()).hexdigest(),
        airframe_adapter_id=adapter_id,
        airframe_adapter_hash=adapter_hash,
        items=(
            MissionItem(
                sequence=0,
                command=MissionCommand.LAND,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=1.3521,
                longitude_deg=103.8198,
                altitude_m=0.0,
            ),
        ),
    )


class _Provider:
    def __init__(self, mission: ExecutableMission) -> None:
        self.config = PixhawkReadOnlyConfig(endpoint="udpin:127.0.0.1:14799", expected_system_id=1)
        self.resolved_endpoint = self.config.endpoint
        self.heartbeat_identity = PixhawkHeartbeatIdentity(
            source_system_id=1,
            source_component_id=1,
            autopilot_id=12,
            vehicle_type_id=1,
            system_status_id=4,
            mavlink_version=3,
        )
        self.autopilot_version_identity = PixhawkAutopilotVersionIdentity(
            source_system_id=1,
            source_component_id=1,
            capabilities=(1 << 63) | 17,
            flight_sw_version=0x010203FF,
            middleware_sw_version=0x040506C0,
            os_sw_version=0x07080940,
            board_version=0x10203040,
            flight_custom_version_hex="0102030405060708",
            middleware_custom_version_hex="1112131415161718",
            os_custom_version_hex="2122232425262728",
            vendor_id=9900,
            product_id=42,
            device_uid_source="none",
            device_uid_hash=None,
        )
        self.autopilot_version_observed_at_s = 10.0
        self.is_read_only = True
        self.messages_received = 0
        self.messages_transmitted = 0
        self.message_type_counts: dict[str, int] = {}
        self._emitted = False
        self._tracker = Px4MissionFeedbackTracker(
            mission,
            config=Px4MissionFeedbackConfig(expected_system_id=1, expected_component_id=1),
        )

    def snapshot(self, *, now_s: float) -> object:
        if not self._emitted:
            for message_type in ("MISSION_CURRENT", "MISSION_ITEM_REACHED"):
                assert self._tracker.ingest_message(
                    {
                        "message_type": message_type,
                        "seq": 0,
                        "source_system_id": 1,
                        "source_component_id": 1,
                    },
                    observed_at_s=now_s,
                )
                self.messages_received += 1
                self.message_type_counts[message_type] = 1
            self._emitted = True
        return object()

    def mission_feedback_snapshot(self, *, now_s: float):
        return self._tracker.snapshot(now_s=now_s)


def _build_feedback_sources(root: Path) -> list[tuple[str, str, Path, Path, Path]]:
    mission_paths: dict[str, Path] = {}
    for vehicle_class in ADAPTERS:
        path = root / "missions" / f"{vehicle_class}.json"
        _write_json(path, _mission(vehicle_class).to_mapping())
        mission_paths[vehicle_class] = path

    bindings: list[tuple[str, str, Path, Path, Path]] = []
    for sequence, (mode, vehicle_class) in enumerate(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS):
        mission = _mission(vehicle_class)
        provider = _Provider(mission)
        vehicle_hash = derive_vehicle_identity_hash(provider.heartbeat_identity)
        endpoint_hash = derive_endpoint_binding_hash(
            provider.config,
            resolved_endpoint=provider.resolved_endpoint,
        )
        profile = (
            RuntimeProfile.hil(
                vehicle_identity_hash=vehicle_hash,
                firmware_identity_hash=provider.autopilot_version_identity.identity_hash,
                airframe_profile_hash=mission.airframe_profile_hash,
                endpoint_binding_hash=endpoint_hash,
            )
            if mode == "hil"
            else RuntimeProfile.flight(
                vehicle_identity_hash=vehicle_hash,
                firmware_identity_hash=provider.autopilot_version_identity.identity_hash,
                airframe_profile_hash=mission.airframe_profile_hash,
                signing_identity_hash=SIGNING_HASH,
                endpoint_binding_hash=endpoint_hash,
            )
        )
        profile_path = root / "profiles" / "feedback" / mode / f"{vehicle_class}.json"
        _write_json(
            profile_path,
            sign_runtime_profile_document(profile, hmac_key=KEY, key_id=KEY_ID),
        )
        report = observe_px4_mission_feedback(
            mission,
            provider,
            runtime_mode=mode,
            duration_s=0.0,
            clock=lambda: 10.0,
            observed_at=lambda sequence=sequence: datetime(2026, 9, 7, 12, sequence, tzinfo=UTC),
            runtime_profile_path=profile_path,
            runtime_profile_hmac_key=KEY,
            runtime_profile_key_id=KEY_ID,
            runtime_signing_identity_hash=SIGNING_HASH if mode == "real" else None,
        )
        report_path = root / "feedback" / mode / f"{vehicle_class}.json"
        _write_json(report_path, report)
        bindings.append(
            (mode, vehicle_class, mission_paths[vehicle_class], profile_path, report_path)
        )
    return bindings


def _build_policy_set(root: Path) -> dict[str, object]:
    identity = {
        "vehicle_identity_hash": "c" * 64,
        "firmware_identity_hash": "d" * 64,
        "airframe_profile_hash": "e" * 64,
        "endpoint_binding_hash": "f" * 64,
    }
    paths = {
        "replay": root / "profiles" / "policy" / "replay.json",
        "sim": root / "profiles" / "policy" / "sim.json",
        "hil": root / "profiles" / "policy" / "hil.json",
        "real": root / "profiles" / "policy" / "real.json",
    }
    _write_json(
        paths["replay"], {"schema_version": 1, "profile": RuntimeProfile.replay().to_mapping()}
    )
    _write_json(paths["sim"], {"schema_version": 1, "profile": RuntimeProfile.sim().to_mapping()})
    _write_json(
        paths["hil"],
        sign_runtime_profile_document(
            RuntimeProfile.hil(**identity),
            hmac_key=KEY,
            key_id=KEY_ID,
        ),
    )
    _write_json(
        paths["real"],
        sign_runtime_profile_document(
            RuntimeProfile.flight(signing_identity_hash=SIGNING_HASH, **identity),
            hmac_key=KEY,
            key_id=KEY_ID,
        ),
    )
    policy = verify_runtime_profile_binding_set(
        [
            {"runtime_mode": mode, "profile_path": f"profiles/policy/{mode}.json"}
            for mode in ("replay", "sim")
        ]
        + [
            {
                "runtime_mode": "hil",
                "profile_path": "profiles/policy/hil.json",
                "observed_identity": identity,
            },
            {
                "runtime_mode": "real",
                "profile_path": "profiles/policy/real.json",
                "observed_identity": {**identity, "signing_identity_hash": SIGNING_HASH},
            },
        ],
        base_directory=root,
        hmac_keys={"hil": KEY, "real": KEY},
        expected_key_ids={"hil": KEY_ID, "real": KEY_ID},
    )
    path = root / "runtime-profile-binding-set-v185.json"
    _write_json(path, policy)
    return policy


def _schema_errors(document: object, schema_path: Path) -> int:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(root: Path, schema_path: Path, parity_path: Path) -> dict[str, object]:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    policy = _build_policy_set(root)
    feedback_bindings = _build_feedback_sources(root)
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    parity_hashes = parity.get("parity", parity).get("px4_mission_path_hash")
    feedback = build_px4_mission_feedback_binding(
        feedback_bindings,
        manifest_directory=root,
        output_identity=root / "joined-output.json",
        release_label="0.3.0-pre.8",
        px4_mission_path_hash=parity_hashes,
    )
    feedback_path = root / "px4-mission-feedback-binding-v185.json"
    _write_json(feedback_path, feedback)
    joined = verify_px4_mission_feedback_profile_binding(
        root / "runtime-profile-binding-set-v185.json",
        feedback_path,
        base_directory=root,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash=feedback["px4_mission_path_hash"],
    )
    joined_path = root / "px4-mission-feedback-profile-binding-v185.json"
    _write_json(joined_path, joined)
    summary = {
        "profile_set": {
            "path": str((root / "runtime-profile-binding-set-v185.json").relative_to(root)),
            "bytes": (root / "runtime-profile-binding-set-v185.json").stat().st_size,
            "sha256": hashlib.sha256(
                (root / "runtime-profile-binding-set-v185.json").read_bytes()
            ).hexdigest(),
            "gate_passed": policy["gate_passed"],
        },
        "feedback_binding": {
            "path": feedback_path.name,
            "bytes": feedback_path.stat().st_size,
            "sha256": hashlib.sha256(feedback_path.read_bytes()).hexdigest(),
            "gate_passed": feedback["gate_passed"],
        },
        "joined_report": {
            "path": joined_path.name,
            "bytes": joined_path.stat().st_size,
            "sha256": hashlib.sha256(joined_path.read_bytes()).hexdigest(),
            "gate_passed": joined["gate_passed"],
            "schema_errors": _schema_errors(joined, schema_path),
            "leaf_count": len(joined["leaf_bindings"]),
        },
        "feedback_source_count": len(feedback_bindings),
        "claim_boundary": joined["claim_boundary"],
    }
    _write_json(root / "summary-v185.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("configs/schemas/px4-mission-feedback-profile-binding.schema.json"),
    )
    parser.add_argument(
        "--parity",
        type=Path,
        default=Path("configs/release-parity.json"),
    )
    args = parser.parse_args()
    summary = build_evidence(args.output_directory, args.schema, args.parity)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["joined_report"]["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
