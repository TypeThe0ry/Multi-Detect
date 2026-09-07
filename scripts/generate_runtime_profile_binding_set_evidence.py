"""Generate deterministic v184 RuntimeProfile binding-set evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.runtime import RuntimeProfile
from multidetect.runtime_profile_binding_set import verify_runtime_profile_binding_set
from multidetect.runtime_profiles import sign_runtime_profile_document

_KEY = b"multidetect-v184-profile-preflight-demo-key-material-32-bytes"
_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _profile_document(profile: RuntimeProfile, *, key_id: str | None = None) -> dict[str, object]:
    if key_id is None:
        return {"schema_version": 1, "profile": profile.to_mapping()}
    return sign_runtime_profile_document(profile, hmac_key=_KEY, key_id=key_id)


def _identity(*, signing: str | None = None) -> dict[str, str]:
    identity = {
        "vehicle_identity_hash": _A,
        "firmware_identity_hash": _E,
        "airframe_profile_hash": _B,
        "endpoint_binding_hash": _D,
    }
    if signing is not None:
        identity["signing_identity_hash"] = signing
    return identity


def _source_summary(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _validate(document: dict[str, object], schema_path: Path) -> int:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(output_directory: Path, schema_path: Path) -> dict[str, object]:
    output_directory.mkdir(parents=True, exist_ok=True)
    profile_paths = {
        "replay": output_directory / "replay-profile-v184.json",
        "sim": output_directory / "sim-profile-v184.json",
        "hil": output_directory / "hil-profile-v184.json",
        "real": output_directory / "real-profile-v184.json",
    }
    _write_json(profile_paths["replay"], _profile_document(RuntimeProfile.replay()))
    _write_json(profile_paths["sim"], _profile_document(RuntimeProfile.sim()))
    _write_json(
        profile_paths["hil"],
        _profile_document(
            RuntimeProfile.hil(
                vehicle_identity_hash=_A,
                firmware_identity_hash=_E,
                airframe_profile_hash=_B,
                endpoint_binding_hash=_D,
            ),
            key_id="hil-key-v1",
        ),
    )
    _write_json(
        profile_paths["real"],
        _profile_document(
            RuntimeProfile.flight(
                vehicle_identity_hash=_A,
                firmware_identity_hash=_E,
                airframe_profile_hash=_B,
                signing_identity_hash=_C,
                endpoint_binding_hash=_D,
            ),
            key_id="flight-key-v1",
        ),
    )
    specifications = [
        {
            "runtime_mode": "replay",
            "profile_path": profile_paths["replay"].name,
            "observed_identity": None,
        },
        {
            "runtime_mode": "sim",
            "profile_path": profile_paths["sim"].name,
            "observed_identity": None,
        },
        {
            "runtime_mode": "hil",
            "profile_path": profile_paths["hil"].name,
            "observed_identity": _identity(),
        },
        {
            "runtime_mode": "real",
            "profile_path": profile_paths["real"].name,
            "observed_identity": _identity(signing=_C),
        },
    ]
    full = verify_runtime_profile_binding_set(
        specifications,
        base_directory=output_directory,
        hmac_keys={"hil": _KEY, "real": _KEY},
        expected_key_ids={"hil": "hil-key-v1", "real": "flight-key-v1"},
    )
    subset = verify_runtime_profile_binding_set(
        specifications[:2],
        base_directory=output_directory,
    )
    full_path = output_directory / "runtime-profile-binding-set-v184.json"
    subset_path = output_directory / "runtime-profile-binding-set-default-v184.json"
    _write_json(full_path, full)
    _write_json(subset_path, subset)
    return {
        "full": full,
        "subset": subset,
        "full_report": _source_summary(full_path),
        "subset_report": _source_summary(subset_path),
        "profile_sources": {mode: _source_summary(path) for mode, path in profile_paths.items()},
        "full_schema_errors": _validate(full, schema_path),
        "subset_schema_errors": _validate(subset, schema_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("configs/schemas/runtime-profile-binding-set.schema.json"),
    )
    args = parser.parse_args()
    summary = build_evidence(args.output_directory, args.schema)
    print(
        json.dumps(
            {
                "full_gate_passed": summary["full"]["gate_passed"],
                "full_schema_errors": summary["full_schema_errors"],
                "default_gate_passed": summary["subset"]["gate_passed"],
                "default_missing_modes": ["hil", "real"],
                "default_schema_errors": summary["subset_schema_errors"],
                "full_report": summary["full_report"],
                "default_report": summary["subset_report"],
                "profile_sources": summary["profile_sources"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["full"]["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
