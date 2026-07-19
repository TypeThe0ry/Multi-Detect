from __future__ import annotations

from multidetect.navigation_fusion_acceptance import run_navigation_fusion_acceptance


def test_navigation_fusion_acceptance_covers_scale_and_consistency_gates() -> None:
    report = run_navigation_fusion_acceptance()

    assert report["passed"] is True
    assert report["consistent_three_source"]["validity"] == "valid"
    assert report["consistent_three_source"]["sources"] == ("gps", "vio", "air_data")
    assert report["unscaled_vio_gate"]["validity"] == "degraded"
    assert "vio:absolute_scale_invalid" in report["unscaled_vio_gate"][
        "source_diagnostics"
    ]
    assert report["conflicting_pair_gate"]["validity"] == "invalid"
    assert report["outlier_rejection"]["rejected_sources"] == ("air_data",)
    assert report["messages_transmitted"] == 0
    assert report["flight_control_enabled"] is False
