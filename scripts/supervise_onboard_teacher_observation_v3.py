"""Owned Windows Job supervision for the observation-only V3 CLI.

No model, flight authorization, Mission or automatic deployment. Original R1
and R2 experiments stay unchanged; every call requires new output directories.
"""

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

from scripts import supervise_onboard_navigation_exact_load as owned  # noqa: E402


def supervise(output, observation, *, timeout_s=300, context_timeout_s=30):
    if not 60 <= timeout_s <= 600 or not 1 <= context_timeout_s <= 60:
        raise ValueError("Bounded absolute and observation deadlines required")
    output, observation = Path(output).resolve(), Path(observation).resolve()
    for path in (output, observation):
        if not path.is_relative_to(ROOT) or path.exists():
            raise ValueError("Fresh contained output directories required")
    output.mkdir(exist_ok=False)
    previous = json.loads(
        (
            ROOT
            / (
                "artifacts/diagnostics/onboard-teacher-gazebo-observation-supervisor-20261008-r2/"
                "supervised-run.json"
            )
        ).read_text(encoding="utf-8")
    )
    # Actual old bindings are authority for frozen dependencies, not proof that
    # this new execution or any hardware passed.
    sources = [Path(row["path"]) for row in previous["sources_before"]]
    expected = {row["path"]: row for row in previous["sources_before"]}
    sources += [
        ROOT / path
        for path in (
            "scripts/onboard_teacher_telemetry_capture_v3.py",
            "tests/test_onboard_teacher_telemetry_capture_v3.py",
            "scripts/onboard_teacher_gazebo_session_v3.py",
            "tests/test_onboard_teacher_gazebo_session_v3.py",
            "scripts/run_onboard_teacher_gazebo_observation_v3.py",
            "tests/test_onboard_teacher_gazebo_observation_v3.py",
            "scripts/supervise_onboard_teacher_observation_v3.py",
        )
    ]
    before = [owned.record(path) for path in sources]
    for row in before:
        if row["path"] in expected and row != expected[row["path"]]:
            raise RuntimeError("Frozen dependency/protected identity differs before execution")
    supervisor_raw = Path(owned.__file__).read_bytes()
    if len(supervisor_raw) != 29530 or hashlib.sha256(supervisor_raw).hexdigest() != (
        "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69"
    ):
        raise RuntimeError("Generic owned supervisor source differs")
    python = ROOT / ".venv-gazebo-navigation-20261008-r1/Scripts/python.exe"
    command = [
        str(python),
        "-I",
        "-B",
        "-S",
        "-c",
        owned.BOOTSTRAP,
        str(ROOT / "scripts/run_onboard_teacher_gazebo_observation_v3.py"),
        "--output-directory",
        str(observation),
        "--execute",
        "--context-timeout-s",
        str(context_timeout_s),
    ]
    child = owned.run_child(command, output, "observation", time.monotonic() + timeout_s)
    after = [owned.record(path) for path in sources]
    report = {
        "contract": "multidetect.owned-teacher-observation-supervised-run-v3",
        "absolute_timeout_s": timeout_s,
        "child": child,
        "sources_before": before,
        "sources_after": after,
        "sources_unchanged": before == after,
        "flight_commands_authorized": False,
        "model_or_optimizer_performed": False,
        "final_release_allowed": False,
    }
    owned.write_new(output / "supervised-run.json", owned.canonical(report))
    print(
        owned.canonical(
            {
                "native_exit_code": child["native_exit_code"],
                "owned_tree_empty": child["owned_tree_empty"],
                "watchdog_fired": child["watchdog_fired"],
                "sources_unchanged": report["sources_unchanged"],
                "supervised_run": owned.record(output / "supervised-run.json"),
            }
        ).decode("utf-8")
    )
    return 0 if owned.child_complete(child) and before == after else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--observation-directory", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=300)
    parser.add_argument("--context-timeout-s", type=float, default=30)
    args = parser.parse_args(argv)
    return supervise(
        args.output_directory,
        args.observation_directory,
        timeout_s=args.timeout_s,
        context_timeout_s=args.context_timeout_s,
    )


if __name__ == "__main__":
    raise SystemExit(main())
