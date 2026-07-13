from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
from multidetect.cli import main
from multidetect.domain import VehicleTelemetry
from multidetect.pixhawk_bench import (
    PixhawkBenchConfig,
    load_qgc_telemetry_snapshot,
    run_pixhawk_v6x_bench,
)

NOW = datetime(2026, 7, 13, 5, 0, tzinfo=UTC)


def _telemetry(**changes: object) -> VehicleTelemetry:
    value = VehicleTelemetry(
        altitude_agl_m=12.5,
        roll_deg=1.0,
        pitch_deg=-2.0,
        ground_speed_mps=0.2,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        latitude_deg=31.123456,
        longitude_deg=121.654321,
        heading_deg=359.0,
        battery_remaining_pct=73.0,
        satellites_visible=17,
        armed=False,
        flight_mode="AUTO",
        mission_sequence=4,
    )
    return replace(value, **changes)


def _qgc(captured_at: datetime = NOW) -> dict:
    return {
        "schema_version": 1,
        "hardware_model": "Pixhawk V6X",
        "firmware_version": "ArduPilot 4.6.1",
        "captured_at_utc": captured_at.isoformat(),
        "airframe_stationary": True,
        "fields": {
            "latitude_deg": 31.123456,
            "longitude_deg": 121.654321,
            "altitude_agl_m": 12.5,
            "heading_deg": 1.0,
            "ground_speed_mps": 0.2,
            "roll_deg": 1.0,
            "pitch_deg": -2.0,
            "battery_remaining_pct": 73.0,
            "satellites_visible": 17,
            "armed": False,
            "flight_mode": "auto",
            "mission_sequence": 4,
        },
    }


class _Provider:
    is_read_only = True
    messages_transmitted = 0
    config = SimpleNamespace(stale_after_seconds=1.0)

    def __init__(self, telemetry: VehicleTelemetry | None = None) -> None:
        self.telemetry = telemetry or _telemetry()

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        del now_s
        return self.telemetry

    def cached_snapshot(self, *, now_s: float) -> VehicleTelemetry:
        del now_s
        return replace(self.telemetry, link_healthy=False, position_healthy=False)


def test_pixhawk_bench_passes_read_only_qgc_and_staleness_checks() -> None:
    clock_value = 10.0

    def clock() -> float:
        nonlocal clock_value
        clock_value += 0.1
        return clock_value

    result = run_pixhawk_v6x_bench(
        _Provider(),
        _qgc(),
        PixhawkBenchConfig(minimum_samples=3, sample_interval_seconds=0),
        clock=clock,
        sleeper=lambda _seconds: None,
        observed_at=lambda: NOW,
    )

    assert result["event"] == "pixhawk_v6x_bench_passed"
    assert result["passed"] is True
    assert result["fresh_sample_count"] == 3
    assert result["messages_transmitted_by_jetson"] == 0
    assert result["qgc_field_match"] is True
    assert result["link_loss_fail_closed"] is True
    assert result["link_loss_method"] == "cached_staleness_without_receive"
    assert result["physical_release_enabled"] is False


def test_pixhawk_bench_rejects_qgc_field_mismatch_and_transmission() -> None:
    class _UnsafeProvider(_Provider):
        messages_transmitted = 1

    qgc = _qgc()
    qgc["fields"]["latitude_deg"] = 30.0

    result = run_pixhawk_v6x_bench(
        _UnsafeProvider(),
        qgc,
        PixhawkBenchConfig(minimum_samples=1, sample_interval_seconds=0),
        clock=lambda: 10.0,
        sleeper=lambda _seconds: None,
        observed_at=lambda: NOW,
    )

    assert result["passed"] is False
    assert result["qgc_field_match"] is False
    assert "Jetson transmitted messages to Pixhawk" in result["reasons"]
    assert "Pixhawk telemetry does not match the QGC bench snapshot" in result["reasons"]


def test_pixhawk_bench_rejects_stale_qgc_and_incomplete_fresh_samples() -> None:
    result = run_pixhawk_v6x_bench(
        _Provider(_telemetry(position_healthy=False)),
        _qgc(NOW - timedelta(minutes=3)),
        PixhawkBenchConfig(
            minimum_samples=2,
            sample_interval_seconds=0,
            maximum_qgc_age_seconds=120,
        ),
        clock=lambda: 10.0,
        sleeper=lambda _seconds: None,
        observed_at=lambda: NOW,
    )

    assert result["passed"] is False
    assert result["fresh_sample_count"] == 0
    assert "required number of fresh" in " ".join(result["reasons"])
    assert "QGC telemetry snapshot is stale or in the future" in result["reasons"]


def test_qgc_snapshot_loader_validates_stationary_complete_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "qgc.json"
    path.write_text(json.dumps(_qgc()), encoding="utf-8")

    loaded = load_qgc_telemetry_snapshot(path)

    assert loaded["hardware_model"] == "Pixhawk V6X"
    assert loaded["fields"]["mission_sequence"] == 4

    loaded["airframe_stationary"] = False
    path.write_text(json.dumps(loaded), encoding="utf-8")
    with pytest.raises(ValueError, match="airframe_stationary"):
        load_qgc_telemetry_snapshot(path)


def test_pixhawk_bench_cli_writes_machine_readable_evidence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    qgc_path = tmp_path / "qgc.json"
    output = tmp_path / "pixhawk.json"
    qgc_path.write_text(json.dumps(_qgc()), encoding="utf-8")

    class _CliProvider:
        def __init__(self, config) -> None:
            assert config.endpoint == "udp:127.0.0.1:14550"

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "PixhawkReadOnlyTelemetryProvider", _CliProvider)
    monkeypatch.setattr(
        cli_module,
        "run_pixhawk_v6x_bench",
        lambda *_args: {
            "event": "pixhawk_v6x_bench_passed",
            "passed": True,
            "messages_transmitted_by_jetson": 0,
        },
    )

    assert (
        main(
            [
                "pixhawk-v6x-bench",
                "--endpoint",
                "udp:127.0.0.1:14550",
                "--qgc-snapshot",
                str(qgc_path),
                "--minimum-samples",
                "3",
                "--sample-interval-seconds",
                "0",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    emitted = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == emitted
    assert emitted["messages_transmitted_by_jetson"] == 0
