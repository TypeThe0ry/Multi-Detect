"""Current-source Windows Job supervisor for the bounded readonly origin probe.

Reuses the owned process-tree watchdog, not an old experiment's frozen source
snapshot. Records exact selected sources for this invocation; neither those
hashes nor successful diagnostics authorize Mission or qualify origin.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import require_real  # noqa: E402
from scripts import supervise_onboard_navigation_exact_load as owned  # noqa: E402

SOURCE_PATHS = (
    "scripts/supervise_teacher_origin_probe_v1.py",
    "scripts/probe_onboard_teacher_gazebo_navref.py",
    "scripts/supervise_onboard_navigation_exact_load.py",
    "scripts/onboard_teacher_gazebo_session.py",
    "scripts/onboard_teacher_gazebo_session_v3.py",
    "scripts/onboard_teacher_gazebo_session_v4.py",
    "scripts/onboard_teacher_sim_operator.py",
    "scripts/onboard_teacher_sim_operator_v2.py",
    "scripts/onboard_teacher_native_prefix.py",
    "scripts/onboard_teacher_telemetry_capture_v3.py",
    "scripts/onboard_teacher_observed_context_v2.py",
    "scripts/run_onboard_teacher_sim_operator_v2.py",
    "scripts/run_onboard_teacher_gazebo_observation_v2.py",
    "scripts/run_simulator_docker.py",
    "scripts/run_simulator_docker_acceptance.py",
    "scripts/gazebo_sensor_stream.py",
    "scripts/gazebo_depth_wire.py",
    "configs/product-version.json",
    "configs/training/onboard-policy-current-reference-v1.json",
    "configs/release-parity.json",
    "configs/simulation/teacher-sim-operator-20261008-r2.json",
    "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
    "configs/scenarios/urban_inspection_multirotor_001.runtime.json",
    "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf",
    "simulation/gazebo/models/x500_mono_cam/model.sdf",
    "simulation/gazebo/models/multidetect_search_target_v1/model.sdf",
    "src/multidetect/autonomy/contracts.py",
    "src/multidetect/clock.py",
)


def supervise(output_directory, diagnostics, *, timeout_s=300):
    duration = require_real(timeout_s, "timeout_s", minimum=60, maximum=600)
    output, diagnostics = Path(output_directory).resolve(), Path(diagnostics).resolve()
    for path in (output, diagnostics):
        if not path.is_relative_to(ROOT / "artifacts/diagnostics") or path.exists():
            raise ValueError("fresh contained supervisor/diagnostic directories required")
    if (
        output == diagnostics
        or output.is_relative_to(diagnostics)
        or diagnostics.is_relative_to(output)
    ):
        raise ValueError("separate non-nested supervisor and diagnostic paths required")
    before = [owned.record(ROOT / path) for path in SOURCE_PATHS]
    watchdog = next(
        row for row in before if row["path"].endswith("supervise_onboard_navigation_exact_load.py")
    )
    if watchdog["bytes"] != 29530 or watchdog["sha256"] != (
        "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69"
    ):
        raise ValueError("frozen owned process-tree watchdog differs")
    output.mkdir(exist_ok=False)
    command = [
        str(ROOT / ".venv-gazebo-navigation-20261008-r1/Scripts/python.exe"),
        "-I",
        "-B",
        "-S",
        "-c",
        owned.BOOTSTRAP,
        str(ROOT / "scripts/probe_onboard_teacher_gazebo_navref.py"),
        "--output-directory",
        str(diagnostics),
        "--operator-intent",
        str(ROOT / "configs/simulation/teacher-sim-operator-20261008-r2.json"),
        "--observation-s",
        "20",
        "--execute",
    ]
    child = owned.run_child(command, output, "teacher-origin-probe", time.monotonic() + duration)
    after = [owned.record(ROOT / path) for path in SOURCE_PATHS]
    receipt_path = diagnostics / "origin-probe.json"
    receipt = json.loads(receipt_path.read_bytes()) if receipt_path.is_file() else {}
    report = {
        "contract": "multidetect.teacher-origin-probe-supervised-v1",
        "child": child,
        "sources_before": before,
        "sources_after": after,
        "selected_sources_unchanged_during_run": before == after,
        "source_inventory_scope": "listed files only; not a complete dependency attestation",
        "absolute_timeout_s": duration,
        "origin_probe_receipt": owned.record(receipt_path) if receipt_path.is_file() else None,
        "probe_status": receipt.get("status"),
        "closed_snapshot_is_live_context": False,
        "origin_qualified": False,
        "current_runtime_release_parity_revalidation_claimed": False,
        "flight_commands_authorized": False,
        "model_or_optimizer_performed": False,
        "candidate_promoted": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    owned.write_new(output / "supervised-run.json", owned.canonical(report))
    print(
        owned.canonical(
            {
                "native_exit_code": child["native_exit_code"],
                "owned_tree_empty": child["owned_tree_empty"],
                "watchdog_fired": child["watchdog_fired"],
                "selected_sources_unchanged": before == after,
                "supervised_run": owned.record(output / "supervised-run.json"),
            }
        ).decode()
    )
    return 0 if owned.child_complete(child) and before == after else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--diagnostic-directory", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=300)
    args = parser.parse_args(argv)
    return supervise(args.output_directory, args.diagnostic_directory, timeout_s=args.timeout_s)


if __name__ == "__main__":
    raise SystemExit(main())
