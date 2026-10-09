"""NEW source completion followed by bounded actual committed-packet freshness.

V1 remains frozen. This version waits for real low-rate event packets after
native/camera work instead of ending at a temporarily empty receiver queue.
No source timestamp, two-second freshness contract or RX path is changed.
"""

from __future__ import annotations

import copy
from pathlib import Path

from multidetect.autonomy.teacher_live_navigation_checkpoint import REQUIRED_MESSAGES
from scripts import collect_teacher_live_navigation_checkpoint_v1 as frozen

CONTRACT = "multidetect.teacher-live-navigation-collection-v2"
TARGET_CONTEXT_AGE_S = 0.75
MINIMUM_FINALIZATION_HEADROOM_S = 0.4


def _latest_context_state(capture, now):
    """Only inspect the writer's already-published original committed times."""
    times, indices, missing = {}, {}, []
    for kind in REQUIRED_MESSAGES:
        row = capture._latest.get(kind)
        if row is None:
            missing.append(kind)
        else:
            times[kind] = row["received_at_s"]
            indices[kind] = row["record_index"]
    ages = {kind: now - at for kind, at in times.items()}
    ready = (
        not missing
        and all(0 <= age < TARGET_CONTEXT_AGE_S for age in ages.values())
        and max(times.values()) - min(times.values()) < TARGET_CONTEXT_AGE_S
    )
    return {
        "observed_at_s": now,
        "original_received_at_s": times,
        "original_record_indices": indices,
        "ages_s": ages,
        "missing": missing,
        "ready": ready,
        "received_times_renewed": False,
    }


def _wait_for_current_packets(session, receiver, capture, worker, deadline, report):
    started = session.clock.monotonic()
    frozen._require(len(worker.camera_updates) == 2, "Two original current camera frames required")
    oldest = min(
        *(frame["host_return_at_s"] for frame in worker.camera_updates),
        worker.rows["navigation-after"]["started_at_s"],
        worker.rows["owned-inspect-after"]["started_at_s"],
    )
    latest_deadline = min(deadline, oldest + 2 - MINIMUM_FINALIZATION_HEADROOM_S)
    report["freshness_wait"] = {
        "started_at_s": started,
        "oldest_camera_native_at_s": oldest,
        "deadline_s": latest_deadline,
        "finished_at_s": None,
        "target_context_age_s": TARGET_CONTEXT_AGE_S,
        "finalization_headroom_s": MINIMUM_FINALIZATION_HEADROOM_S,
        "telemetry_requests_sent": False,
        "latest": None,
    }
    wait = report["freshness_wait"]
    try:
        while True:
            frozen._check_open_current(session, receiver, capture, deadline)
            now = session.clock.monotonic()
            state = _latest_context_state(capture, now)
            wait["latest"] = state
            frozen._require(
                now < latest_deadline,
                "Original camera/native lifetime leaves no finalization headroom",
            )
            if state["ready"]:
                return
            batch = receiver.read_batch(min(0.01, latest_deadline - now))
            if batch:
                capture.append_batch(batch)
    finally:
        wait["finished_at_s"] = session.clock.monotonic()


