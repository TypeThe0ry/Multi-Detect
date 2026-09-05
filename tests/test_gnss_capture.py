from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.gnss_capture import (
    GNSS_CAPTURE_EVIDENCE_TYPE,
    capture_gnss_samples,
    verify_gnss_capture_report,
    verify_main,
    write_gnss_capture_report,
)

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        self.value += 0.1
        return self.value

    def epoch(self) -> float:
        return 1_800_000_000.0 + self.value


class _Message:
    def __init__(self, message_type: str, values: dict[str, object] | None = None) -> None:
        self.message_type = message_type
        self.values = values or {}

    def get_type(self) -> str:
        return self.message_type

    def to_dict(self) -> dict[str, object]:
        return dict(self.values)


class _Connection:
    def __init__(self, messages: list[_Message]) -> None:
        self.messages = list(messages)
        self.calls: list[tuple[bool, float]] = []
        self.closed = False

    def recv_match(self, *, blocking: bool, timeout: float) -> _Message | None:
        self.calls.append((blocking, timeout))
        return self.messages.pop(0) if self.messages else None

    def close(self) -> None:
        self.closed = True


def _valid_raw(**overrides: object) -> _Message:
    values: dict[str, object] = {
        "time_usec": 12_345_678,
        "fix_type": 3,
        "satellites_visible": 12,
        "lat": 1_300_000,
        "lon": 103_800_000,
        "alt": 15_000,
        "eph": 120,
        "epv": 250,
        "vel": 325,
        "cog": 9_000,
    }
    values.update(overrides)
    return _Message("GPS_RAW_INT", values)


def test_capture_preserves_raw_coordinate_quality_and_stays_receive_only() -> None:
    clock = _Clock()
    connection = _Connection(
        [
            _valid_raw(),
            _Message(
                "GLOBAL_POSITION_INT",
                {
                    "lat": 1_300_000,
                    "lon": 103_800_000,
                    "alt": 15_000,
                    "relative_alt": 12_000,
                    "vx": 100,
                    "vy": -50,
                    "vz": 0,
                    "hdg": 9_000,
                },
            ),
            _Message("HEARTBEAT"),
        ]
    )
    calls: list[dict[str, object]] = []

    def factory(endpoint: str, **kwargs: object) -> _Connection:
        calls.append({"endpoint": endpoint, **kwargs})
        return connection

    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.8,
        connection_factory=factory,
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
    )

    assert calls == [
        {
            "endpoint": "udpin:127.0.0.1:14562",
            "autoreconnect": False,
            "robust_parsing": True,
        }
    ]
    assert connection.closed is True
    assert report["read_only"] is True
    assert report["messages_transmitted"] == 0
    assert report["contains_truth"] is False
    assert report["summary"]["gps_raw_samples"] == 1  # type: ignore[index]
    assert report["summary"]["gps_raw_qualified_samples"] == 1  # type: ignore[index]
    raw = report["records"][0]  # type: ignore[index]
    assert raw["latitude_deg"] == pytest.approx(0.13)  # type: ignore[index]
    assert raw["longitude_deg"] == pytest.approx(10.38)  # type: ignore[index]
    assert raw["altitude_m"] == pytest.approx(15.0)  # type: ignore[index]
    assert raw["horizontal_accuracy_m"] == pytest.approx(1.2)  # type: ignore[index]
    assert raw["vertical_accuracy_m"] == pytest.approx(2.5)  # type: ignore[index]
    assert raw["qualified"] is True  # type: ignore[index]
    assert report["summary"]["gate_passed"] is True  # type: ignore[index]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"satellites_visible": 255}, "satellites_unknown"),
        ({"lat": 900_000_000}, "coordinates_invalid"),
        ({"fix_type": 2}, "fix_type_insufficient"),
        ({"eph": 65535}, "horizontal_accuracy_unknown"),
        ({"epv": 65535}, "vertical_accuracy_unknown"),
    ],
)
def test_capture_marks_wire_quality_failures_without_reusing_old_values(
    overrides: dict[str, object], reason: str
) -> None:
    clock = _Clock()
    connection = _Connection([_valid_raw(**overrides)])
    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.4,
        connection_factory=lambda *_args, **_kwargs: connection,
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
    )
    raw = report["records"][0]  # type: ignore[index]
    assert raw["qualified"] is False  # type: ignore[index]
    assert raw["qualification_reason"] == reason  # type: ignore[index]
    assert report["summary"]["gps_raw_qualified_samples"] == 0  # type: ignore[index]
    assert report["summary"]["gate_passed"] is False  # type: ignore[index]


