#!/usr/bin/env python3
"""Run the Iowa X1 Altair pre-acceptance matrix against one local GGUF server."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

SITE = "rw_farm_iowa_w6km"
WORLD = f"{SITE}__clear_noon"
SCENARIO = f"configs/scenarios/realworld/delta_tailsitter_gimbal_cam/{WORLD}.scenario.json"
INSTALL = Path("/data/projects/Multi-Detect-env/Multi-Detect")
LLAMA = INSTALL / "artifacts/cache/llama-cpp/b11552/llama-server"
ARCHIVE = Path("/data/scratch/x1-scn-run")
ASSETS = Path("/data/datasets/multidetect-sim-assets/wide-collect/assets") / SITE
WORLD_ROOT = Path("/data/scratch/cx-wc-sampling/.tmp/x1-iowa-world")
ASSUMED = "configs/simulation/x1-assumed-sim-bounds-iowa.json"
EVIDENCE_FAULTS = "configs/simulation/x1-evidence-follow-faults-v1.json"
SENTENCE = "Fly over the yard at 85 m AGL, then return home and land."
EARLY_MARKERS = ("x1_gpu_unavailable", "SITL slots busy", "existing wide-area flight on requested slots",
                 "x1_start_load_exceeded", "host one-minute load must be below",
                 "Resource temporarily unavailable", "requires 28 GiB")
SHARE_MARKERS = tuple(Path("/data/scratch") / f"x1-allow-{name}"
                      for name in ("training-share", "fluidx3d-share", "sunshine-compute"))
CLEANUP_S = 12
DIRECT_HTTP = build_opener(ProxyHandler({}))


@dataclass(frozen=True)
class Case:
    id: str
    phase: str
    expected: tuple[str, ...]
    cadence_s: float
    tool_budget: int
    expected_s: int
    budget_s: int
    fault: str | None = None
    schedule: str | None = None
    assumed: bool = False
    trigger: str = "event"
    sentence: str = SENTENCE
    return_home: bool = True
    nominal_mc: bool = False
    max_quiet_actions: int = 0
    gate_checks: tuple[str, ...] = ()
    assumed_bounds: str | None = None
    mission_package: str | None = None
    min_airborne_s: int = 0
    opt_in: bool = False

    def __post_init__(self):
        if self.assumed_bounds and not self.assumed:
            raise ValueError("custom ASSUMED_SIM bounds require an assumed SITL case")


LONG_RANGE_PACKAGE = "configs/simulation/x1-longrange-mission.json"
LONG_RANGE_BOUNDS = "configs/simulation/x1-assumed-sim-bounds-iowa-longrange.json"
LONG_RANGE_WIND = "configs/simulation/x1-longrange-wind-faults-v1.json"
LONG_RANGE_SENTENCE = (
    "Fly over the far north-east marker at 85 m AGL, then return home and land."
)

CORRIDOR_BOUNDS = "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json"
CORRIDOR_DOGLEG_PACKAGE = "configs/simulation/x1-corridor-dogleg-mission.json"
CORRIDOR_DOGLEG_SENTENCE = (
    "Fly over the west beacon and then the east beacon at 47 m AGL. "
    "Fly back over home in fixed-wing mode at the same height, then transition and land vertically."
)


CASES = (
    Case("mc-hover-waypoint-return", "mc", ("continue",), 2, 0, 360, 900,
         sentence="Stay in multicopter mode. Hover over home at 85 m AGL, fly to the yard at 85 m AGL, "
                  "then return home and land without transitioning to fixed wing.", nominal_mc=True),
    Case("vtol-transect", "fw", ("continue",), 1, 0, 300, 900),
    Case("ramped-wind", "fw", ("continue",), 0, 2, 300, 900, "wind_step",
         str(ARCHIVE / "faults-fw-wind.json")),
    # "mc" is the fault/decision phase of the Iowa FW mission, not the nominal MC route.
    Case("mc-link-loss", "mc", ("return",), 0, 1, 300, 900, "link_loss",
         "configs/simulation/x1-native-handoff-faults-v1.json", assumed=True,
         gate_checks=("event_tool_first",)),
    Case("mc-battery-drop", "mc", ("return", "land-now"), 0, 1, 300, 900, "battery_drop",
         "configs/simulation/x1-mc-battery-faults-v1.json", assumed=True, return_home=False,
         gate_checks=("event_tool_first", "battery_structure")),
    Case("evidence-follow-event", "fw", ("retask",), 0, 3, 420, 1200, "target_found",
         EVIDENCE_FAULTS, assumed=True,
         gate_checks=("event_tool_first", "retask_structure", "retask_timing")),
    Case("evidence-follow-cadence", "fw", ("retask",), 1, 3, 420, 1200, "target_found",
         EVIDENCE_FAULTS, assumed=True, trigger="cadence",
         gate_checks=("retask_structure", "retask_timing")),
    Case("quiet-cadence", "steady", ("continue",), 4, 0, 300, 900,
         gate_checks=("unnecessary_actions",)),
    # Legacy single-waypoint experiments, kept for explicit replay.
    Case("long-range-transect", "fw", ("continue",), 10, 0, 840, 1800,
         sentence=LONG_RANGE_SENTENCE, assumed=True, assumed_bounds=LONG_RANGE_BOUNDS,
         mission_package=LONG_RANGE_PACKAGE, min_airborne_s=600, opt_in=True, gate_checks=("unnecessary_actions",)),
    Case("long-range-wind", "fw", ("continue",), 10, 0, 840, 1800, "wind_step",
         LONG_RANGE_WIND, sentence=LONG_RANGE_SENTENCE, assumed=True,
         assumed_bounds=LONG_RANGE_BOUNDS, mission_package=LONG_RANGE_PACKAGE,
         min_airborne_s=600, opt_in=True, gate_checks=("unnecessary_actions",)),
    Case("corridor-dogleg", "fw", ("continue",), 10, 0, 360, 900,
         sentence=CORRIDOR_DOGLEG_SENTENCE, assumed=True, assumed_bounds=CORRIDOR_BOUNDS,
         mission_package=CORRIDOR_DOGLEG_PACKAGE, opt_in=True,
         gate_checks=("unnecessary_actions",)),
)


def select_cases(names, *, include_opt_in=False):
    known = {case.id for case in CASES}
    if names and (set(names) - known or len(names) != len(set(names))):
        raise ValueError("case names must be known and unique")
    if names:
        return [case for case in CASES if case.id in names]
    return [case for case in CASES if include_opt_in or not case.opt_in]


def write_json(path, obj):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def local_json(port, endpoint, body=None, *, timeout):
    request = Request(f"http://127.0.0.1:{port}{endpoint}",
                      json.dumps(body).encode() if body is not None else None,
                      {"Content-Type": "application/json"})
    with DIRECT_HTTP.open(request, timeout=timeout) as response:
        return json.load(response)


def terminate_owned(process, grace_s=CLEANUP_S):
    if process.poll() is None:
        process.terminate()
        try:
            return process.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.wait()


class Server:
    def __init__(self, args, root):
        self.args = args
        self.root = root
        self.process = None

    def __enter__(self):
        port = self.args.port
        with socket.socket() as sock:
            # A previous suite's connections may linger in TIME_WAIT; a live listener still fails the bind.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        cuda = self.args.cuda_device
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="" if cuda is None else str(cuda))
        command = [str(self.args.llama_server), "-m", str(self.args.checkpoint), "--host", "127.0.0.1",
                   "--port", str(port), "-ngl", "0" if cuda is None else "all", "-c", "8192",
                   "-np", "1", "--no-webui", "--cache-ram", "0"]
        if cuda is None:
            command += ["--device", "none"]
        try:
            with (self.root / "llama-server.log").open("wb") as log:
                self.process = subprocess.Popen(command, cwd=self.args.llama_server.parent, env=env,
                                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                                start_new_session=True)
            write_json(self.root / "server.json", {"pid": self.process.pid, "argv": command,
                                                   "cuda_device": cuda, "state": "started"})
            deadline = time.monotonic() + self.args.startup_timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                if self.process.poll() is not None:
                    raise RuntimeError(f"llama-server exited with {self.process.returncode}")
                try:
                    health = local_json(port, "/health", timeout=min(1, remaining))
                    if health.get("status") == "ok" and self.process.poll() is None:
                        return self
                except (OSError, ValueError):
                    pass
                time.sleep(min(.2, max(0, deadline - time.monotonic())))
            raise TimeoutError("llama-server readiness deadline")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self.process is not None:
            rc = terminate_owned(self.process)
            path = self.root / "server.json"
            if path.exists():
                write_json(path, {**read_json(path), "state": "stopped", "exit_status": rc})


def shared_planner(port, timeout):
    """Cache this attempt's real planner reply for the identical validated flight prompt."""
    first = None

    def post(prompt, schema):
        nonlocal first
        if first is None:
            body = {"model": "local", "temperature": 0, "max_tokens": 768,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": "mission_plan", "strict": True, "schema": schema}}}
            raw = local_json(port, "/v1/chat/completions", body, timeout=timeout)
            first = prompt, schema, raw["choices"][0]["message"]["content"]
        if (prompt, schema) != first[:2]:
            raise ValueError("planner prompt changed between preflight and flight")
        return first[2]

    return post


def run_worker(spec):
    from scripts import run_x1_autonomy_demo as demo
    from scripts.realworld import x1_site_evidence

    case, args = spec["case"], spec["args"]
    output = Path(spec["attempt_dir"])
    mission_package = ROOT / case["mission_package"] if case.get("mission_package") else args["mission_package"]
    common = ["--world", SITE, "--scenario", SCENARIO, "--sentence", case["sentence"],
              "--mission-package", str(mission_package), "--checkpoint", args["checkpoint"],
              "--llama-server", args["llama_server"], "--planner-port", str(args["port"]),
              "--decision-port", str(args["port"]), "--slot", str(spec["slot"]),
              "--decision-cadence-s", str(case["cadence_s"]), "--perception-tools",
              "--decision-tool-budget", str(case["tool_budget"]), "--generate-live-evidence"]
    if case.get("assumed_bounds") and not case["assumed"]:
        raise ValueError("custom ASSUMED_SIM bounds require an assumed SITL case")
    if case["assumed"]:
        from scripts.run_x1_autonomy_demo import ASSUMED_SIM_FLAG

        common += [ASSUMED_SIM_FLAG, str(ROOT / (case.get("assumed_bounds") or ASSUMED))]
    if case["nominal_mc"]:
        common += ["--nominal-mc"]
    if case["fault"] == "target_found" and case["trigger"] == "cadence":
        common += ["--target-cadence-only"]
    if case["fault"]:
        schedule = case["schedule"]
        common += ["--contingency-scenario", str(schedule), "--inject-fault", case["fault"]]
        if case["fault"] == "wind_step":
            common += ["--wind-airspeed-plugin", args["wind_plugin"]]
    post = shared_planner(args["port"], args["planner_timeout_s"])
    rc = demo.main([*common, "--plan-only", "--output", str(output / "preflight")], shared_post=post)
    if rc:
        return rc
    if args["plan_only"] and case.get("assumed_bounds") != CORRIDOR_BOUNDS:
        return 0
    preflight = read_json(output / "preflight/result.json")
    if case.get("assumed_bounds") == CORRIDOR_BOUNDS:
        scenario = preflight["contingency_scenario"]
        if not demo.matches_assumed_corridor_plan(
            fault=case["fault"], scope=scenario["motion_scope_sha256"],
            base_digest=scenario["live_evidence"]["base_mission_sha256"],
        ):
            raise ValueError(f"{demo.CORRIDOR_BINDING_ERROR} and pinned dogleg plan")
    if args["plan_only"]:
        return 0
    bounds = output / "iowa.site-bounds.json"
    x1_site_evidence.write_bounds(bounds, preflight, read_json(Path(args["site_inventory"])))
    common += ["--live-evidence-bounds", str(bounds)]
    return demo.main([*common, "--decision-audit", str(output / "decisions.jsonl"),
                      "--output", str(output / "flight")], shared_post=post)


def read_trace(path):
    rows, errors = [], []
    if path.exists():
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected object")
                rows.append(row)
            except ValueError:
                errors.append(f"invalid audit line {index}")
    return rows, errors


def case_requests(case, trace):
    requests = {row["request"]["request_id"]: row["request"] for row in trace
                if row.get("kind") in {"turn_sent", "request_opened"}}
    selected = {}
    for key, request in requests.items():
        state = request.get("state", {})
        mode = ((state.get("telemetry") or {}).get("vtol") or {}).get("state")
        phase_matches = mode in {"mc", "fw"} if case.phase == "steady" else mode == case.phase
        if not phase_matches or request.get("trigger", "event") != (case.trigger if case.fault else "cadence"):
            continue
        if case.fault and not any(event.get("type") == case.fault
                                  and 0 <= state["t_ms"] - event["t_ms"] <= 3000 for event in state.get("events", [])):
            continue
        selected[key] = case.expected
    return requests, selected


def check_flight(case, result, trace):
    flight = result.get("flight", {})
    contingency = result.get("contingency_scenario", {})
    markers = contingency.get("markers", [])
    receipts = {row["request_id"]: row for row in trace if row.get("kind") == "receipt"}
    requests, relevant = case_requests(case, trace)
    model_actions = {}
    for row in trace:
        if row.get("kind") != "turn_result" or not isinstance(row.get("model_output"), str):
            continue
        try:
            payload = json.loads(row["model_output"])
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("action"), str):
            model_actions[row["request_id"]] = payload["action"]
    statuses = {"continue": {"continued"}, "return": {"accepted_unconfirmed", "sent_unconfirmed"},
                "land-now": {"accepted_unconfirmed", "sent_unconfirmed"}, "retask": {"installed"}}
    admitted = [r for key, r in receipts.items() if key in relevant
                and model_actions.get(key) == r.get("action") and r.get("action") in case.expected
                and r.get("status") in statuses.get(r.get("action"), ())]
    no_action = case.expected == ("continue",)
    safe_vetoes = {"missing_or_unbound_motion_proof", "unverified_clearance", "energy_or_reserve_limit"}
    continued_or_vetoed = [r for key, r in receipts.items() if key in relevant
                            and model_actions.get(key) == r.get("action") == "continue"
                            and r.get("status") == "rejected" and r.get("reason") in safe_vetoes]
    quiet = [key for key, req in requests.items() if req.get("trigger") == "cadence"
             and not any(0 <= req["state"]["t_ms"] - event["t_ms"] <= 3000
                         for event in req["state"].get("events", []))]
    unnecessary = sum(model_actions.get(key) not in {None, "continue"} for key in quiet)
    expected_receipts = admitted + (continued_or_vetoed if no_action else [])
    covered = {r["request_id"] for r in expected_receipts}
    # Faults can have several samples before one handoff takes effect. Quiet
    # scenarios require the expected decision on every cadence tick in scope.
    expected_receipt = bool(covered) and (case.fault is not None or covered == set(relevant))
    native = flight.get("native_handoff") or {}
    effect = (not native and not any(m.get("message_type") in {"scenario_plan_replaced", "scenario_native_command"}
                                    for m in markers)) if no_action else True
    if case.expected == ("retask",):
        effect = any(r["request_id"] == marker.get("request_id") for r in admitted for marker in markers
                     if marker.get("message_type") == "scenario_plan_replaced")
        # The demo only marks flight pass after verify_evidence_follow succeeds.
        effect = effect and contingency.get("evidence_follow", {}).get("landed_monotonic_ns") is not None
    if set(case.expected) & {"return", "land-now"}:
        effect = (native.get("confirmed") is True and native.get("landed_by_native") is True
                  and any(r["action"] == native.get("action") for r in admitted))
    checks = {"plan_valid": result.get("validator", {}).get("verdict") == "accepted",
              "flight_pass": flight.get("status") == "pass", "landing": flight.get("landing") is True,
              "return_home": ((not case.return_home and native.get("action") == "land-now")
                              or flight.get("return_home") is True),
              "expected_receipt": expected_receipt, "executor_effect": effect,
              "quiet_action_limit": unnecessary <= case.max_quiet_actions}
    if case.min_airborne_s:
        duration = flight.get("takeoff_complete_to_landing_sim_s")
        checks["min_airborne_time"] = (type(duration) in (int, float) and math.isfinite(duration)
                                        and duration >= case.min_airborne_s)
        checks["waypoint_reached"] = bool(flight.get("waypoints_reached"))
        cadence_receipts = [key for key in quiet if key in receipts and model_actions.get(key) == "continue"]
        checks["repeated_cadence"] = len(cadence_receipts) >= 2
    if case.id == "corridor-dogleg":
        # Vetoes are safe outcomes, but do not demonstrate multi-leg admission.
        cruise_steps = [requests[receipt["request_id"]].get("mission_progress", {}) for receipt in admitted]
        fw_legs = {"w", "w_2", "w_3"}
        cruise_legs = [step.get("step_id") for step in cruise_steps
                       if step.get("phase") == "cruise" and step.get("primitive") == "fw_goto_waypoint"
                       and step.get("step_id") in fw_legs]
        checks["repeated_fw_continue_admitted"] = len(cruise_legs) >= 2 and bool({"w", "w_2"} & set(cruise_legs))
        checks["all_fw_legs_reached"] = fw_legs <= set(flight.get("waypoints_reached", []))
    if case.nominal_mc:
        checks["mc_only_route"] = flight.get("mc_only_confirmed") is True and bool(flight.get("waypoints_reached"))
    if case.fault:
        checks["fault_injected"] = any(m.get("message_type") == "scenario_injected" and m.get("fault") == case.fault
                                       for m in markers)
        checks["fault_observed"] = any(m.get("message_type") == "scenario_observed" and m.get("fault") == case.fault
                                       and (case.fault != "wind_step" or (m.get("source") == "mavlink"
                                            and bool(m.get("wind_method")) and m.get("wind_age_ms") is not None))
                                       for m in markers)
        if case.fault == "wind_step":
            checks["fault_restored"] = any(m.get("message_type") == "scenario_restored" and m.get("fault") == case.fault
                                           and m.get("reason") == "window_end" for m in markers)
    return checks, {"quiet_ticks": len(quiet), "unnecessary_actions": unnecessary, "admitted": admitted,
                    "no_action_safety_vetoes": continued_or_vetoed}


