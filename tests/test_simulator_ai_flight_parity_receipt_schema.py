from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from test_simulator_ai_flight_parity_receipt import _fixture

from scripts.create_simulator_ai_flight_parity_receipt import build_receipt

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "configs/schemas/simulator-ai-flight-parity-receipt.schema.json"


def _current_receipt_path() -> Path:
    progress = json.loads(
        (ROOT / "docs/current-progress.json").read_text(encoding="utf-8")
    )
    entry = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    receipt_path = ROOT / entry["receipt"]
    if not receipt_path.is_file():
        raise AssertionError(f"current progress receipt is missing: {receipt_path}")
    return receipt_path


def test_simulator_ai_flight_parity_schema_is_valid() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_current_indexed_simulator_ai_flight_parity_receipt_matches_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    receipt = json.loads(_current_receipt_path().read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(receipt)


def test_new_builder_receipt_matches_schema(tmp_path: Path) -> None:
    ai, policy, parity, _runtime = _fixture(tmp_path)
    receipt = build_receipt(ai, policy, parity, tmp_path / "composite.receipt.json")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(receipt)
