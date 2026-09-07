from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.runtime import RuntimeProfile
from multidetect.runtime_profile_binding_set import (
    DEFAULT_REQUIRED_MODES,
    SCHEMA,
    RuntimeProfileBindingSetError,
    main,
    verify_runtime_profile_binding_set,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "configs/schemas/runtime-profile-binding-set.schema.json"
KEY = b"runtime-profile-binding-set-test-key-material-32-bytes-minimum"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def _write_json(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _write_profile(
    path: Path,
    profile: RuntimeProfile,
    *,
    key_id: str | None = None,
) -> Path:
    if key_id is None:
        document: dict[str, object] = {
            "schema_version": 1,
            "profile": profile.to_mapping(),
        }
    else:
        document = sign_runtime_profile_document(profile, hmac_key=KEY, key_id=key_id)
    return _write_json(path, document)


def _identity(*, signing: str | None = None) -> dict[str, str]:
    identity = {
        "vehicle_identity_hash": HASH_A,
        "firmware_identity_hash": HASH_E,
        "airframe_profile_hash": HASH_B,
        "endpoint_binding_hash": HASH_D,
    }
    if signing is not None:
        identity["signing_identity_hash"] = signing
    return identity


def _spec(mode: str, path: Path, identity: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "runtime_mode": mode,
        "profile_path": str(path),
        "observed_identity": identity,
    }


def test_replay_and_sim_profiles_are_complete_and_schema_valid(tmp_path: Path) -> None:
    replay = _write_profile(tmp_path / "replay.json", RuntimeProfile.replay())
    sim = _write_profile(tmp_path / "sim.json", RuntimeProfile.sim())

    document = verify_runtime_profile_binding_set(
        [_spec("sim", sim), _spec("replay", replay)],
        required_modes=("replay", "sim"),
        base_directory=tmp_path,
    )

    assert document["schema"] == SCHEMA
    assert document["covered_modes"] == ["replay", "sim"]
    assert document["complete"] is True
    assert document["gate_passed"] is True
    assert all(document["checks"].values())
    assert [entry["runtime_mode"] for entry in document["bindings"]] == ["replay", "sim"]
    assert all(entry["checks"]["output_locked"] for entry in document["bindings"])
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)


def test_default_four_mode_profile_set_stays_closed_until_hil_and_real_exist(
    tmp_path: Path,
) -> None:
    replay = _write_profile(tmp_path / "replay.json", RuntimeProfile.replay())
    sim = _write_profile(tmp_path / "sim.json", RuntimeProfile.sim())

    document = verify_runtime_profile_binding_set(
        [_spec("replay", replay), _spec("sim", sim)],
        base_directory=tmp_path,
    )

    assert tuple(document["required_modes"]) == DEFAULT_REQUIRED_MODES
    assert document["complete"] is False
    assert document["gate_passed"] is False
    assert document["checks"]["required_modes_complete"] is False
    assert document["checks"]["all_profiles_passed"] is True


def test_signed_hil_and_real_profiles_bind_identity_and_remain_locked(tmp_path: Path) -> None:
    replay = _write_profile(tmp_path / "replay.json", RuntimeProfile.replay())
    sim = _write_profile(tmp_path / "sim.json", RuntimeProfile.sim())
    hil = _write_profile(
        tmp_path / "hil.json",
        RuntimeProfile.hil(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash=HASH_E,
            airframe_profile_hash=HASH_B,
            endpoint_binding_hash=HASH_D,
        ),
        key_id="hil-key-v1",
    )
    real = _write_profile(
        tmp_path / "real.json",
        RuntimeProfile.flight(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash=HASH_E,
            airframe_profile_hash=HASH_B,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=HASH_D,
        ),
        key_id="flight-key-v1",
    )

    document = verify_runtime_profile_binding_set(
        [
            _spec("real", real, _identity(signing=HASH_C)),
            _spec("hil", hil, _identity()),
            _spec("sim", sim),
            _spec("replay", replay),
        ],
        base_directory=tmp_path,
        hmac_keys={"hil": KEY, "real": KEY},
        expected_key_ids={"hil": "hil-key-v1", "real": "flight-key-v1"},
    )

    assert document["complete"] is True
    assert document["gate_passed"] is True
    assert all(document["checks"].values())
    assert [entry["runtime_mode"] for entry in document["bindings"]] == [
        "replay",
        "sim",
        "hil",
        "real",
    ]
    for entry in document["bindings"]:
        assert entry["checks"]["output_locked"] is True
        assert entry["result"]["flight_control_enabled"] is False
        assert entry["result"]["output_locked"] is True


def test_duplicate_mode_and_profile_reuse_fail_closed(tmp_path: Path) -> None:
    replay = _write_profile(tmp_path / "replay.json", RuntimeProfile.replay())
    document = verify_runtime_profile_binding_set(
        [_spec("replay", replay), _spec("replay", replay)],
        required_modes=("replay",),
        base_directory=tmp_path,
    )

    assert document["gate_passed"] is False
    assert document["checks"]["entries_unique"] is False
    assert document["checks"]["profile_paths_distinct"] is False


def test_profile_symlink_is_rejected_before_resolution(tmp_path: Path) -> None:
    replay = _write_profile(tmp_path / "replay.json", RuntimeProfile.replay())
    link = tmp_path / "replay-link.json"
    try:
        link.symlink_to(replay)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(RuntimeProfileBindingSetError, match="must not be a symbolic link"):
        verify_runtime_profile_binding_set(
            [_spec("replay", link)],
            required_modes=("replay",),
            base_directory=tmp_path,
        )


def test_tampered_signed_profile_is_entry_error_and_schema_valid(tmp_path: Path) -> None:
    hil = _write_profile(
        tmp_path / "hil.json",
        RuntimeProfile.hil(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash=HASH_E,
            airframe_profile_hash=HASH_B,
            endpoint_binding_hash=HASH_D,
        ),
        key_id="hil-key-v1",
    )
    document = json.loads(hil.read_text(encoding="utf-8"))
    document["profile"]["endpoint_binding_hash"] = HASH_C
    _write_json(hil, document)

    result = verify_runtime_profile_binding_set(
        [_spec("hil", hil, _identity())],
        required_modes=("hil",),
        base_directory=tmp_path,
        hmac_keys={"hil": KEY},
        expected_key_ids={"hil": "hil-key-v1"},
    )

    entry = result["bindings"][0]
    assert result["gate_passed"] is False
    assert result["checks"]["all_profiles_passed"] is False
    assert result["checks"]["no_errors"] is False
    assert entry["gate_passed"] is False
    assert entry["error"]["type"] == "RuntimeProfileDocumentError"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result)


