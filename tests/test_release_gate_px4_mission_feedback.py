from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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
from multidetect.px4_mission_feedback import (
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackTracker,
)
from multidetect.px4_mission_feedback_binding import (
    PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS,
    verify_px4_mission_feedback_binding,
)
from multidetect.px4_mission_feedback_cli import (
    observe_px4_mission_feedback,
    write_px4_mission_feedback_observation,
)
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    ReleaseGateManifestError,
    build_release_gate_manifest,
    create_release_gate_manifest,
    write_release_gate_manifest,
)
from multidetect.release_gate_manifest import (
    main as manifest_main,
)
from multidetect.runtime import RuntimeProfile
from multidetect.runtime_launcher import (
    derive_endpoint_binding_hash,
    derive_vehicle_identity_hash,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"
SCHEMA_PATH = REPO_ROOT / "configs" / "schemas" / "release-gate-manifest.schema.json"
MODES = ("replay", "sim", "hil", "real")
PARITY = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}
_ADAPTERS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}
RUNTIME_KEY = b"release-feedback-runtime-profile-test-key-material"
RUNTIME_KEY_ID = "release-feedback-test-key"
SIGNING_IDENTITY_HASH = hashlib.sha256(b"release-mavlink-signing-key").hexdigest()


def _autopilot_version_identity() -> PixhawkAutopilotVersionIdentity:
    return PixhawkAutopilotVersionIdentity(
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


def _write_mode_evidence(root: Path) -> dict[str, Path]:
    evidence: dict[str, Path] = {}
    for mode in MODES:
        path = root / "evidence" / f"{mode}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "gate_passed": True,
                    "runtime_mode": mode,
                }
            ),
            encoding="utf-8",
        )
        evidence[mode] = path
    return evidence


def _complete_statuses() -> dict[str, dict[str, str]]:
    return {
        mode: {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": (
                "not_required" if mode in {"replay", "sim"} else "passed"
            ),
            "real_control_acceptance": (
                "not_required" if mode in {"replay", "sim"} else "passed"
            ),
        }
        for mode in MODES
    }


