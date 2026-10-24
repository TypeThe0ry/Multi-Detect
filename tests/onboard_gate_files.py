"""Single source of truth for the deterministic onboard pytest file set."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
_EXACT = {
    "test_step_triangulation.py",
    "test_onboard_loop_step_triangulation.py",
    "test_onboard_policy_step_triangulation.py",
    "test_llama_server_logs.py",
    "test_near_depth.py",
    "test_fast_identity.py",
    "test_build_flight_identity_eval.py",
    "test_autonomy_planner.py",
    "test_energy_estimator.py",
    "test_ground_model.py",
    "test_onboard_ground_policy.py",
    "test_mission_executor.py",
    "test_alert_closer_look.py",
    "test_small_unjudged_revisit.py",
    "test_look_geometry.py",
    "test_count_small_reject_votes.py",
    "test_count_revisit_priority.py",
    "test_revisit_trace.py",
    "test_mission_executor_remaining_gaps.py",
    "test_mission_executor_no_fly.py",
    "test_mission_package_preflight.py",
    "test_mission_security.py",
    "test_onboard_preflight.py",
    "test_onboard_main.py",
    "test_onboard_main_operator_link.py",
    "test_onboard_real_mode_harness.py",
    "test_onboard_real_process.py",
    "test_onboard_health.py",
    "test_onboard_model_split.py",
    "test_onboard_operator_model_split.py",
    "test_safety_envelope.py",
    "test_mission_planner.py",
    "test_mission_plan.py",
    "test_mission_planning.py",
    "test_mission_loop_timing.py",
    "test_mission_energy_window_audit.py",
    "test_mission_suite.py",
    "test_mission_summary.py",
    "test_mission_supervisor.py",
    "test_plan_from_task.py",
    "test_plan_intent.py",
    "test_mover_verification_smoke.py",
    "test_place_memory_integration.py",
    "test_place_verdict_memory.py",
    "test_supervisor_evidence.py",
    "test_score_onboard_loop.py",
    "test_e2e_operator_acceptance.py",
    "test_sim_world_gen.py",
    "test_fixed_wing_scenario_derivation.py",
    "test_airframe_camera_mount_pitch.py",
    "test_fw_search.py",
    "test_sim_teacher_policies.py",
    "test_fw_geometry.py",
    "test_motion_intent.py",
    "test_replay.py",
    "test_replay_ground_projection.py",
    "test_replay_onboard_visual_clips.py",
    "test_replay_place_verdict_memory.py",
}
# Need local model/asset caches that are absent from fresh worktrees; not hermetic.
_EXCLUDE = {"test_onboard_policy_r37_evidence.py", "test_sim_world_confusers.py"}
_PREFIXES = (
    "test_build_plan_sft",
    "test_build_slow_system_sft",
    "test_slow_system_",
    "test_onboard_policy",
    "test_policy_v2_",
    "test_onboard_runtime",
    "test_onboard_loop",
    "test_replay_",
)


def files() -> list[Path]:
    return sorted(
        {
            p
            for p in ROOT.glob("test_*.py")
            if p.name not in _EXCLUDE and (p.name in _EXACT or p.name.startswith(_PREFIXES))
        }
    )


def validate() -> list[Path]:
    paths = files()
    missing = sorted(_EXACT - {p.name for p in paths})
    if missing:
        raise FileNotFoundError("onboard gate files missing: " + ", ".join(missing))
    return paths


if __name__ == "__main__":
    print("\n".join(str(p) for p in validate()))