def test_identity_mismatch_fails_closed_and_keeps_output_locked(tmp_path: Path) -> None:
    real = _write_profile(
        tmp_path / "real.json",
        RuntimeProfile.flight(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash=HASH_E,
            airframe_profile_hash=HASH_B,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=HASH_D,
        ),
        key_id="flight-key-v1",
    )
    wrong = _identity(signing=HASH_C)
    wrong["vehicle_identity_hash"] = HASH_B
    result = verify_runtime_profile_binding_set(
        [_spec("real", real, wrong)],
        required_modes=("real",),
        base_directory=tmp_path,
        hmac_keys={"real": KEY},
        expected_key_ids={"real": "flight-key-v1"},
    )

    entry = result["bindings"][0]
    assert result["gate_passed"] is False
    assert entry["checks"]["identity_bound"] is False
    assert entry["checks"]["output_locked"] is True
    assert entry["result"]["flight_control_enabled"] is False


def test_cli_success_writes_schema_valid_document(tmp_path: Path, capsys) -> None:
    replay = _write_profile(tmp_path / "replay.json", RuntimeProfile.replay())
    output = tmp_path / "profile-set.json"
    exit_code = main(
        [
            "--required-modes",
            "replay",
            "--require-signature-modes",
            "",
            "--require-firmware-identity-modes",
            "",
            "--base-directory",
            str(tmp_path),
            "--binding",
            "replay",
            replay.name,
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True


def test_cli_structural_error_is_schema_valid(tmp_path: Path, capsys) -> None:
    output = tmp_path / "profile-set-error.json"
    exit_code = main(
        [
            "--required-modes",
            "replay",
            "--base-directory",
            str(tmp_path),
            "--binding",
            "invalid-mode",
            "profile.json",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 1
    document = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    assert document["event"] == "runtime_profile_binding_set_error"
    assert document["gate_passed"] is False
    assert document["error"]["type"] == "RuntimeProfileBindingSetError"
    assert json.loads(capsys.readouterr().out)["event"] == "runtime_profile_binding_set_error"
