from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.run_fixed_wing_mission_compile_acceptance import run


def test_fixed_wing_mission_compile_acceptance_gate() -> None:
    evidence = run()
    assert evidence["gate_passed"] is True
    assert evidence["runtime_mode"] == "SIM"
    assert evidence["truth_free"] is True
    assert evidence["physical_output_enabled"] is False
    assert evidence["dispatcher"] == "compile_executable_mission"
    assert evidence["vehicle_class"] == "fixed-wing"
    assert evidence["vehicle_class_normalized"] == "fixed-wing"
    assert evidence["item_count"] == 5
    checks = evidence["checks"]
    assert all(checks.values())


def test_fixed_wing_mission_compile_cli_writes_json(tmp_path: Path) -> None:
    from scripts.run_fixed_wing_mission_compile_acceptance import main

    output = tmp_path / "fixed-wing-mission.json"
    assert main(["--out", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["gate_passed"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", payload["protocol_hash"])
    assert re.fullmatch(r"[0-9a-f]{64}", payload["executable_mission_hash"])
    assert payload["checks"]["protocol_hash_covers_wire_items"] is True
