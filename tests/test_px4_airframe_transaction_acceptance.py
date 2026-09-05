from __future__ import annotations

import copy
import json
from pathlib import Path

from multidetect.px4_mission_acceptance import verify_px4_mission_transaction_acceptance
from scripts.run_px4_airframe_transaction_acceptance import main, run
from scripts.verify_px4_airframe_transaction_acceptance import verify_file


def test_all_px4_airframes_use_the_same_journalized_transaction_path(tmp_path: Path) -> None:
    evidence = run(journal_directory=tmp_path / "journals", run_id="test-shared-path")

    assert evidence["gate_passed"] is True
    assert evidence["runtime_mode"] == "SIM"
    assert evidence["truth_free"] is True
    assert evidence["physical_output_enabled"] is False
    assert evidence["shared_dispatcher"] == "compile_executable_mission"
    assert evidence["shared_transport"] == "in_process_mission_item_int"
    entries = evidence["entries"]
    assert isinstance(entries, list)
    assert {entry["vehicle_class"] for entry in entries} == {
        "vtol",
        "fixed-wing",
        "multirotor",
    }
    for entry in entries:
        assert entry["gate_passed"] is True
        assert entry["state_path"] == [
            "authorized",
            "uploading",
            "uploaded",
            "readback_verified",
            "committed",
            "start_permitted",
        ]
        assert all(entry["checks"].values())
        journal = entry["mission_journal"]
        assert Path(journal["path"]).is_file()
        assert journal["state"] == "start_permitted"
        assert journal["recovery_required"] is False
        assert journal["requested_sequences"] == list(range(entry["item_count"]))
        assert journal["record_count"] >= entry["item_count"] + 6


def test_verifier_reopens_each_journal_and_rejects_tampered_tail(tmp_path: Path) -> None:
    evidence = run(journal_directory=tmp_path / "journals", run_id="test-tamper")
    assert verify_px4_mission_transaction_acceptance(evidence)["gate_passed"] is True

    tampered = copy.deepcopy(evidence)
    target = Path(tampered["entries"][1]["mission_journal"]["path"])
    target.write_bytes(target.read_bytes() + b'{"record_type":"partial-tail"')
    result = verify_px4_mission_transaction_acceptance(tampered)
    assert result["gate_passed"] is False
    entry_result = result["entries"][1]
    assert entry_result["gate_passed"] is False
    assert entry_result["checks"]["journal_sha256_match"] is False
    assert entry_result["checks"]["journal_integrity"] is False


def test_cli_writes_cross_airframe_evidence_and_standalone_verifier(tmp_path: Path) -> None:
    output = tmp_path / "px4-airframe-transaction.json"
    assert main(["--out", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["release_label"].startswith("0.3.0-pre.")
    assert payload["gate_passed"] is True
    verified = verify_file(output)
    assert verified["gate_passed"] is True
    assert verified["checks"]["vehicle_classes_complete"] is True
