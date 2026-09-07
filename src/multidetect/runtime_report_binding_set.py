"""Verify a deterministic set of runtime result/report bindings.

``runtime_report_binding`` proves that one producer result and one persisted
``RuntimeRunReport`` describe the same execution.  This module adds the next
audit boundary: a reviewer can bind several mode-specific pairs together and
prove that the required runtime modes are covered without reusing a result or
sidecar across modes.

The set verifier is deliberately a software identity gate.  It does not start
a runtime, connect to a vehicle, or turn a Replay/SIM record into HIL/REAL or
flight acceptance.  Unknown result events, malformed entries, missing modes,
duplicate modes and source reuse all remain fail-closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .runtime_report_binding import (
    RuntimeReportBindingError,
    verify_runtime_report_binding,
)

SCHEMA = "multidetect.runtime-report-binding-set"
SCHEMA_VERSION = 1
EVENT = "runtime_report_binding_set_verified"
ERROR_EVENT = "runtime_report_binding_set_error"
ALLOWED_MODES = ("replay", "sim", "hil", "real")
DEFAULT_REQUIRED_MODES = ALLOWED_MODES
_MODE_ORDER = {mode: index for index, mode in enumerate(ALLOWED_MODES)}
_CHECK_NAMES = (
    "required_modes_valid",
    "entries_have_required_fields",
    "required_modes_complete",
    "entries_unique",
    "mode_declarations_match",
    "result_paths_distinct",
    "runtime_report_paths_distinct",
    "source_paths_distinct",
    "all_bindings_passed",
    "no_errors",
)
_CLAIM_BOUNDARY = {
    "software_runtime_identity_only": True,
    "hardware_acceptance": "not_claimed",
    "hil_acceptance": "not_claimed",
    "real_acceptance": "not_claimed",
    "flight_acceptance": "not_claimed",
}


class RuntimeReportBindingSetError(ValueError):
    """Raised when a binding-set input is structurally unsafe."""


def _normalise_mode(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeReportBindingSetError(f"{label} must be a string")
    mode = value.strip().lower()
    if mode not in ALLOWED_MODES:
        raise RuntimeReportBindingSetError(f"{label} must be one of {', '.join(ALLOWED_MODES)}")
    return mode


def _normalise_modes(values: Sequence[object], *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise RuntimeReportBindingSetError(f"{label} must be an array")
    modes = tuple(
        _normalise_mode(value, label=f"{label}[{index}]") for index, value in enumerate(values)
    )
    if not modes:
        raise RuntimeReportBindingSetError(f"{label} must contain at least one mode")
    if len(set(modes)) != len(modes):
        raise RuntimeReportBindingSetError(f"{label} must not contain duplicate modes")
    return tuple(sorted(modes, key=_MODE_ORDER.__getitem__))


def _require_bool(value: object, *, label: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise RuntimeReportBindingSetError(f"{label} must be a boolean")
    return value


def _resolve_source(value: object, *, label: str, base_directory: Path) -> tuple[Path, str]:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeReportBindingSetError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    try:
        if candidate.is_symlink():
            raise RuntimeReportBindingSetError(f"{label} must not be a symbolic link")
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base_directory)
    except RuntimeReportBindingSetError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeReportBindingSetError(
            f"{label} must resolve inside base directory {base_directory}"
        ) from exc
    try:
        relative = resolved.relative_to(base_directory).as_posix()
    except ValueError as exc:  # pragma: no cover - guarded above
        raise RuntimeReportBindingSetError(f"{label} is outside base directory") from exc
    return resolved, relative


def _source_info(path: Path, *, relative: str) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeReportBindingSetError(f"source file does not exist: {relative}")
    raw = path.read_bytes()
    if not raw:
        raise RuntimeReportBindingSetError(f"source file is empty: {relative}")
    return {
        "path": relative,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _entry_spec(
    raw: Mapping[str, Any],
    *,
    index: int,
    base_directory: Path,
    require_complete_modes: set[str],
    require_embedded_modes: set[str],
) -> dict[str, object]:
    allowed = {
        "runtime_mode",
        "result_path",
        "runtime_report_path",
        "result",
        "runtime_report",
        "require_complete",
        "require_embedded_report",
    }
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise RuntimeReportBindingSetError(f"bindings[{index}] has unknown field(s): {unknown!r}")
    mode = _normalise_mode(raw.get("runtime_mode"), label=f"bindings[{index}].runtime_mode")
    result_value = raw.get("result_path", raw.get("result"))
    report_value = raw.get("runtime_report_path", raw.get("runtime_report"))
    result_path, result_relative = _resolve_source(
        result_value,
        label=f"bindings[{index}].result_path",
        base_directory=base_directory,
    )
    report_path, report_relative = _resolve_source(
        report_value,
        label=f"bindings[{index}].runtime_report_path",
        base_directory=base_directory,
    )
    return {
        "runtime_mode": mode,
        "result_path": result_path,
        "runtime_report_path": report_path,
        "result_relative": result_relative,
        "runtime_report_relative": report_relative,
        "require_complete": _require_bool(
            raw.get("require_complete"),
            label=f"bindings[{index}].require_complete",
            default=mode in require_complete_modes,
        ),
        "require_embedded_report": _require_bool(
            raw.get("require_embedded_report"),
            label=f"bindings[{index}].require_embedded_report",
            default=mode in require_embedded_modes,
        ),
    }


def _relative_binding(binding: Mapping[str, Any], *, base_directory: Path) -> dict[str, object]:
    """Copy a single-binding result while making paths portable."""

    result = dict(binding.get("result", {}))
    report = dict(binding.get("runtime_report", {}))
    for section, value in (("result", result), ("runtime_report", report)):
        raw_path = value.get("path")
        if isinstance(raw_path, str):
            try:
                value["path"] = Path(raw_path).resolve().relative_to(base_directory).as_posix()
            except (OSError, RuntimeError, ValueError):
                value["path"] = raw_path
        if section == "result":
            result = value
        else:
            report = value
    return {
        "result": result,
        "runtime_report": report,
        "expectations": dict(binding.get("expectations", {})),
        "checks": dict(binding.get("checks", {})),
        "gate_passed": binding.get("gate_passed") is True,
        "reasons": list(binding.get("reasons", [])),
    }


def verify_runtime_report_binding_set(
    bindings: Sequence[Mapping[str, Any]],
    *,
    required_modes: Sequence[object] = DEFAULT_REQUIRED_MODES,
    base_directory: str | Path = ".",
    require_complete_modes: Sequence[object] = ("replay",),
    require_embedded_modes: Sequence[object] = ("replay",),
) -> dict[str, Any]:
    """Verify a set of mode-specific result/report pairs.

    ``bindings`` is intentionally an explicit sequence; the verifier never
    discovers files in ``artifacts/``.  Every source is resolved below
    ``base_directory`` and is reopened by ``verify_runtime_report_binding``.
    """

    root = Path(base_directory).expanduser().resolve()
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes, bytearray)):
        raise RuntimeReportBindingSetError("bindings must be an array")
    required = _normalise_modes(required_modes, label="required_modes")
    complete_modes = set(_normalise_modes(require_complete_modes, label="require_complete_modes"))
    embedded_modes = set(_normalise_modes(require_embedded_modes, label="require_embedded_modes"))

    specs: list[dict[str, object]] = []
    for index, raw in enumerate(bindings):
        if not isinstance(raw, Mapping):
            raise RuntimeReportBindingSetError(f"bindings[{index}] must be an object")
        specs.append(
            _entry_spec(
                raw,
                index=index,
                base_directory=root,
                require_complete_modes=complete_modes,
                require_embedded_modes=embedded_modes,
            )
        )

    specs.sort(key=lambda item: _MODE_ORDER[str(item["runtime_mode"])])
    covered_modes = tuple(str(item["runtime_mode"]) for item in specs)
    unique_modes = len(set(covered_modes)) == len(covered_modes)
    mode_complete = set(required).issubset(set(covered_modes))
    entries: list[dict[str, Any]] = []
    for spec in specs:
        mode = str(spec["runtime_mode"])
        result_path = spec["result_path"]
        report_path = spec["runtime_report_path"]
        assert isinstance(result_path, Path)
        assert isinstance(report_path, Path)
        result_relative = str(spec["result_relative"])
        report_relative = str(spec["runtime_report_relative"])
        entry: dict[str, Any] = {
            "runtime_mode": mode,
            "result": _source_info(result_path, relative=result_relative),
            "runtime_report": _source_info(report_path, relative=report_relative),
            "expectations": {
                "require_embedded_report": spec["require_embedded_report"],
                "require_complete": spec["require_complete"],
            },
        }
        try:
            binding = verify_runtime_report_binding(
                result_path,
                report_path,
                expected_runtime_mode=mode,
                require_embedded_report=bool(spec["require_embedded_report"]),
                require_complete=bool(spec["require_complete"]),
            )
            entry["binding"] = _relative_binding(binding, base_directory=root)
            entry["checks"] = dict(binding.get("checks", {}))
            entry["gate_passed"] = binding.get("gate_passed") is True
            entry["reasons"] = list(binding.get("reasons", []))
        except (OSError, RuntimeReportBindingError, TypeError, ValueError) as exc:
            message = str(exc) or type(exc).__name__
            entry["binding"] = None
            entry["checks"] = {name: False for name in _SINGLE_BINDING_CHECK_NAMES}
            entry["gate_passed"] = False
            entry["reasons"] = [f"{type(exc).__name__}: {message}"]
            entry["error"] = {"type": type(exc).__name__, "message": message}
        entries.append(entry)

    result_paths = [str(entry["result"]["path"]) for entry in entries]
    report_paths = [str(entry["runtime_report"]["path"]) for entry in entries]
    all_source_paths = result_paths + report_paths
    declarations_match = all(
        entry["runtime_mode"] == entry.get("binding", {}).get("result", {}).get("runtime_mode")
        for entry in entries
        if isinstance(entry.get("binding"), Mapping)
    ) and all(isinstance(entry.get("binding"), Mapping) for entry in entries)
    checks = {
        "required_modes_valid": True,
        "entries_have_required_fields": all(
            isinstance(entry.get("result"), Mapping)
            and isinstance(entry.get("runtime_report"), Mapping)
            for entry in entries
        ),
        "required_modes_complete": mode_complete,
        "entries_unique": unique_modes,
        "mode_declarations_match": declarations_match,
        "result_paths_distinct": len(result_paths) == len(set(result_paths)),
        "runtime_report_paths_distinct": len(report_paths) == len(set(report_paths)),
        "source_paths_distinct": len(all_source_paths) == len(set(all_source_paths)),
        "all_bindings_passed": bool(entries)
        and all(entry.get("gate_passed") is True for entry in entries),
        "no_errors": all("error" not in entry for entry in entries),
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "required_modes": list(required),
        "covered_modes": sorted(set(covered_modes), key=_MODE_ORDER.__getitem__),
        "binding_count": len(entries),
        "complete": mode_complete,
        "bindings": entries,
        "checks": checks,
        "gate_passed": all(checks.values()),
        "reasons": [name for name in _CHECK_NAMES if not checks[name]],
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }


_SINGLE_BINDING_CHECK_NAMES = (
    "result_json_valid",
    "runtime_report_valid",
    "result_event_supported",
    "result_gate_passed",
    "runtime_mode_matches",
    "descriptor_hash_matches",
    "input_hash_matches",
    "processed_frame_count_matches",
    "processed_frame_ids_match",
    "termination_contract_matches",
    "embedded_report_matches",
    "sim_truth_free",
    "sim_closed",
)


def _write_json(path: Path, document: Mapping[str, Any]) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


def _parse_mode_csv(value: str, *, label: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in value.split(",") if part.strip())
    return _normalise_modes(values, label=label)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding",
        action="append",
        nargs=3,
        metavar=("MODE", "RESULT_JSON", "RUNTIME_REPORT_JSON"),
        required=True,
        help="one mode/result/report tuple; repeat for each mode",
    )
    parser.add_argument(
        "--required-modes",
        default=",".join(DEFAULT_REQUIRED_MODES),
        help="comma-separated required modes (default: replay,sim,hil,real)",
    )
    parser.add_argument(
        "--base-directory",
        type=Path,
        default=Path.cwd(),
        help="root under which all source paths must resolve",
    )
    parser.add_argument(
        "--complete-mode",
        action="append",
        default=[],
        metavar="MODE",
        help="require natural stream exhaustion for this mode; repeatable",
    )
    parser.add_argument(
        "--embedded-mode",
        action="append",
        default=[],
        metavar="MODE",
        help="require an embedded report for this mode; repeatable",
    )
    parser.add_argument("--out", type=Path, help="optional JSON verification report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        required_modes = _parse_mode_csv(args.required_modes, label="required_modes")
        complete_modes = tuple(args.complete_mode) or ("replay",)
        embedded_modes = tuple(args.embedded_mode) or ("replay",)
        specifications = [
            {
                "runtime_mode": values[0],
                "result_path": values[1],
                "runtime_report_path": values[2],
            }
            for values in args.binding
        ]
        document = verify_runtime_report_binding_set(
            specifications,
            required_modes=required_modes,
            base_directory=args.base_directory,
            require_complete_modes=complete_modes,
            require_embedded_modes=embedded_modes,
        )
        exit_code = 0 if document["gate_passed"] else 1
    except (OSError, RuntimeReportBindingSetError, TypeError, ValueError) as exc:
        message = str(exc) or type(exc).__name__
        document = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "event": ERROR_EVENT,
            "required_modes": [],
            "covered_modes": [],
            "binding_count": 0,
            "complete": False,
            "bindings": [],
            "checks": {name: False for name in _CHECK_NAMES},
            "gate_passed": False,
            "reasons": [f"{type(exc).__name__}: {message}"],
            "claim_boundary": dict(_CLAIM_BOUNDARY),
            "error": {"type": type(exc).__name__, "message": message},
        }
        exit_code = 1
    if args.out is not None:
        _write_json(args.out, document)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_MODES",
    "DEFAULT_REQUIRED_MODES",
    "ERROR_EVENT",
    "EVENT",
    "RuntimeReportBindingSetError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_parser",
    "main",
    "verify_runtime_report_binding_set",
]
