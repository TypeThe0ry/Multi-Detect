"""Reopen a retained co-window capture; never reconstruct live flight authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy import sim_navigation_reference as navref  # noqa: E402
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding  # noqa: E402
from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import _json_new  # noqa: E402

EXTRACTION = (
    ROOT / "artifacts/diagnostics/onboard-teacher-navref-image-assets-20261008-r2/extraction.json"
)


def binding(path, *, cap=32 * 1024 * 1024):
    path = Path(path).absolute()
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(ROOT)
        or any(p.is_symlink() for p in path.parents)
    ):
        raise ValueError("contained original regular file required")
    before = path.stat()
    if not 0 < before.st_size <= cap:
        raise ValueError("source exceeds explicit byte cap")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_dev,
        after.st_ino,
    ):
        raise ValueError("source changed during binding")
    return EvidenceBinding(
        path.relative_to(ROOT).as_posix(),
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        "retained-navigation-window-source",
        None,
    )


def audit(capture, output_directory):
    directory = Path(output_directory).resolve()
    if directory.exists() or not directory.is_relative_to(ROOT / "artifacts/diagnostics"):
        raise ValueError("fresh contained output required")
    source = binding(capture)
    directory.mkdir(exist_ok=False)
    report = {
        "contract": "multidetect.teacher-navigation-window-file-audit-v1",
        "status": "not-audited",
        "capture_source": source.to_mapping(),
        "scope": "retained coordinate window only; not live context/runtime or flight admission",
        "audit": None,
        "error": None,
        "claims": {
            k: False
            for k in (
                "origin_qualified",
                "live_context_created",
                "flight_authority",
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
        document = strict_json_loads(Path(capture).read_bytes())
        extract_source = binding(EXTRACTION, cap=128 * 1024)
        extraction = strict_json_loads(EXTRACTION.read_bytes())
        if (
            extraction.get("status") != "original-image-assets-recorded"
            or extraction.get("image") != document["plan"]["docker_plan"]["image"]
            or extraction.get("never_started_identity_verified") is not True
            or extraction.get("owned_container_absent_after_cleanup") is not True
            or extraction.get("injected_native_used") is not False
        ):
            raise ValueError("retained fixed-image extraction does not match original runtime")
        models = {
            "x500_mono_cam": binding(ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf"),
        }
        for name, key in (("x500", "x500-model.sdf"), ("x500_base", "x500-base-model.sdf")):
            declared = extraction["assets"][key]["source"]
            actual = binding(declared["path"], cap=128 * 1024)
            if (actual.bytes, actual.sha256) != (declared["bytes"], declared["sha256"]):
                raise ValueError("fixed-image extracted model identity differs")
            models[name] = actual
        result = navref.audit_sim_navigation_window(
            artifact_root=ROOT,
            capture_source=source,
            world_source=binding(
                ROOT / "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf"
            ),
            model_sources=models,
            now_s=time.perf_counter(),
            clock_id=document["clock"]["session_id"],
        ).to_mapping()
        if result.get("origin_qualified") is not False or result.get("derived_origin") is not None:
            raise ValueError("retained-only audit must never return active navigation authority")
        if result.get("window_coordinates_verified") is not True:
            raise ValueError("retained window coordinates did not verify")
        if binding(capture) != source or binding(EXTRACTION, cap=128 * 1024) != extract_source:
            raise ValueError("audit sources changed during consumer access")
        report.update(
            status="retained-window-coordinates-verified",
            audit=result,
            extraction_source=extract_source.to_mapping(),
        )
    except Exception as error:
        report.update(
            status="retained-window-audit-refused", error=f"{type(error).__name__}: {error}"
        )
    receipt = _json_new(directory / "navigation-window-audit.json", report)
    print(json.dumps({"status": report["status"], "error": report["error"], "receipt": receipt}))
    return 0 if report["status"] == "retained-window-coordinates-verified" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    return audit(args.capture, args.output_directory)


if __name__ == "__main__":
    raise SystemExit(main())
