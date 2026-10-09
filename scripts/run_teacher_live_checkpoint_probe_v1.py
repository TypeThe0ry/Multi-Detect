"""Observe a CURRENT navigation checkpoint in one owned SIM; never fly.

The worker keeps the sole RX and main-thread dual-commit writer alive while
the bounded provider obtains original RGB/depth and native reference reads.
It records refusal as refusal, then drains and closes its own container. A
closed report is historical evidence, not a renewable live checkpoint.
Execution must be launched by the process-tree supervisor in this repository.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import require_int, require_real  # noqa: E402
from scripts.collect_onboard_teacher_gazebo_origin_window import (  # noqa: E402
    SOURCE_PATHS as WINDOW_SOURCES,
)
from scripts.collect_onboard_teacher_gazebo_origin_window import (  # noqa: E402
    WindowTelemetryCapture,
    _error,
)
from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan  # noqa: E402
from scripts.onboard_teacher_observed_context import REQUIRED_MESSAGES  # noqa: E402
from scripts.onboard_teacher_sim_operator_v2 import (  # noqa: E402
    DisposableSimOperatorIntent,
    OwnedTeacherGazeboSessionV5,
    SimOperator,
)
from scripts.probe_onboard_teacher_gazebo_navref import _record, _require  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import (  # noqa: E402
    EXTRA_MESSAGES,
    _json_new,
    _profile,
    _runtime_source,
    _source,
    _verify_source,
)

CONTRACT = "multidetect.teacher-current-navigation-probe-v1"
SCENARIO = "configs/scenarios/urban_inspection_multirotor_001.teacher_spawn_probe_r1.scenario.json"
RUNTIME = "configs/scenarios/urban_inspection_multirotor_001.teacher_spawn_probe_r1.runtime.json"
EXTRACTION = "artifacts/diagnostics/onboard-teacher-navref-image-assets-20261008-r2/extraction.json"
MODEL_PATHS = {
    "x500_mono_cam": "simulation/gazebo/models/x500_mono_cam/model.sdf",
    "x500": "artifacts/diagnostics/onboard-teacher-navref-image-assets-20261008-r2/x500-model.sdf",
    "x500_base": (
        "artifacts/diagnostics/onboard-teacher-navref-image-assets-20261008-r2/x500-base-model.sdf"
    ),
}
SOURCE_PATHS = tuple(
    dict.fromkeys(
        (
            *WINDOW_SOURCES,
            "scripts/run_teacher_live_checkpoint_probe_v1.py",
            "scripts/collect_teacher_live_navigation_checkpoint_v1.py",
            "src/multidetect/autonomy/teacher_live_navigation_checkpoint.py",
            SCENARIO,
            RUNTIME,
            EXTRACTION,
            *MODEL_PATHS.values(),
        )
    )
)
LIFECYCLE_SOURCE_PATHS = (
    "scripts/onboard_teacher_sortie_budget_v1.py",
    "scripts/onboard_teacher_sortie_artifact_ledger_v1.py",
    "scripts/onboard_teacher_telemetry_capture_v4.py",
    "scripts/collect_teacher_live_navigation_checkpoint_v2.py",
    "scripts/collect_teacher_live_navigation_checkpoint_v3.py",
    "src/multidetect/autonomy/teacher_live_navigation_checkpoint_v2.py",
)
FALSE_CLAIMS = {
    name: False
    for name in (
        "closed_checkpoint_is_current",
        "exact_synchronization_claimed",
        "Mission_uploaded",
        "Grant_created",
        "ARM_sent",
        "flight_executed",
        "model_or_optimizer_performed",
        "candidate_promoted",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "complete_sortie_budget_enforced_by_probe",
    )
}


def fresh_output(path, root=ROOT):
    requested = Path(path).absolute()
    base = (Path(root).resolve() / "artifacts/diagnostics").resolve(strict=True)
    parent = requested.parent.resolve(strict=True)
    resolved = parent / requested.name
    _require(
        parent.is_relative_to(base)
        and resolved != base
        and not requested.is_symlink()
        and not resolved.exists(),
        "fresh contained diagnostics output required",
    )
    return resolved


def source_inventory(plan, root=ROOT, collector_version=1):
    extra = (
        ("scripts/collect_teacher_live_navigation_checkpoint_v2.py",)
        if collector_version == 2
        else LIFECYCLE_SOURCE_PATHS
        if collector_version == 3
        else ()
    )
    paths = (*SOURCE_PATHS, *extra, plan.source_manifest_path, plan.docker_plan.world_path)
    unique = dict.fromkeys((Path(root) / path).resolve(strict=True) for path in paths)
    return [_record(path) for path in unique]


def whole_file_source(path, root=ROOT):
    # XML is an entire byte source, not JSON at the root pointer "".
    return replace(_source(path, root), json_pointer=None)


def validate_capture_versions(capture_version, collector_version):
    _require(
        type(capture_version) is int
        and type(collector_version) is int
        and (capture_version, collector_version) in {(3, 1), (3, 2), (4, 3)},
        "explicit compatible capture/collector versions required",
    )


def lifecycle_budget_for_probe(session, queue_capacity):
    """One finite SIM allocation at the original session start, not a Grant.

    Ceilings are declarations enforced by the NEW writer/ledger. The no-flight
    probe neither measures flight performance nor consumes a Mission permit.
    """
    from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget

    started = session.deadline_s - session.plan.session_timeout_s
    budget = TeacherSortieLifecycleBudget(
        clock_id=session.clock.session_id,
        started_at_s=started,
        preflight_s=120,
        transfer_s=20,
        mission_mode_s=5,
        normal_arm_s=10,
        route_s=100,
        rtl_land_s=30,
        cleanup_s=25,
        packet_rate_ceiling_hz=220,
        protocol_packet_reserve=128,
        maximum_raw_record_bytes=4608,
        maximum_projection_record_bytes=8192,
        maximum_receipt_record_bytes=8192,
        snapshot_bytes=16 * 1024 * 1024,
        camera_bytes=64 * 1024 * 1024,
        evidence_bytes=16 * 1024 * 1024,
        start_camera_reserve_bytes=32 * 1024 * 1024,
        start_evidence_reserve_bytes=1024 * 1024,
        queue_capacity=queue_capacity,
        maximum_consumer_blocked_s=1,
        maximum_dual_commit_s=0.5,
    )
    _require(budget.deadline_s <= session.deadline_s, "session cannot cover bound lifecycle")
    budget.assert_phase(
        "preflight", now_s=session.clock.monotonic(), clock_id=session.clock.session_id
    )
    return budget


def observation_streams_ready(capture, now_s):
    return all(
        capture.latest_message(
            kind, now_s=now_s, maximum_age_s=30 if kind == "GPS_GLOBAL_ORIGIN" else 2
        )
        is not None
        for kind in (*REQUIRED_MESSAGES, *EXTRA_MESSAGES)
    )


def wait_reference_stability(capture, clock, drain, *, deadline_s, stable_s=5):
    """Wait for observed startup HOME/reference stability; never freeze HOME.

    All packets continue to be retained. This is a scheduling precondition,
    not a replacement for the qualifier's co-window reset/source checks.
    """
    duration = require_real(stable_s, "reference stable duration", minimum=1, maximum=10)
    started = clock.monotonic()
    previous = since = None
    changes = []
    while True:
        drain()
        now = clock.monotonic()
        _require(now < deadline_s, "bounded HOME/reference stabilization wait exhausted")
        rows = {
            kind: capture.latest_message(
                kind,
                now_s=now,
                maximum_age_s=30 if kind == "GPS_GLOBAL_ORIGIN" else 2,
            )
            for kind in ("HOME_POSITION", "GPS_GLOBAL_ORIGIN", "LOCAL_POSITION_NED")
        }
        state = None
        if all(row is not None for row in rows.values()):
            home = rows["HOME_POSITION"]["message"]
            origin = rows["GPS_GLOBAL_ORIGIN"]["message"]
            local = rows["LOCAL_POSITION_NED"]["message"]
            speed_sq = sum(
                require_real(local[k], "observed ground velocity") ** 2 for k in ("vx", "vy", "vz")
            )
            if speed_sq <= 0.3**2:
                state = (
                    tuple(
                        home[k]
                        for k in ("latitude", "longitude", "altitude", "x", "y", "z", "time_usec")
                    ),
                    tuple(origin[k] for k in ("latitude", "longitude", "altitude", "time_usec")),
                )
        if state is None:
            previous = since = None
            continue
        if state != previous:
            _require(len(changes) < 128, "bounded reference change history exhausted")
            previous, since = state, now
            changes.append(
                {"observed_at_s": now, "HOME": list(state[0]), "GPS_GLOBAL_ORIGIN": list(state[1])}
            )
        if now - since >= duration:
            return {
                "started_at_s": started,
                "finished_at_s": now,
                "declared_stability_s": duration,
                "unchanged_since_s": since,
                "observed_reference_changes": changes,
                "latest_original_record_indices": {
                    k: row["record_index"] for k, row in rows.items()
                },
                "latest_original_received_at_s": {
                    k: row["received_at_s"] for k, row in rows.items()
                },
                "packets_discarded": False,
                "HOME_manually_set": False,
                "static_origin_is_current_sensor": False,
                "static_origin_rechecked_against_current_native_EKF_by_qualifier": True,
                "navigation_qualified": False,
            }


def run_probe(
    plan,
    output_directory,
    *,
    operator_intent,
    execute=False,
    readiness_timeout_s=30,
    queue_capacity=512,
    collector_version=1,
    capture_version=3,
    artifact_root=ROOT,
    session_factory=None,
    checkpoint_collector=None,
    operator_factory=None,
):
    _require(type(execute) is bool, "explicit boolean execute required")
    validate_capture_versions(capture_version, collector_version)
    if capture_version == 4:
        _require(plan.session_timeout_s >= 310, "session cannot cover bound lifecycle")
    _require(
        type(operator_intent) is DisposableSimOperatorIntent, "exact SIM operator intent required"
    )
    _require(plan.frame_count == 2, "two original camera frames required")
    duration = require_real(readiness_timeout_s, "readiness_timeout_s", minimum=1, maximum=60)
    capacity = require_int(queue_capacity, "queue_capacity", minimum=32, maximum=4096)
    root = Path(artifact_root).resolve(strict=True)
    output = fresh_output(output_directory, root)
    before = source_inventory(plan, root, collector_version)
    _, runtime_source = _runtime_source(plan, root)
    injected = any(
        value is not None for value in (session_factory, checkpoint_collector, operator_factory)
    )
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "error": None,
        "execute_requested": execute,
        "collector_version": collector_version,
        "capture_version": capture_version,
        "lifecycle_budget": None,
        "artifact_capacity": None,
        "injected_dependencies_used": injected,
        "external_SIM_observed_here": False,
        "checkpoint_qualified_while_session_open": False,
        "checkpoint_is_historical_after_close": True,
        "plan": plan.to_mapping(),
        "sources_before": before,
        "sources_after": None,
        "selected_sources_unchanged_during_run": False,
        "source_inventory_scope": "listed sources only, not complete dependency attestation",
        "queue_capacity": capacity,
        "claims": dict(FALSE_CLAIMS),
        "close_errors": [],
        "raw_telemetry": None,
    }
    if not execute:
        return report
    if checkpoint_collector is None:
        if collector_version == 1:
            from scripts.collect_teacher_live_navigation_checkpoint_v1 import (
                collect_live_checkpoint,
            )
        elif collector_version == 2:
            from scripts.collect_teacher_live_navigation_checkpoint_v2 import (
                collect_live_checkpoint,
            )
        else:
            from scripts.collect_teacher_live_navigation_checkpoint_v3 import (
                collect_live_checkpoint,
            )

        checkpoint_collector = collect_live_checkpoint
    output.mkdir(exist_ok=False)
    _json_new(output / "plan.json", report["plan"])
    session = receiver = capture = ledger = None
    primary = None
    capture_closed = False
    try:
        session = (session_factory or OwnedTeacherGazeboSessionV5)(plan, execute=True)
        with session:
            deadline = min(session.deadline_s, session.clock.monotonic() + 180)
            budget = lifecycle_budget_for_probe(session, capacity) if capture_version == 4 else None
            if budget is not None:
                deadline = min(deadline, budget.phase_deadline_s("preflight"))
                report["lifecycle_budget"] = budget.to_mapping()
                report["lifecycle_budget_sha256"] = budget.canonical_hash

            def drain(timeout=0.01, *, closing=False):
                batch = receiver.read_batch(timeout)
                if batch:
                    capture.append_batch(batch)
                if not closing:
                    receiver.raise_if_failed()
                    _require(session.clock.monotonic() < deadline, "probe deadline exhausted")

            try:
                intent = replace(
                    operator_intent, experiment_id="teacher-current-navigation-20261008-r1"
                )
                operator = (operator_factory or SimOperator)(
                    session, intent, output / "sim-operator", repository_root=root
                )
                report["operator"] = operator.apply()
                report["clock"] = session.clock.identity()
                profile_path = output / "profile.json"
                _json_new(profile_path, _profile(plan))
                profile_source = _source(profile_path, root)
                capture_class, capture_options = WindowTelemetryCapture, {}
                if budget is not None:
                    from scripts.onboard_teacher_sortie_artifact_ledger_v1 import (
                        TeacherSortieArtifactLedger,
                    )
                    from scripts.onboard_teacher_telemetry_capture_v4 import (
                        TeacherTelemetryCaptureV4,
                    )

                    ledger = TeacherSortieArtifactLedger(
                        budget=budget,
                        clock=session.clock.monotonic,
                        camera_directory=output / "camera-owned",
                        evidence_directory=output / "evidence-owned",
                    )
                    capture_class, capture_options = TeacherTelemetryCaptureV4, {"budget": budget}
                capture = capture_class(
                    output / "telemetry",
                    clock=session.clock.monotonic,
                    clock_id=session.clock.session_id,
                    transaction_id="current-navigation-" + uuid4().hex,
                    local_origin_id="unresolved-current-navigation-" + uuid4().hex,
                    **capture_options,
                )
                receiver = session.start_receiver(queue_capacity=capacity)
                ready_deadline = min(deadline, session.clock.monotonic() + duration)
                while (
                    capture.latest_message(
                        "HEARTBEAT", now_s=session.clock.monotonic(), maximum_age_s=2
                    )
                    is None
                ):
                    drain()
                    _require(
                        session.clock.monotonic() < ready_deadline,
                        "initial heartbeat wait exhausted",
                    )
                report["stream_requests"] = receiver.request_streams(
                    (*REQUIRED_MESSAGES, *EXTRA_MESSAGES)
                )
                while not observation_streams_ready(capture, session.clock.monotonic()):
                    drain()
                    _require(
                        session.clock.monotonic() < ready_deadline, "fresh context wait exhausted"
                    )
                drain(0)
                report["navigation_stabilization"] = wait_reference_stability(
                    capture, session.clock, drain, deadline_s=ready_deadline
                )
                report["checkpoint_attempt_started_at_s"] = session.clock.monotonic()
                checkpoint = checkpoint_collector(
                    session,
                    receiver,
                    capture,
                    plan,
                    root,
                    ledger.evidence_directory / "live-collection"
                    if ledger
                    else output / "live-collection",
                    profile_source=profile_source,
                    runtime_source=runtime_source,
                    world_source=whole_file_source(plan.docker_plan.world_path, root),
                    model_sources={
                        name: whole_file_source(root / path, root)
                        for name, path in MODEL_PATHS.items()
                    },
                    **({"artifact_ledger": ledger} if ledger else {}),
                )
                report["checkpoint_attempt_finished_at_s"] = session.clock.monotonic()
                _require(
                    checkpoint.qualified_at_s
                    <= session.clock.monotonic()
                    < checkpoint.expires_at_s,
                    "collector returned an already expired/future checkpoint",
                )
                report["checkpoint_source"] = checkpoint.checkpoint_source.to_mapping()
                report["checkpoint_sha256"] = checkpoint.canonical_hash
                report["checkpoint_expires_at_s"] = checkpoint.expires_at_s
                report["checkpoint_qualified_while_session_open"] = True
                report["status"] = "checkpoint-recorded"
                _verify_source(runtime_source, root)
            except BaseException as error:
                primary = error
                report.update(status="checkpoint-rejected", error=_error(error))
                if session is not None:
                    report["checkpoint_attempt_finished_at_s"] = session.clock.monotonic()
            finally:
                # Stop the producer first, then consume only the finite queue.
                if receiver is not None:
                    try:
                        session.stop_receiver()
                        state = receiver.to_mapping()
                        _require(
                            state["thread_alive"] is False, "RX did not exit before final drain"
                        )
                        remaining = require_int(
                            state["queued_packets"], "queued packets", minimum=0, maximum=capacity
                        )
                        while remaining:
                            previous = receiver.to_mapping()["consumed_packets"]
                            drain(0, closing=True)
                            count = receiver.to_mapping()["consumed_packets"] - previous
                            _require(
                                0 < count <= remaining, "finite closing queue did not progress"
                            )
                            remaining -= count
                        receiver.raise_if_failed()
                    except BaseException as error:
                        report["close_errors"].append(_error(error))
                try:
                    session.stop_camera()
                except BaseException as error:
                    report["close_errors"].append(_error(error))
                if capture is not None:
                    try:
                        closed = (
                            capture.seal()
                            if primary is None and not report["close_errors"]
                            else capture.abort(
                                "Current checkpoint refused; no execution or training authority"
                            )
                        )
                        capture_closed = True
                        report["raw_telemetry"] = {
                            key: closed[key]
                            for key in (
                                "status",
                                "receipt",
                                "record_count",
                                "packet_attempt_count",
                                "accepted_source_count",
                                "foreign_source_count",
                                "orphan_packet_count",
                                "data_budget_charged_bytes",
                                "journal_sources",
                                "commit_counts",
                            )
                            if key in closed
                        }
                    except BaseException as error:
                        report["close_errors"].append(_error(error))
                if ledger is not None:
                    try:
                        report["artifact_capacity"] = ledger.capacity()
                    except BaseException as error:
                        report["artifact_capacity_error"] = _error(error)
                        report["close_errors"].append(_error(error))
                    finally:
                        ledger.close()
    except BaseException as error:
        report.update(status="diagnostics-failed", error=_error(error))
    finally:
        if capture is not None and not capture_closed:
            try:
                capture.abort("Worker failed; no current observation or authority")
            except BaseException as error:
                report["close_errors"].append(_error(error))
        for name, value in (("session", session), ("receiver", receiver)):
            if value is not None:
                try:
                    report[name] = value.to_mapping()
                except BaseException as error:
                    report["close_errors"].append(_error(error))
        try:
            after = source_inventory(plan, root, collector_version)
            report["sources_after"] = after
            _require(before == after, "selected source changed during current probe")
            _verify_source(runtime_source, root)
            report["selected_sources_unchanged_during_run"] = True
            _require(
                report.get("session", {}).get("cleanup_verified") is True,
                "owned container cleanup unverified",
            )
        except BaseException as error:
            report["close_errors"].append(_error(error))
        raw = report["raw_telemetry"] or {}
        report["external_SIM_observed_here"] = (
            not injected and raw.get("accepted_source_count", 0) > 0
        )
        if report["close_errors"]:
            report["status"] = "diagnostics-failed"
        report["receipt"] = _json_new(output / "current-navigation-probe.json", report)
    return report


def main(argv=None):
    from scripts.diagnose_onboard_teacher_px4_events import strict_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=ROOT / SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--operator-intent", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--collector-version", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--capture-version", type=int, choices=(3, 4), default=3)
    parser.add_argument("--session-timeout-s", type=float, default=240)
    args = parser.parse_args(argv)
    output = fresh_output(args.output_directory)
    job = fresh_output(output.parent / (output.name + "-job"))
    intent = DisposableSimOperatorIntent.from_mapping(
        strict_json(args.operator_intent.read_bytes())
    )
    plan = build_teacher_session_plan(
        args.scenario, job, frame_count=2, session_timeout_s=args.session_timeout_s
    )
    report = run_probe(
        plan,
        output,
        operator_intent=intent,
        execute=args.execute,
        collector_version=args.collector_version,
        capture_version=args.capture_version,
    )
    from multidetect.autonomy.serialization import canonical_json

    print(
        canonical_json(
            {
                key: report.get(key)
                for key in (
                    "status",
                    "error",
                    "external_SIM_observed_here",
                    "checkpoint_qualified_while_session_open",
                    "receipt",
                )
            }
        )
    )
    return 0 if report["status"] in {"planned", "checkpoint-recorded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
