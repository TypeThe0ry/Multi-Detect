"""Command line entry point: ``python -m multidetect.simulator``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .closed_loop import main as closed_loop_main
from .external_stack import ExternalStackError, build_external_stack_plan
from .interactive_acceptance import main as interactive_acceptance_main
from .isolation import discover_simulator_tools
from .orchestrator import SimulatorOrchestrator, SimulatorOrchestratorError
from .px4_launch import Px4LaunchError, build_px4_launch_plan
from .runtime_preflight import build_runtime_preflight_document
from .scenario_matrix import main as scenario_matrix_main
from .scenario_runner import main as scenario_main
from .stack import (
    DEFAULT_METADATA_PORT,
    DEFAULT_RTSP_PORT,
    SimulatorStackError,
    SimulatorStackPorts,
    build_stack_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multi-Detect isolated 3-D simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="probe PX4/Gazebo/Docker availability")
    probe.add_argument("--json", action="store_true", dest="as_json")
    runtime_preflight = sub.add_parser(
        "runtime-preflight",
        help=(
            "write read-only native PX4/Gazebo, Docker daemon and WSL readiness "
            "evidence without starting a runtime"
        ),
    )
    runtime_preflight.add_argument(
        "--out",
        type=Path,
        required=True,
        help="JSON evidence path",
    )
    runtime_preflight.add_argument(
        "--repository-root",
        type=Path,
        help=(
            "optional repository root for read-only world/model asset binding; "
            "defaults to the package repository when --scenario is supplied"
        ),
    )
    runtime_preflight.add_argument(
        "--scenario",
        type=Path,
        help="optional SIM scenario manifest to resolve during asset preflight",
    )
    runtime_preflight.add_argument(
        "--px4-workdir",
        type=Path,
        help="optional PX4 checkout to validate against etc/init.d-posix/rcS",
    )
    stack_plan = sub.add_parser(
        "stack-plan",
        help=(
            "resolve repository-owned SIM world/model assets and emit a hash-bound "
            "PX4/Gazebo launch plan without starting processes"
        ),
    )
    stack_plan.add_argument(
        "--scenario",
        type=Path,
        required=True,
        help="SIM scenario manifest (.scenario.json)",
    )
    stack_plan.add_argument(
        "--out",
        type=Path,
        help="optional JSON evidence path (the plan is also printed to stdout)",
    )
    stack_plan.add_argument(
        "--repository-root",
        "--repo-root",
        dest="repository_root",
        type=Path,
        help="repository root containing simulation/gazebo assets",
    )
    stack_plan.add_argument(
        "--gazebo-executable",
        "--gazebo",
        dest="gazebo_executable",
        default="gz",
        help="bare gz/gazebo/ign command or an existing absolute executable path",
    )
    stack_plan.add_argument(
        "--px4-executable",
        "--px4",
        dest="px4_executable",
        default="px4",
        help="bare px4 command or an existing absolute executable path",
    )
    stack_plan.add_argument(
        "--gazebo-flavor",
        "--flavor",
        dest="gazebo_flavor",
        choices=("gz", "gazebo", "ign"),
        help="override the Gazebo command flavor (must match --gazebo)",
    )
    stack_plan.add_argument(
        "--host",
        default="127.0.0.1",
        help="literal loopback host for every SIM endpoint",
    )
    stack_plan.add_argument(
        "--px4-control-port",
        "--px4-port",
        dest="px4_control_port",
        type=int,
        default=14540,
        help="loopback PX4 MANUAL_CONTROL output port",
    )
    stack_plan.add_argument(
        "--px4-telemetry-port",
        dest="px4_telemetry_port",
        type=int,
        default=14541,
        help="loopback PX4 telemetry input port",
    )
    stack_plan.add_argument(
        "--qgc-output-port",
        dest="qgc_output_port",
        type=int,
        default=14649,
        help="loopback QGC telemetry/output port",
    )
    stack_plan.add_argument(
        "--qgc-input-port",
        dest="qgc_input_port",
        type=int,
        default=14650,
        help="loopback QGC MANUAL_CONTROL input port",
    )
    stack_plan.add_argument(
        "--metadata-port",
        dest="metadata_port",
        type=int,
        default=14640,
        help="loopback metadata/acceptance port",
    )
    stack_plan.add_argument(
        "--rtsp-port",
        dest="rtsp_port",
        type=int,
        default=8554,
        help="loopback SIM camera RTSP port",
    )
    stack_plan.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
        help="plan headless Gazebo rendering (default)",
    )
    stack_plan.add_argument(
        "--gui",
        dest="headless",
        action="store_false",
        help="plan a visible Gazebo renderer instead of headless mode",
    )
    stack_plan.add_argument(
        "--realtime",
        dest="realtime",
        action="store_true",
        default=True,
        help="run Gazebo in realtime mode (default)",
    )
    stack_plan.add_argument(
        "--no-realtime",
        dest="realtime",
        action="store_false",
        help="plan paused/non-realtime Gazebo execution",
    )
    stack_plan.add_argument(
        "--px4-workdir",
        type=Path,
        help="optional PX4 working directory inside the repository",
    )
    external_plan = sub.add_parser(
        "external-plan",
        help=(
            "turn a repository-owned stack plan into PX4/Gazebo process specs "
            "and JSON evidence without starting processes"
        ),
    )
    external_plan.add_argument(
        "--scenario",
        type=Path,
        required=True,
        help="SIM scenario manifest (.scenario.json)",
    )
    external_plan.add_argument(
        "--repository-root", "--repo-root", dest="repository_root", type=Path
    )
    external_plan.add_argument("--job-dir", type=Path, help="disposable evidence/job directory")
    external_plan.add_argument("--log-dir", type=Path, help="log directory below --job-dir")
    external_plan.add_argument("--px4-workdir", type=Path)
    external_plan.add_argument("--px4-executable", "--px4", dest="px4_executable", default="px4")
    external_plan.add_argument(
        "--gazebo-executable", "--gazebo", dest="gazebo_executable", default="gz"
    )
    external_plan.add_argument(
        "--gazebo-flavor",
        "--flavor",
        dest="gazebo_flavor",
        choices=("gz", "gazebo", "ign"),
    )
    external_plan.add_argument("--host", default="127.0.0.1")
    external_plan.add_argument(
        "--px4-control-port",
        "--px4-port",
        dest="px4_control_port",
        type=int,
        default=14540,
    )
    external_plan.add_argument("--px4-telemetry-port", type=int, default=14541)
    external_plan.add_argument("--qgc-output-port", type=int, default=14649)
    external_plan.add_argument("--qgc-input-port", type=int, default=14650)
    external_plan.add_argument("--metadata-port", type=int, default=14640)
    external_plan.add_argument("--rtsp-port", type=int, default=8554)
    external_plan.add_argument("--headless", dest="headless", action="store_true", default=True)
    external_plan.add_argument("--gui", dest="headless", action="store_false")
    external_plan.add_argument("--realtime", dest="realtime", action="store_true", default=True)
    external_plan.add_argument("--no-realtime", dest="realtime", action="store_false")
    mode = external_plan.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true")
    mode.add_argument(
        "--strict",
        dest="dry_run",
        action="store_false",
        help="require both tools and a resolved PX4 workdir (still never starts them)",
    )
    external_plan.set_defaults(dry_run=True)
    external_px4_launch_mode = external_plan.add_mutually_exclusive_group()
    external_px4_launch_mode.add_argument(
        "--px4-launch",
        dest="px4_launch_enabled",
        action="store_true",
        help="include a job-local hash-bound PX4 startup wrapper in the process plan",
    )
    external_px4_launch_mode.add_argument(
        "--no-px4-launch",
        dest="px4_launch_enabled",
        action="store_false",
        help="keep the raw PX4 command in the external process plan (default)",
    )
    external_plan.set_defaults(px4_launch_enabled=False)
    external_plan.add_argument(
        "--px4-launch-strict",
        dest="px4_launch_strict",
        action="store_true",
        default=None,
        help="require PX4 wrapper readiness when --px4-launch is enabled",
    )
    external_plan.add_argument(
        "--inherit-environment",
        action="store_true",
        help="record that a future runner may inherit host environment (not expanded here)",
    )
    external_plan.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="add a validated environment overlay; bound SIM keys cannot change",
    )
    external_plan.add_argument("--out", type=Path)
    px4_launch_plan = sub.add_parser(
        "px4-launch-plan",
        help=(
            "prepare a job-local, hash-bound PX4 startup wrapper and patched "
            "rc.mavlink without starting PX4"
        ),
    )
    px4_launch_plan.add_argument(
        "--scenario",
        type=Path,
        required=True,
        help="SIM scenario manifest (.scenario.json)",
    )
    px4_launch_plan.add_argument(
        "--repository-root",
        "--repo-root",
        dest="repository_root",
        type=Path,
        help="repository root containing simulation/gazebo assets",
    )
    px4_launch_plan.add_argument(
        "--job-dir",
        type=Path,
        help="job-owned directory for generated wrapper/rc.mavlink/evidence",
    )
    px4_launch_plan.add_argument(
        "--px4-workdir",
        type=Path,
        help="PX4 build directory containing etc/init.d-posix/rcS",
    )
    px4_launch_plan.add_argument(
        "--px4-executable",
        "--px4",
        dest="px4_executable",
        default="px4",
        help="bare px4 command or an existing absolute executable path",
    )
    px4_launch_plan.add_argument(
        "--host",
        default="127.0.0.1",
        help="literal loopback host for the generated PX4 MAVLink endpoints",
    )
    px4_launch_plan.add_argument(
        "--px4-control-port",
        "--control-port",
        dest="px4_control_port",
        type=int,
        default=14540,
        help="loopback PX4 MANUAL_CONTROL/GCS port",
    )
    px4_launch_plan.add_argument(
        "--px4-telemetry-port",
        "--telemetry-port",
        dest="px4_telemetry_port",
        type=int,
        default=14541,
        help="loopback PX4 telemetry publication port",
    )
    launch_mode = px4_launch_plan.add_mutually_exclusive_group()
    launch_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="generate diagnostic artifacts even when PX4 files/tools are absent (default)",
    )
    launch_mode.add_argument(
        "--strict",
        dest="dry_run",
        action="store_false",
        help="require PX4 executable, workdir and startup files to be launch-ready",
    )
    px4_launch_plan.set_defaults(dry_run=True)
    px4_launch_plan.add_argument(
        "--no-write-evidence",
        dest="write_evidence",
        action="store_false",
        help="do not write the default job-local px4-launch.json (stdout is still emitted)",
    )
    px4_launch_plan.set_defaults(write_evidence=True)
    px4_launch_plan.add_argument(
        "--out",
        type=Path,
        help="optional evidence path; it must remain below --job-dir",
    )
    closed_loop = sub.add_parser(
        "closed-loop",
        help=(
            "run the deterministic multirotor/VTOL/fixed-wing closed-loop "
            "fixture without external I/O"
        ),
    )
    closed_loop.add_argument("--mode", choices=("vtol", "fixed-wing", "multirotor"), default="vtol")
    closed_loop.add_argument("--seed", type=int, default=42017)
    closed_loop.add_argument("--steps", type=int, default=60)
    closed_loop.add_argument("--dt", type=float, default=0.1)
    closed_loop.add_argument("--wind-east", dest="wind_east", type=float, default=0.0)
    closed_loop.add_argument("--wind-north", dest="wind_north", type=float, default=0.0)
    closed_loop.add_argument("--out", type=Path)
    interactive_acceptance = sub.add_parser(
        "interactive-acceptance",
        help=(
            "exercise the native QGC MANUAL_CONTROL/telemetry router against "
            "the deterministic rotorcraft/VTOL/fixed-wing model"
        ),
    )
    interactive_acceptance.add_argument(
        "--mode",
        choices=("both", "all", "vtol", "fixed-wing", "multirotor"),
        default="both",
    )
    interactive_acceptance.add_argument("--out", type=Path)
    scenario = sub.add_parser(
        "scenario",
        help="run a verified runtime-channel scenario through the deterministic SIM loop",
    )
    scenario.add_argument("--scenario", type=Path, required=True)
    scenario.add_argument(
        "--mission",
        type=Path,
        help=(
            "optional ExecutableMission or PlannedMissionCompilation JSON to bind "
            "to each scenario step"
        ),
    )
    scenario.add_argument(
        "--start-mission",
        action="store_true",
        help="explicitly send MISSION_START after binding --mission",
    )
    scenario.add_argument("--frames", type=int, default=600)
    scenario.add_argument("--dt", type=float, default=0.1)
    scenario.add_argument("--mode", choices=("vtol", "fixed-wing", "multirotor"))
    scenario.add_argument(
        "--no-ai",
        action="store_true",
        help="keep the authority paused and exercise only the deterministic flight model",
    )
    scenario.add_argument(
        "--fault",
        action="append",
        dest="fault_ids",
        help="select a runtime fault event (repeat the option for multiple IDs)",
    )
    scenario.add_argument("--out", type=Path)
    scenario_matrix = sub.add_parser(
        "scenario-matrix",
        help=(
            "run every repository-owned deterministic SIM scenario and verify "
            "repeatable truth-free evidence"
        ),
    )
    scenario_matrix.add_argument(
        "--scenario-directory",
        "--directory",
        dest="scenario_directory",
        type=Path,
        help=(
            "directory containing *.scenario.json manifests (default: repository configs/scenarios)"
        ),
    )
    scenario_matrix.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        type=Path,
        help="explicit manifest; repeat for multiple scenarios (default discovers all)",
    )
    scenario_matrix.add_argument("--frames", type=int, default=120)
    scenario_matrix.add_argument("--dt", type=float, default=0.1)
    scenario_matrix.add_argument("--no-ai", action="store_true")
    scenario_matrix.add_argument("--no-arm", action="store_true")
    scenario_matrix.add_argument("--no-repeat", action="store_true")
    scenario_matrix.add_argument("--out", type=Path)
    run = sub.add_parser("run", help="validate and run one simulator job")
    run.add_argument("--scenario", type=Path, required=True)
    run.add_argument("--qgc", type=Path)
    run.add_argument("--seed", type=int)
    run.add_argument("--frames", type=int, default=600)
    authority = run.add_mutually_exclusive_group()
    authority.add_argument("--manual", action="store_true", help="start with manual authority")
    authority.add_argument("--ai", action="store_true", help="explicitly enable AI authority")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument(
        "--interactive",
        dest="interactive",
        action="store_true",
        help="use the native QGC-to-PX4 loopback telemetry/control router",
    )
    mode.add_argument(
        "--legacy-control-gateway",
        dest="interactive",
        action="store_false",
        help="force the legacy MANUAL_CONTROL-only gateway",
    )
    run.set_defaults(interactive=None)
    stack_mode = run.add_mutually_exclusive_group()
    stack_mode.add_argument(
        "--stack-plan-enabled",
        "--enable-stack-plan",
        "--stack-plan",
        dest="stack_plan_enabled",
        action="store_true",
        help=("resolve the repository-owned PX4/Gazebo stack plan before a run (default)"),
    )
    stack_mode.add_argument(
        "--no-stack-plan",
        "--disable-stack-plan",
        dest="stack_plan_enabled",
        action="store_false",
        help="disable repository stack planning and retain legacy/custom process specs",
    )
    run.set_defaults(stack_plan_enabled=True)
    run.add_argument(
        "--stack-repository-root",
        "--stack-repo-root",
        "--repository-root",
        dest="stack_repository_root",
        type=Path,
        help="repository root containing simulation/gazebo assets",
    )
    run.add_argument(
        "--stack-px4-executable",
        "--stack-px4",
        "--px4-executable",
        "--px4",
        dest="stack_px4_executable",
        help="PX4 executable/command used by the repository stack plan",
    )
    run.add_argument(
        "--stack-gazebo-executable",
        "--stack-gazebo",
        "--gazebo-executable",
        "--gazebo",
        dest="stack_gazebo_executable",
        help="Gazebo executable/command used by the repository stack plan",
    )
    run.add_argument(
        "--stack-gazebo-flavor",
        "--stack-flavor",
        "--gazebo-flavor",
        "--flavor",
        dest="stack_gazebo_flavor",
        choices=("gz", "gazebo", "ign"),
        help="override the Gazebo command flavor in the stack plan",
    )
    run.add_argument(
        "--stack-px4-workdir",
        "--stack-workdir",
        "--px4-workdir",
        dest="stack_px4_workdir",
        type=Path,
        help="PX4 working directory for the relative etc/init.d-posix/rcS script",
    )
    run.add_argument(
        "--stack-host",
        dest="stack_host",
        help="literal loopback host for stack-plan ports (defaults to endpoint host)",
    )
    run.add_argument(
        "--stack-px4-control-port",
        "--stack-control-port",
        dest="stack_px4_control_port",
        type=int,
        help="override the stack-plan PX4 MANUAL_CONTROL output port",
    )
    run.add_argument(
        "--stack-px4-telemetry-port",
        "--stack-telemetry-port",
        dest="stack_px4_telemetry_port",
        type=int,
        help="override the stack-plan PX4 telemetry input port",
    )
    run.add_argument(
        "--stack-qgc-output-port",
        "--stack-qgc-output",
        dest="stack_qgc_output_port",
        type=int,
        help="override the stack-plan QGC isolated-HIL output port",
    )
    run.add_argument(
        "--stack-qgc-input-port",
        "--stack-qgc-input",
        dest="stack_qgc_input_port",
        type=int,
        help="override the stack-plan QGC control input port",
    )
    run.add_argument(
        "--stack-metadata-port",
        dest="stack_metadata_port",
        type=int,
        help=f"override the stack-plan metadata port (default {DEFAULT_METADATA_PORT})",
    )
    run.add_argument(
        "--stack-rtsp-port",
        dest="stack_rtsp_port",
        type=int,
        help=f"override the stack-plan camera RTSP port (default {DEFAULT_RTSP_PORT})",
    )
    px4_launch_mode = run.add_mutually_exclusive_group()
    px4_launch_mode.add_argument(
        "--px4-launch",
        dest="px4_launch_enabled",
        action="store_true",
        help="prepare/use the job-local hash-bound PX4 startup wrapper",
    )
    px4_launch_mode.add_argument(
        "--no-px4-launch",
        dest="px4_launch_enabled",
        action="store_false",
        help="disable PX4 startup-wrapper preparation and use the stack command directly",
    )
    run.set_defaults(px4_launch_enabled=None)
    run.add_argument(
        "--px4-launch-strict",
        dest="px4_launch_strict",
        action="store_true",
        default=None,
        help="turn PX4 wrapper readiness failures into a hard run error",
    )
    run.add_argument(
        "--qgc-control-mode",
        choices=("read_only", "sim_flight"),
        default=None,
        help=(
            "QGC command policy for interactive SIM: bounded sim_flight is the "
            "default when native Fly View is enabled; read_only is an explicit probe mode"
        ),
    )
    run.add_argument("--headless", action="store_true")
    run.add_argument("--realtime", action="store_true")
    run.add_argument("--pause-at", type=float)
    run.add_argument("--fault")
    run.add_argument("--job-root", type=Path)
    run.add_argument(
        "--qgc-control-port",
        type=int,
        default=14650,
        help="loopback UDP port for QGC MANUAL_CONTROL frames",
    )
    run.add_argument(
        "--px4-control-endpoint",
        default="udpout:127.0.0.1:14540",
        help="literal loopback PX4 SITL control endpoint",
    )
    run.add_argument(
        "--throttle-wire-mode",
        choices=("qgc_legacy", "signed"),
        default="qgc_legacy",
        help="MANUAL_CONTROL throttle encoding shared by QGC, AI, and SIM model",
    )
    run.add_argument(
        "--manual-watchdog-seconds",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "fail-safe stale QGC MANUAL_CONTROL input after this deadline; "
            "interactive mode defaults to 0.5 s, headless mode stays disabled"
        ),
    )
    run.add_argument(
        "--qgc-output-port",
        type=int,
        default=14649,
        help="QGC isolated-HIL local telemetry/receive UDP port (interactive mode)",
    )
    run.add_argument(
        "--px4-telemetry-port",
        type=int,
        default=14541,
        help="loopback UDP port where the interactive router receives PX4 telemetry",
    )
    run.add_argument(
        "--gateway-start-timeout",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="fail if the QGC control gateway cannot bind within this time",
    )
    run.add_argument(
        "--readiness-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "optional loopback MAVLink readiness receive window; when omitted "
            "the orchestrator keeps its non-blocking default"
        ),
    )
    heartbeat_mode = run.add_mutually_exclusive_group()
    heartbeat_mode.add_argument(
        "--readiness-require-heartbeat",
        dest="readiness_require_heartbeat",
        action="store_true",
        default=None,
        help="require a PX4 HEARTBEAT during the readiness probe",
    )
    heartbeat_mode.add_argument(
        "--readiness-no-heartbeat",
        dest="readiness_require_heartbeat",
        action="store_false",
        help="do not require a MAVLink HEARTBEAT (explicit compatibility switch)",
    )
    readiness_mode = run.add_mutually_exclusive_group()
    readiness_mode.add_argument(
        "--readiness-strict",
        dest="readiness_strict",
        action="store_true",
        default=None,
        help="block a real run unless required processes/heartbeat are ready",
    )
    readiness_mode.add_argument(
        "--readiness-nonstrict",
        dest="readiness_strict",
        action="store_false",
        help="record readiness failures without turning them into a hard error",
    )
    run.add_argument("--dry-run", action="store_true", help="validate and write evidence only")
    run.add_argument("--keep-job", action="store_true", help="keep job directory after exit")
    run.add_argument(
        "--ai-step",
        nargs="?",
        const="__default__",
        metavar="JSON",
        help=(
            "execute one deterministic AI step from a telemetry JSON file; "
            "omit JSON for a neutral default observation"
        ),
    )
    run.add_argument(
        "--ai-live",
        "--live-ai",
        dest="live_ai",
        action="store_true",
        help=(
            "feed validated PX4 telemetry from the interactive loopback router "
            "into the shared truth-free AI executor"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "probe":
        payload = {
            name: capability.__dict__
            if hasattr(capability, "__dict__")
            else {
                "name": capability.name,
                "executable": capability.executable,
                "available": capability.available,
                "version": capability.version,
                "reason": capability.reason,
                "daemon_available": capability.daemon_available,
                "daemon_version": capability.daemon_version,
                "daemon_reason": capability.daemon_reason,
            }
            for name, capability in discover_simulator_tools().items()
        }
        print(
            json.dumps(payload, ensure_ascii=False, indent=2)
            if args.as_json
            else "\n".join(_format_capability(name, item) for name, item in payload.items())
        )
        return 0
    if args.command == "runtime-preflight":
        try:
            preflight_kwargs = {
                key: value
                for key, value in {
                    "repository_root": args.repository_root,
                    "scenario": args.scenario,
                    "px4_workdir": args.px4_workdir,
                }.items()
                if value is not None
            }
            document = build_runtime_preflight_document(**preflight_kwargs)
            target = args.out.expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # Windows PowerShell/console code pages are not guaranteed to
            # encode every Unicode character that can appear in a host path or
            # backend diagnostic.  Keep the persisted JSON human-readable, but
            # escape stdout so the CLI always exits successfully on GBK/legacy
            # consoles as well as UTF-8 terminals.
            print(json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True))
            return 0
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            print(f"simulator runtime-preflight: {exc}", file=sys.stderr)
            return 2
    if args.command == "stack-plan":
        return _run_stack_plan(args)
    if args.command == "external-plan":
        return _run_external_plan(args)
    if args.command == "px4-launch-plan":
        return _run_px4_launch_plan(args)
    if args.command == "closed-loop":
        forwarded = [
            "--mode",
            args.mode,
            "--seed",
            str(args.seed),
            "--steps",
            str(args.steps),
            "--dt",
            str(args.dt),
            "--wind-east",
            str(args.wind_east),
            "--wind-north",
            str(args.wind_north),
        ]
        if args.out is not None:
            forwarded.extend(("--out", str(args.out)))
        return closed_loop_main(forwarded)
    if args.command == "interactive-acceptance":
        forwarded = ["--mode", args.mode]
        if args.out is not None:
            forwarded.extend(("--out", str(args.out)))
        return interactive_acceptance_main(forwarded)
    if args.command == "scenario":
        forwarded = [
            "--scenario",
            str(args.scenario),
            "--frames",
            str(args.frames),
            "--dt",
            str(args.dt),
        ]
        if args.mission is not None:
            forwarded.extend(("--mission", str(args.mission)))
        if args.start_mission:
            forwarded.append("--start-mission")
        if args.mode is not None:
            forwarded.extend(("--mode", args.mode))
        if args.no_ai:
            forwarded.append("--no-ai")
        for fault_id in args.fault_ids or ():
            forwarded.extend(("--fault", fault_id))
        if args.out is not None:
            forwarded.extend(("--out", str(args.out)))
        return scenario_main(forwarded)
    if args.command == "scenario-matrix":
        forwarded: list[str] = [
            "--frames",
            str(args.frames),
            "--dt",
            str(args.dt),
        ]
        if args.scenario_directory is not None:
            forwarded.extend(("--scenario-directory", str(args.scenario_directory)))
        for scenario_path in args.scenarios or ():
            forwarded.extend(("--scenario", str(scenario_path)))
        if args.no_ai:
            forwarded.append("--no-ai")
        if args.no_arm:
            forwarded.append("--no-arm")
        if args.no_repeat:
            forwarded.append("--no-repeat")
        if args.out is not None:
            forwarded.extend(("--out", str(args.out)))
        return scenario_matrix_main(forwarded)
    try:
        stack_ports = _stack_ports_from_run_args(args)
        orchestrator_kwargs: dict[str, object] = {
            "qgc": args.qgc,
            "seed": args.seed,
            "frames": args.frames,
            "realtime": args.realtime,
            "headless": args.headless,
            "dry_run": args.dry_run,
            "job_root": args.job_root,
            "pause_at": args.pause_at,
            "fault": args.fault,
            "qgc_control_port": args.qgc_control_port,
            "px4_control_endpoint": args.px4_control_endpoint,
            "throttle_wire_mode": args.throttle_wire_mode,
            "gateway_start_timeout_s": args.gateway_start_timeout,
            "interactive": args.interactive,
            "qgc_control_mode": args.qgc_control_mode,
            "qgc_output_port": args.qgc_output_port,
            "px4_telemetry_port": args.px4_telemetry_port,
            "stack_plan_enabled": args.stack_plan_enabled,
            "stack_repository_root": args.stack_repository_root,
            "stack_ports": stack_ports,
            "stack_gazebo_executable": args.stack_gazebo_executable,
            "stack_px4_executable": args.stack_px4_executable,
            "stack_gazebo_flavor": args.stack_gazebo_flavor,
            "stack_px4_workdir": args.stack_px4_workdir,
        }
        if args.manual_watchdog_seconds is not None:
            orchestrator_kwargs["manual_watchdog_s"] = args.manual_watchdog_seconds
        # ``px4_launch_enabled/strict`` are optional in the orchestrator API;
        # omit them when the operator did not select either switch so older
        # embedders/fakes keep their pre-launch-wrapper call signature.
        if args.px4_launch_enabled is not None or args.px4_launch_strict is not None:
            orchestrator_kwargs.update(
                {
                    "px4_launch_enabled": args.px4_launch_enabled,
                    "px4_launch_strict": args.px4_launch_strict,
                }
            )
        # Readiness knobs are optional at the CLI boundary so older embedders
        # and test doubles that implement the pre-readiness constructor keep
        # working when the operator does not request the new contract.
        if (
            args.readiness_timeout is not None
            or args.readiness_require_heartbeat is not None
            or args.readiness_strict is not None
        ):
            if args.readiness_timeout is not None:
                orchestrator_kwargs["readiness_timeout_s"] = args.readiness_timeout
            if args.readiness_require_heartbeat is not None:
                orchestrator_kwargs["readiness_require_heartbeat"] = (
                    args.readiness_require_heartbeat
                )
            if args.readiness_strict is not None:
                orchestrator_kwargs["readiness_strict"] = args.readiness_strict
        # Keep the new live-AI constructor knob opt-in so older embedders and
        # test doubles that do not know it retain the historical signature.
        if args.live_ai:
            orchestrator_kwargs["live_ai"] = True
        orchestrator = SimulatorOrchestrator(args.scenario, **orchestrator_kwargs)
        if args.manual:
            orchestrator.authority.resume_manual()
        if args.ai or args.ai_step is not None or args.live_ai:
            orchestrator.enable_ai()
            if args.ai and orchestrator.authority.state.value == "manual":
                # ``--manual`` and ``--ai`` are mutually exclusive, but an
                # externally selected initial state can still be manual.
                orchestrator.ai_executor.release_manual_control()
        result = orchestrator.start()
        if args.ai_step is not None:
            payload = _load_ai_snapshot(args.ai_step)
            observed_at = float(payload["observed_at_s"])
            orchestrator.step_ai(payload, now_s=observed_at)
            # Refresh the running result so callers using --keep-job can see
            # the decision counters immediately; stop() publishes the final
            # stopped snapshot when --keep-job is absent.
            result = orchestrator.last_result or result
        if result.status == "running" and not args.keep_job:
            orchestrator.stop()
            result = orchestrator.last_result or result
        print(json.dumps(result.to_mapping(), ensure_ascii=False, indent=2, default=str))
        return 0
    except (SimulatorOrchestratorError, ValueError, RuntimeError) as exc:
        print(f"simulator: {exc}", file=sys.stderr)
        return 2


def _format_capability(name: str, item: dict[str, object]) -> str:
    state = "available" if item["available"] else "missing"
    version = f" - {item['version']}" if item["version"] else ""
    daemon = ""
    daemon_available = item.get("daemon_available")
    if daemon_available is not None:
        daemon_state = "ready" if daemon_available else "unavailable"
        daemon_version = item.get("daemon_version")
        daemon = f"; daemon: {daemon_state}"
        if daemon_version:
            daemon += f" ({daemon_version})"
        elif item.get("daemon_reason"):
            daemon += f" ({item['daemon_reason']})"
    return f"{name}: {state}{version}{daemon}"


def _run_stack_plan(args: argparse.Namespace) -> int:
    """Build and print a repository-owned stack plan for the CLI command.

    Planning is deliberately side-effect free with respect to PX4/Gazebo: the
    planner only resolves/validates files and records executable availability.
    ``--out`` writes the exact hash-bound JSON document that is printed to
    stdout, making the command convenient both for humans and CI evidence.
    """

    try:
        ports = SimulatorStackPorts(
            host=args.host,
            px4_control_port=args.px4_control_port,
            px4_telemetry_port=args.px4_telemetry_port,
            qgc_output_port=args.qgc_output_port,
            qgc_input_port=args.qgc_input_port,
            metadata_port=args.metadata_port,
            rtsp_port=args.rtsp_port,
        )
        plan = build_stack_plan(
            args.scenario,
            repository_root=args.repository_root,
            ports=ports,
            gazebo_executable=args.gazebo_executable,
            px4_executable=args.px4_executable,
            gazebo_flavor=args.gazebo_flavor,
            headless=args.headless,
            realtime=args.realtime,
            px4_workdir=args.px4_workdir,
        )
        if args.out is not None:
            plan.write_json(args.out)
        print(
            json.dumps(
                plan.to_mapping_with_hash(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (SimulatorStackError, OSError, TypeError, ValueError) as exc:
        print(f"simulator stack-plan: {exc}", file=sys.stderr)
        return 2


def _run_external_plan(args: argparse.Namespace) -> int:
    """Build process/cwd/env/log evidence without starting PX4 or Gazebo."""

    try:
        ports = SimulatorStackPorts(
            host=args.host,
            px4_control_port=args.px4_control_port,
            px4_telemetry_port=args.px4_telemetry_port,
            qgc_output_port=args.qgc_output_port,
            qgc_input_port=args.qgc_input_port,
            metadata_port=args.metadata_port,
            rtsp_port=args.rtsp_port,
        )
        stack_plan = build_stack_plan(
            args.scenario,
            repository_root=args.repository_root,
            ports=ports,
            gazebo_executable=args.gazebo_executable,
            px4_executable=args.px4_executable,
            gazebo_flavor=args.gazebo_flavor,
            headless=args.headless,
            realtime=args.realtime,
            px4_workdir=args.px4_workdir,
        )
        overlays = _parse_external_env(args.env)
        external = build_external_stack_plan(
            stack_plan,
            job_dir=args.job_dir,
            log_dir=args.log_dir,
            px4_workdir=args.px4_workdir,
            env_overrides=overlays,
            inherit_environment=args.inherit_environment,
            dry_run=args.dry_run,
            strict=not args.dry_run,
            px4_launch_enabled=args.px4_launch_enabled,
            px4_launch_strict=args.px4_launch_strict,
        )
        if args.out is not None:
            external.write_json(args.out)
        print(
            json.dumps(
                external.to_mapping_with_hash(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (ExternalStackError, SimulatorStackError, OSError, TypeError, ValueError) as exc:
        print(f"simulator external-plan: {exc}", file=sys.stderr)
        return 2


def _run_px4_launch_plan(args: argparse.Namespace) -> int:
    """Create and print the job-local PX4 startup wrapper plan.

    ``px4_launch`` is intentionally a planner.  This command generates the
    copied ``px4-rc.mavlink``/shell wrapper and hash-bound readiness evidence,
    but never starts PX4 or opens a transport.  A strict invocation therefore
    acts as a preflight gate rather than an implicit process launcher.
    """

    try:
        ports = SimulatorStackPorts(
            host=args.host,
            px4_control_port=args.px4_control_port,
            px4_telemetry_port=args.px4_telemetry_port,
        )
        stack_plan = build_stack_plan(
            args.scenario,
            repository_root=args.repository_root,
            ports=ports,
            # PX4 launch planning only consumes the stack's PX4 identity and
            # endpoints; Gazebo remains represented by the normal repository
            # plan and is never started by this command.
            gazebo_executable="gz",
            px4_executable=args.px4_executable,
            headless=True,
            realtime=True,
        )
        launch = build_px4_launch_plan(
            stack_plan,
            job_dir=args.job_dir,
            px4_workdir=args.px4_workdir,
            control_port=args.px4_control_port,
            telemetry_port=args.px4_telemetry_port,
            target_host=args.host,
            dry_run=args.dry_run,
            strict=not args.dry_run,
            write_evidence=args.write_evidence,
        )
        if args.out is not None:
            launch.write_evidence(args.out)
        print(
            json.dumps(
                launch.to_mapping_with_hash(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (Px4LaunchError, SimulatorStackError, OSError, TypeError, ValueError) as exc:
        print(f"simulator px4-launch-plan: {exc}", file=sys.stderr)
        return 2


def _parse_external_env(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"--env requires KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        if key in result:
            raise ValueError(f"duplicate --env key: {key}")
        result[key] = value
    return result


def _stack_ports_from_run_args(args: argparse.Namespace) -> SimulatorStackPorts | None:
    """Build an explicit stack-port contract for the ``run`` command.

    The orchestrator already owns the QGC/telemetry/control endpoint checks.
    This helper only creates a :class:`SimulatorStackPorts` object when one of
    the stack-specific port knobs was supplied, preserving the historical
    ``run`` defaults and avoiding a second, silently divergent topology.  The
    existing ``--qgc-control-port``, ``--qgc-output-port``,
    ``--px4-telemetry-port`` and ``--px4-control-endpoint`` options remain the
    source of truth for those legs; stack aliases must agree with them.
    """

    names = (
        "stack_host",
        "stack_px4_control_port",
        "stack_px4_telemetry_port",
        "stack_qgc_output_port",
        "stack_qgc_input_port",
        "stack_metadata_port",
        "stack_rtsp_port",
    )
    if not any(getattr(args, name, None) is not None for name in names):
        return None

    host, control_port = _parse_run_loopback_udpout(args.px4_control_endpoint)
    selected_host = host if args.stack_host is None else str(args.stack_host)
    selected_control = (
        control_port if args.stack_px4_control_port is None else int(args.stack_px4_control_port)
    )
    selected_telemetry = (
        int(args.px4_telemetry_port)
        if args.stack_px4_telemetry_port is None
        else int(args.stack_px4_telemetry_port)
    )
    selected_qgc_output = (
        int(args.qgc_output_port)
        if args.stack_qgc_output_port is None
        else int(args.stack_qgc_output_port)
    )
    selected_qgc_input = (
        int(args.qgc_control_port)
        if args.stack_qgc_input_port is None
        else int(args.stack_qgc_input_port)
    )

    # Do not let a stack alias silently point the adapter/router at a different
    # socket.  Callers that intentionally move a leg should use the existing
    # run option as well (or pass a complete stack plan through the API).
    mismatches = {
        "stack-host": (selected_host, host),
        "stack-px4-control-port": (selected_control, control_port),
        "stack-px4-telemetry-port": (selected_telemetry, int(args.px4_telemetry_port)),
        "stack-qgc-output-port": (selected_qgc_output, int(args.qgc_output_port)),
        "stack-qgc-input-port": (selected_qgc_input, int(args.qgc_control_port)),
    }
    for option, (observed, expected) in mismatches.items():
        if observed != expected:
            raise ValueError(
                f"--{option} conflicts with the existing run endpoint: "
                f"expected {expected!r}, got {observed!r}"
            )

    return SimulatorStackPorts(
        host=selected_host,
        px4_control_port=selected_control,
        px4_telemetry_port=selected_telemetry,
        qgc_output_port=selected_qgc_output,
        qgc_input_port=selected_qgc_input,
        metadata_port=(
            DEFAULT_METADATA_PORT
            if args.stack_metadata_port is None
            else int(args.stack_metadata_port)
        ),
        rtsp_port=(
            DEFAULT_RTSP_PORT if args.stack_rtsp_port is None else int(args.stack_rtsp_port)
        ),
    )


def _parse_run_loopback_udpout(value: object) -> tuple[str, int]:
    """Parse the literal loopback ``udpout`` form accepted by the adapter.

    Keeping this tiny parser local to the CLI avoids importing a private
    orchestrator helper while accepting both IPv6 spellings emitted by the
    stack planner: ``udpout:::1:<port>`` and ``udpout:[::1]:<port>``.
    """

    if not isinstance(value, str) or not value.startswith("udpout:"):
        raise ValueError(
            "--px4-control-endpoint must be udpout:127.0.0.1:<port> or udpout:::1:<port>"
        )
    target = value[len("udpout:") :]
    if target.startswith("["):
        close = target.find("]:")
        if close <= 1:
            raise ValueError(f"invalid --px4-control-endpoint: {value!r}")
        host = target[1:close]
        port_text = target[close + 2 :]
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            raise ValueError(f"invalid --px4-control-endpoint: {value!r}")
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("--px4-control-endpoint must use literal loopback")
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid --px4-control-endpoint port: {port_text!r}") from exc
    if not 1024 <= port <= 65535:
        raise ValueError("--px4-control-endpoint port must be in 1024..65535")
    return host, port


def _load_ai_snapshot(value: str) -> dict[str, object]:
    """Load a truth-free telemetry mapping for ``--ai-step``."""

    if value == "__default__":
        return {
            "observed_at_s": 0.0,
            "altitude_agl_m": 30.0,
            "ground_speed_mps": 0.0,
            "heading_deg": 0.0,
            "link_healthy": True,
            "armed": True,
        }
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise SimulatorOrchestratorError(f"AI telemetry JSON not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SimulatorOrchestratorError(f"invalid AI telemetry JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SimulatorOrchestratorError("AI telemetry JSON must be an object")
    if "observed_at_s" not in raw:
        if "timestamp_s" in raw:
            raw["observed_at_s"] = raw.pop("timestamp_s")
        elif "time_s" in raw:
            raw["observed_at_s"] = raw.pop("time_s")
    if "observed_at_s" not in raw:
        raise SimulatorOrchestratorError(
            "AI telemetry JSON requires observed_at_s (or timestamp_s/time_s)"
        )
    return {str(key): item for key, item in raw.items()}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
