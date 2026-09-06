from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_current_progress_matches_version_and_release_parity() -> None:
    progress = _json(ROOT / "docs/current-progress.json")
    version = _json(ROOT / "configs/product-version.json")
    parity_path = ROOT / "configs/release-parity.json"
    parity = _json(parity_path)

    assert progress["schema_version"] == 1
    assert progress["product_version"] == {
        "release_label": version["release_label"],
        "numeric": version["numeric"],
        "python": version["python"],
        "channel": version["channel"],
    }
    assert progress["release_parity"]["bytes"] == parity_path.stat().st_size
    assert progress["release_parity"]["file_sha256"] == _sha256(parity_path)
    assert progress["release_parity"]["hashes"] == parity["parity"]


def test_current_progress_keeps_prerelease_and_claim_boundaries_explicit() -> None:
    progress = _json(ROOT / "docs/current-progress.json")

    assert progress["product_version"]["channel"] == "prerelease"
    assert progress["release_status"] == {
        "final_version_released": False,
        "gate_passed": False,
        "release_allowed": False,
        "blocker_count": 31,
    }
    assert progress["model_status"]["status"] == "quarantined"
    assert progress["model_status"]["production_approved"] is False
    assert progress["model_status"]["accuracy_validated_for_production"] is False
    for claim in (
        "qgc_manual_visual_acceptance",
        "hardware_acceptance",
        "outdoor_gnss_acceptance",
        "hil_acceptance",
        "real_acceptance",
        "flight_acceptance",
    ):
        assert progress["claims"][claim] == "not_claimed"


def test_readme_is_a_bounded_entry_page_with_durable_status_links() -> None:
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    archive = ROOT / "docs/history/README-development-log-through-v56.md"

    assert readme_path.stat().st_size < 50_000
    assert "docs/current-progress.md" in readme
    assert "docs/current-progress.json" in readme
    assert "docs/development-update-policy.md" in readme
    assert "CHANGELOG.md" in readme
    assert "docs/implementation-roadmap.md" in readme
    assert archive.is_file()
    assert archive.stat().st_size > 500_000
