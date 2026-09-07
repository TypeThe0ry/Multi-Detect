"""Canonical receive-only HIL/REAL runtime observation result builder.

The runtime-report binding verifier deliberately consumes a small, explicit
result shape for bounded HIL/REAL observation captures.  This module gives
producers a typed way to create that shape from the same ``RuntimeRunReport``
that is written as the sidecar.  Descriptor/input hashes, frame identity and
termination fields are therefore derived once instead of being copied into a
second hand-written mapping.

The builder is receive-only by construction: it emits the safety fields used
by ``runtime_report_binding`` with zero application messages transmitted and
all flight/hardware output paths locked.  A Pixhawk capture that sends
telemetry request messages remains a different producer contract and should
not be converted to this result without an explicit control-vs-telemetry
counter split.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_bool,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .runtime import (
    RuntimeMode,
    RuntimeRunReport,
    load_runtime_run_report,
    write_runtime_run_report,
)

SCHEMA = "multidetect.runtime-observation-result"
SCHEMA_VERSION = 1
_OBSERVATION_MODES = frozenset({RuntimeMode.HIL, RuntimeMode.REAL})
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "event",
        "runtime_mode",
        "gate_passed",
        "runtime_descriptor_hash",
        "runtime_input_hash",
        "processed_frame_count",
        "processed_frame_ids",
        "stream_exhausted",
        "max_frames",
        "observation_only",
        "read_only",
        "receive_only",
        "flight_control_enabled",
        "hardware_outputs_present",
        "messages_transmitted",
        "output_locked",
        "runtime_run_report",
    }
)


def _observation_mode(value: object) -> RuntimeMode:
    if isinstance(value, RuntimeMode):
        mode = value
    else:
        try:
            mode = RuntimeMode(require_text(value, "runtime_mode").lower())
        except (AutonomyContractError, ValueError) as exc:
            raise AutonomyContractError("runtime_mode must be hil or real") from exc
    if mode not in _OBSERVATION_MODES:
        raise AutonomyContractError("runtime_mode must be hil or real")
    return mode


def _validate_report_termination(report: RuntimeRunReport) -> None:
    """Mirror the explicit termination relation enforced by the binding verifier."""

    if report.stream_exhausted and report.max_frames is not None:
        raise AutonomyContractError(
            "stream_exhausted observation reports must set max_frames to null"
        )
    if not report.stream_exhausted and report.max_frames is None:
        raise AutonomyContractError(
            "bounded observation reports must set max_frames to a non-negative integer"
        )


class RuntimeObservationResultError(AutonomyContractError):
    """Raised when a receive-only HIL/REAL result violates its typed contract."""


@dataclass(frozen=True, slots=True, init=False)
class RuntimeObservationResult:
    """Hash-bound result document for one receive-only HIL/REAL observation run.

    ``runtime_run_report`` is the single source of truth for descriptor/input
    identity, processed frame IDs and stream termination.  The public mapping
    mirrors the v186 result contract consumed by ``runtime_report_binding``.
    """

    mode: RuntimeMode
    runtime_run_report: RuntimeRunReport
    gate_passed: bool
    schema_version: int

    def __init__(
        self,
        mode: RuntimeMode,
        runtime_run_report: RuntimeRunReport,
        *,
        gate_passed: bool = True,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        object.__setattr__(self, "mode", _observation_mode(mode))
        if not isinstance(runtime_run_report, RuntimeRunReport):
            raise TypeError("runtime_run_report must be a RuntimeRunReport")
        _validate_report_termination(runtime_run_report)
        object.__setattr__(self, "runtime_run_report", runtime_run_report)
        object.__setattr__(self, "gate_passed", require_bool(gate_passed, "gate_passed"))
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(schema_version, expected=SCHEMA_VERSION),
        )

    @property
    def event(self) -> str:
        return f"{self.mode.value}_runtime_observation_finished"

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        report = self.runtime_run_report
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "runtime_mode": self.mode.value,
            "gate_passed": self.gate_passed,
            "runtime_descriptor_hash": report.descriptor_hash,
            "runtime_input_hash": report.input_hash,
            "processed_frame_count": report.processed_frame_count,
            "processed_frame_ids": list(report.processed_frame_ids),
            "stream_exhausted": report.stream_exhausted,
            "max_frames": report.max_frames,
            "observation_only": True,
            "read_only": True,
            "receive_only": True,
            "flight_control_enabled": False,
            "hardware_outputs_present": False,
            "messages_transmitted": 0,
            "output_locked": True,
            "runtime_run_report": report.to_mapping_with_hash(),
        }

    def to_mapping_with_hash(self) -> dict[str, Any]:
        """Return the result mapping with an optional canonical ``sha256`` field."""

        result = self.to_mapping()
        result["sha256"] = self.canonical_hash
        return result

    @classmethod
    def from_mapping(cls, raw: object) -> RuntimeObservationResult:
        """Strictly parse and re-derive a persisted observation result."""

        try:
            value = strict_object(
                raw,
                name="RuntimeObservationResult",
                required=_REQUIRED_FIELDS,
                optional=frozenset({"sha256"}),
            )
        except AutonomyContractError as exc:
            raise RuntimeObservationResultError(str(exc)) from exc
        report_raw = value["runtime_run_report"]
        try:
            report = RuntimeRunReport.from_mapping(report_raw)
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise RuntimeObservationResultError(f"RuntimeRunReport: {exc}") from exc
        try:
            result = cls(
                _observation_mode(value["runtime_mode"]),
                report,
                gate_passed=require_bool(value["gate_passed"], "gate_passed"),
                schema_version=require_schema_version(
                    value["schema_version"], expected=SCHEMA_VERSION
                ),
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise RuntimeObservationResultError(str(exc)) from exc

        expected = result.to_mapping()
        for name, expected_value in expected.items():
            if value[name] != expected_value:
                raise RuntimeObservationResultError(
                    f"{name} does not match canonical RuntimeObservationResult"
                )
        if "sha256" in value:
            try:
                claimed = require_sha256(value["sha256"], "sha256")
            except AutonomyContractError as exc:
                raise RuntimeObservationResultError(str(exc)) from exc
            if claimed != result.canonical_hash:
                raise RuntimeObservationResultError(
                    "RuntimeObservationResult sha256 does not match canonical data"
                )
        return result


def write_runtime_observation_result(
    path: str | Path,
    result: RuntimeObservationResult,
    *,
    include_hash: bool = False,
) -> Path:
    """Atomically write a deterministic result document.

    ``include_hash`` is opt-in so the default output exactly matches the
    result shape consumed by the v186 binding verifier and existing evidence.
    The sidecar should be written with :func:`write_runtime_run_report`.
    """

    if not isinstance(result, RuntimeObservationResult):
        raise TypeError("result must be a RuntimeObservationResult")
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = result.to_mapping_with_hash() if include_hash else result.to_mapping()
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


def load_runtime_observation_result(path: str | Path) -> RuntimeObservationResult:
    """Read and strictly verify one persisted observation result."""

    source = Path(path).expanduser().resolve()
    try:
        raw = strict_json_loads(
            source.read_text(encoding="utf-8"),
            name="RuntimeObservationResult",
        )
    except OSError as exc:
        raise RuntimeObservationResultError(f"unable to read result: {exc}") from exc
    return RuntimeObservationResult.from_mapping(raw)


def write_runtime_observation_pair(
    result_path: str | Path,
    runtime_report_path: str | Path,
    result: RuntimeObservationResult,
    *,
    include_result_hash: bool = False,
) -> tuple[Path, Path]:
    """Write the result and its matching RuntimeRunReport sidecar."""

    if not isinstance(result, RuntimeObservationResult):
        raise TypeError("result must be a RuntimeObservationResult")
    report_path = write_runtime_run_report(runtime_report_path, result.runtime_run_report)
    result_file = write_runtime_observation_result(
        result_path,
        result,
        include_hash=include_result_hash,
    )
    return result_file, report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_report", type=Path, help="existing RuntimeRunReport sidecar JSON")
    parser.add_argument("--runtime-mode", choices=("hil", "real"), required=True)
    parser.add_argument("--out", type=Path, required=True, help="observation result JSON")
    parser.add_argument(
        "--include-hash",
        action="store_true",
        help="include the optional result sha256 field",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = load_runtime_run_report(args.runtime_report)
        result = RuntimeObservationResult(RuntimeMode(args.runtime_mode), report)
        output = write_runtime_observation_result(
            args.out,
            result,
            include_hash=args.include_hash,
        )
    except (OSError, AutonomyContractError, TypeError, ValueError) as exc:
        print(f"runtime observation build failed: {exc}")
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RuntimeObservationResult",
    "RuntimeObservationResultError",
    "build_parser",
    "main",
    "SCHEMA",
    "SCHEMA_VERSION",
    "load_runtime_observation_result",
    "write_runtime_observation_pair",
    "write_runtime_observation_result",
]