def test_writer_and_verifier_recompute_counts_and_protect_existing_file(tmp_path: Path) -> None:
    clock = _Clock()
    connection = _Connection([_valid_raw()])
    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.4,
        connection_factory=lambda *_args, **_kwargs: connection,
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
    )
    output = tmp_path / "gnss.json"
    assert write_gnss_capture_report(output, report) == output.resolve()
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["evidence_type"] == GNSS_CAPTURE_EVIDENCE_TYPE
    verified = verify_gnss_capture_report(output, require_gate_passed=True)
    assert verified["passed"] is True
    assert verified["gate_passed"] is True
    document["summary"]["gps_raw_samples"] = 99
    assert verify_gnss_capture_report(document)["passed"] is False
    with pytest.raises(FileExistsError):
        write_gnss_capture_report(output, report)
    write_gnss_capture_report(output, report, overwrite=True)


def test_verifier_rejects_tampered_quality_and_decode_contract(tmp_path: Path) -> None:
    clock = _Clock()
    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.4,
        connection_factory=lambda *_args, **_kwargs: _Connection([_valid_raw()]),
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
    )
    output = tmp_path / "gnss.json"
    write_gnss_capture_report(output, report)
    document = json.loads(output.read_text(encoding="utf-8"))

    document["summary"]["qualified_sample_gate_passed"] = False
    document["summary"]["decode_error_count"] = 1
    document["decode_errors"] = "forged"
    document["records"][0]["qualified"] = False
    document["records"][0]["qualification_reason"] = None
    verified = verify_gnss_capture_report(document)

    assert verified["passed"] is False
    assert verified["gate_passed"] is False
    assert any("qualified and qualification_reason" in reason for reason in verified["reasons"])
    assert any("decode_errors must be an array" in reason for reason in verified["reasons"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_horizontal_accuracy_m": 6.1},
        {"max_vertical_accuracy_m": 10.1},
        {"max_age_s": 1.6},
    ],
)
def test_relaxed_capture_thresholds_never_pass_canonical_policy_gate(
    overrides: dict[str, float],
) -> None:
    clock = _Clock()
    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.4,
        connection_factory=lambda *_args, **_kwargs: _Connection([_valid_raw()]),
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
        **overrides,
    )

    assert report["summary"]["qualified_sample_gate_passed"] is True  # type: ignore[index]
    assert report["summary"]["threshold_policy_passed"] is False  # type: ignore[index]
    assert report["summary"]["gate_passed"] is False  # type: ignore[index]
    verified = verify_gnss_capture_report(report)
    assert verified["passed"] is True
    assert verified["gate_passed"] is False


def test_verify_main_checks_a_saved_report_without_opening_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    report = capture_gnss_samples(
        "udpin:127.0.0.1:14562",
        duration_seconds=0.4,
        connection_factory=lambda *_args, **_kwargs: _Connection([_valid_raw()]),
        monotonic_clock=clock.monotonic,
        epoch_clock=clock.epoch,
    )
    output = tmp_path / "gnss.json"
    write_gnss_capture_report(output, report)

    assert verify_main([str(output), "--require-gate"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["passed"] is True
    assert rendered["gate_passed"] is True


def test_saved_report_verifier_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"event":"one","event":"two"}\n', encoding="utf-8")

    verified = verify_gnss_capture_report(duplicate)

    assert verified["passed"] is False
    assert verified["gate_passed"] is False
    assert any("duplicate JSON object key" in reason for reason in verified["reasons"])


def test_capture_schema_is_strict_and_wrapper_help_works_without_pythonpath() -> None:
    schema_path = ROOT / "configs" / "schemas" / "gnss-capture.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["evidence_type"]["const"] == GNSS_CAPTURE_EVIDENCE_TYPE
    assert schema["properties"]["messages_transmitted"]["const"] == 0
    assert "threshold_policy_passed" in schema["$defs"]["summary"]["required"]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for script, marker in (
        ("capture_gnss_samples.py", "--minimum-qualified-samples"),
        ("verify_gnss_capture.py", "--require-gate"),
    ):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--help"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, (script, completed.stderr)
        assert marker in completed.stdout