def _mission(vehicle_class: str) -> ExecutableMission:
    adapter_id, adapter_hash = _ADAPTERS[vehicle_class]
    return ExecutableMission(
        mission_id=f"feedback-{vehicle_class}",
        platform_plan_hash="a" * 64,
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


class _FeedbackProvider:
    def __init__(self, mission: ExecutableMission) -> None:
        self.config = PixhawkReadOnlyConfig(
            endpoint="udpin:127.0.0.1:14599",
            expected_system_id=1,
        )
        self.resolved_endpoint = self.config.endpoint
        self.heartbeat_identity = PixhawkHeartbeatIdentity(
            source_system_id=1,
            source_component_id=1,
            autopilot_id=12,
            vehicle_type_id=1,
            system_status_id=4,
            mavlink_version=3,
        )
        self.autopilot_version_identity = _autopilot_version_identity()
        self.autopilot_version_observed_at_s = 10.0
        self.is_read_only = True
        self.messages_received = 0
        self.messages_transmitted = 0
        self.message_type_counts: dict[str, int] = {}
        self._emitted = False
        self._tracker = Px4MissionFeedbackTracker(
            mission,
            config=Px4MissionFeedbackConfig(
                expected_system_id=1,
                expected_component_id=1,
            ),
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


def _feedback_sources(
    tmp_path: Path,
) -> list[tuple[str, str, Path, Path, Path]]:
    mission_paths: dict[str, Path] = {}
    for vehicle_class in _ADAPTERS:
        mission_path = tmp_path / "missions" / f"{vehicle_class}.json"
        mission_path.parent.mkdir(parents=True, exist_ok=True)
        mission_path.write_text(
            json.dumps(_mission(vehicle_class).to_mapping()),
            encoding="utf-8",
        )
        mission_paths[vehicle_class] = mission_path

    bindings: list[tuple[str, str, Path, Path, Path]] = []
    for sequence, (mode, vehicle_class) in enumerate(
        PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS
    ):
        mission = _mission(vehicle_class)
        provider = _FeedbackProvider(mission)
        vehicle_identity_hash = derive_vehicle_identity_hash(
            provider.heartbeat_identity
        )
        endpoint_binding_hash = derive_endpoint_binding_hash(
            provider.config,
            resolved_endpoint=provider.resolved_endpoint,
        )
        profile = (
            RuntimeProfile.hil(
                vehicle_identity_hash=vehicle_identity_hash,
                firmware_identity_hash=provider.autopilot_version_identity.identity_hash,
                airframe_profile_hash=mission.airframe_profile_hash,
                endpoint_binding_hash=endpoint_binding_hash,
            )
            if mode == "hil"
            else RuntimeProfile.flight(
                vehicle_identity_hash=vehicle_identity_hash,
                firmware_identity_hash=provider.autopilot_version_identity.identity_hash,
                airframe_profile_hash=mission.airframe_profile_hash,
                signing_identity_hash=SIGNING_IDENTITY_HASH,
                endpoint_binding_hash=endpoint_binding_hash,
            )
        )
        profile_path = tmp_path / "profiles" / mode / f"{vehicle_class}.json"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            json.dumps(
                sign_runtime_profile_document(
                    profile,
                    hmac_key=RUNTIME_KEY,
                    key_id=RUNTIME_KEY_ID,
                )
            ),
            encoding="utf-8",
        )
        report = observe_px4_mission_feedback(
            mission,
            provider,
            runtime_mode=mode,
            duration_s=0.0,
            clock=lambda: 10.0,
            observed_at=lambda sequence=sequence: datetime(
                2026,
                9,
                4,
                12,
                sequence,
                tzinfo=UTC,
            ),
            runtime_profile_path=profile_path,
            runtime_profile_hmac_key=RUNTIME_KEY,
            runtime_profile_key_id=RUNTIME_KEY_ID,
            runtime_signing_identity_hash=(
                SIGNING_IDENTITY_HASH if mode == "real" else None
            ),
        )
        report_path = tmp_path / "feedback" / mode / f"{vehicle_class}.json"
        write_px4_mission_feedback_observation(report_path, report)
        bindings.append(
            (
                mode,
                vehicle_class,
                mission_paths[vehicle_class],
                profile_path,
                report_path,
            )
        )
    return bindings


def test_release_manifest_binds_and_reopens_six_mission_feedback_sources(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"

    document = create_release_gate_manifest(
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=bindings,
    )

    binding = document["px4_mission_feedback"]
    assert len(binding["observations"]) == 6
    assert binding["required_runtime_modes"] == ["hil", "real"]
    assert binding["required_vehicle_classes"] == ["fixed-wing", "multirotor", "vtol"]
    assert binding["px4_mission_path_hash"] == PARITY["px4_mission_path_hash"]
    assert binding["read_only"] is True
    assert binding["messages_transmitted"] == 0
    assert binding["checks"]["all_firmware_identities_bound"] is True
    assert all(
        len(observation["firmware_identity_hash"]) == 64
        for observation in binding["observations"]
    )
    assert all(binding["checks"].values())

    verification = verify_px4_mission_feedback_binding(
        binding,
        manifest_directory=output.parent,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash=PARITY["px4_mission_path_hash"],
    )
    assert verification["passed"] is True
    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is True
    assert report["px4_mission_feedback"]["present"] is True
    assert report["px4_mission_feedback"]["passed"] is True
    assert len(report["px4_mission_feedback"]["observations"]) == 6


def test_release_gate_rejects_feedback_report_tampering_after_binding(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=bindings,
    )
    write_release_gate_manifest(output, document)

    report_path = bindings[0][4]
    report_path.write_bytes(report_path.read_bytes() + b"\n")
    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["px4_mission_feedback"]["passed"] is False
    assert any(
        "feedback report SHA-256 does not match" in reason
        for reason in report["px4_mission_feedback"]["reasons"]
    )


def test_release_gate_rejects_runtime_profile_tampering_after_binding(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=bindings,
    )
    write_release_gate_manifest(output, document)

    profile_path = bindings[0][3]
    profile_path.write_bytes(profile_path.read_bytes() + b"\n")
    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["px4_mission_feedback"]["passed"] is False
    assert any(
        "runtime profile SHA-256 does not match" in reason
        or "runtime profile file SHA-256 does not match" in reason
        for reason in report["px4_mission_feedback"]["reasons"]
    )


@pytest.mark.parametrize(
    ("identity_field", "expected_reason"),
    (
        (
            "vehicle_identity_hash",
            "vehicle_identity_hash does not match the runtime binding",
        ),
        (
            "firmware_identity_hash",
            "firmware_identity_hash does not match the runtime binding",
        ),
    ),
)
def test_release_gate_rejects_runtime_identity_record_forgery(
    tmp_path: Path,
    identity_field: str,
    expected_reason: str,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=_feedback_sources(tmp_path),
    )
    document["px4_mission_feedback"]["observations"][0][identity_field] = "f" * 64
    write_release_gate_manifest(output, document)

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["px4_mission_feedback"]["passed"] is False
    assert any(
        expected_reason in reason
        for reason in report["px4_mission_feedback"]["reasons"]
    )


def test_release_binding_rejects_legacy_v1_feedback_report(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    mode, vehicle_class, mission_path, profile_path, _ = bindings[0]
    mission = _mission(vehicle_class)
    legacy_report = observe_px4_mission_feedback(
        mission,
        _FeedbackProvider(mission),
        runtime_mode=mode,
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 13, 0, tzinfo=UTC),
    )
    legacy_path = tmp_path / "legacy-feedback-v1.json"
    write_px4_mission_feedback_observation(legacy_path, legacy_report)
    bindings[0] = (
        mode,
        vehicle_class,
        mission_path,
        profile_path,
        legacy_path,
    )

    with pytest.raises(ReleaseGateManifestError, match="legacy v1 observation"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "manifest.json",
            version_manifest_path=VERSION_MANIFEST,
            px4_mission_feedback_evidence=bindings,
        )


def test_release_gate_rejects_boolean_zero_transmit_forgery(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=_feedback_sources(tmp_path),
    )
    document["px4_mission_feedback"]["observations"][0][
        "messages_transmitted"
    ] = False
    write_release_gate_manifest(output, document)

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["px4_mission_feedback"]["passed"] is False
    assert any(
        "messages_transmitted must be zero" in reason
        for reason in report["px4_mission_feedback"]["reasons"]
    )


def test_feedback_binding_rejects_incomplete_coverage_and_wrong_airframe(
    tmp_path: Path,
) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    common = {
        "parity": PARITY,
        "statuses": _complete_statuses(),
        "output_path": tmp_path / "manifest.json",
        "version_manifest_path": VERSION_MANIFEST,
    }
    with pytest.raises(ReleaseGateManifestError, match="exactly six observations"):
        build_release_gate_manifest(
            evidence,
            px4_mission_feedback_evidence=bindings[:-1],
            **common,
        )

    wrong = list(bindings)
    mode, vehicle_class, _, profile_path, report_path = wrong[0]
    wrong[0] = (
        mode,
        vehicle_class,
        bindings[-1][2],
        profile_path,
        report_path,
    )
    with pytest.raises(ReleaseGateManifestError, match="Mission uses adapter"):
        build_release_gate_manifest(
            evidence,
            px4_mission_feedback_evidence=wrong,
            **common,
        )


def test_feedback_binding_is_bound_to_four_mode_px4_path_hash(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=bindings,
    )
    replacement = "f" * 64
    assert replacement != PARITY["px4_mission_path_hash"]
    for mode in MODES:
        document["modes"][mode]["parity"]["px4_mission_path_hash"] = replacement
    write_release_gate_manifest(output, document)

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is False
    assert report["px4_mission_feedback"]["passed"] is False
    assert report["px4_mission_feedback"]["checks"]["px4_mission_path_bound"] is False
    assert any(
        "px4_mission_path_hash does not match" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_final_release_requires_px4_mission_feedback_matrix(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    output = tmp_path / "manifest" / "release-gate.json"
    create_release_gate_manifest(
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
    )
    release_version = tmp_path / "product-version-release.json"
    release_version.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0",
                "numeric": "0.3.0",
                "python": "0.3.0",
                "channel": "release",
            }
        ),
        encoding="utf-8",
    )
    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["release_label"] = "0.3.0"
    output.write_text(json.dumps(raw), encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=release_version)
    assert report["release_allowed"] is False
    assert any(
        "final release requires a hash-bound HIL/REAL x three-airframe PX4 Mission "
        "feedback matrix" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_manifest_cli_binds_six_feedback_sources(tmp_path: Path) -> None:
    evidence = _write_mode_evidence(tmp_path)
    bindings = _feedback_sources(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "manifest" / "release-gate.json"
    arguments = ["--out", str(output), "--parity-json", str(parity_file)]
    for mode, vehicle_class, mission_path, profile_path, report_path in bindings:
        arguments.extend(
            [
                "--px4-mission-feedback-evidence",
                mode,
                vehicle_class,
                str(mission_path),
                str(profile_path),
                str(report_path),
            ]
        )
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert manifest_main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["px4_mission_feedback"]["gate_passed"] is True
    assert len(raw["px4_mission_feedback"]["observations"]) == 6


def test_release_gate_schema_declares_px4_feedback_matrix(tmp_path: Path) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert "px4_mission_feedback" in schema["properties"]
    definition = schema["$defs"]["px4MissionFeedbackRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["required_runtime_modes"]["const"] == ["hil", "real"]
    assert definition["properties"]["gate_passed"]["const"] is True
    assert definition["properties"]["hardware_acceptance"]["const"] is False
    observation_definition = schema["$defs"]["px4MissionFeedbackObservationRecord"]
    assert "firmware_identity_hash" in observation_definition["required"]
    assert "all_firmware_identities_bound" in definition["properties"]["checks"][
        "required"
    ]

    evidence = _write_mode_evidence(tmp_path)
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=tmp_path / "manifest.json",
        version_manifest_path=VERSION_MANIFEST,
        px4_mission_feedback_evidence=_feedback_sources(tmp_path),
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