def retryable(result, log):
    detail = " ".join(str(section.get("reason", "")) for section in
                      (result.get("flight", {}), result.get("validator", {})))
    return any(marker in detail or marker in log for marker in EARLY_MARKERS)


def work_command(spec_path):
    return [sys.executable, str(Path(__file__).resolve()), "--worker", str(spec_path)]


def retask_validator(attempt, result, *, assumed, max_distance_m=None):
    from multidetect.autonomy.serialization import canonical_sha256
    from scripts.run_x1_autonomy_demo import load_sim_package

    path = attempt / "flight/mission-package.json"
    if path.is_file():
        document = read_json(path)
        if canonical_sha256(document) != result.get("mission_package_sha256"):
            raise ValueError("generated retask validation package does not match flight digest")
        package = load_sim_package(document, assumed=assumed, max_distance_m=max_distance_m)
    else:
        package = None

    def validate(plan, request):
        if package is None:
            return "generated flight mission package is missing"
        if package.mission_id != request.get("state", {}).get("mission_id"):
            return "generated flight mission package does not match the request mission"
        try:
            package.validate_plan(plan)
        except ValueError as exc:
            return str(exc)
        return None

    return validate


def run_case(case, args, root, env, *, command_factory=work_command, server_alive=lambda: True):
    from scripts import run_x1_autonomy_demo as demo
    from scripts.x1_closed_loop_harvest import harvest
    from scripts.realworld.x1_lock import FLIGHT_LOCK_FD_ENV

    max_distance_m = None
    if case.assumed_bounds == CORRIDOR_BOUNDS:
        max_distance_m, _ = demo.verified_assumed_corridor(ROOT / case.assumed_bounds)
    elif case.assumed_bounds:
        max_distance_m = demo.verified_assumed_radius(ROOT / case.assumed_bounds)
    lock_fd = env.get(FLIGHT_LOCK_FD_ENV)
    pass_fds = () if lock_fd is None else (int(lock_fd),)

    path = root / case.id
    path.mkdir()
    start = time.monotonic()
    budget = min(args.case_budget_s, case.budget_s) if args.case_budget_s is not None else case.budget_s
    deadline = start + budget
    attempts, all_failures = [], []
    result, checks, scores, metrics = {}, {}, {}, {}
    status = "budget_exceeded"
    for index in range(1, args.retries + 2):
        if time.monotonic() >= deadline:
            break
        if not server_alive():
            status = "server_lost"
            break
        slot = args.slots[(index - 1) % len(args.slots)]
        attempt = path / f"attempt-{index:02d}"
        attempt.mkdir()
        spec_path = attempt / "worker.json"
        write_json(spec_path, {"attempt_dir": str(attempt), "case": asdict(case), "slot": slot,
                               "args": {key: str(value) if isinstance(value, Path) else value
                                        for key, value in vars(args).items()}})
        argv = command_factory(spec_path)
        timed_out = False
        with (attempt / "runner.log").open("wb") as log:
            proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, pass_fds=pass_fds)
            try:
                write_json(attempt / "process.json", {"pid": proc.pid, "argv": argv})
                remaining = max(0, deadline - time.monotonic())
                rc = proc.wait(timeout=max(0, remaining - min(CLEANUP_S, remaining / 4)))
            except subprocess.TimeoutExpired:
                timed_out = True
                rc = terminate_owned(proc, max(0, deadline - time.monotonic()))
            except BaseException:
                terminate_owned(proc, max(0, min(CLEANUP_S, deadline - time.monotonic())))
                raise
        paths = (attempt / "flight/result.json", attempt / "preflight/result.json", attempt / "worker-error.json")
        artifacts = {str(p.relative_to(attempt)): read_json(p) for p in paths if p.exists()}
        result = next(iter(artifacts.values()), {})
        trace, trace_errors = read_trace(attempt / "decisions.jsonl")
        _, expected_by_request = case_requests(case, trace)
        failures, metrics = harvest(
            trace, case_id=case.id, attempt=index, fault=case.fault,
            expected_by_request=expected_by_request,
            event_tool_first="event_tool_first" in case.gate_checks,
            check_retask_timing=True,
            check_battery_structure="battery_structure" in case.gate_checks,
            validate_retask=(retask_validator(attempt, result, assumed=case.assumed,
                                              max_distance_m=max_distance_m) if trace else None),
        )
        all_failures.extend(failures)
        checks, scores = check_flight(case, result, trace)
        checks["audit_complete"] = (not trace_errors and bool(trace) and not any(metrics.get(key) for key in
                                    ("missing_receipts", "orphan_results", "invalid_trace_rows", "duplicate_trace_rows")))
        if timed_out:
            status = "budget_exceeded"
        elif (artifacts.get("worker-error.json", {}).get("detail", "").startswith(demo.CORRIDOR_BINDING_ERROR)
              or result.get("validator", {}).get("reason", "").startswith((
                  "ASSUMED_SIM requires the exact Iowa route", demo.CORRIDOR_BINDING_ERROR))):
            status = "blocked_evidence_scope"
        elif rc == 0 and args.plan_only:
            status = "plan_only"
        else:
            status = "pass" if rc == 0 and all(checks.values()) else "fail"
        attempts.append({"attempt": index, "slot": slot, "pid": proc.pid, "argv": argv, "exit_status": rc,
                         "timed_out": timed_out, "audit_errors": trace_errors, "metrics": metrics,
                         "runner_result": next((str(p) for p in paths if p.exists()), None),
                         "worker_error": artifacts.get("worker-error.json")})
        log_text = (attempt / "runner.log").read_text(encoding="utf-8", errors="replace")
        if status != "fail" or trace or not retryable(result, log_text):
            break
        status = "admission_failed"
        if index <= args.retries:
            if deadline - time.monotonic() <= args.retry_interval_s:
                status = "budget_exceeded"
                break
            time.sleep(args.retry_interval_s)
    record = {"case": asdict(case), "status": status, "hard_budget_s": budget,
              "elapsed_s": round(time.monotonic() - start, 3), "attempts": attempts, "checks": checks,
              "observed": scores, "metrics": metrics, "failure_rows": len(all_failures), "flight_result": result}
    write_json(path / "result.json", record)
    return record, all_failures


