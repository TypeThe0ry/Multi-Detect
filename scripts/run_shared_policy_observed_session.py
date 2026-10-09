"""Generate a deterministic truth-free observed multi-task policy-session record.

This is a software evidence harness for the same observed-session API used by
runtime integrations.  It does not create a backend, open MAVLink, or grant
flight authority.  A fixed clock and a bounded monitor callback provide the
minimum deterministic completion signals needed to exercise re-planning after
each completed task.
"""

from __future__ import annotations

# ruff: noqa: E402 -- repository-local CLI bootstrap before package imports
import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "src", _ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.contracts import ContextSnapshot, GeoPoint
from multidetect.autonomy.policy_artifact import load_policy_artifact
from multidetect.autonomy.policy_contract import PolicyAction, SharedTaskPolicy
from multidetect.autonomy.task_session import ObservedTaskSession, run_observed_task_session
from multidetect.clock import ManualClock
from multidetect.mission_planning import compile_planned_task_mission_from_runtime
from scripts.vtol_selected_task_campaign import load_campaign_definition

DEFAULT_CAMPAIGN = _ROOT / "configs" / "scenarios" / "vtol-ranked-routes.synthetic-campaign.json"
DEFAULT_ARTIFACT = (
    _ROOT
    / "artifacts"
    / "evaluation"
    / "policy-artifacts-refresh-20260920-campaign-ranker"
    / "candidate-policy-artifact.json"
)
DEFAULT_OUTPUT = _ROOT / "artifacts" / "evaluation" / "shared-policy-observed-session-20260924.json"
DEFAULT_PARITY = _ROOT / "configs" / "release-parity.json"
AIRFRAME_PROFILE_SHA256 = "deff802efea71ae996f5fb0b2aae0ce6997ed9443d6255b935f313120c1c1e7f"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _release_parity(path: Path = DEFAULT_PARITY) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    parity = document.get("parity")
    if not isinstance(parity, dict):
        raise ValueError("release parity document is missing parity mapping")
    expected = {
        "core_algorithm_hash",
        "px4_mission_path_hash",
        "safety_policy_hash",
        "task_contract_hash",
        "ui_contract_hash",
    }
    if set(parity) != expected or not all(
        isinstance(value, str) and len(value) == 64 for value in parity.values()
    ):
        raise ValueError("release parity mapping is malformed")
    return {
        "path": str(path.relative_to(_ROOT)).replace("\\", "/"),
        "bytes": path.stat().st_size,
        "file_sha256": _file_sha256(path),
        "hashes": dict(parity),
    }


def _context(home: GeoPoint) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="observed-session-context-001",
        captured_at_s=10.0,
        captured_at_utc="2026-09-24T00:00:00Z",
        aircraft_position=home,
        home_position=home,
        airframe_profile_id="px4_vtol_gazebo_v1",
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _complete_monitor(compilation: Any, monitor: Any, clock: ManualClock) -> None:
    clock.advance(2.0)
    terminal = len(compilation.compiled_mission.executable_mission.items) - 1
    for message_type in ("MISSION_CURRENT", "MISSION_ITEM_REACHED"):
        monitor.ingest_message(
            {
                "message_type": message_type,
                "seq": terminal,
                "source_system_id": 1,
                "source_component_id": 1,
            }
        )
    monitor.ingest_message(
        {
            "message_type": "HEARTBEAT",
            "base_mode": 0,
            "source_system_id": 1,
            "source_component_id": 1,
        }
    )
    monitor.ingest_message(
        {
            "message_type": "EXTENDED_SYS_STATE",
            "landed_state": 1,
            "source_system_id": 1,
            "source_component_id": 1,
        }
    )


def _verify_session_policy(
    session: ObservedTaskSession,
    *,
    candidates: list[Any],
    runtimes: dict[str, object],
    shared_policy: SharedTaskPolicy,
    initial_context: ContextSnapshot,
) -> bool:
    """Recompute every selection and compare it with the saved Mission plan."""

    pending = list(candidates)
    current = initial_context
    for step in session.steps:
        decision, selected = shared_policy.decide(pending, current, "vtol")
        if (
            decision.action is not PolicyAction.SELECT
            or decision.planner_revalidated is not True
            or decision.truth_free is not True
            or selected is None
            or decision.task_id != selected.candidate.task.task_id
            or selected.candidate.task.task_id != step.compilation.estimate.task_id
        ):
            return False
        task_id = selected.candidate.task.task_id
        expected = compile_planned_task_mission_from_runtime(
            selected.candidate.task,
            current,
            "vtol",
            runtime=runtimes[task_id],
            airframe_profile_hash=shared_policy.artifact.airframe_profile_hash,
        )
        if expected.plan != selected.plan or expected.plan != step.compilation.plan:
            return False
        if step.after is None:
            return False
        pending = [item for item in pending if item.task.task_id != task_id]
        current = step.after
    return not pending and session.stop_reason == "all_tasks_completed"


