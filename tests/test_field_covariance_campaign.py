from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect import field_covariance_campaign as campaign


def _write_config(tmp_path: Path) -> Path:
    entries: list[dict[str, object]] = []
    for index, (mode, vehicle) in enumerate(campaign.REQUIRED_COMBINATIONS):
        source_dir = tmp_path / "sources" / mode / vehicle
        source_dir.mkdir(parents=True, exist_ok=True)
        mission = source_dir / "mission.json"
        feedback = source_dir / "feedback.json"
        profile = source_dir / "profile.json"
        for path in (mission, feedback, profile):
            path.write_text("{}", encoding="utf-8")
        entries.append(
            {
                "runtime_mode": mode,
                "vehicle_class": vehicle,
                "endpoint": f"serial:COM{index + 3}",
                "baud": 57600,
                "mission_path": str(mission.relative_to(tmp_path)),
                "mission_feedback_path": str(feedback.relative_to(tmp_path)),
                "runtime_profile_path": str(profile.relative_to(tmp_path)),
                "report_path": f"reports/{mode}/{vehicle}.json",
            }
        )
    path = tmp_path / "campaign.json"
    path.write_text(
        json.dumps(
            {
                "schema": campaign.SCHEMA,
                "schema_version": campaign.SCHEMA_VERSION,
                "release_label": "0.3.0-pre.8",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _verified_record(
    mode: str,
    vehicle: str,
    report_path: Path,
    *,
    manifest_directory: Path,
) -> dict[str, object]:
    del mode, vehicle, manifest_directory
    return {
        "report_sha256": "a" * 64,
        "report_bytes": report_path.stat().st_size,
        "evidence_sha256": "b" * 64,
        "records_verified": 12,
        "firmware_identity_hash": "c" * 64,
    }


def _ready_preflight(config: campaign.CampaignConfig) -> dict[str, object]:
    return {
        "schema": "multidetect.pixhawk-covariance-field-source-preflight/v1",
        "schema_version": 1,
        "release_label": config.release_label,
        "config_path": str(config.path),
        "config_sha256": "d" * 64,
        "records": [
            {
                "runtime_mode": leaf.runtime_mode,
                "vehicle_class": leaf.vehicle_class,
                "status": "ready",
                "gate_passed": True,
            }
            for leaf in config.leaves
        ],
        "ready_count": 6,
        "blocked_count": 0,
        "gate_passed": True,
    }


def _ready_online_probe(
    config: campaign.CampaignConfig, **kwargs: object
) -> tuple[int, dict[str, object]]:
    del kwargs
    leaf = config.leaves[0]
    return 0, {
        "schema": "multidetect.pixhawk-covariance-online-preflight/v1",
        "runtime_mode": leaf.runtime_mode,
        "vehicle_class": leaf.vehicle_class,
        "gate_passed": True,
        "evidence_sha256": "e" * 64,
    }


def test_campaign_schema_is_valid_and_closed() -> None:
    schema = json.loads(campaign.CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["entry"]["additionalProperties"] is False
    example = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "configs"
            / "pixhawk-covariance-field-campaign.example.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(example)


def test_status_orders_exact_matrix_and_selects_one_next_leaf(tmp_path: Path) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))

    status = campaign.campaign_status(config)

    assert [leaf.key for leaf in config.leaves] == list(campaign.REQUIRED_COMBINATIONS)
    assert status["passed_count"] == 0
    assert status["pending_count"] == 6
    assert status["failed_count"] == 0
    assert status["next_leaf"]["runtime_mode"] == "hil"
    assert status["next_leaf"]["vehicle_class"] == "fixed-wing"
    assert status["e7_arguments"] == []


def test_capture_next_dry_run_never_opens_transport_or_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    monkeypatch.setattr(campaign, "campaign_preflight", _ready_preflight)

    exit_code, status = campaign.capture_next(
        config,
        samples=30,
        interval_seconds=0.2,
        minimum_odometry_messages=10,
        minimum_accepted_covariances=5,
        dry_run=True,
    )

    assert exit_code == 0
    assert status["dry_run"] is True
    assert status["capture_command"][0] == campaign.sys.executable
    assert "--runtime-mode" in status["capture_command"]
    assert "hil" in status["capture_command"]
    assert "--vehicle-class" not in status["capture_command"]
    assert not config.leaves[0].report_path.exists()


def test_complete_status_emits_all_six_e7_argument_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    for leaf in config.leaves:
        leaf.report_path.parent.mkdir(parents=True, exist_ok=True)
        leaf.report_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", _verified_record)

    status = campaign.campaign_status(config)

    assert status["complete"] is True
    assert status["passed_count"] == 6
    assert status["pending_count"] == 0
    assert status["failed_count"] == 0
    assert status["e7_arguments"].count("--pixhawk-covariance-evidence") == 6


def test_existing_failed_report_blocks_capture_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    first = config.leaves[0].report_path
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("{}", encoding="utf-8")

    def reject(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise ValueError("source binding drift")

    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", reject)
    with pytest.raises(campaign.FieldCovarianceCampaignError, match="immutable report"):
        campaign.capture_next(
            config,
            samples=30,
            interval_seconds=0.2,
            minimum_odometry_messages=10,
            minimum_accepted_covariances=5,
            dry_run=False,
        )
    assert first.read_text(encoding="utf-8") == "{}"


def test_zero_capture_process_exit_still_fails_when_report_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    monkeypatch.setattr(campaign, "campaign_preflight", _ready_preflight)
    monkeypatch.setattr(campaign, "probe_next", _ready_online_probe)
    first = config.leaves[0].report_path

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def reject(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise ValueError("schema mismatch")

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", reject)
    exit_code, status = campaign.capture_next(
        config,
        samples=30,
        interval_seconds=0.2,
        minimum_odometry_messages=10,
        minimum_accepted_covariances=5,
        dry_run=False,
    )

    assert exit_code == 1
    assert status["capture_process_exit_code"] == 0
    assert status["capture_verified"] is False
    assert status["failed_count"] == 1


def test_capture_persists_and_independently_reloads_online_preflight_before_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    monkeypatch.setattr(campaign, "campaign_preflight", _ready_preflight)
    monkeypatch.setattr(campaign, "probe_next", _ready_online_probe)
    monkeypatch.setattr(campaign, "verify_pixhawk_covariance_leaf", _verified_record)
    probe_path = tmp_path / "journal" / "online-preflight.json"
    events: list[str] = []

    def write_probe(path: Path, document: dict[str, object], *, overwrite: bool) -> Path:
        assert overwrite is False
        events.append("write-probe")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def load_probe(path: Path, *, maximum_age_seconds: float) -> dict[str, object]:
        assert path == probe_path.resolve()
        assert maximum_age_seconds == campaign.DEFAULT_MAXIMUM_AGE_SECONDS
        events.append("verify-probe")
        return json.loads(path.read_text(encoding="utf-8"))

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        events.append("capture")
        report = config.leaves[0].report_path
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(campaign, "write_online_covariance_preflight_observation", write_probe)
    monkeypatch.setattr(campaign, "load_online_covariance_preflight_observation", load_probe)
    monkeypatch.setattr(campaign.subprocess, "run", run)

    exit_code, result = campaign.capture_next(
        config,
        samples=30,
        interval_seconds=0.2,
        minimum_odometry_messages=10,
        minimum_accepted_covariances=5,
        dry_run=False,
        probe_report_out=probe_path,
    )

    assert exit_code == 0
    assert events == ["write-probe", "verify-probe", "capture"]
    assert result["online_preflight_path"] == str(probe_path.resolve())
    assert result["online_preflight_verified_before_capture"] is True
    assert result["online_preflight_file_sha256"] == campaign._sha256(probe_path)
    assert result["capture_verified"] is True


def test_capture_probe_output_cannot_alias_covariance_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    monkeypatch.setattr(campaign, "campaign_preflight", _ready_preflight)
    monkeypatch.setattr(campaign, "probe_next", _ready_online_probe)

    with pytest.raises(campaign.FieldCovarianceCampaignError, match="aliases"):
        campaign.capture_next(
            config,
            samples=30,
            interval_seconds=0.2,
            minimum_odometry_messages=10,
            minimum_accepted_covariances=5,
            dry_run=False,
            probe_report_out=config.leaves[0].report_path,
        )
    assert not config.leaves[0].report_path.exists()


def test_preflight_orders_all_six_ready_sources_and_binds_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))

    class Mission:
        mission_id = "field-preflight"
        canonical_hash = "1" * 64
        airframe_profile_hash = "2" * 64

        def __init__(self, leaf: campaign.CampaignLeaf) -> None:
            adapter = campaign._ADAPTER_BY_VEHICLE_CLASS[leaf.vehicle_class]
            self.airframe_adapter_id, self.airframe_adapter_hash = adapter

    missions = {leaf.mission_path: Mission(leaf) for leaf in config.leaves}

    def load_mission(path: Path) -> Mission:
        return missions[path]

    def load_feedback(
        path: Path, *, expected_mission: Mission, runtime_profile_path: Path
    ) -> dict[str, object]:
        leaf = next(item for item in config.leaves if item.mission_feedback_path == path)
        assert expected_mission is missions[leaf.mission_path]
        assert runtime_profile_path == leaf.runtime_profile_path
        return {
            "runtime_mode": leaf.runtime_mode,
            "feedback_gate_passed": True,
            "transport": {
                "configured_endpoint": leaf.endpoint,
                "baud": leaf.baud,
                "read_only": True,
                "messages_transmitted": 0,
            },
            "runtime_binding": {
                "runtime_mode": leaf.runtime_mode,
                "gate_passed": True,
                "signature_valid": True,
                "profile_valid": True,
                "flight_control_enabled": False,
                "output_locked": True,
                "profile": {"profile_id": "hil" if leaf.runtime_mode == "hil" else "flight"},
                "observed_identity": {
                    "vehicle_identity_hash": "3" * 64,
                    "firmware_identity_hash": "4" * 64,
                    "endpoint_binding_hash": "5" * 64,
                    "signing_identity_hash": None,
                },
                "key_id": "field-key",
                "binding_sha256": "6" * 64,
            },
        }

    monkeypatch.setattr(campaign, "load_executable_mission_document", load_mission)
    monkeypatch.setattr(campaign, "load_px4_mission_feedback_observation", load_feedback)

    result = campaign.campaign_preflight(config)

    assert result["gate_passed"] is True
    assert result["ready_count"] == 6
    assert result["blocked_count"] == 0
    assert [
        (record["runtime_mode"], record["vehicle_class"]) for record in result["records"]
    ] == list(campaign.REQUIRED_COMBINATIONS)
    assert all(record["output_locked"] is True for record in result["records"])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("adapter", "Mission adapter"),
        ("mode", "runtime_mode"),
        ("endpoint", "configured endpoint"),
        ("baud", "baud"),
        ("write", "receive-only"),
        ("signature", "signature was not qualified"),
        ("flight_control", "enabled flight-control"),
        ("unlock", "output is not locked"),
    ],
)
def test_preflight_blocks_semantic_source_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    target = config.leaves[0]

    class Mission:
        mission_id = "field-preflight"
        canonical_hash = "1" * 64
        airframe_profile_hash = "2" * 64
        airframe_adapter_id = campaign.PX4_FIXED_WING_ADAPTER_ID
        airframe_adapter_hash = campaign.PX4_FIXED_WING_ADAPTER_HASH

    mission = Mission()
    if mutation == "adapter":
        mission.airframe_adapter_id = "wrong-adapter"

    feedback: dict[str, object] = {
        "runtime_mode": "real" if mutation == "mode" else target.runtime_mode,
        "feedback_gate_passed": True,
        "transport": {
            "configured_endpoint": "serial:COM99" if mutation == "endpoint" else target.endpoint,
            "baud": 115200 if mutation == "baud" else target.baud,
            "read_only": mutation != "write",
            "messages_transmitted": 1 if mutation == "write" else 0,
        },
        "runtime_binding": {
            "runtime_mode": target.runtime_mode,
            "gate_passed": True,
            "signature_valid": mutation != "signature",
            "profile_valid": True,
            "flight_control_enabled": mutation == "flight_control",
            "output_locked": mutation != "unlock",
            "profile": {"profile_id": "hil"},
            "observed_identity": {},
            "key_id": "field-key",
            "binding_sha256": "6" * 64,
        },
    }
    monkeypatch.setattr(campaign, "load_executable_mission_document", lambda path: mission)
    monkeypatch.setattr(
        campaign,
        "load_px4_mission_feedback_observation",
        lambda *args, **kwargs: feedback,
    )

    result = campaign._source_preflight(target)

    assert result["status"] == "blocked"
    assert message in result["reason"]


