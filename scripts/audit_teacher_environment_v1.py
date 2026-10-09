"""Recompute a retained static safety environment; never grant live authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding  # noqa: E402
from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from multidetect.autonomy.sim_teacher_environment import (  # noqa: E402
    load_static_teacher_environment,
)
from scripts.run_onboard_teacher_gazebo_observation_v2 import _json_new  # noqa: E402


def audit(input_receipt, output_directory, queries, *, artifact_root=ROOT):
    root = Path(artifact_root).resolve(strict=True)
    directory = Path(output_directory).resolve()
    if directory.exists() or not directory.is_relative_to(root / "artifacts/diagnostics"):
        raise ValueError("fresh contained output directory required")
    source = Path(input_receipt)
    if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
        raise ValueError("contained regular input required")
    before = source.stat()
    if not source.is_file() or not 0 < before.st_size <= 1024 * 1024:
        raise ValueError("bounded input receipt required")
    raw = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_dev,
        after.st_ino,
    ):
        raise ValueError("input receipt changed during read")
    original = strict_json_loads(raw)
    if original.get("contract") != "multidetect.teacher-loaded-world-file-audit-v1":
        raise ValueError("expected loaded-world source listing")
    declaration = original.get("audit")
    if not isinstance(declaration, dict):
        raise ValueError("loaded-world source listing missing")
    if not isinstance(queries, (list, tuple)) or not 1 <= len(queries) <= 64:
        raise ValueError("one to 64 explicit surface queries required")
    directory.mkdir(parents=True, exist_ok=False)
    report = {
        "contract": "multidetect.teacher-static-environment-file-audit-v1",
        "status": "not-audited",
        "input_receipt_source": EvidenceBinding(
            source.resolve().relative_to(root).as_posix(),
            len(raw),
            hashlib.sha256(raw).hexdigest(),
            "retained-source-listing-not-authority",
            None,
        ).to_mapping(),
        "scope": "retained original static geometry, not a current live checkpoint",
        "environment": None,
        "surface_queries": [],
        "error": None,
        "claims": {
            name: False
            for name in (
                "legacy_map_DEM_flags_set",
                "current_inputs_fresh",
                "origin_qualified",
                "contact_verified",
                "complete_route_clearance_passed",
                "Mission_approved",
                "ARM_sent",
                "flight_executed",
                "optimizer_performed",
                "candidate_promoted",
                "hil_real_evidence",
                "outdoor_gnss_evidence",
                "final_release_allowed",
            )
        },
    }
    try:
        environment = load_static_teacher_environment(
            artifact_root=root,
            generated_sdf_source=EvidenceBinding.from_mapping(declaration["generated_sdf_source"]),
            world_source=EvidenceBinding.from_mapping(declaration["world_source"]),
            include_model_sources={
                uri: EvidenceBinding.from_mapping(binding)
                for uri, binding in declaration["include_model_sources"].items()
            },
        )
        results = [environment.sample_surface_bound(*query).to_mapping() for query in queries]
        environment.revalidate_sources(root)
        if source.read_bytes() != raw:
            raise ValueError("input receipt changed while auditing environment")
        report.update(
            status="retained-environment-recomputed",
            environment=environment.to_mapping(),
            environment_sha256=environment.canonical_hash,
            surface_queries=results,
        )
    except Exception as error:
        report.update(status="environment-audit-refused", error=f"{type(error).__name__}: {error}")
    binding = _json_new(directory / "environment-audit.json", report)
    print(json.dumps({"status": report["status"], "error": report["error"], "receipt": binding}))
    return 0 if report["status"] == "retained-environment-recomputed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loaded-world-audit", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--query-enu", nargs=2, type=float, action="append", required=True)
    args = parser.parse_args(argv)
    return audit(args.loaded_world_audit, args.output_directory, args.query_enu)


if __name__ == "__main__":
    raise SystemExit(main())