def _session_mapping(
    session: ObservedTaskSession,
    *,
    planner_revalidated: bool,
) -> dict[str, object]:
    return {
        "stop_reason": session.stop_reason,
        "remaining_task_ids": list(session.remaining_task_ids),
        "selected_task_ids": [step.compilation.estimate.task_id for step in session.steps],
        "step_count": len(session.steps),
        "planner_revalidated": planner_revalidated,
        "steps": [
            {
                "task_id": step.compilation.estimate.task_id,
                "before_context_sha256": step.compilation.plan.context_snapshot_hash,
                "after_context_sha256": (
                    step.after.canonical_hash if step.after is not None else None
                ),
                "plan_sha256": step.compilation.plan.canonical_hash,
                "executable_mission_sha256": step.compilation.compiled_mission.wire_hash,
                "wire_hash": step.compilation.wire_hash,
                "ranker_sha256": step.ranker_sha256,
                "policy_artifact_sha256": step.policy_artifact_sha256,
                "planner_revalidated": planner_revalidated,
                "outcome": {
                    "mission_hash": step.outcome.mission_hash,
                    "status": step.outcome.status,
                    "reason": step.outcome.reason,
                    "elapsed_s": step.outcome.elapsed_s,
                },
            }
            for step in session.steps
        ],
        "final_context_sha256": session.context.canonical_hash,
    }


def run_evidence(
    campaign_path: Path = DEFAULT_CAMPAIGN,
    artifact_path: Path = DEFAULT_ARTIFACT,
    parity_path: Path = DEFAULT_PARITY,
) -> dict[str, object]:
    candidates, runtimes, ranker = load_campaign_definition(campaign_path)
    if ranker is None:
        raise ValueError("campaign must contain an explicit ranker")
    file_sha256 = _file_sha256(artifact_path)
    artifact = load_policy_artifact(artifact_path, expected_sha256=file_sha256, ranker=ranker)
    if artifact.airframe_profile_hash != AIRFRAME_PROFILE_SHA256:
        raise ValueError("policy artifact airframe identity mismatch")
    shared_policy = SharedTaskPolicy(ranker, artifact)
    release_parity = _release_parity(parity_path.resolve(strict=True))
    home = GeoPoint.from_mapping(runtimes[candidates[0].task.task_id]["home_position"])

    def execute_once() -> ObservedTaskSession:
        context = _context(home)
        clock = ManualClock(
            context.captured_at_s,
            datetime.fromisoformat(context.captured_at_utc.replace("Z", "+00:00")),
        )

        def observe(compilation: Any, monitor: Any) -> ContextSnapshot:
            _complete_monitor(compilation, monitor, clock)
            return replace(
                context,
                snapshot_id=f"after-{compilation.estimate.task_id}",
                captured_at_s=clock.monotonic(),
                captured_at_utc=clock.utc_now().isoformat(),
            )

        return run_observed_task_session(
            candidates,
            context,
            "vtol",
            runtimes=runtimes,
            airframe_profile_hash=AIRFRAME_PROFILE_SHA256,
            clock=clock,
            observe_task=observe,
            expected_system_id=1,
            expected_component_id=1,
            shared_policy=shared_policy,
        )

    first = execute_once()
    second = execute_once()
    first_policy_revalidated = _verify_session_policy(
        first,
        candidates=candidates,
        runtimes=runtimes,
        shared_policy=shared_policy,
        initial_context=_context(home),
    )
    second_policy_revalidated = _verify_session_policy(
        second,
        candidates=candidates,
        runtimes=runtimes,
        shared_policy=shared_policy,
        initial_context=_context(home),
    )
    first_mapping = _session_mapping(
        first,
        planner_revalidated=first_policy_revalidated,
    )
    second_mapping = _session_mapping(
        second,
        planner_revalidated=second_policy_revalidated,
    )
    first_hash = _canonical_sha256(first_mapping)
    second_hash = _canonical_sha256(second_mapping)
    return {
        "schema_version": 1,
        "event": "shared_policy_observed_session_finished",
        "campaign": str(campaign_path.relative_to(_ROOT)).replace("\\", "/"),
        "campaign_sha256": _file_sha256(campaign_path),
        "policy_artifact": str(artifact_path.relative_to(_ROOT)).replace("\\", "/"),
        "policy_artifact_file_sha256": file_sha256,
        "policy_artifact_canonical_sha256": shared_policy.artifact_sha256,
        "ranker_sha256": ranker.canonical_hash,
        "airframe_profile_sha256": AIRFRAME_PROFILE_SHA256,
        "release_parity": release_parity,
        "session": first_mapping,
        "repeat_session_sha256": second_hash,
        "session_sha256": first_hash,
        "deterministic_repeat_passed": first_hash == second_hash,
        "all_tasks_completed": first.stop_reason == "all_tasks_completed",
        "planner_revalidated": first_policy_revalidated and second_policy_revalidated,
        "truth_free": True,
        "backend_started": False,
        "mavlink_sent": False,
        "actuator_capability_granted": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--policy-artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = run_evidence(args.campaign.resolve(), args.policy_artifact.resolve())
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = args.out.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["all_tasks_completed"] and result["deterministic_repeat_passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
