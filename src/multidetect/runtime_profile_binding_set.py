"""Verify an explicit set of mode-bound runtime profile documents.

``runtime_profiles`` validates one signed profile document and one observed
identity.  This module adds the next software-side preflight boundary: a
reviewer can reopen several profile sidecars together, prove that the
required runtime modes are covered, and verify that HIL/REAL profiles are
signature- and identity-bound while every output decision remains locked.

The verifier is intentionally explicit and read-only.  It never discovers
files, starts a runtime, connects to a vehicle or enables flight control.
Missing modes, duplicate modes, reused profile files, malformed identities,
missing HMAC material and signature/identity mismatches all fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .runtime import RuntimeMode
from .runtime_profiles import (
    RuntimeIdentity,
    RuntimeProfileDocumentError,
    load_runtime_profile_document,
)

SCHEMA = "multidetect.runtime-profile-binding-set"
SCHEMA_VERSION = 1
EVENT = "runtime_profile_binding_set_verified"
ERROR_EVENT = "runtime_profile_binding_set_error"
ALLOWED_MODES = ("replay", "sim", "hil", "real")
DEFAULT_REQUIRED_MODES = ALLOWED_MODES
DEFAULT_SIGNATURE_MODES = ("hil", "real")
DEFAULT_FIRMWARE_IDENTITY_MODES = ("hil", "real")
DEFAULT_OUTPUT_LOCKED_MODES = ALLOWED_MODES
_MODE_ORDER = {mode: index for index, mode in enumerate(ALLOWED_MODES)}
_EXPECTED_PROFILE_IDS = {
    "replay": frozenset({"replay"}),
    "sim": frozenset({"sim"}),
    "hil": frozenset({"hil"}),
    "real": frozenset({"observer", "flight"}),
}
_CHECK_NAMES = (
    "required_modes_valid",
    "entries_have_required_fields",
    "required_modes_complete",
    "entries_unique",
    "mode_declarations_match",
    "profile_paths_distinct",
    "all_required_signatures_valid",
    "all_required_identities_bound",
    "all_outputs_locked",
    "all_profiles_passed",
    "no_errors",
)
_ENTRY_CHECK_NAMES = (
    "profile_document_valid",
    "runtime_mode_matches",
    "profile_kind_matches",
    "signature_policy_matches",
    "key_id_bound",
    "identity_bound",
    "firmware_identity_bound",
    "output_locked",
    "profile_gate_passed",
)
_CLAIM_BOUNDARY = {
    "software_runtime_profile_identity_only": True,
    "hardware_acceptance": "not_claimed",
    "hil_acceptance": "not_claimed",
    "real_acceptance": "not_claimed",
    "flight_acceptance": "not_claimed",
}


class RuntimeProfileBindingSetError(ValueError):
    """Raised when a profile-binding-set input is structurally unsafe."""


def _normalise_mode(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeProfileBindingSetError(f"{label} must be a string")
    mode = value.strip().lower()
    if mode not in ALLOWED_MODES:
        raise RuntimeProfileBindingSetError(f"{label} must be one of {', '.join(ALLOWED_MODES)}")
    return mode


def _normalise_modes(
    values: Sequence[object], *, label: str, allow_empty: bool = False
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise RuntimeProfileBindingSetError(f"{label} must be an array")
    modes = tuple(
        _normalise_mode(value, label=f"{label}[{index}]") for index, value in enumerate(values)
    )
    if not modes and not allow_empty:
        raise RuntimeProfileBindingSetError(f"{label} must contain at least one mode")
    if len(set(modes)) != len(modes):
        raise RuntimeProfileBindingSetError(f"{label} must not contain duplicate modes")
    return tuple(sorted(modes, key=_MODE_ORDER.__getitem__))


def _normalise_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeProfileBindingSetError(f"{label} must be an object")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise RuntimeProfileBindingSetError(f"{label} keys must be non-empty strings")
        result[raw_key.strip().lower()] = raw_value
    return result


def _normalise_key_mapping(value: object, *, label: str) -> dict[str, bytes]:
    raw = _normalise_mapping(value, label=label)
    result: dict[str, bytes] = {}
    for mode, key in raw.items():
        _normalise_mode(mode, label=f"{label} key")
        if not isinstance(key, bytes):
            raise RuntimeProfileBindingSetError(f"{label}[{mode}] must contain bytes")
        result[mode] = key
    return result


def _normalise_expected_key_ids(value: object) -> dict[str, str]:
    raw = _normalise_mapping(value, label="expected_key_ids")
    result: dict[str, str] = {}
    for mode, key_id in raw.items():
        _normalise_mode(mode, label="expected_key_ids key")
        if not isinstance(key_id, str) or not key_id.strip():
            raise RuntimeProfileBindingSetError(
                f"expected_key_ids[{mode}] must be a non-empty string"
            )
        result[mode] = key_id.strip()
    return result


def _resolve_source(value: object, *, label: str, base_directory: Path) -> tuple[Path, str]:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise RuntimeProfileBindingSetError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    try:
        if candidate.is_symlink():
            raise RuntimeProfileBindingSetError(f"{label} must not be a symbolic link")
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base_directory)
    except RuntimeProfileBindingSetError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeProfileBindingSetError(
            f"{label} must resolve inside base directory {base_directory}"
        ) from exc
    try:
        relative = resolved.relative_to(base_directory).as_posix()
    except ValueError as exc:  # pragma: no cover - guarded above
        raise RuntimeProfileBindingSetError(f"{label} is outside base directory") from exc
    return resolved, relative


def _source_info(path: Path, *, relative: str) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeProfileBindingSetError(f"profile source file does not exist: {relative}")
    payload = path.read_bytes()
    if not payload:
        raise RuntimeProfileBindingSetError(f"profile source file is empty: {relative}")
    return {
        "path": relative,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _entry_spec(
    raw: Mapping[str, Any],
    *,
    index: int,
    base_directory: Path,
) -> dict[str, object]:
    allowed = {"runtime_mode", "profile_path", "profile", "observed_identity"}
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise RuntimeProfileBindingSetError(f"bindings[{index}] has unknown field(s): {unknown!r}")
    mode = _normalise_mode(raw.get("runtime_mode"), label=f"bindings[{index}].runtime_mode")
    profile_value = raw.get("profile_path", raw.get("profile"))
    profile_path, profile_relative = _resolve_source(
        profile_value,
        label=f"bindings[{index}].profile_path",
        base_directory=base_directory,
    )
    observed_identity = raw.get("observed_identity")
    if observed_identity is not None and not isinstance(observed_identity, Mapping):
        raise RuntimeProfileBindingSetError(
            f"bindings[{index}].observed_identity must be an object or null"
        )
    return {
        "runtime_mode": mode,
        "profile_path": profile_path,
        "profile_relative": profile_relative,
        "observed_identity": observed_identity,
    }


def _relative_result(result: Mapping[str, Any], *, base_directory: Path) -> dict[str, Any]:
    output = dict(result)
    raw_path = output.get("path")
    if isinstance(raw_path, str):
        try:
            output["path"] = Path(raw_path).resolve().relative_to(base_directory).as_posix()
        except (OSError, RuntimeError, ValueError):
            output["path"] = raw_path
    return output


def _document_key_id(path: Path) -> tuple[bool, str | None]:
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, None
    if not isinstance(raw, Mapping):
        return False, None
    key_id = raw.get("key_id")
    return True, key_id if isinstance(key_id, str) and key_id.strip() else None


def verify_runtime_profile_binding_set(
    bindings: Sequence[Mapping[str, Any]],
    *,
    required_modes: Sequence[object] = DEFAULT_REQUIRED_MODES,
    base_directory: str | Path = ".",
    hmac_keys: Mapping[str, bytes] | None = None,
    expected_key_ids: Mapping[str, str] | None = None,
    require_signature_modes: Sequence[object] = DEFAULT_SIGNATURE_MODES,
    require_firmware_identity_modes: Sequence[object] = DEFAULT_FIRMWARE_IDENTITY_MODES,
    require_output_locked_modes: Sequence[object] = DEFAULT_OUTPUT_LOCKED_MODES,
) -> dict[str, Any]:
    """Verify explicit mode/profile sidecars without enabling any output.

    HMAC keys are supplied in memory by the caller and are never copied into
    the returned document.  For CLI use, map mode names to environment
    variables and let :func:`main` resolve them before calling this function.
    """

    root = Path(base_directory).expanduser().resolve()
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes, bytearray)):
        raise RuntimeProfileBindingSetError("bindings must be an array")
    required = _normalise_modes(required_modes, label="required_modes")
    signature_modes = set(
        _normalise_modes(
            require_signature_modes,
            label="require_signature_modes",
            allow_empty=True,
        )
    )
    firmware_modes = set(
        _normalise_modes(
            require_firmware_identity_modes,
            label="require_firmware_identity_modes",
            allow_empty=True,
        )
    )
    output_modes = set(
        _normalise_modes(
            require_output_locked_modes,
            label="require_output_locked_modes",
            allow_empty=True,
        )
    )
    keys = _normalise_key_mapping(hmac_keys, label="hmac_keys")
    key_ids = _normalise_expected_key_ids(expected_key_ids)
    specs: list[dict[str, object]] = []
    for index, raw in enumerate(bindings):
        if not isinstance(raw, Mapping):
            raise RuntimeProfileBindingSetError(f"bindings[{index}] must be an object")
        specs.append(_entry_spec(raw, index=index, base_directory=root))
    specs.sort(key=lambda item: _MODE_ORDER[str(item["runtime_mode"])])

    covered_modes = tuple(str(item["runtime_mode"]) for item in specs)
    unique_modes = len(set(covered_modes)) == len(covered_modes)
    mode_complete = set(required).issubset(set(covered_modes))
    entries: list[dict[str, Any]] = []
    for spec in specs:
        mode = str(spec["runtime_mode"])
        profile_path = spec["profile_path"]
        assert isinstance(profile_path, Path)
        profile_relative = str(spec["profile_relative"])
        source = _source_info(profile_path, relative=profile_relative)
        identity_raw = spec.get("observed_identity")
        identity: RuntimeIdentity | None = None
        entry_reasons: list[str] = []
        checks = {name: False for name in _ENTRY_CHECK_NAMES}
        document_json_valid, document_key_id = _document_key_id(profile_path)
        checks["profile_document_valid"] = document_json_valid
        if not document_json_valid:
            entry_reasons.append("runtime profile document is not a JSON object")
        identity_input_valid = True
        if identity_raw is not None:
            try:
                identity = RuntimeIdentity.from_mapping(identity_raw)
            except (TypeError, ValueError, KeyError, RuntimeProfileDocumentError) as exc:
                identity_input_valid = False
                entry_reasons.append(f"observed identity is invalid: {exc}")
        expected_key_id = key_ids.get(mode)
        signature_required = mode in signature_modes
        firmware_required = mode in firmware_modes
        key_supplied = mode in keys
        profile_result: dict[str, Any] | None = None
        loaded_profile_id: str | None = None
        try:
            loaded = load_runtime_profile_document(
                profile_path,
                expected_mode=RuntimeMode(mode),
                observed_identity=identity,
                hmac_key=keys.get(mode),
                expected_key_id=expected_key_id,
                require_signature=signature_required,
                allow_flight_control_output=False,
                require_firmware_identity_binding=firmware_required,
            )
            profile_result = _relative_result(loaded.to_mapping(), base_directory=root)
            loaded_profile_id = str(loaded.profile.profile_id.value)
            checks["runtime_mode_matches"] = True
            checks["profile_kind_matches"] = loaded_profile_id in _EXPECTED_PROFILE_IDS[mode]
            checks["signature_policy_matches"] = (
                loaded.signature_valid if signature_required else True
            )
            checks["key_id_bound"] = (
                document_key_id == expected_key_id
                if signature_required
                else document_key_id is None
            )
            checks["identity_bound"] = loaded.identity_valid and identity_input_valid
            checks["firmware_identity_bound"] = (
                loaded.profile.firmware_identity_hash is not None and loaded.identity_valid
                if firmware_required
                else True
            )
            checks["output_locked"] = loaded.output_locked
            checks["profile_gate_passed"] = (
                loaded.profile_valid
                and checks["profile_kind_matches"]
                and checks["signature_policy_matches"]
                and checks["key_id_bound"]
                and checks["identity_bound"]
                and checks["firmware_identity_bound"]
                and (loaded.output_locked if mode in output_modes else True)
            )
            if not checks["profile_kind_matches"]:
                entry_reasons.append("runtime profile kind does not match its mode")
            if not checks["signature_policy_matches"]:
                entry_reasons.append("required runtime profile signature is invalid")
            if not checks["key_id_bound"]:
                entry_reasons.append("runtime profile key_id is not bound to the expected key")
            if not checks["identity_bound"]:
                entry_reasons.append("runtime profile identity is not bound to the observation")
            if not checks["firmware_identity_bound"]:
                entry_reasons.append("runtime profile firmware identity is not bound")
            if mode in output_modes and not checks["output_locked"]:
                entry_reasons.append("runtime profile output is not locked")
            if not loaded.profile_valid:
                entry_reasons.append(loaded.reason)
        except (OSError, RuntimeProfileDocumentError, TypeError, ValueError) as exc:
            message = str(exc) or type(exc).__name__
            entry_reasons.append(f"{type(exc).__name__}: {message}")
            entry_error = {"type": type(exc).__name__, "message": message}
        else:
            entry_error = None
        entry_gate = checks["profile_gate_passed"] is True
        entries.append(
            {
                "runtime_mode": mode,
                "profile": source,
                "observed_identity": (identity.to_mapping() if identity is not None else None),
                "expectations": {
                    "require_signature": signature_required,
                    "require_firmware_identity_binding": firmware_required,
                    "require_output_locked": mode in output_modes,
                    "hmac_key_supplied": key_supplied,
                    "expected_key_id": expected_key_id,
                },
                "document_key_id": document_key_id,
                "result": profile_result,
                "checks": checks,
                "gate_passed": entry_gate,
                "reasons": entry_reasons,
            }
        )
        if entry_error is not None:
            entries[-1]["error"] = entry_error

    profile_paths = [str(entry["profile"]["path"]) for entry in entries]
    declarations_match = all(
        isinstance(entry.get("result"), Mapping)
        and isinstance(entry["result"].get("profile"), Mapping)
        and entry["result"]["profile"].get("profile_id")
        in _EXPECTED_PROFILE_IDS[entry["runtime_mode"]]
        for entry in entries
    )
    checks = {
        "required_modes_valid": True,
        "entries_have_required_fields": all(
            isinstance(entry.get("profile"), Mapping)
            and isinstance(entry.get("runtime_mode"), str)
            and "result" in entry
            and "checks" in entry
            for entry in entries
        ),
        "required_modes_complete": mode_complete,
        "entries_unique": unique_modes,
        "mode_declarations_match": declarations_match,
        "profile_paths_distinct": len(profile_paths) == len(set(profile_paths)),
        "all_required_signatures_valid": all(
            entry["checks"]["signature_policy_matches"]
            for entry in entries
            if entry["runtime_mode"] in signature_modes
        ),
        "all_required_identities_bound": all(
            entry["checks"]["identity_bound"] and entry["checks"]["firmware_identity_bound"]
            for entry in entries
            if entry["runtime_mode"] in firmware_modes or entry["runtime_mode"] in signature_modes
        ),
        "all_outputs_locked": all(
            entry["checks"]["output_locked"]
            for entry in entries
            if entry["runtime_mode"] in output_modes
        ),
        "all_profiles_passed": bool(entries)
        and all(entry.get("gate_passed") is True for entry in entries),
        "no_errors": all(not entry.get("error") for entry in entries),
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "required_modes": list(required),
        "covered_modes": sorted(set(covered_modes), key=_MODE_ORDER.__getitem__),
        "binding_count": len(entries),
        "complete": mode_complete,
        "policies": {
            "require_signature_modes": sorted(signature_modes, key=_MODE_ORDER.__getitem__),
            "require_firmware_identity_modes": sorted(firmware_modes, key=_MODE_ORDER.__getitem__),
            "require_output_locked_modes": sorted(output_modes, key=_MODE_ORDER.__getitem__),
        },
        "bindings": entries,
        "checks": checks,
        "gate_passed": all(checks.values()),
        "reasons": [name for name in _CHECK_NAMES if not checks[name]],
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }


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


def _parse_mode_csv(value: str, *, label: str, allow_empty: bool = False) -> tuple[str, ...]:
    values = tuple(part.strip() for part in value.split(",") if part.strip())
    return _normalise_modes(values, label=label, allow_empty=allow_empty)


def _parse_mode_value_pairs(values: Sequence[Sequence[str]], *, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, pair in enumerate(values):
        if len(pair) != 2:
            raise RuntimeProfileBindingSetError(f"{label}[{index}] must contain MODE VALUE")
        mode = _normalise_mode(pair[0], label=f"{label}[{index}].mode")
        if mode in result:
            raise RuntimeProfileBindingSetError(f"{label} must not repeat mode {mode}")
        result[mode] = pair[1]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding",
        action="append",
        nargs=2,
        metavar=("MODE", "PROFILE_JSON"),
        required=True,
        help="one mode/profile tuple; repeat for each mode",
    )
    parser.add_argument(
        "--identity-json",
        action="append",
        nargs=2,
        default=[],
        metavar=("MODE", "JSON_OBJECT"),
        help="observed identity JSON for a HIL/REAL mode; repeatable",
    )
    parser.add_argument(
        "--hmac-key-env",
        action="append",
        nargs=2,
        default=[],
        metavar=("MODE", "ENV_NAME"),
        help="environment variable containing the HMAC key for a mode",
    )
    parser.add_argument(
        "--expected-key-id",
        action="append",
        nargs=2,
        default=[],
        metavar=("MODE", "KEY_ID"),
        help="expected signed-profile key_id for a mode",
    )
    parser.add_argument(
        "--required-modes",
        default=",".join(DEFAULT_REQUIRED_MODES),
        help="comma-separated required modes (default: replay,sim,hil,real)",
    )
    parser.add_argument(
        "--require-signature-modes",
        default=",".join(DEFAULT_SIGNATURE_MODES),
        help="comma-separated modes requiring a signature",
    )
    parser.add_argument(
        "--require-firmware-identity-modes",
        default=",".join(DEFAULT_FIRMWARE_IDENTITY_MODES),
        help="comma-separated modes requiring firmware identity binding",
    )
    parser.add_argument(
        "--require-output-locked-modes",
        default=",".join(DEFAULT_OUTPUT_LOCKED_MODES),
        help="comma-separated modes that must keep output locked",
    )
    parser.add_argument(
        "--base-directory",
        type=Path,
        default=Path.cwd(),
        help="root under which all profile paths must resolve",
    )
    parser.add_argument("--out", type=Path, help="optional JSON verification report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        required_modes = _parse_mode_csv(args.required_modes, label="required_modes")
        signature_modes = _parse_mode_csv(
            args.require_signature_modes,
            label="require_signature_modes",
            allow_empty=True,
        )
        firmware_modes = _parse_mode_csv(
            args.require_firmware_identity_modes,
            label="require_firmware_identity_modes",
            allow_empty=True,
        )
        output_modes = _parse_mode_csv(
            args.require_output_locked_modes,
            label="require_output_locked_modes",
            allow_empty=True,
        )
        key_envs = _parse_mode_value_pairs(args.hmac_key_env, label="hmac_key_env")
        expected_key_ids = _parse_mode_value_pairs(
            args.expected_key_id,
            label="expected_key_id",
        )
        hmac_keys: dict[str, bytes] = {}
        for mode, variable_name in key_envs.items():
            variable = variable_name.strip()
            if not variable:
                raise RuntimeProfileBindingSetError(f"hmac_key_env[{mode}] must name an env var")
            raw_key = os.environ.get(variable)
            if raw_key is None or not raw_key:
                raise RuntimeProfileBindingSetError(
                    f"HMAC key environment variable is missing: {variable}"
                )
            hmac_keys[mode] = raw_key.encode("utf-8")
        identity_by_mode: dict[str, object] = {}
        for mode, raw_json in _parse_mode_value_pairs(
            args.identity_json,
            label="identity_json",
        ).items():
            try:
                identity_by_mode[mode] = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise RuntimeProfileBindingSetError(
                    f"identity_json[{mode}] is not valid JSON"
                ) from exc
        specifications = []
        for mode, profile_path in args.binding:
            clean_mode = _normalise_mode(mode, label="binding mode")
            specifications.append(
                {
                    "runtime_mode": clean_mode,
                    "profile_path": profile_path,
                    "observed_identity": identity_by_mode.get(clean_mode),
                }
            )
        document = verify_runtime_profile_binding_set(
            specifications,
            required_modes=required_modes,
            base_directory=args.base_directory,
            hmac_keys=hmac_keys,
            expected_key_ids=expected_key_ids,
            require_signature_modes=signature_modes,
            require_firmware_identity_modes=firmware_modes,
            require_output_locked_modes=output_modes,
        )
        exit_code = 0 if document["gate_passed"] else 1
    except (OSError, RuntimeProfileBindingSetError, TypeError, ValueError) as exc:
        message = str(exc) or type(exc).__name__
        document = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "event": ERROR_EVENT,
            "required_modes": [],
            "covered_modes": [],
            "binding_count": 0,
            "complete": False,
            "policies": {
                "require_signature_modes": [],
                "require_firmware_identity_modes": [],
                "require_output_locked_modes": [],
            },
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
    "DEFAULT_FIRMWARE_IDENTITY_MODES",
    "DEFAULT_OUTPUT_LOCKED_MODES",
    "DEFAULT_REQUIRED_MODES",
    "DEFAULT_SIGNATURE_MODES",
    "ERROR_EVENT",
    "EVENT",
    "RuntimeProfileBindingSetError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_parser",
    "main",
    "verify_runtime_profile_binding_set",
]
