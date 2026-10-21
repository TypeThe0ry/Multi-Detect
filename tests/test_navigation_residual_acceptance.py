from __future__ import annotations

import json
from pathlib import Path

from multidetect import PRODUCT_VERSION
from scripts.create_navigation_residual_acceptance import build_record


def test_navigation_residual_acceptance_record_is_deterministically_qualified() -> None:
    root = Path(__file__).resolve().parents[1]
    record = build_record(root)

    # Keep the acceptance artifact tied to the implementation and parity
    # manifest; this prevents a copied fixture from silently outliving the
    # navigation fusion source it describes.
    assert record["release_label"] == PRODUCT_VERSION.release_label
    assert record["software_status"] == "pass_with_environment_gap"
    assert record["hardware_status"] == "offline_pending"
    assert record["release_allowed"] is False
    assert record["checks"] == {
        "nominal_not_inflated": True,
        "spread_inflated": True,
        "spread_sigma_wider": True,
        "advisory_only": True,
        "no_flight_control": True,
    }
    assert record["scenarios"]["nominal"]["validity"] == "valid"
    assert record["scenarios"]["spread_inside_consistency_gate"]["validity"] == "degraded"
    assert (
        "navigation_velocity_uncertainty_inflated"
        in record["scenarios"]["spread_inside_consistency_gate"]["reasons"]
    )
    json.dumps(record, ensure_ascii=False, allow_nan=False)
