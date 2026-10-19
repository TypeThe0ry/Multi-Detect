from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_v030_pre1_foundation import build_evidence, main

ROOT = Path(__file__).resolve().parents[1]


def test_pre1_foundation_acceptance_is_current_and_deterministic(tmp_path: Path) -> None:
    first = build_evidence(root=ROOT)
    second = build_evidence(root=ROOT)

    assert first == second
    assert first["foundation_baseline"] == "0.3.0-pre.1"
    manifest = json.loads((ROOT / "configs/product-version.json").read_text(encoding="utf-8"))
    assert first["current_release_label"] == manifest["release_label"]
    assert first["gate_passed"] is True
    assert first["truth_free"] is True
    assert first["policy_applied_to_actuator"] is False
    assert first["final_release_allowed"] is False
    assert first["candidate_promoted"] is False
    assert set(first["checks"]) == {
        "runtime_mode",
        "clock",
        "runtime_profile_isolation",
        "runtime_backend",
        "scenario_manifest_runtime_channel",
        "core_task_contract",
        "deterministic_replay",
        "deterministic_simulator",
    }
    assert first["checks"]["runtime_backend"]["protocol_is_runtime_backend"] is True
    assert first["checks"]["deterministic_replay"]["same_report_hash_on_repeat"] is True
    assert first["checks"]["deterministic_simulator"]["same_evidence_hash_on_repeat"] is True

    output = tmp_path / "foundation.json"
    assert main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["sha256"] == first["sha256"]


def test_pre1_foundation_acceptance_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "foundation.json"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--output", str(output)])

    assert error.value.code == 2
    assert output.read_text(encoding="utf-8") == "existing"
