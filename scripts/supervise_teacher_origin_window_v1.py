"""Bound the NEW concurrent origin-window worker with an owned Windows Job.

The source inventory is explicitly partial. Closed receipts are not live state
or navigation qualification. Reuses the existing process-tree watchdog exactly.
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
from scripts.supervise_teacher_origin_probe_v1 import SOURCE_PATHS as PREVIOUS_SOURCES  # noqa: E402

SOURCE_PATHS = tuple(
    dict.fromkeys(
        (
            "scripts/supervise_teacher_origin_window_v1.py",
            "scripts/collect_onboard_teacher_gazebo_origin_window.py",
            *PREVIOUS_SOURCES,
        )
    )
)
MAXIMUM_RECEIPT_BYTES = 32 * 1024 * 1024


def supervise(output_directory, diagnostics, *, timeout_s=300):
    duration = require_real(timeout_s, "timeout_s", minimum=60, maximum=600)
    output, diagnostics = Path(output_directory).resolve(), Path(diagnostics).resolve()
    if any(
        not path.is_relative_to(ROOT / "artifacts/diagnostics") or path.exists()
        for path in (output, diagnostics)
    ):
        raise ValueError("fresh contained supervisor and diagnostic directories required")
    if (
        output == diagnostics
        or output.is_relative_to(diagnostics)
        or diagnostics.is_relative_to(output)
    ):
        raise ValueError("separate non-nested output paths required")
    before = [owned.record(ROOT / path) for path in SOURCE_PATHS]
    watchdog = next(
        x for x in before if x["path"].endswith("supervise_onboard_navigation_exact_load.py")
    )
    if watchdog["bytes"] != 29530 or watchdog["sha256"] != (
        "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69"
    ):
        raise ValueError("owned process-tree watchdog source differs")
    output.mkdir(exist_ok=False)
    command = [
        str(ROOT / ".venv-gazebo-navigation-20261008-r1/Scripts/python.exe"),
        "-I",
        "-B",
        "-S",
        "-c",
        owned.BOOTSTRAP,
        str(ROOT / "scripts/collect_onboard_teacher_gazebo_origin_window.py"),
        "--output-directory",
        str(diagnostics),
        "--operator-intent",
        str(ROOT / "configs/simulation/teacher-sim-operator-20261008-r2.json"),
        "--observation-s",
        "30",
        "--execute",
    ]
    child = owned.run_child(command, output, "teacher-origin-window", time.monotonic() + duration)
    after = [owned.record(ROOT / path) for path in SOURCE_PATHS]
    receipt_path = diagnostics / "origin-window.json"
    receipt_binding = (
        owned.record(receipt_path, cap=MAXIMUM_RECEIPT_BYTES) if receipt_path.is_file() else None
    )
    receipt = json.loads(receipt_path.read_bytes()) if receipt_binding else {}
    # The binding is checked a second time after JSON access; merely opening the
    # receipt again neither refreshes sources nor attests a running container.
    if receipt_binding and owned.record(receipt_path, cap=MAXIMUM_RECEIPT_BYTES) != receipt_binding:
        raise ValueError("worker receipt changed during supervised read")
    report = {
        "contract": "multidetect.teacher-origin-window-supervised-v1",
        "child": child,
        "sources_before": before,
        "sources_after": after,
        "selected_sources_unchanged_during_run": before == after,
        "source_inventory_scope": "listed files only, not complete dependency attestation",
        "absolute_timeout_s": duration,
        "receipt_byte_cap": MAXIMUM_RECEIPT_BYTES,
        "origin_window_receipt": receipt_binding,
        "worker_status": receipt.get("status"),
        "claims": {
            k: False
            for k in (
                "closed_snapshot_is_live_context",
                "origin_qualified",
                "flight_commands_authorized",
                "current_runtime_release_parity_revalidation_claimed",
                "model_or_optimizer_performed",
                "candidate_promoted",
                "hil_real_evidence",
                "outdoor_gnss_evidence",
                "final_release_allowed",
            )
        },
    }
    owned.write_new(output / "supervised-run.json", owned.canonical(report))
    complete = (
        owned.child_complete(child)
        and before == after
        and receipt.get("contract") == "multidetect.teacher-SIM-origin-window-v1"
        and receipt.get("status") == "origin-window-recorded"
    )
    print(
        owned.canonical(
            {
                "native_exit_code": child["native_exit_code"],
                "owned_tree_empty": child["owned_tree_empty"],
                "watchdog_fired": child["watchdog_fired"],
                "selected_sources_unchanged": before == after,
                "worker_status": receipt.get("status"),
                "supervised_run": owned.record(output / "supervised-run.json"),
            }
        ).decode()
    )
    return 0 if complete else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--diagnostic-directory", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=300)
    args = parser.parse_args(argv)
    return supervise(args.output_directory, args.diagnostic_directory, timeout_s=args.timeout_s)


if __name__ == "__main__":
    raise SystemExit(main())