def collect_live_checkpoint(
    session,
    receiver,
    capture,
    plan,
    root,
    output,
    *,
    profile_source=None,
    runtime_source=None,
    world_source=None,
    model_sources,
    parameter_baseline=None,
    timeout_s=20.0,
):
    """Same V1 callable inputs, with bounded unique-RX event freshness wait.

    Current V3/Window capture is supported here. NEW explicit V4/CP-v2 budget
    admission is separate and must not be inferred from this V3 return type.
    ``collection.json`` retains exact inputs/timings on any later refusal.
    """
    timeout = frozen._finite_timeout(timeout_s)
    frozen._check_open(session, receiver, capture, plan)
    root, directory = Path(root).resolve(), Path(output).resolve()
    frozen._require(
        directory.is_relative_to(root) and directory != root and not directory.exists(),
        "Fresh contained collection output required",
    )
    frozen._require(
        type(model_sources) is dict and bool(model_sources),
        "Original model source closure required",
    )
    frozen._require(
        parameter_baseline is None or type(parameter_baseline) is dict,
        "Original source-bound parameter baseline row required",
    )
    directory.mkdir()
    sources = directory / "sources"
    sources.mkdir()
    started = session.clock.monotonic()
    deadline = min(session.deadline_s, started + timeout)
    worker, primary = None, None
    report = {
        "contract": CONTRACT,
        "clock_id": session.clock.session_id,
        "container_id": session.container_id,
        "owner_token": session.owner_token,
        "started_at_s": started,
        "finished_at_s": None,
        "deadline_s": deadline,
        "native_rows": {},
        "camera_updates": [],
        "parameter_baseline": parameter_baseline,
        "baseline_reused": parameter_baseline is not None,
        "collection_complete": False,
        "checkpoint_qualified": False,
        "writer_sealed": False,
        "RX_restarted": False,
        "worker_exited": False,
        "error": None,
        "claims": dict(frozen.CLAIMS),
    }
    try:
        if profile_source is None:
            path = directory / "profile.json"
            frozen._json_new(path, frozen._profile(plan))
            profile_source = frozen._source(path, root)
        if runtime_source is None:
            _, runtime_source = frozen._runtime_source(plan, root)
        if world_source is None:
            world_source = frozen._source(plan.docker_plan.world_path, root)
        for source in (profile_source, runtime_source, world_source, *model_sources.values()):
            frozen._verify_source(source, root)
        report["sources"] = {
            "profile": profile_source.to_mapping(),
            "runtime": runtime_source.to_mapping(),
            "world": world_source.to_mapping(),
            "models": {k: v.to_mapping() for k, v in model_sources.items()},
        }
        worker = frozen._SourceOwner(session, sources, deadline, parameter_baseline)
        worker.thread.start()
        while not worker.done.is_set():
            frozen._check_open_current(session, receiver, capture, deadline)
            batch = receiver.read_batch(0.01)
            if batch:
                capture.append_batch(batch)
        worker.thread.join(timeout=0.1)
        frozen._require(not worker.thread.is_alive(), "Scoped source worker exit unobserved")
        if worker.failure is not None:
            raise worker.failure
        report["collection_complete"] = True
        _wait_for_current_packets(session, receiver, capture, worker, deadline, report)
        report["prefix_export_started_at_s"] = session.clock.monotonic()
        prefix = frozen.capture_teacher_committed_prefix(
            capture,
            artifact_root=root,
            output_directory=directory / "prefix",
            clock=session.clock,
        )
        report["prefix_export_finished_at_s"] = session.clock.monotonic()
        report["prefix_source"] = prefix.to_mapping()
        now = session.clock.monotonic()
        report["qualification_started_at_s"] = now
        checkpoint = frozen.qualify_teacher_live_navigation_checkpoint(
            artifact_root=root,
            output_directory=directory / "checkpoint",
            prefix_source=prefix,
            profile_source=profile_source,
            runtime_source=runtime_source,
            world_source=world_source,
            model_sources=model_sources,
            camera_updates=worker.camera_updates,
            parameter_baseline=worker.baseline,
            navigation_before=worker.rows["navigation-before"],
            navigation_after=worker.rows["navigation-after"],
            owned_inspect_before=worker.rows["owned-inspect-before"],
            owned_inspect_after=worker.rows["owned-inspect-after"],
            plan=plan.to_mapping(),
            container_id=session.container_id,
            owner_token=session.owner_token,
            now_s=now,
            clock_id=session.clock.session_id,
            live_capture=capture,
            live_clock=session.clock,
        )
        report["qualification_finished_at_s"] = session.clock.monotonic()
        report["checkpoint_source"] = checkpoint.checkpoint_source.to_mapping()
        report["checkpoint_qualified"] = True
        return checkpoint
    except BaseException as error:
        primary, report["error"] = error, frozen._error(error)
        raise
    finally:
        if worker is not None:
            worker.cancelled.set()
            worker.thread.join(timeout=5)
            report["worker_exited"] = not worker.thread.is_alive()
            if report["worker_exited"]:
                report["native_rows"] = copy.deepcopy(worker.rows)
                report["camera_updates"] = copy.deepcopy(worker.camera_updates)
                report["camera_stats"] = copy.deepcopy(worker.camera_stats)
                report["parameter_baseline"] = copy.deepcopy(worker.baseline)
            else:
                report["cleanup_error"] = (
                    "Scoped source worker still live; outer supervisor required"
                )
        report["finished_at_s"] = session.clock.monotonic()
        report["writer_closed_at_return"] = capture._closed
        report["RX_at_return"] = receiver.to_mapping()
        try:
            frozen._json_new(directory / "collection.json", report)
            if worker is not None and not report["worker_exited"] and primary is None:
                raise RuntimeError(report["cleanup_error"])
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"Collection receipt persistence also failed: {frozen._error(error)}")


__all__ = ["CONTRACT", "collect_live_checkpoint"]
