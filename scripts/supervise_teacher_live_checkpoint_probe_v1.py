"""Own the no-flight live checkpoint worker and verify exact-container cleanup."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import require_real  # noqa: E402
from scripts import supervise_onboard_navigation_exact_load as owned  # noqa: E402
from scripts.run_teacher_live_checkpoint_probe_v1 import (  # noqa: E402
    CONTRACT,
    FALSE_CLAIMS,
    LIFECYCLE_SOURCE_PATHS,
    SCENARIO,
    fresh_output,
    validate_capture_versions,
)
from scripts.run_teacher_live_checkpoint_probe_v1 import (  # noqa: E402
    SOURCE_PATHS as WORKER_SOURCES,
)

SOURCE_PATHS = tuple(
    dict.fromkeys(
        (
            *WORKER_SOURCES,
            "scripts/supervise_teacher_live_checkpoint_probe_v1.py",
            "scripts/supervise_onboard_navigation_exact_load.py",
            "configs/simulation/teacher-sim-operator-20261008-r2.json",
            "scripts/collect_teacher_live_navigation_checkpoint_v2.py",
        )
    )
)
RECEIPT_CAP = 32 * 1024 * 1024


def _native(command, output, name):
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if len(result.stdout) + len(result.stderr) > 256 * 1024:
        raise ValueError("bounded cleanup output exceeded")
    row = {"command": command, "native_exit_code": result.returncode, "streams": {}}
    for label, payload in (("stdout", result.stdout), ("stderr", result.stderr)):
        path = output / (name + "." + label + ".bin")
        owned.write_new(path, payload)
        row["streams"][label] = owned.record(path, cap=256 * 1024)
    return result, row


def verify_cleanup(diagnostics, output):
    """Never delete by name; match this run's original intent and exact CID."""
    session_dir = diagnostics.parent / (diagnostics.name + "-job") / "teacher-owned-session"
    intent_path = session_dir / "intent.json"
    if not intent_path.is_file():
        return {
            "verified": False,
            "error": "no durable session ownership intent",
            "native_rows": [],
        }
    intent = json.loads(intent_path.read_bytes())
    token = intent["owner_token"]
    docker = intent["plan"]["docker_plan"]
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValueError("original ownership token malformed")
    cid_path = session_dir / "native-container.cid"
    cid = cid_path.read_text().strip() if cid_path.is_file() else None
    if cid is not None and re.fullmatch(r"[0-9a-f]{64}", cid) is None:
        raise ValueError("original owned CID malformed")
    target = cid or docker["container_name"]
    executable = docker["docker_executable"]
    installed = shutil.which("docker")
    if installed is None or Path(executable).resolve() != Path(installed).resolve():
        raise ValueError("cleanup executable differs from local Docker")
    from scripts.run_simulator_docker import PINNED_IMAGE

    if docker["image"] != PINNED_IMAGE or docker["container_name"] != "multidetect-teacher-gazebo":
        raise ValueError("cleanup plan differs from exact disposable SIM scope")
    inspected, row = _native(
        [executable, "container", "inspect", target], output, "cleanup-inspect-before"
    )
    rows = [row]

    def absent(result, exact):
        return (
            result.returncode == 1
            and re.search(
                rf"No such (?:container|object):\s*{re.escape(exact)}(?:\s|$)",
                result.stderr.decode("utf-8", errors="replace"),
                re.IGNORECASE,
            )
            is not None
        )

    if absent(inspected, target):
        return {"verified": True, "already_absent": True, "container_id": cid, "native_rows": rows}
    if inspected.returncode != 0:
        return {
            "verified": False,
            "error": "Docker inspect failure is not absence",
            "native_rows": rows,
        }
    records = json.loads(inspected.stdout)
    if type(records) is not list or len(records) != 1:
        raise ValueError("one exact owned container inspection required")
    record = records[0]
    config, identity = record.get("Config", {}), record.get("Id", "")
    labels = config.get("Labels", {})
    if not (
        re.fullmatch(r"[0-9a-f]{64}", identity)
        and (cid is None or identity == cid)
        and record.get("Name") == "/" + docker["container_name"]
        and config.get("Image") == docker["image"]
        and labels.get("multidetect.teacher-session-owner") == token
        and labels.get("multidetect.teacher-session-purpose") == "truth-free-teacher-observation"
        and labels.get("multidetect.purpose") == "simulator-docker"
    ):
        return {
            "verified": False,
            "error": "foreign ownership; container untouched",
            "native_rows": rows,
        }
    failed = False
    for name, args in (
        ("cleanup-stop", ["stop", "--timeout", "2", identity]),
        ("cleanup-rm", ["rm", identity]),
    ):
        command = [executable, *args]
        try:
            result, row = _native(command, output, name)
            rows.append(row)
            failed |= result.returncode != 0
        except (OSError, subprocess.TimeoutExpired) as error:
            failed = True
            rows.append(
                {
                    "command": command,
                    "native_exit_code": None,
                    "error": f"{type(error).__name__}: {error}"[:2048],
                }
            )
    final_absent = False
    command = [executable, "container", "inspect", identity]
    try:
        result, row = _native(command, output, "cleanup-inspect-after")
        rows.append(row)
        final_absent = absent(result, identity)
    except (OSError, subprocess.TimeoutExpired) as error:
        failed = True
        rows.append(
            {
                "command": command,
                "native_exit_code": None,
                "error": f"{type(error).__name__}: {error}"[:2048],
            }
        )
    return {
        "verified": not failed and final_absent,
        "absence_observed_after_cleanup": final_absent,
        "already_absent": False,
        "container_id": identity,
        "native_rows": rows,
    }