def summarize(root, records, failures, error=None):
    from scripts.x1_closed_loop_harvest import merge_metrics

    metrics = merge_metrics([attempt["metrics"] for row in records for attempt in row["attempts"]])
    summary = {"cases": records, "counts": dict(Counter(r["status"] for r in records)),
               "metrics": metrics, "failure_rows": len(failures), "error": error}
    write_json(root / "summary.json", summary)
    with (root / "failures.jsonl").open("x", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n")
    markdown = ["# X1 闭环预验收", ""]
    if error:
        markdown.extend([f"执行错误 `{error}`", ""])
    markdown += ["| 用例 | 状态 | 耗时 (s) | 上限 (s) | 未通过的检查 | 失败轮 |",
                 "| --- | --- | ---: | ---: | --- | ---: |"]
    for row in records:
        failed = ", ".join(key for key, ok in row["checks"].items() if not ok)
        markdown.append(f"| {row['case']['id']} | {row['status']} | {row['elapsed_s']} | {row['hard_budget_s']} | "
                        f"{failed} | {row['failure_rows']} |")
    markdown += ["", "| 指标 | 结果 |", "| --- | --- |"]
    ratios = {
        "动作正确率": ("action_matches", "expected_action_turns"),
        "工具选择正确率": ("tool_matches", "expected_tool_turns"),
        "Retask 安装率": ("retasks_installed", "retasks_proposed"),
        "安静巡航多余动作率": ("quiet_unnecessary_actions", "quiet_cruise_turns"),
        "Schema 合法率": ("schema_valid", "turns_sent"),
        "请求标识回显率": ("identity_valid", "turns_sent"),
        "事件工具优先": ("event_tool_first_compliant", "event_tool_first_required"),
        "Retask 结构合法率": ("retasks_structure_valid", "retasks_proposed"),
        "Retask 时机合规率": ("retasks_timing_valid", "retasks_timing_checked"),
        "低电量结构合法率": ("battery_structure_valid", "battery_structure_checked"),
        "决策超时率": ("deadline_missed", None),
    }
    for label, (numerator, denominator) in ratios.items():
        count = metrics[numerator]
        total = metrics[denominator] if denominator is not None else metrics["deadline_missed"] + metrics["deadline_on_time"]
        value = f"{count}/{total} ({count / total:.1%})" if total else "无样本"
        markdown.append(f"| {label} | {value} |")
    markdown.append(f"| 执行器证据范围拒绝 | {metrics['evidence_scope_rejections']} |")
    for label, key in (("逐轮延时直方图 (ms)", "turn_latency_ms_histogram"),
                       ("未通过原因", "failure_reason_counts")):
        markdown.append(f"| {label} | `{json.dumps(metrics[key], separators=(',', ':'))}` |")
    (root / "summary.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-cases", action="store_true", help="print the case matrix without starting processes")
    parser.add_argument("--case", action="append", default=[], help="select a named case (repeatable)")
    parser.add_argument("--checkpoint", type=lambda path: Path(path).resolve())
    parser.add_argument("--llama-server", type=lambda path: Path(path).resolve(), default=LLAMA)
    parser.add_argument("--cuda-device", type=int, help="physical CUDA index for Altair (default: CPU)")
    parser.add_argument("--port", type=int, default=9957, help="single port for planning and decision requests")
    parser.add_argument("--slot", action="append", type=int, choices=(5, 6, 7))
    parser.add_argument("--mission-package", type=lambda path: Path(path).resolve(), default=ARCHIVE / "iowa-context.json")
    parser.add_argument("--world-root", type=lambda path: Path(path).resolve(), default=WORLD_ROOT)
    parser.add_argument("--models-root", type=lambda path: Path(path).resolve(), default=ASSETS / "restored/models")
    parser.add_argument("--site-inventory", type=lambda path: Path(path).resolve(), default=Path(
        "/data/projects/Multi-Detect-env/mdwt/cx-x1-site-evidence/.tmp/site-products/iowa-obstacle-inventory.json"))
    parser.add_argument("--wind-plugin", type=lambda path: Path(path).resolve(),
                        default=ARCHIVE / "wind-plugin-build/libx1_wind_airspeed.so")
    parser.add_argument("--retries", type=int, default=8, help="maximum admission retries per case")
    parser.add_argument("--retry-interval-s", type=float, default=30)
    parser.add_argument("--case-budget-s", type=float, help="lower each case's table budget")
    parser.add_argument("--startup-timeout-s", type=float, default=180)
    parser.add_argument("--planner-timeout-s", type=float, default=180)
    parser.add_argument("--output", type=lambda path: Path(path).resolve())
    parser.add_argument("--plan-only", action="store_true", help="run the real planner and plan-only validation")
    args = parser.parse_args(argv)
    try:
        selected = select_cases(args.case, include_opt_in=args.list_cases)
    except ValueError as exc:
        parser.error(str(exc))
    args.slots = tuple(args.slot or (7, 6, 5))
    if len(set(args.slots)) != len(args.slots):
        parser.error("--slot values must be unique")
    if not 1 <= args.port <= 65535 or (args.cuda_device is not None and args.cuda_device < 0) or args.retries < 0:
        parser.error("invalid port, CUDA index, or retry count")
    seconds = (args.startup_timeout_s, args.planner_timeout_s, args.case_budget_s, args.retry_interval_s)
    if any(value is not None and (not math.isfinite(value) or value <= 0) for value in seconds):
        parser.error("durations must be positive and finite")
    if args.list_cases:
        return args, selected
    if args.output is None or not args.output.is_relative_to(ROOT / ".tmp") or args.output == ROOT / ".tmp":
        parser.error("--output must be a fresh directory inside the repository .tmp/")
    required = [args.checkpoint, args.llama_server, args.mission_package, args.world_root / f"{WORLD}.sdf"]
    required += [ROOT / case.mission_package for case in selected if case.mission_package]
    required += [ROOT / case.assumed_bounds for case in selected if case.assumed_bounds]
    if not args.plan_only:
        required.append(args.site_inventory)
    if any(case.fault == "wind_step" for case in selected):
        required.append(args.wind_plugin)
    for path in required:
        if path is None or not path.is_file():
            parser.error(f"required local input unavailable: {path}")
    if not os.access(args.llama_server, os.X_OK):
        parser.error("--llama-server must be executable")
    if not args.models_root.is_dir():
        parser.error("--models-root must be a directory")
    return args, selected


def process_environment(args):
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}:{ROOT}", PYTHONUTF8="1", TMPDIR=str(args.output / "tmp"),
               MULTIDETECT_RTF_WORLD_ROOT=str(args.output / "world"),
               MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=str(args.models_root), MULTIDETECT_SITL_SLOTS="8")
    env.pop("MULTIDETECT_RTF_MODEL_ROOT", None)
    no_proxy = env.get("no_proxy", env.get("NO_PROXY", ""))
    env["no_proxy"] = ",".join(filter(None, (no_proxy, "127.0.0.1", "localhost")))
    env.setdefault("RTF_MATRIX_START_LOAD_MAX", "60")
    return env


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def worker_terminated(signum, _frame):
    # The collector's exception path reaps its owned readers and container.
    raise RuntimeError(f"worker terminated by signal {signum}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--worker":
        signal.signal(signal.SIGTERM, worker_terminated)
        signal.signal(signal.SIGINT, worker_terminated)
        try:
            return run_worker(read_json(Path(argv[1])))
        except KeyboardInterrupt:
            return 130
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            write_json(Path(argv[1]).parent / "worker-error.json", {"code": type(exc).__name__, "detail": str(exc)})
            print(str(exc), file=sys.stderr)
            return 1
    args, selected = parse_args(argv)
    if args.list_cases:
        print(json.dumps([asdict(case) for case in selected], indent=2))
        return 0
    if not args.plan_only and sys.executable != str(INSTALL / ".venv-vlm/bin/python"):
        print("use the wrapper or the .venv-vlm Python path for live SITL", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "tmp").mkdir()
    # The collector only resolves world overrides inside its repository root.
    world = args.output / "world"
    world.mkdir()
    shutil.copyfile(args.world_root / f"{WORLD}.sdf", world / f"{WORLD}.sdf")
    env = process_environment(args)
    write_json(args.output / "admission.json", {
        "slots": args.slots, "start_load_max": env["RTF_MATRIX_START_LOAD_MAX"],
        "share_markers_at_start": {str(path): path.exists() for path in SHARE_MARKERS}})
    records, failures = [], []
    error = None
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with Server(args, args.output) as server:
            for case in selected:
                record, harvest_rows = run_case(case, args, args.output, env,
                                                server_alive=lambda: server.process.poll() is None)
                records.append(record)
                failures.extend(harvest_rows)
    except KeyboardInterrupt:
        error = "interrupted"
    except (OSError, ValueError, RuntimeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    summarize(args.output, records, failures, error)
    print(args.output / "summary.json")
    if error is not None:
        print(error, file=sys.stderr)
        return 130 if error == "interrupted" else 1
    success = "plan_only" if args.plan_only else "pass"
    return 0 if all(record["status"] == success for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
