"""Reopen a retained native generated-world reply; never grant live authority."""

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
from multidetect.autonomy.sim_loaded_world import audit_loaded_static_geometry  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import _json_new  # noqa: E402


def _binding(path):
    path = Path(path).resolve(strict=True)
    relative = path.relative_to(ROOT).as_posix()
    before = path.stat()
    if not path.is_file() or not 0 < before.st_size <= 1024 * 1024:
        raise ValueError("bounded contained original source required")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError("source changed during binding")
    return EvidenceBinding(relative, len(raw), hashlib.sha256(raw).hexdigest(), relative, None)


def audit(native_output, output_directory):
    directory = Path(output_directory).resolve()
    if directory.exists() or not directory.is_relative_to(ROOT / "artifacts/diagnostics"):
        raise ValueError("fresh contained output required")
    source = _binding(native_output)
    directory.mkdir(exist_ok=False)
    report = {
        "contract": "multidetect.teacher-loaded-world-file-audit-v1",
        "status": "not-audited",
        "generated_sdf_source": source.to_mapping(),
        "scope": (
            "retained server response definitions only; "
            "not live identity/freshness or solver contact"
        ),
        "audit": None,
        "error": None,
        "claims": {
            k: False
            for k in (
                "origin_qualified",
                "route_clearance_passed",
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
        result = audit_loaded_static_geometry(
            artifact_root=ROOT,
            generated_sdf_source=source,
            world_source=_binding(
                ROOT / "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf"
            ),
            include_model_sources={
                "model://multidetect_search_target_v1": _binding(
                    ROOT / "simulation/gazebo/models/multidetect_search_target_v1/model.sdf"
                )
            },
        )
        report.update(
            status="loaded-definitions-match"
            if result.collision_definitions_match
            else "loaded-definitions-differ",
            audit=result.to_mapping(),
        )
    except Exception as error:
        report.update(
            status="loaded-definitions-audit-refused", error=f"{type(error).__name__}: {error}"
        )
    binding = _json_new(directory / "loaded-world-audit.json", report)
    print(json.dumps({"status": report["status"], "error": report["error"], "receipt": binding}))
    return 0 if report["status"] == "loaded-definitions-match" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-output", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    return audit(args.native_output, args.output_directory)


if __name__ == "__main__":
    raise SystemExit(main())