def test_capture_next_refuses_blocked_sources_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    called = False

    def run(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(campaign.subprocess, "run", run)

    with pytest.raises(campaign.FieldCovarianceCampaignError, match="source preflight blocked"):
        campaign.capture_next(
            config,
            samples=30,
            interval_seconds=0.2,
            minimum_odometry_messages=10,
            minimum_accepted_covariances=5,
            dry_run=False,
        )
    assert called is False


def test_preflight_reopens_sources_and_detects_postcheck_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    target = config.leaves[0]

    class Mission:
        mission_id = "field-preflight"
        canonical_hash = "1" * 64
        airframe_profile_hash = "2" * 64
        airframe_adapter_id = campaign.PX4_FIXED_WING_ADAPTER_ID
        airframe_adapter_hash = campaign.PX4_FIXED_WING_ADAPTER_HASH

    monkeypatch.setattr(campaign, "load_executable_mission_document", lambda path: Mission())

    def load_feedback(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        if target.runtime_profile_path.read_text(encoding="utf-8") != "{}":
            raise ValueError("runtime profile file SHA-256 does not match observation")
        return {
            "runtime_mode": target.runtime_mode,
            "feedback_gate_passed": True,
            "transport": {
                "configured_endpoint": target.endpoint,
                "baud": target.baud,
                "read_only": True,
                "messages_transmitted": 0,
            },
            "runtime_binding": {
                "runtime_mode": target.runtime_mode,
                "gate_passed": True,
                "signature_valid": True,
                "profile_valid": True,
                "flight_control_enabled": False,
                "output_locked": True,
                "profile": {"profile_id": "hil"},
                "observed_identity": {},
                "key_id": "field-key",
                "binding_sha256": "6" * 64,
            },
        }

    monkeypatch.setattr(campaign, "load_px4_mission_feedback_observation", load_feedback)
    assert campaign._source_preflight(target)["status"] == "ready"

    target.runtime_profile_path.write_text('{"tampered":true}', encoding="utf-8")
    second = campaign._source_preflight(target)

    assert second["status"] == "blocked"
    assert "SHA-256" in second["reason"]


def test_preflight_cli_returns_one_for_blocked_sources(tmp_path: Path, capsys: object) -> None:
    path = _write_config(tmp_path)

    exit_code = campaign.main(["preflight", "--config", str(path)])

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["gate_passed"] is False
    assert output["blocked_count"] == 6


def test_verify_probe_cli_applies_default_expiry_and_matches_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    config_path = _write_config(tmp_path)
    config = campaign.load_campaign_config(config_path)
    report_path = tmp_path / "probe.json"
    report_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    def load(path: Path, *, maximum_age_seconds: float) -> dict[str, object]:
        captured["path"] = path
        captured["maximum_age_seconds"] = maximum_age_seconds
        return {
            "source_binding": {
                "campaign_config_path": str(config.path),
                "campaign_config_sha256": campaign._sha256(config.path),
            },
            "gate_passed": True,
        }

    monkeypatch.setattr(campaign, "load_online_covariance_preflight_observation", load)
    exit_code = campaign.main(
        ["verify-probe", "--config", str(config_path), "--probe-report", str(report_path)]
    )

    assert exit_code == 0
    assert captured == {
        "path": report_path,
        "maximum_age_seconds": campaign.DEFAULT_MAXIMUM_AGE_SECONDS,
    }
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True


def test_cli_status_output_cannot_overwrite_sources_reports_or_probe_artifact(
    tmp_path: Path, capsys: object
) -> None:
    config_path = _write_config(tmp_path)
    config = campaign.load_campaign_config(config_path)
    assert (
        campaign.main(["status", "--config", str(config_path), "--status-out", str(config_path)])
        == 2
    )
    assert "aliases" in capsys.readouterr().err

    shared = tmp_path / "shared-operational.json"
    assert (
        campaign.main(
            [
                "capture-next",
                "--config",
                str(config_path),
                "--probe-report-out",
                str(shared),
                "--status-out",
                str(shared),
            ]
        )
        == 2
    )
    assert "aliases" in capsys.readouterr().err
    assert not shared.exists()
    assert not config.leaves[0].report_path.exists()


def test_online_probe_stops_on_current_identity_match_and_closes_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    leaf = config.leaves[0]
    times = iter((10.0, 10.0, 10.1, 10.1, 10.2))

    class Provider:
        instance: Provider | None = None

        def __init__(self, provider_config: object) -> None:
            self.config = provider_config
            self.samples = 0
            self.closed = False
            Provider.instance = self

        def snapshot(self, *, now_s: float) -> object:
            assert now_s >= 10.0
            self.samples += 1
            return object()

        def diagnostics(self, *, now_s: float) -> dict[str, object]:
            return {
                "configured_endpoint": leaf.endpoint,
                "resolved_endpoint": "COM3",
                "read_only": True,
                "hardware_control_enabled": False,
                "telemetry_configuration_enabled": False,
                "messages_received": self.samples,
                "messages_transmitted": self.samples,
                "transport_link_healthy": True,
                "qualified_link_healthy": True,
                "qualification": {"required": True, "passed": True, "reasons": []},
                "autopilot_version_requests": {
                    "attempts": self.samples,
                    "response_observed": self.samples >= 2,
                },
                "heartbeat_identity": {"source_system_id": 1},
                "autopilot_version_identity": (
                    {"identity_hash": "a" * 64} if self.samples >= 2 else None
                ),
            }

        def close(self) -> None:
            self.closed = True

    def binding(**kwargs: object) -> dict[str, object]:
        diagnostics = kwargs["diagnostics"]
        passed = diagnostics["autopilot_version_identity"] is not None
        return {
            "checks": {
                "heartbeat_identity_exact": True,
                "current_firmware_identity_exact": passed,
            },
            "gate_passed": passed,
            "binding_sha256": "b" * 64,
        }

    monkeypatch.setattr(campaign, "PixhawkReadOnlyTelemetryProvider", Provider)
    monkeypatch.setattr(campaign, "build_pixhawk_covariance_mode_binding", binding)

    exit_code, result = campaign._online_probe_leaf(
        leaf,
        samples=5,
        interval_seconds=0.1,
        clock=lambda: next(times),
        sleeper=lambda seconds: None,
    )

    assert exit_code == 0
    assert result["gate_passed"] is True
    assert result["probe"]["sample_count"] == 2
    assert result["identity_query"]["attempts"] == 2
    assert result["identity_query"]["budget_valid"] is True
    assert result["claims"]["hardware_acceptance"] is False
    assert Provider.instance is not None and Provider.instance.closed is True
    assert Provider.instance.config.request_ranging_streams is False
    assert Provider.instance.config.request_autopilot_version is True


def test_online_probe_fails_closed_after_bounded_identity_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    leaf = config.leaves[0]

    class Provider:
        def __init__(self, provider_config: object) -> None:
            self.config = provider_config
            self.samples = 0
            self.closed = False

        def snapshot(self, *, now_s: float) -> object:
            del now_s
            self.samples += 1
            return object()

        def diagnostics(self, *, now_s: float) -> dict[str, object]:
            del now_s
            attempts = min(self.samples, 3)
            return {
                "configured_endpoint": leaf.endpoint,
                "resolved_endpoint": "COM3",
                "read_only": True,
                "hardware_control_enabled": False,
                "telemetry_configuration_enabled": False,
                "messages_received": self.samples,
                "messages_transmitted": attempts,
                "transport_link_healthy": True,
                "qualified_link_healthy": False,
                "qualification": {"required": True, "passed": None, "reasons": []},
                "autopilot_version_requests": {
                    "attempts": attempts,
                    "response_observed": False,
                },
                "heartbeat_identity": {},
                "autopilot_version_identity": None,
            }

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(campaign, "PixhawkReadOnlyTelemetryProvider", Provider)
    monkeypatch.setattr(
        campaign,
        "build_pixhawk_covariance_mode_binding",
        lambda **kwargs: {
            "checks": {
                "heartbeat_identity_exact": False,
                "current_firmware_identity_exact": False,
            },
            "gate_passed": False,
        },
    )

    exit_code, result = campaign._online_probe_leaf(
        leaf,
        samples=4,
        interval_seconds=0.1,
        clock=iter((10.0, 10.0, 10.1, 10.2, 10.3, 10.4)).__next__,
        sleeper=lambda seconds: None,
    )

    assert exit_code == 1
    assert result["gate_passed"] is False
    assert result["identity_query"]["attempts"] == 3
    assert result["identity_query"]["budget_valid"] is True
    assert result["reasons"] == [
        "mode_binding:current_firmware_identity_exact",
        "mode_binding:heartbeat_identity_exact",
    ]


def test_capture_next_online_probe_failure_never_starts_capture_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    called = False
    monkeypatch.setattr(campaign, "campaign_preflight", _ready_preflight)
    monkeypatch.setattr(
        campaign,
        "probe_next",
        lambda config, **kwargs: (1, {"gate_passed": False, "reasons": ["mismatch"]}),
    )

    def run(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(campaign.subprocess, "run", run)

    exit_code, result = campaign.capture_next(
        config,
        samples=30,
        interval_seconds=0.2,
        minimum_odometry_messages=10,
        minimum_accepted_covariances=5,
        dry_run=False,
    )

    assert exit_code == 1
    assert result["capture_started"] is False
    assert called is False


def test_config_rejects_duplicate_leaf_even_with_six_entries(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][-1]["runtime_mode"] = document["entries"][0]["runtime_mode"]
    document["entries"][-1]["vehicle_class"] = document["entries"][0]["vehicle_class"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(campaign.FieldCovarianceCampaignError, match="duplicate"):
        campaign.load_campaign_config(path)


def test_config_schema_rejects_unknown_entry_field(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["entries"][0]["claim"] = "trust-me"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(campaign.FieldCovarianceCampaignError, match="schema validation"):
        campaign.load_campaign_config(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"samples": 0}, "samples"),
        ({"interval_seconds": float("nan")}, "interval_seconds"),
        ({"minimum_odometry_messages": -1}, "minimum_odometry_messages"),
        ({"minimum_accepted_covariances": 0}, "minimum_accepted_covariances"),
    ],
)
def test_capture_next_rejects_invalid_budget_before_transport(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    config = campaign.load_campaign_config(_write_config(tmp_path))
    values: dict[str, object] = {
        "samples": 30,
        "interval_seconds": 0.2,
        "minimum_odometry_messages": 10,
        "minimum_accepted_covariances": 5,
        "dry_run": False,
    }
    values.update(overrides)

    with pytest.raises(campaign.FieldCovarianceCampaignError, match=message):
        campaign.capture_next(config, **values)
