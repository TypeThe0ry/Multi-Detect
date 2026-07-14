from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import multidetect.cli as cli_module
from multidetect.cli import main
from multidetect.compat import UTC
from multidetect.payload_bench_evidence import (
    check_inert_payload_hardware_bench,
    sign_payload_bench_message,
)

NOW = datetime(2026, 7, 13, 6, 30, tzinfo=UTC)
CONTROLLER_KEY = b"controller-hardware-bench-key-material-v1"
SENSOR_KEY = b"independent-sensor-bench-key-material-v1"


def _common(message_type: str, source_id: str, key_id: str, cycle_id: str, sequence: int):
    return {
        "protocol_version": 1,
        "message_type": message_type,
        "bench_id": "inert-bench-001",
        "cycle_id": cycle_id,
        "source_id": source_id,
        "key_id": key_id,
        "sequence": sequence,
        "observed_at_utc": NOW.isoformat(),
        "hardware_observed": True,
        "simulation_only": False,
        "inert_load": True,
    }


def _logs(tmp_path: Path, *, confirmed_cycles: int = 20, include_uncertain: bool = True):
    controller = []
    sensor = []
    for index in range(1, confirmed_cycles + 1):
        cycle_id = f"cycle-{index:03d}"
        controller.append(
            sign_payload_bench_message(
                _common("controller_cycle", "controller-1", "controller-key-v1", cycle_id, index)
                | {
                    "status": "executed",
                    "controller_healthy": True,
                    "interlock_healthy": True,
                    "automatic_retry_count": 0,
                    "firmware_version": "controller-fw-1.0.0",
                },
                hmac_key=CONTROLLER_KEY,
            )
        )
        sensor.append(
            sign_payload_bench_message(
                _common("sensor_confirmation", "sensor-1", "sensor-key-v1", cycle_id, index)
                | {"payload_absent": True, "sensor_healthy": True},
                hmac_key=SENSOR_KEY,
            )
        )
    if include_uncertain:
        controller.append(
            sign_payload_bench_message(
                _common(
                    "controller_cycle",
                    "controller-1",
                    "controller-key-v1",
                    "fault-injection-001",
                    confirmed_cycles + 1,
                )
                | {
                    "status": "uncertain",
                    "controller_healthy": False,
                    "interlock_healthy": True,
                    "automatic_retry_count": 0,
                    "firmware_version": "controller-fw-1.0.0",
                },
                hmac_key=CONTROLLER_KEY,
            )
        )
    controller_path = tmp_path / "controller.jsonl"
    sensor_path = tmp_path / "sensor.jsonl"
    controller_path.write_text(
        "".join(json.dumps(item) + "\n" for item in controller), encoding="utf-8"
    )
    sensor_path.write_text("".join(json.dumps(item) + "\n" for item in sensor), encoding="utf-8")
    return controller_path, sensor_path


def _check(controller: Path, sensor: Path, **changes):
    arguments = {
        "controller_log": controller,
        "sensor_log": sensor,
        "controller_hmac_key": CONTROLLER_KEY,
        "sensor_hmac_key": SENSOR_KEY,
        "bench_id": "inert-bench-001",
        "controller_id": "controller-1",
        "sensor_id": "sensor-1",
        "controller_key_id": "controller-key-v1",
        "sensor_key_id": "sensor-key-v1",
        "inert_load_only": True,
        "people_excluded_from_test_area": True,
        "minimum_confirmed_cycles": 20,
        "now": NOW,
    }
    arguments.update(changes)
    return check_inert_payload_hardware_bench(**arguments)


def test_payload_bench_accepts_20_confirmed_cycles_and_uncertain_no_retry(tmp_path: Path) -> None:
    controller, sensor = _logs(tmp_path)

    result = _check(controller, sensor)

    assert result["event"] == "inert_payload_hardware_bench_passed"
    assert result["passed"] is True
    assert result["confirmed_cycles"] == 20
    assert result["uncertain_fault_injection_cycles"] == 1
    assert result["independent_confirmation_verified"] is True
    assert result["uncertain_result_no_retry_verified"] is True
    assert result["command_channel_present"] is False
    assert result["physical_release_approved"] is False


def test_payload_bench_rejects_missing_uncertain_fault_injection(tmp_path: Path) -> None:
    controller, sensor = _logs(tmp_path, include_uncertain=False)

    result = _check(controller, sensor)

    assert result["passed"] is False
    assert result["uncertain_result_no_retry_verified"] is False
    assert "no uncertain-result fault injection" in " ".join(result["reasons"])


def test_payload_bench_rejects_tampered_controller_record(tmp_path: Path) -> None:
    controller, sensor = _logs(tmp_path)
    records = controller.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(records[0])
    tampered["automatic_retry_count"] = 1
    records[0] = json.dumps(tampered)
    controller.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="authentication failed"):
        _check(controller, sensor)


def test_payload_bench_requires_operator_inert_and_people_exclusion_declarations(
    tmp_path: Path,
) -> None:
    controller, sensor = _logs(tmp_path)

    result = _check(
        controller,
        sensor,
        inert_load_only=False,
        people_excluded_from_test_area=False,
    )

    assert result["passed"] is False
    assert "inert-load-only" in " ".join(result["reasons"])
    assert "people exclusion" in " ".join(result["reasons"])


def test_payload_bench_cli_writes_hardware_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    controller, sensor = _logs(tmp_path)
    output = tmp_path / "evidence.json"
    monkeypatch.setenv("CONTROLLER_BENCH_KEY", CONTROLLER_KEY.decode())
    monkeypatch.setenv("SENSOR_BENCH_KEY", SENSOR_KEY.decode())
    monkeypatch.setattr(
        cli_module,
        "check_inert_payload_hardware_bench",
        lambda **kwargs: check_inert_payload_hardware_bench(**kwargs, now=NOW),
    )

    assert (
        main(
            [
                "inert-payload-bench-check",
                "--controller-log",
                str(controller),
                "--sensor-log",
                str(sensor),
                "--controller-hmac-key-env",
                "CONTROLLER_BENCH_KEY",
                "--sensor-hmac-key-env",
                "SENSOR_BENCH_KEY",
                "--bench-id",
                "inert-bench-001",
                "--controller-id",
                "controller-1",
                "--sensor-id",
                "sensor-1",
                "--controller-key-id",
                "controller-key-v1",
                "--sensor-key-id",
                "sensor-key-v1",
                "--inert-load-only",
                "--people-excluded-from-test-area",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    emitted = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == emitted
    assert emitted["confirmed_cycles"] == 20
    assert emitted["command_channel_present"] is False
