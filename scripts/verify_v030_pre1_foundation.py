"""Verify the v0.3.0-pre.1 runtime foundation on the current checkout.

This is a source-bound software acceptance helper.  It exercises the same
runtime contracts that the later SIM/HIL/REAL adapters consume, but it never
opens a vehicle transport, sends a MAVLink command, or grants actuator
authority.  The receipt deliberately records the current prerelease label and
the historical foundation baseline separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "src", _ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy import AutonomyContractError  # noqa: E402
from multidetect.autonomy.policy_artifact import PolicyArtifact  # noqa: E402
from multidetect.autonomy.policy_contract import PolicyAction, SharedTaskPolicy  # noqa: E402
from multidetect.autonomy.serialization import canonical_json, canonical_sha256  # noqa: E402
from multidetect.autonomy.task_ranker import TaskRanker  # noqa: E402
from multidetect.backends import ReplayBackend  # noqa: E402
from multidetect.clock import Clock, ManualClock  # noqa: E402
from multidetect.domain import FrameObservation, VehicleTelemetry  # noqa: E402
from multidetect.replay import ReplayRecording  # noqa: E402
from multidetect.runtime import (  # noqa: E402
    AutonomyRuntime,
    RuntimeBackend,
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeEndpointClass,
    RuntimeMode,
    RuntimeProfile,
    RuntimeProfileId,
)
from multidetect.simulation import load_runtime_scenario  # noqa: E402
from multidetect.simulation.index import load_scenario_index  # noqa: E402
from multidetect.simulator import (  # noqa: E402
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
)
from scripts.train_task_ranker import load_dataset  # noqa: E402

FOUNDATION_BASELINE = "0.3.0-pre.1"
CURRENT_SCHEMA_VERSION = 1
DEFAULT_SCENARIO = _ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
DEFAULT_DATASET = _ROOT / "configs/training/task-ranker-budget-v1.json"
DEFAULT_PARITY = _ROOT / "configs/release-parity.json"

_SOURCE_FILES = (
    "scripts/verify_v030_pre1_foundation.py",
    "src/multidetect/clock.py",
    "src/multidetect/runtime.py",
    "src/multidetect/backends/base.py",
    "src/multidetect/backends/replay.py",
    "src/multidetect/simulation/index.py",
    "src/multidetect/simulation/runtime_loader.py",
    "src/multidetect/simulation/runtime_input.py",
    "src/multidetect/autonomy/contracts.py",
    "src/multidetect/autonomy/policy_contract.py",
    "src/multidetect/simulator/scenario_runner.py",
    "configs/schemas/runtime-descriptor.schema.json",
    "configs/schemas/runtime-profile.schema.json",
    "configs/schemas/runtime-run-report.schema.json",
    "configs/schemas/scenario-manifest.schema.json",
    "configs/schemas/scenario-runtime-input.schema.json",
    "ground-control/MultiDetectGCS/custom/src/FlyViewCustomLayer.qml",
    "ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.cc",
)


@dataclass(slots=True)
class _ObservationProcessor:
    calls: list[tuple[str, float]] = field(default_factory=list)

    def process_observation(self, frame: FrameObservation, *, now_s: float) -> str:
        self.calls.append((frame.frame_id, now_s))
        return f"processed:{frame.frame_id}"


def _frame(frame_id: str, captured_at_s: float) -> FrameObservation:
    return FrameObservation(
        frame_id=frame_id,
        captured_at_s=captured_at_s,
        detections=(),
        telemetry=VehicleTelemetry(
            altitude_agl_m=50.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=15.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=True,
            release_zone_clear=True,
        ),
    )


def _hash_file(root: Path, relative: str) -> dict[str, object]:
    path = (root / relative).resolve(strict=True)
    payload = path.read_bytes()
    return {
        "path": relative.replace("\\", "/"),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _check_runtime_modes() -> dict[str, object]:
    values = tuple(item.value for item in RuntimeMode)
    expected = ("replay", "sim", "hil", "real")
    return {"passed": values == expected, "values": list(values), "expected": list(expected)}


def _check_clock() -> dict[str, object]:
    clock = ManualClock(initial_monotonic_s=10.0)
    before = clock.monotonic()
    after = clock.advance(1.25)
    return {
        "passed": isinstance(clock, Clock)
        and before == 10.0
        and after == 11.25
        and clock.monotonic() == 11.25,
        "initial_monotonic_s": before,
        "advanced_monotonic_s": after,
        "utc_is_aware": clock.utc_now().tzinfo is not None,
        "rewind_rejected": _rewind_rejected(clock),
    }


def _rewind_rejected(clock: ManualClock) -> bool:
    try:
        clock.advance_to(clock.monotonic() - 0.01)
    except ValueError:
        return True
    return False


def _check_profiles() -> dict[str, object]:
    sim = RuntimeProfile.sim()
    replay = RuntimeProfile.replay()
    exact = sim.capabilities == (RuntimeCapability.OBSERVE, RuntimeCapability.SIMULATION_TRANSPORT)
    endpoint = sim.endpoint_class is RuntimeEndpointClass.SIMULATOR
    mismatch_rejected = False
    try:
        RuntimeDescriptor(
            mode=RuntimeMode.REAL,
            profile=sim,
            backend_id="pre1-foundation-invalid",
            airframe_profile_hash="a" * 64,
        )
    except AutonomyContractError:
        mismatch_rejected = True
    return {
        "passed": (
            exact
            and endpoint
            and replay.profile_id is RuntimeProfileId.REPLAY
            and mismatch_rejected
        ),
        "sim_capabilities": [item.value for item in sim.capabilities],
        "sim_endpoint_class": sim.endpoint_class.value,
        "replay_profile_id": replay.profile_id.value,
        "mode_mismatch_rejected": mismatch_rejected,
        "flight_control_in_sim_profile": RuntimeCapability.FLIGHT_CONTROL_OUTPUT.value
        in {item.value for item in sim.capabilities},
    }


def _run_replay_once() -> tuple[str, str, tuple[str, ...]]:
    recording = ReplayRecording(
        "v030-pre1-foundation-replay",
        (_frame("foundation-0", 0.0), _frame("foundation-1", 1.0)),
        "a" * 64,
    )
    backend = ReplayBackend(recording)
    processor = _ObservationProcessor()
    results, report = AutonomyRuntime(backend=backend, processor=processor).run_with_report()
    return report.canonical_hash, report.input_hash, tuple(item.frame_id for item in results)


def _check_runtime_backend() -> dict[str, object]:
    backend = ReplayBackend(
        ReplayRecording("v030-pre1-foundation-protocol", (_frame("f", 0.0),), "a" * 64)
    )
    return {
        "passed": isinstance(backend, RuntimeBackend)
        and backend.descriptor.mode is RuntimeMode.REPLAY
        and isinstance(backend.clock, ManualClock),
        "protocol_is_runtime_backend": isinstance(backend, RuntimeBackend),
        "descriptor_mode": RuntimeMode.REPLAY.value,
        "input_hash": backend.input_hash,
        "clock_type": type(backend.clock).__name__,
    }


def _check_deterministic_replay() -> dict[str, object]:
    first = _run_replay_once()
    second = _run_replay_once()
    return {
        "passed": first == second and first[2] == ("foundation-0", "foundation-1"),
        "first_report_hash": first[0],
        "second_report_hash": second[0],
        "processed_frame_ids": list(first[2]),
        "same_report_hash_on_repeat": first[0] == second[0],
        "same_input_hash_on_repeat": first[1] == second[1],
    }


def _check_scenario(root: Path, scenario_path: Path) -> dict[str, object]:
    source, index = load_scenario_index(scenario_path)
    runtime_input = load_runtime_scenario(scenario_path, mode=RuntimeMode.SIM)
    mapping = runtime_input.to_mapping()
    forbidden = {"truth", "expectations", "targets", "ground_truth"}
    no_truth_channel = forbidden.isdisjoint(mapping)
    return {
        "passed": runtime_input.scenario_manifest_hash == index.canonical_hash and no_truth_channel,
        "scenario_id": runtime_input.scenario_id,
        "scenario_version": runtime_input.scenario_version,
        "scenario_manifest_sha256": index.canonical_hash,
        "runtime_input_sha256": runtime_input.canonical_hash,
        "runtime_channel_path": index.runtime_input.path,
        "manifest_path": str(source.relative_to(root)).replace("\\", "/"),
        "truth_free_runtime_mapping": no_truth_channel,
        "fault_count": len(runtime_input.faults),
    }


def _check_task_contract(root: Path, dataset_path: Path) -> dict[str, object]:
    groups, _, dataset_identity = load_dataset(dataset_path)
    case = groups["train"][0]
    ranker = TaskRanker((0.0, 0.0, 0.0), "v030-pre1-foundation-ranker")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="v030-pre1-foundation-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=case.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    decision, evaluated = SharedTaskPolicy(ranker, artifact).decide(
        case.candidates,
        case.context,
        case.vehicle_class,
    )
    passed = (
        decision.action is PolicyAction.SELECT
        and decision.planner_revalidated
        and decision.truth_free
        and evaluated is not None
    )
    return {
        "passed": passed,
        "dataset_path": str(dataset_path.relative_to(root)).replace("\\", "/"),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "dataset_provenance": dataset_identity.get("provenance"),
        "vehicle_class": case.vehicle_class,
        "decision": decision.to_mapping(),
        "selected_plan_sha256": evaluated.plan.canonical_hash if evaluated is not None else None,
        "policy_applied_to_actuator": False,
    }


def _check_simulator(root: Path, scenario_path: Path) -> dict[str, object]:
    config = SimulatorScenarioRunnerConfig(
        frames=2,
        enable_ai=False,
        arm_on_start=False,
        use_world_origin=False,
    )
    first = SimulatorScenarioRunner(scenario_path, config=config).run()
    second = SimulatorScenarioRunner(scenario_path, config=config).run()
    return {
        "passed": (
            first.completed_frames == 2
            and second.completed_frames == 2
            and first.evidence_hash == second.evidence_hash
        ),
        "scenario_path": str(scenario_path.relative_to(root)).replace("\\", "/"),
        "completed_frames": first.completed_frames,
        "evidence_sha256": first.evidence_hash,
        "repeat_evidence_sha256": second.evidence_hash,
        "same_evidence_hash_on_repeat": first.evidence_hash == second.evidence_hash,
        "actuator_authority": False,
    }


def build_evidence(
    *,
    root: Path = _ROOT,
    scenario_path: Path = DEFAULT_SCENARIO,
    dataset_path: Path = DEFAULT_DATASET,
    parity_path: Path = DEFAULT_PARITY,
) -> dict[str, object]:
    root = root.resolve()
    scenario_path = scenario_path.resolve()
    dataset_path = dataset_path.resolve()
    parity_path = parity_path.resolve()
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    checks = {
        "runtime_mode": _check_runtime_modes(),
        "clock": _check_clock(),
        "runtime_profile_isolation": _check_profiles(),
        "runtime_backend": _check_runtime_backend(),
        "scenario_manifest_runtime_channel": _check_scenario(root, scenario_path),
        "core_task_contract": _check_task_contract(root, dataset_path),
        "deterministic_replay": _check_deterministic_replay(),
        "deterministic_simulator": _check_simulator(root, scenario_path),
    }
    passed = all(bool(value.get("passed")) for value in checks.values())
    source_hashes = [_hash_file(root, relative) for relative in _SOURCE_FILES]
    evidence: dict[str, object] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "event": "v030_pre1_foundation_acceptance",
        "foundation_baseline": FOUNDATION_BASELINE,
        "current_release_label": parity.get("release_label"),
        "scope": "current_source_revalidation_of_pre1_foundation",
        "source_hashes": source_hashes,
        "parity_file": {
            "path": str(parity_path.relative_to(root)).replace("\\", "/"),
            "bytes": parity_path.stat().st_size,
            "sha256": hashlib.sha256(parity_path.read_bytes()).hexdigest(),
            "release_label": parity.get("release_label"),
        },
        "checks": checks,
        "gate_passed": passed,
        "truth_free": True,
        "policy_applied_to_actuator": False,
        "flight_control_enabled": False,
        "hardware_acceptance": "not_claimed",
        "hil_real_acceptance": "not_claimed",
        "outdoor_gnss_acceptance": "not_claimed",
        "final_release_allowed": False,
        "candidate_promoted": False,
        "claims_boundary": (
            "software contract and deterministic Replay/Simulator foundation only; "
            "not HIL, REAL, hardware, outdoor GNSS, visual acceptance or final release"
        ),
    }
    evidence["sha256"] = canonical_sha256(evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_ROOT)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--release-parity", type=Path, default=DEFAULT_PARITY)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = args.output.resolve()
    if output.exists():
        parser.error(f"output must be new: {output}")
    evidence = build_evidence(
        root=root,
        scenario_path=args.scenario,
        dataset_path=args.dataset,
        parity_path=args.release_parity,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json(evidence) + "\n", encoding="utf-8", newline="\n")
    print(
        canonical_json(
            {
                "path": str(output),
                "sha256": evidence["sha256"],
                "gate_passed": evidence["gate_passed"],
            }
        )
    )
    return 0 if evidence["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
