"""NEW exact V4 budget adapter using frozen V1 source owner/V2 freshness wait.

No old collector, resource ceiling or live lease is modified. The active
phase deadline and writer budget were bound once before collection. Actual
Docker/RX/session creation is the caller's responsibility, never this module.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import replace
from pathlib import Path

from multidetect.autonomy import teacher_live_navigation_checkpoint_v2 as checkpoint_api
from scripts import collect_teacher_live_navigation_checkpoint_v1 as source_api
from scripts import collect_teacher_live_navigation_checkpoint_v2 as wait_api

CONTRACT = "multidetect.teacher-live-navigation-collection-v3"


def _prepare_source_reservations(ledger, sources, camera_directory, plan, baseline):
    """Only the MAIN owner charges all finite worker paths before APIs."""
    reservations = {}
    names = [
        "loaded-world-sdf",
        "owned-inspect-before",
        "navigation-before",
        "navigation-after",
        "owned-inspect-after",
    ]
    if baseline is None:
        names.insert(0, "parameter-baseline")
    for name in names:
        for suffix, limit in (
            ("-attempt.json", 65536),
            ("-result.json", 65536),
            (".stdout.bin", source_api.NATIVE_LIMIT_BYTES),
            (".stderr.bin", source_api.NATIVE_LIMIT_BYTES),
        ):
            path = sources / f"{name}{suffix}"
            reservations[path.as_posix()] = ledger.reserve_write(
                "evidence",
                path,
                maximum_bytes=limit,
            )
    for index in range(2):
        target = camera_directory / f"camera-{index:03d}"
        target.mkdir()
        for filename, limit in (
            ("rgb-bgr24.bin", plan.width * plan.height * 3),
            ("depth.bin", plan.width * plan.height * 4),
            ("camera.json", 128 * 1024),
        ):
            path = target / filename
            reservations[path.as_posix()] = ledger.reserve_write(
                "camera",
                path,
                maximum_bytes=limit,
            )
    return reservations


def _commit_worker_reservations(ledger, reservations):
    from scripts.onboard_teacher_sortie_artifact_ledger_v1 import LedgerFileBinding

    for absolute, reservation in reservations.items():
        if reservation.index not in ledger._pending:
            continue  # This exact source was already durably committed.
        path = Path(absolute)
        if not path.exists():
            continue  # Pending credit remains and prevents any control grant.
        size = path.stat().st_size
        source_api._require(
            size <= reservation.maximum_bytes,
            "Actual worker file exceeded permanently charged capacity",
        )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while raw := stream.read(1024 * 1024):
                digest.update(raw)
        ledger.commit_write(reservation, LedgerFileBinding(absolute, size, digest.hexdigest()))


class _PrecreditedSourceOwner(source_api._SourceOwner):
    """Same native owner, with camera bytes in its independent charged scope."""

    def __init__(self, *args, camera_directory, reservations, **kwargs):
        super().__init__(*args, **kwargs)
        self.camera_directory, self.reservations = camera_directory, reservations

    def _native(self, name):
        from multidetect.autonomy.serialization import canonical_json

        # The frozen native helper has fixed fields, integer/bool results and
        # three errors bounded to 1024 characters. Bound the only variable
        # command/path fields before that helper can write an attempt/result.
        command_bytes = len(canonical_json(self._command(name)).encode())
        paths = [self.directory / f"{name}.{label}.bin" for label in ("stdout", "stderr")]
        source_api._require(
            command_bytes <= 8192
            and all(len(canonical_json(p.as_posix()).encode()) <= 8192 for p in paths)
            and all(
                self.reservations[
                    (self.directory / f"{name}-{stage}.json").as_posix()
                ].maximum_bytes
                >= 65536
                for stage in ("attempt", "result")
            ),
            "Fixed native metadata exceeds its pre-write envelope",
        )
        return super()._native(name)

    def _camera(self):
        session = self.session
        self._now()
        backend = session._camera(session.plan, session.container_id, session.clock)
        camera, primary = None, None
        try:
            camera = source_api.BoundedTeacherCamera(
                backend,
                session.plan,
                session.clock,
                self.deadline_s,
            )
            with session._camera_lock:
                source_api._require(session._active_camera is None, "Camera owner overlap refused")
                session._active_camera = camera
            for index in range(2):
                self._now()
                observation = camera.read(deadline_s=self.deadline_s)
                target = self.camera_directory / f"camera-{index:03d}"
                # Bounds are checked BEFORE the frozen byte writer. Metadata
                # path/bytes/SHA are deterministic from these same originals.
                metadata = observation.to_mapping()
                payloads = {}
                for name, raw in (
                    ("rgb-bgr24", observation.rgb_bgr24),
                    ("depth", getattr(observation.depth, "payload", None)),
                ):
                    path = target / f"{name}.bin"
                    ticket = self.reservations[path.as_posix()]
                    source_api._require(
                        type(raw) is bytes and 0 < len(raw) <= ticket.maximum_bytes,
                        "Camera original exceeds its prior physical reservation",
                    )
                    payloads[name] = {
                        "path": path.as_posix(),
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                from multidetect.autonomy.serialization import canonical_json
                from scripts.run_onboard_teacher_gazebo_observation_v2 import FALSE_CLAIMS

                metadata["persisted_payloads"], metadata["claims"] = payloads, dict(FALSE_CLAIMS)
                predicted = (canonical_json(metadata) + "\n").encode()
                source_api._require(
                    len(predicted)
                    <= self.reservations[(target / "camera.json").as_posix()].maximum_bytes,
                    "Camera metadata exceeds its prior physical reservation",
                )
                binding = source_api._persist_camera(target, observation)
                source_api._require(
                    binding["bytes"] == len(predicted)
                    and binding["sha256"] == hashlib.sha256(predicted).hexdigest(),
                    "Persisted camera metadata differs from prebounded originals",
                )
                self.camera_updates.append(
                    {
                        "window_index": 0,
                        "frame_index": index,
                        "camera": binding,
                        "host_return_at_s": observation.host_return_stamp.value_ns / 1e9,
                        "source_sensor_stamp_ns": observation.provenance.sensor_stamp_ns,
                    }
                )
            camera.close(require_complete=True)
            self.camera_stats = camera.source.stats.to_mapping()
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                if camera is not None:
                    camera.close(require_complete=primary is None)
                else:
                    backend.close()
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"Scoped camera cleanup also failed: {source_api._error(error)}")
            finally:
                with session._camera_lock:
                    if session._active_camera is camera:
                        session._active_camera = None


def _scope(session, receiver, capture, plan, phase):
    source_api._require(
        isinstance(session, source_api.OwnedTeacherGazeboSessionV3)
        and type(plan) is source_api.TeacherSessionPlan
        and session.plan is plan
        and session.execute is True
        and session._entered is True
        and not session._closed
        and session._failure is None,
        "Actual open original owned session/plan required",
    )
    source_api._require(
        isinstance(receiver, source_api.ConcurrentTelemetryReceiver)
        and session.receiver is receiver
        and receiver._owner == threading.get_ident() == session._owner
        and receiver.clock == session.clock.monotonic
        and receiver.to_mapping()["thread_alive"] is True
        and not receiver._stop.is_set(),
        "Original live unique RX/main consumer required",
    )
    receiver.raise_if_failed()
    budget = checkpoint_api._writer(capture, session.clock)
    budget.assert_phase(phase, now_s=session.clock.monotonic(), clock_id=session.clock.session_id)
    source_api._require(
        receiver.capacity == budget.queue_capacity,
        "Actual RX capacity differs from bound lifecycle allocation",
    )
    source_api._require(
        plan.frame_count == 2
        and session._active_camera is None
        and (session._camera_thread is None or not session._camera_thread.is_alive()),
        "Declared two-frame plan with no overlapping camera required",
    )
    session._remaining()
    return budget


def collect_live_checkpoint(
    session,
    receiver,
    capture,
    plan,
    root,
    output,
    *,
    model_sources,
    profile_source=None,
    runtime_source=None,
    world_source=None,
    parameter_baseline=None,
    phase="preflight",
    timeout_s=20.0,
    artifact_ledger=None,
):
    """Return typed CP-v2 from exact V4 budget, with original times preserved.

    Native/camera ownership and bounded event freshness are the unchanged
    previous implementations. This adapter owns only NEW typed V4 admission,
    phase deadlines, exact whole-file SDF defaults and CP-v2 persistence.
    """
    timeout = source_api._finite_timeout(timeout_s)
    budget = _scope(session, receiver, capture, plan, phase)
    checkpoint_api._ledger_scope(artifact_ledger, capture, session.clock)
    root, directory = Path(root).resolve(), Path(output).resolve()
    source_api._require(
        directory.is_relative_to(root) and directory != root and not directory.exists(),
        "Fresh contained lifecycle collection output required",
    )
    source_api._require(
        type(model_sources) is dict
        and bool(model_sources)
        and all(s.json_pointer is None for s in model_sources.values()),
        "Exact whole-file original SDF model sources required",
    )
    source_api._require(
        parameter_baseline is None or type(parameter_baseline) is dict,
        "Original source-bound parameter baseline row required",
    )
    directory.mkdir()
    sources = directory / "sources"
    sources.mkdir()
    camera_directory, reservations = None, {}
    started = session.clock.monotonic()
    deadline = min(session.deadline_s, budget.phase_deadline_s(phase), started + timeout)
    worker, primary, checkpoint = None, None, None
    report = {
        "contract": CONTRACT,
        "clock_id": session.clock.session_id,
        "container_id": session.container_id,
        "owner_token": session.owner_token,
        "phase": phase,
        "lifecycle_budget": budget.to_mapping(),
        "lifecycle_budget_sha256": budget.canonical_hash,
        "artifact_ledger_enforced": artifact_ledger is not None,
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
        "claims": dict(source_api.CLAIMS),
    }
    try:
        if profile_source is None:
            path = directory / "profile.json"
            checkpoint_api._save_json(root, path, source_api._profile(plan), artifact_ledger)
            profile_source = source_api._source(path, root)
        if runtime_source is None:
            _, runtime_source = source_api._runtime_source(plan, root)
        if world_source is None:
            world_source = replace(
                source_api._source(plan.docker_plan.world_path, root), json_pointer=None
            )
        source_api._require(
            world_source.json_pointer is None, "World SDF must bind exact whole-file original bytes"
        )
        for source in (profile_source, runtime_source, world_source, *model_sources.values()):
            source_api._verify_source(source, root)
        report["sources"] = {
            "profile": profile_source.to_mapping(),
            "runtime": runtime_source.to_mapping(),
            "world": world_source.to_mapping(),
            "models": {k: v.to_mapping() for k, v in model_sources.items()},
        }
        if artifact_ledger is None:
            worker = source_api._SourceOwner(session, sources, deadline, parameter_baseline)
        else:
            source_api._require(
                directory.is_relative_to(artifact_ledger.evidence_directory),
                "Collection originals must remain in their charged evidence scope",
            )
            camera_directory = artifact_ledger.camera_directory / directory.name
            camera_directory.mkdir()
            reservations = _prepare_source_reservations(
                artifact_ledger,
                sources,
                camera_directory,
                plan,
                parameter_baseline,
            )
            report["prior_worker_reserved_bytes"] = {
                kind: sum(r.maximum_bytes for r in reservations.values() if r.category == kind)
                for kind in ("camera", "evidence")
            }
            report["reservation_refunds_permitted"] = False
            worker = _PrecreditedSourceOwner(
                session,
                sources,
                deadline,
                parameter_baseline,
                camera_directory=camera_directory,
                reservations=reservations,
            )
        worker.thread.start()
        while not worker.done.is_set():
            source_api._check_open_current(session, receiver, capture, deadline)
            budget.assert_phase(
                phase, now_s=session.clock.monotonic(), clock_id=session.clock.session_id
            )
            batch = receiver.read_batch(0.01)
            if batch:
                capture.append_batch(batch)
        worker.thread.join(timeout=0.1)
        source_api._require(not worker.thread.is_alive(), "Scoped native/camera exit unobserved")
        if worker.failure is not None:
            raise worker.failure
        if artifact_ledger is not None:
            _commit_worker_reservations(artifact_ledger, reservations)
        report["collection_complete"] = True
        wait_api._wait_for_current_packets(session, receiver, capture, worker, deadline, report)
        report["prefix_export_started_at_s"] = session.clock.monotonic()
        prefix = checkpoint_api.capture_teacher_committed_prefix_v2(
            capture,
            artifact_root=root,
            output_directory=directory / "prefix",
            clock=session.clock,
            navigation_before=worker.rows["navigation-before"],
            artifact_ledger=artifact_ledger,
        )
        report["prefix_export_finished_at_s"] = session.clock.monotonic()
        report["prefix_source"] = prefix.to_mapping()
        now = session.clock.monotonic()
        report["qualification_started_at_s"] = now
        checkpoint = checkpoint_api.qualify_teacher_live_navigation_checkpoint_v2(
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
            artifact_ledger=artifact_ledger,
        )
        _scope(session, receiver, capture, plan, phase)
        source_api._require(
            session.clock.monotonic() < deadline,
            "Collection actual phase deadline exhausted while checking sources",
        )
        report["qualification_finished_at_s"] = session.clock.monotonic()
        report["checkpoint_source"] = checkpoint.checkpoint_source.to_mapping()
        report["checkpoint_qualified"] = True
    except BaseException as error:
        primary, report["error"] = error, source_api._error(error)
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
                if artifact_ledger is not None:
                    try:
                        _commit_worker_reservations(artifact_ledger, reservations)
                    except BaseException as error:
                        if primary is None:
                            raise
                        primary.add_note(
                            f"Worker original ledger commit also failed: {source_api._error(error)}"
                        )
            else:
                report["cleanup_error"] = (
                    "Scoped source worker still live; outer supervisor required"
                )
        report["finished_at_s"] = session.clock.monotonic()
        report["writer_closed_at_return"] = capture._closed
        report["RX_at_return"] = receiver.to_mapping()
        try:
            checkpoint_api._save_json(root, directory / "collection.json", report, artifact_ledger)
            if worker is not None and not report["worker_exited"] and primary is None:
                raise RuntimeError(report["cleanup_error"])
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(
                f"Collection receipt persistence also failed: {source_api._error(error)}"
            )
    # Worker cleanup and collection receipt serialization/fsync/ledger costs
    # are also actual elapsed time, not excluded from the original lease.
    _scope(session, receiver, capture, plan, phase)
    source_api._require(
        session.clock.monotonic() < min(deadline, checkpoint.expires_at_s),
        "Original checkpoint expired during collection receipt persistence",
    )
    return checkpoint


__all__ = ["CONTRACT", "collect_live_checkpoint"]
