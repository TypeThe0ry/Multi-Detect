"""Owned Windows Job/watchdog for read-only PX4/Gazebo diagnostic execution."""

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

from multidetect.autonomy.serialization import require_real  # noqa: E402
from scripts import supervise_onboard_navigation_exact_load as owned  # noqa: E402


def supervise(output, diagnostics, *, timeout_s=300, observation_s=20):
    timeout = require_real(timeout_s, "timeout_s", minimum=60, maximum=600)
    observation = require_real(observation_s, "observation_s", minimum=1, maximum=30)
    output, diagnostics = Path(output).resolve(), Path(diagnostics).resolve()
    require_root = ROOT / "artifacts/diagnostics"
    for path in (output, diagnostics):
        if not path.is_relative_to(require_root) or path.exists():
            raise ValueError("Fresh contained diagnostic directories required")
    previous = json.loads(
        (
            require_root
            / ("onboard-teacher-gazebo-observation-supervisor-20261008-r3/supervised-run.json")
        ).read_text(encoding="utf-8")
    )
    expected = {row["path"]: row for row in previous["sources_before"]}
    sources = [Path(row["path"]) for row in previous["sources_before"]]
    sources += [
        ROOT / path
        for path in (
            "scripts/onboard_teacher_gazebo_session_v4.py",
            "tests/test_onboard_teacher_gazebo_session_v4.py",
            "scripts/run_onboard_teacher_px4_diagnostics.py",
            "tests/test_run_onboard_teacher_px4_diagnostics.py",
            "scripts/diagnose_onboard_teacher_px4_events.py",
            "tests/test_diagnose_onboard_teacher_px4_events.py",
            "scripts/diagnose_onboard_teacher_px4_parameters.py",
            "tests/test_onboard_teacher_px4_parameter_diagnostics.py",
            "scripts/extract_onboard_teacher_px4_metadata.py",
            "tests/test_extract_onboard_teacher_px4_metadata.py",
            "scripts/supervise_onboard_teacher_px4_diagnostics.py",
            ".venv-gazebo-navigation-20261008-r1/Lib/site-packages/pymavlink/dialects/v20/common.py",
            "artifacts/diagnostics/onboard-teacher-px4-image-metadata-20261008-r2/all_events.json",
            "artifacts/diagnostics/onboard-teacher-px4-image-metadata-20261008-r2/parameters.json",
        )
    ]
    before = [owned.record(path) for path in sources]
    for row in before:
        if row["path"] in expected and row != expected[row["path"]]:
            raise RuntimeError("Frozen dependency or protected identity changed before execution")
    raw = Path(owned.__file__).read_bytes()
    if len(raw) != 29530 or hashlib.sha256(raw).hexdigest() != (
        "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69"
    ):
        raise RuntimeError("Original owned Job supervisor changed")
    output.mkdir(exist_ok=False)
    command = [
        str(ROOT / ".venv-gazebo-navigation-20261008-r1/Scripts/python.exe"),
        "-I",
        "-B",
        "-S",
        "-c",
        owned.BOOTSTRAP,
        str(ROOT / "scripts/run_onboard_teacher_px4_diagnostics.py"),
        "--output-directory",
        str(diagnostics),
        "--execute",
        "--observation-s",
        str(observation),
    ]
    child = owned.run_child(command, output, "px4-diagnostics", time.monotonic() + timeout)
    after = [owned.record(path) for path in sources]
    report = {
        "contract": "multidetect.teacher-PX4-readonly-supervised-run-v1",
        "absolute_timeout_s": timeout,
        "observation_s": observation,
        "child": child,
        "sources_before": before,
        "sources_after": after,
        "sources_unchanged": before == after,
        "flight_commands_authorized": False,
        "parameters_changed": False,
        "model_or_optimizer_performed": False,
        "context_qualified": False,
        "final_release_allowed": False,
    }
    owned.write_new(output / "supervised-run.json", owned.canonical(report))
    print(
        owned.canonical(
            {
                "native_exit_code": child["native_exit_code"],
                "owned_tree_empty": child["owned_tree_empty"],
                "watchdog_fired": child["watchdog_fired"],
                "sources_unchanged": before == after,
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
    parser.add_argument("--observation-s", type=float, default=20)
    args = parser.parse_args(argv)
    return supervise(
        args.output_directory,
        args.diagnostic_directory,
        timeout_s=args.timeout_s,
        observation_s=args.observation_s,
    )


if __name__ == "__main__":
    raise SystemExit(main())
