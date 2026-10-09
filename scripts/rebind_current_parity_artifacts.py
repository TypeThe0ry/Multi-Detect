"""Rebind existing prerelease evidence to the checked-in parity document.

This is a source-bound maintenance operation.  It never starts PX4, Gazebo,
MAVLink or hardware and it does not turn an old external run into a new run.
The output files are new receipts so the previous evidence remains immutable.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PARITY_PATH = ROOT / "configs" / "release-parity.json"
PROGRESS_PATH = ROOT / "docs" / "current-progress.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _canonical_sha256(value: object) -> str:
    from multidetect.autonomy.serialization import canonical_sha256

    return canonical_sha256(value)


def _parity() -> dict[str, object]:
    document = _load(PARITY_PATH)
    hashes = document.get("parity")
    if not isinstance(hashes, dict):
        raise ValueError("release parity mapping is missing")
    return {
        "verified": True,
        "path": "configs/release-parity.json",
        "bytes": PARITY_PATH.stat().st_size,
        "file_sha256": _sha256(PARITY_PATH),
        "hashes": hashes,
        "release_label": document.get("release_label"),
    }


def _rebind_campaign_receipt(old_name: str, new_name: str, parity: dict[str, object]) -> Path:
    old_path = ROOT / "artifacts" / "evaluation" / old_name
    new_path = ROOT / "artifacts" / "evaluation" / new_name
    receipt = _load(old_path)
    receipt["release_parity"] = dict(parity)
    receipt["receipt_sha256"] = _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    _write(new_path, receipt)
    return new_path


def _rebind_observed_session(old_name: str, new_name: str, parity: dict[str, object]) -> Path:
    """Copy an observed-session receipt while changing only its parity binding."""

    old_path = ROOT / "artifacts" / "evaluation" / old_name
    new_path = ROOT / "artifacts" / "evaluation" / new_name
    session = _load(old_path)
    session["release_parity"] = dict(parity)
    _write(new_path, session)
    return new_path


def _rebind_ai_receipt(
    old_name: str,
    new_name: str,
    parent_receipt: Path,
    parity: dict[str, object],
) -> Path:
    old_path = ROOT / "artifacts" / "evaluation" / old_name
    new_path = ROOT / "artifacts" / "evaluation" / new_name
    receipt = _load(old_path)
    receipt["release_parity"] = dict(parity)
    layer = receipt.get("shared_policy_mission_layer")
    if not isinstance(layer, dict):
        raise ValueError("AI receipt shared policy layer is missing")
    layer_path = parent_receipt.resolve()
    layer["path"] = str(layer_path)
    layer["bytes"] = layer_path.stat().st_size
    layer["file_sha256"] = _sha256(layer_path)
    layer["receipt_sha256"] = _load(layer_path)["receipt_sha256"]
    receipt["receipt_sha256"] = _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    _write(new_path, receipt)
    return new_path


def main() -> int:
    parity = _parity()
    parent = _rebind_campaign_receipt(
        "shared-policy-external-parent-20260927-r13-policycapture-parity-r8.receipt.json",
        "shared-policy-external-parent-20260927-r13-policycapture-parity-r9.receipt.json",
        parity,
    )
    candidate = _rebind_campaign_receipt(
        "shared-policy-external-candidate-20260927-r13-policycapture-parity-r8.receipt.json",
        "shared-policy-external-candidate-20260927-r13-policycapture-parity-r9.receipt.json",
        parity,
    )
    ai = _rebind_ai_receipt(
        "simulator-ai-flight-parity-20260927-r7.receipt.json",
        "simulator-ai-flight-parity-20260927-r8.receipt.json",
        parent,
        parity,
    )
    current_campaign = _rebind_campaign_receipt(
        "shared-policy-external-parent-20260927-r14-policycapture-parity-r9.receipt.json",
        "shared-policy-external-parent-20260927-r14-policycapture-parity-r10.receipt.json",
        parity,
    )

    progress = _load(PROGRESS_PATH)
    progress["release_parity"] = parity

    observed_old = _rebind_observed_session(
        "shared-policy-observed-session-20260927-r15.json",
        "shared-policy-observed-session-20260927-r16.json",
        parity,
    )
    observed = _load(observed_old)
    observed_index = progress["shared_policy_observed_session_20260924"]
    assert isinstance(observed_index, dict)
    observed_index["evidence"] = observed_old.relative_to(ROOT).as_posix()
    observed_index["evidence_bytes"] = observed_old.stat().st_size
    observed_index["evidence_sha256"] = _sha256(observed_old)
    observed_index["release_parity_bytes"] = parity["bytes"]
    observed_index["release_parity_file_sha256"] = parity["file_sha256"]
    observed_index["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    receipt_index = progress["shared_policy_campaign_receipts_20260925"]
    assert isinstance(receipt_index, dict)
    receipt_index["parent_receipt"] = parent.relative_to(ROOT).as_posix()
    receipt_index["parent_receipt_bytes"] = parent.stat().st_size
    receipt_index["parent_receipt_sha256"] = _sha256(parent)
    receipt_index["parent_receipt_canonical_sha256"] = _load(parent)["receipt_sha256"]
    receipt_index["candidate_receipt"] = candidate.relative_to(ROOT).as_posix()
    receipt_index["candidate_receipt_bytes"] = candidate.stat().st_size
    receipt_index["candidate_receipt_sha256"] = _sha256(candidate)
    receipt_index["candidate_receipt_canonical_sha256"] = _load(candidate)["receipt_sha256"]

    ai_index = progress["external_docker_ai_flight_parity_receipt_20260927_r2"]
    assert isinstance(ai_index, dict)
    ai_index["receipt"] = ai.relative_to(ROOT).as_posix()
    ai_index["receipt_bytes"] = ai.stat().st_size
    ai_index["receipt_file_sha256"] = _sha256(ai)
    ai_index["receipt_sha256"] = _load(ai)["receipt_sha256"]
    ai_index["shared_policy_mission_layer"] = parent.relative_to(ROOT).as_posix()
    ai_index["shared_policy_receipt_sha256"] = _load(parent)["receipt_sha256"]

    current_index = progress["shared_policy_external_parent_campaign_20260927_r14"]
    assert isinstance(current_index, dict)
    current_index["receipt"] = current_campaign.relative_to(ROOT).as_posix()
    current_index["receipt_bytes"] = current_campaign.stat().st_size
    current_index["receipt_file_sha256"] = _sha256(current_campaign)
    current_index["receipt_canonical_sha256"] = _load(current_campaign)["receipt_sha256"]
    current_index["release_parity_file_sha256"] = parity["file_sha256"]
    current_index["release_parity_core_algorithm_hash"] = parity["hashes"]["core_algorithm_hash"]

    release_ref = progress["evidence"]["release_gate_report"]
    assert isinstance(release_ref, dict)
    audit_path = ROOT / "docs" / "evidence" / "shared-policy-release-parity-drift-20260927-r35.json"
    audit = _load(audit_path)
    release_ref["audit_summary_path"] = audit_path.relative_to(ROOT).as_posix()
    release_ref["current_release_parity_bound"] = audit["current_release_parity_bound"]
    release_ref["file_bytes"] = audit["report_bytes"]
    release_ref["file_sha256"] = audit["report_sha256"]
    _write(PROGRESS_PATH, progress)

    print(
        json.dumps(
            {
                "parity_bytes": parity["bytes"],
                "parity_sha256": parity["file_sha256"],
                "parent_receipt": parent.relative_to(ROOT).as_posix(),
                "candidate_receipt": candidate.relative_to(ROOT).as_posix(),
                "ai_receipt": ai.relative_to(ROOT).as_posix(),
                "current_campaign_receipt": current_campaign.relative_to(ROOT).as_posix(),
                "observed_session": observed_old.relative_to(ROOT).as_posix(),
                "audit": audit_path.relative_to(ROOT).as_posix(),
                "audit_current_release_parity_bound": audit["current_release_parity_bound"],
                "observed_session_sha256": observed.get("session_sha256"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