def supervise(
    output_directory,
    diagnostics,
    *,
    timeout_s=300,
    capture_version=3,
    collector_version=2,
    session_timeout_s=240,
):
    duration = require_real(timeout_s, "timeout_s", minimum=60, maximum=540)
    validate_capture_versions(capture_version, collector_version)
    session_duration = require_real(session_timeout_s, "session_timeout_s", minimum=1, maximum=540)
    if capture_version == 4 and (session_duration < 310 or duration < session_duration):
        raise ValueError("Supervisor/session must cover the entire NEW bound lifecycle")
    output, diagnostics = fresh_output(output_directory, ROOT), fresh_output(diagnostics, ROOT)
    if (
        output == diagnostics
        or output.is_relative_to(diagnostics)
        or diagnostics.is_relative_to(output)
    ):
        raise ValueError("separate nonnested supervisor/worker paths required")
    paths = tuple(
        dict.fromkeys((*SOURCE_PATHS, *(LIFECYCLE_SOURCE_PATHS if capture_version == 4 else ())))
    )
    before = [owned.record(ROOT / path) for path in paths]
    watchdog = next(
        row for row in before if row["path"].endswith("supervise_onboard_navigation_exact_load.py")
    )
    if (watchdog["bytes"], watchdog["sha256"]) != (
        29530,
        "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69",
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
        str(ROOT / "scripts/run_teacher_live_checkpoint_probe_v1.py"),
        "--scenario",
        str(ROOT / SCENARIO),
        "--output-directory",
        str(diagnostics),
        "--operator-intent",
        str(ROOT / "configs/simulation/teacher-sim-operator-20261008-r2.json"),
        "--execute",
        "--collector-version",
        str(collector_version),
        "--capture-version",
        str(capture_version),
        "--session-timeout-s",
        str(session_duration),
    ]
    child = owned.run_child(
        command, output, "teacher-current-navigation", time.monotonic() + duration
    )
    cleanup = None
    try:
        cleanup = verify_cleanup(diagnostics, output)
    except BaseException as error:
        cleanup = {"verified": False, "error": f"{type(error).__name__}: {error}"[:2048]}
    after = [owned.record(ROOT / path) for path in paths]
    receipt_path = diagnostics / "current-navigation-probe.json"
    binding = owned.record(receipt_path, cap=RECEIPT_CAP) if receipt_path.is_file() else None
    report = json.loads(receipt_path.read_bytes()) if binding else {}
    if binding and owned.record(receipt_path, cap=RECEIPT_CAP) != binding:
        raise ValueError("worker receipt changed while reading")
    result = {
        "contract": "multidetect.teacher-current-navigation-supervised-v1",
        "child": child,
        "sources_before": before,
        "sources_after": after,
        "selected_sources_unchanged_during_run": before == after,
        "source_inventory_scope": "listed sources only, not complete dependency attestation",
        "absolute_worker_timeout_s": duration,
        "capture_version": capture_version,
        "collector_version": collector_version,
        "session_timeout_s": session_duration,
        "cleanup_reserve_s": 60,
        "worker_receipt": binding,
        "worker_status": report.get("status"),
        "independent_cleanup": cleanup,
        "claims": dict(FALSE_CLAIMS),
    }
    owned.write_new(output / "supervised-run.json", owned.canonical(result))
    complete = (
        owned.child_complete(child)
        and before == after
        and cleanup["verified"]
        and report.get("contract") == CONTRACT
        and report.get("status") == "checkpoint-recorded"
    )
    print(
        owned.canonical(
            {
                "native_exit_code": child["native_exit_code"],
                "owned_tree_empty": child["owned_tree_empty"],
                "watchdog_fired": child["watchdog_fired"],
                "selected_sources_unchanged": before == after,
                "worker_status": report.get("status"),
                "independent_cleanup_verified": cleanup["verified"],
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
    parser.add_argument("--capture-version", type=int, choices=(3, 4), default=3)
    parser.add_argument("--collector-version", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--session-timeout-s", type=float, default=240)
    args = parser.parse_args(argv)
    return supervise(
        args.output_directory,
        args.diagnostic_directory,
        timeout_s=args.timeout_s,
        capture_version=args.capture_version,
        collector_version=args.collector_version,
        session_timeout_s=args.session_timeout_s,
    )


if __name__ == "__main__":
    raise SystemExit(main())
