from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.release_mode_evidence import (
    ReleaseModeEvidenceError,
    build_release_mode_evidence,
    main,
    write_release_mode_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODE_EVIDENCE_SCHEMA = (
    REPO_ROOT / "configs" / "schemas" / "release-mode-evidence.schema.json"
)


def _source(path: Path, *, mode: str | None = "sim", gate: object = True) -> Path:
    value: dict[str, object] = {
        "event": "runner_finished",
        "schema_version": 1,
    }
    if mode is not None:
        value["runtime_mode"] = mode
    if gate is not None:
        value["gate_passed"] = gate
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_builds_hash_bound_envelope_without_upgrading_source(tmp_path: Path) -> None:
    source = _source(tmp_path / "sim.json")
    output = tmp_path / "release" / "sim.json"

    document = build_release_mode_evidence(source, mode="sim", output_path=output)

    assert document["schema_version"] == 1
    assert document["runtime_mode"] == "sim"
    assert document["gate_passed"] is True
    assert document["source"]["event"] == "runner_finished"
    assert len(document["source"]["sha256"]) == 64
    write_release_mode_evidence(output, document)
    assert json.loads(output.read_text(encoding="utf-8")) == document


def test_missing_gate_stays_closed(tmp_path: Path) -> None:
    source = _source(tmp_path / "real.json", mode="real", gate=None)

    document = build_release_mode_evidence(source, mode="real", output_path=tmp_path / "out.json")

    assert document["gate_passed"] is False
    assert "source gate_passed is missing or not boolean" in document["reasons"]


def test_accepts_windows_powershell_utf8_bom_source(tmp_path: Path) -> None:
    source = tmp_path / "powershell.json"
    source.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps(
            {
                "event": "runner_finished",
                "runtime_mode": "sim",
                "gate_passed": True,
            }
        ).encode("utf-8")
    )

    document = build_release_mode_evidence(
        source, mode="sim", output_path=tmp_path / "out.json"
    )

    assert document["gate_passed"] is True


def test_rejects_ambiguous_and_nonfinite_source_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"event":"runner_finished","schema_version":1,"runtime_mode":"sim",'
        '"gate_passed":true,"gate_passed":false}',
        encoding="utf-8",
    )
    with pytest.raises(ReleaseModeEvidenceError, match="duplicate JSON key 'gate_passed'"):
        build_release_mode_evidence(duplicate, mode="sim", output_path=tmp_path / "duplicate.out")

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        '{"event":"runner_finished","schema_version":1,"runtime_mode":"sim",'
        '"gate_passed":NaN}',
        encoding="utf-8",
    )
    with pytest.raises(ReleaseModeEvidenceError, match="non-finite JSON number NaN"):
        build_release_mode_evidence(nonfinite, mode="sim", output_path=tmp_path / "nonfinite.out")


def test_missing_source_runtime_requires_explicit_opt_in(tmp_path: Path) -> None:
    source = _source(tmp_path / "combined.json", mode=None)

    with pytest.raises(ReleaseModeEvidenceError, match="allow-missing-source-runtime-mode"):
        build_release_mode_evidence(source, mode="sim", output_path=tmp_path / "out.json")

    document = build_release_mode_evidence(
        source,
        mode="sim",
        output_path=tmp_path / "out.json",
        allow_missing_source_runtime_mode=True,
    )
    assert document["gate_passed"] is True
    assert document["source"]["runtime_mode"] is None
    assert "source runtime_mode was supplied by explicit caller selection" in document["reasons"]


def test_source_mode_mismatch_is_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path / "replay.json", mode="replay")

    with pytest.raises(ReleaseModeEvidenceError, match="expected 'sim'"):
        build_release_mode_evidence(source, mode="sim", output_path=tmp_path / "out.json")

    document = build_release_mode_evidence(
        source,
        mode="sim",
        output_path=tmp_path / "out.json",
        allow_source_runtime_mode_mismatch=True,
    )
    assert document["gate_passed"] is True
    assert "explicitly projected" in document["reasons"][0]


def test_invalid_source_event_is_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path / "sim.json")
    source.write_text(
        json.dumps({"event": 7, "runtime_mode": "sim", "gate_passed": True}),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseModeEvidenceError, match="event must be a non-empty string"):
        build_release_mode_evidence(source, mode="sim", output_path=tmp_path / "out.json")


def test_envelope_bytes_are_deterministic(tmp_path: Path) -> None:
    source = _source(tmp_path / "replay.json", mode="replay")
    first = build_release_mode_evidence(source, mode="replay", output_path=tmp_path / "a.json")
    second = build_release_mode_evidence(source, mode="replay", output_path=tmp_path / "b.json")
    assert first == second
    write_release_mode_evidence(tmp_path / "a.json", first)
    write_release_mode_evidence(tmp_path / "b.json", second)
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


def test_schema_matches_envelope_contract() -> None:
    schema = json.loads(MODE_EVIDENCE_SCHEMA.read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["properties"]["acceptance_scope"] == {
        "const": "explicit_source_gate_projection"
    }
    assert schema["properties"]["runtime_mode"]["enum"] == [
        "replay",
        "sim",
        "hil",
        "real",
    ]
    assert schema["$defs"]["source"]["required"] == [
        "path",
        "sha256",
        "event",
        "runtime_mode",
        "gate_passed",
    ]


def test_cli_main_writes_envelope_and_returns_zero(tmp_path: Path) -> None:
    source = _source(tmp_path / "sim.json")
    output = tmp_path / "envelope.json"

    assert main(
        ["--mode", "sim", "--source", str(source), "--out", str(output)]
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["gate_passed"] is True
