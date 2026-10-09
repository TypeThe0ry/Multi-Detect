"""Thin stdlib terminal audit for the fixed r2 PEFT five-file snapshots.

The frozen v1 audit admits four files but PEFT also saved README.md. Only its
snapshot function is extended, in a private module namespace and restored in
finally. All phase, WAL, parameter, source, and raw-diagnostic gates stay v1.
README bindings are observations made by this audit, not invented historical
checkpoint receipt fields or evidence of model quality. No model is loaded.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-recovery267-terminal-filesystem-audit-v2"
LEGACY_SOURCE = "scripts/audit_onboard_navigation_recovery267_terminal.py"
LEGACY_TEST = "tests/test_audit_onboard_navigation_recovery267_terminal.py"
LEGACY_IDENTITIES = {
    LEGACY_SOURCE: (
        40489,
        "5f0b5989bba74ceb6c158f57b24904699b44e0335211bc2eb683839c928ae2c0",
    ),
    LEGACY_TEST: (
        28212,
        "be90ad9ea520da96c5b18be749f73863a80c015acddc2972f6711eebd0eb404d",
    ),
}
OWN_TEST = "tests/test_audit_onboard_navigation_recovery267_terminal_v2.py"
# Two independent directory/sidecar-only observations: native 0, 35ef58 and
# 9298ca. This extension supports only the fixed r2 snapshots, not arbitrary
# future PEFT exports. No YAML or model-card instructions are evaluated.
README_BYTES = 5438
README_SHA256 = "d306e395fa26bcdc6b8ed4bbe25af043b962231e638503bb035ad40bd4e1a307"
README_LIMIT = 64 * 1024


def source_binding(path):
    original = Path(path)
    if not original.is_absolute():
        original = ROOT / original
    resolved = original.resolve(strict=True)
    if not resolved.is_relative_to(ROOT.resolve()) or original.is_symlink():
        raise ValueError("Audit source escapes the repository or is a symlink")
    size = resolved.stat().st_size
    if not resolved.is_file() or not 0 < size <= 2 * 1024 * 1024:
        raise ValueError("Audit source exceeds its small-file budget")
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        while data := stream.read(65536):
            digest.update(data)
    if resolved.stat().st_size != size:
        raise ValueError("Audit source changed while hashing")
    return {"path": str(resolved), "bytes": size, "sha256": digest.hexdigest()}


def authenticate_legacy():
    rows = []
    for path, (size, digest) in LEGACY_IDENTITIES.items():
        row = source_binding(path)
        if (row["bytes"], row["sha256"]) != (size, digest):
            raise ValueError("Frozen v1 audit identity differs: " + path)
        rows.append(row)
    return rows


def load_legacy():
    before = authenticate_legacy()  # Authenticate bytes before executing them.
    spec = importlib.util.spec_from_file_location(
        "_recovery267_terminal_v2_authenticated_v1", ROOT / LEGACY_SOURCE
    )
    if spec is None or spec.loader is None:
        raise ValueError("Cannot create the isolated frozen audit import")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if authenticate_legacy() != before:
        raise ValueError("Frozen v1 audit changed while importing")
    return module


def audit_sources():
    authenticate_legacy()
    return [
        source_binding(__file__),
        source_binding(OWN_TEST),
        source_binding(LEGACY_SOURCE),
        source_binding(LEGACY_TEST),
    ]


def check_snapshots(legacy, run, train, wal, audit, plan, bindings):
    """The v1 snapshot gate, with exact five files and explicit README binding."""
    expected_steps = list(range(8, 265, 8))
    expected_names = {f"checkpoint-{step:03d}" for step in expected_steps}
    actual_names = {
        path.name for path in (run / "train").iterdir() if path.name.startswith("checkpoint-")
    }
    legacy.check(
        actual_names == expected_names, "Exactly33 periodic checkpoint directories required"
    )
    legacy.check(
        type(train.get("checkpoints")) is list and len(train["checkpoints"]) == 33,
        "Exactly33 periodic checkpoint receipts are required",
    )
    config_reference = legacy.read_bound(plan["warm_start"]["config"])
    results = []
    wal_lines = wal.splitlines(keepends=True)
    for index, step in enumerate([*expected_steps, 267]):
        directory = run / "train" / (f"checkpoint-{step:03d}" if step != 267 else "adapter")
        legacy.check(
            directory.is_dir()
            and {path.name for path in directory.iterdir()}
            == {
                "checkpoint.json",
                "adapter_model.safetensors",
                "adapter_config.json",
                "training-events-prefix.jsonl",
                "README.md",
            },
            "Snapshot must contain exactly the standard four files plus PEFT README.md",
        )
        readme = legacy.binding(directory / "README.md", limit=README_LIMIT)
        legacy.check(
            readme["bytes"] == README_BYTES and readme["sha256"] == README_SHA256,
            "PEFT README differs from the observed fixed r2 sidecar bytes",
        )
        bindings.append(readme)  # v1's final binding re-read also checks README.
        receipt, receipt_binding = legacy.read_json(directory / "checkpoint.json")
        bindings.append(receipt_binding)
        legacy.exact_int(receipt.get("completed_new_updates"), step, "Snapshot completed updates")
        legacy.exact_int(
            receipt.get("cumulative_campaign_attempts"), 13 + step, "Snapshot cumulative budget"
        )
        legacy.check(
            all(
                receipt.get(key) is False
                for key in (
                    "optimizer_state_saved",
                    "scheduler_rng_state_saved",
                    "exact_resume",
                    "candidate_promoted",
                )
            ),
            "Snapshot overstates resume or promotion",
        )
        for key, name in (
            ("weights", "adapter_model.safetensors"),
            ("config", "adapter_config.json"),
            ("journal", "training-events-prefix.jsonl"),
        ):
            actual = legacy.binding(
                directory / name,
                limit=legacy.ADAPTER_LIMIT if key == "weights" else legacy.SMALL_LIMIT,
            )
            legacy.check(
                legacy.normalize(receipt.get(key)) == actual,
                "Snapshot " + key + " bytes/path changed",
            )
            bindings.append(actual)
        legacy.check(
            legacy.canonical(legacy.read_bound(receipt["config"]))
            == legacy.canonical(config_reference),
            "Snapshot adapter configuration differs from the original warm adapter",
        )
        prefix = Path(receipt["journal"]["path"]).read_bytes()
        legacy.check(
            prefix == b"".join(wal_lines[: 4 * step]) and len(prefix.splitlines()) == 4 * step,
            "Snapshot journal is not the exact complete WAL prefix",
        )
        digest, checks = legacy.fp32_parameter_digest(
            directory / "adapter_model.safetensors", audit
        )
        legacy.check(
            digest == legacy.sha(receipt.get("adapter_parameter_sha256")),
            "Snapshot FP32 raw digest differs",
        )
        if step != 267:
            legacy.check(
                legacy.canonical(receipt) == legacy.canonical(train["checkpoints"][index]),
                "Train periodic receipt differs from actual checkpoint receipt",
            )
        results.append(
            {
                "completed_new_updates": step,
                "adapter_parameter_sha256": digest,
                "receipt": receipt_binding,
                "journal_rows": 4 * step,
                "peft_readme": readme,
            }
        )
    final = legacy.read_bound(results[-1]["receipt"])
    legacy.check(
        legacy.normalize(train.get("saved_adapter_weights")) == legacy.normalize(final["weights"])
        and legacy.normalize(train.get("saved_adapter_config"))
        == legacy.normalize(final["config"]),
        "Final saved adapter differs from the train report",
    )
    return results, digest, checks


def audit_terminal(plan_path, plan_sha256):
    legacy = load_legacy()
    sources = audit_sources()
    original = legacy.check_snapshots
    try:
        legacy.check_snapshots = lambda *args: check_snapshots(legacy, *args)
        report = legacy.audit_terminal(plan_path, plan_sha256)
    finally:
        legacy.check_snapshots = original
    if audit_sources() != sources:
        raise ValueError("An audit source changed during verification")
    report.update(
        contract=CONTRACT,
        inherited_audit_contract=legacy.CONTRACT,
        audit_sources=sources,
        audit_source_bindings_unchanged=True,
        snapshot_file_set="exact_standard_four_plus_observed_PEFT_README",
        peft_readme_count=34,
        peft_readme_bindings=[row["peft_readme"] for row in report["snapshots"]],
        peft_readme_bindings_rechecked_after_audit=True,
        peft_readme_observation_scope="current_audit_not_historical_checkpoint_receipt",
        peft_readme_is_parameter_or_quality_evidence=False,
        frozen_v1_snapshot_function_restored=True,
    )
    return report


def output_target(path):
    legacy = load_legacy()
    resolved = legacy.safe_path(path, exists=False)
    parent = legacy.safe_path(legacy.ROOT / "artifacts/diagnostics")
    legacy.check(
        resolved.parent == parent
        and resolved.name.startswith("onboard-navigation-recovery267-terminal-v2-"),
        "Fresh top-level v2 terminal diagnostics prefix is required",
    )
    legacy.check(not resolved.exists(), "Audit output directory is create-only")
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        output = output_target(args.output_dir)
        report = audit_terminal(args.plan, args.plan_sha256)
        report["created_at_utc"] = datetime.now(UTC).isoformat()
        output.mkdir(exist_ok=False)
        path = output / "report.json"
        with path.open("xb") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(
            json.dumps(
                {
                    "event": "recovery267_terminal_v2_filesystem_audit_finished",
                    "success": True,
                    "report": load_legacy().binding(path),
                }
            ),
            flush=True,
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "recovery267_terminal_v2_filesystem_audit_rejected",
                    "success": False,
                    "failure_type": type(error).__name__,
                    "failure": str(error)[:4096],
                    "model_loaded_by_audit": False,
                    "GPU_used_by_audit": False,
                    "optimizer_performed_by_audit": False,
                    "actual_tool_exit_status_independently_verified": False,
                    "actual_process_exit_independently_observed": False,
                    "quality_claim": False,
                    "candidate_promoted": False,
                    "execution_authority": False,
                    "final_release_allowed": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
