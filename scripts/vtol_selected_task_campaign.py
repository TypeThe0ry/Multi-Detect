"""Sequential owned-SIM missions using the existing shared selector/transaction.

The caller owns the exact container and telemetry session. This module does
not create a container, grant new authority, or reset energy between sorties.
"""

from __future__ import annotations

import hashlib
import json

# ruff: noqa: E402 -- repository-local CLI bootstrap before package imports
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
for _import_path in (_ROOT, _ROOT / "src", _ROOT / "scripts"):
    if str(_import_path) not in sys.path:
        sys.path.insert(0, str(_import_path))

_RELEASE_PARITY_KEYS = (
    "core_algorithm_hash",
    "px4_mission_path_hash",
    "safety_policy_hash",
    "task_contract_hash",
    "ui_contract_hash",
)


def _capture_release_parity(path: str | Path) -> dict[str, object]:
    """Capture and validate release parity at one campaign boundary."""
    resolved = Path(path).expanduser().resolve(strict=True)
    raw = resolved.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VtolMissionAcceptanceError("release parity document is invalid JSON") from exc
    if not isinstance(document, dict):
        raise VtolMissionAcceptanceError("release parity document must be a JSON object")
    hashes = document.get("parity")
    if not isinstance(hashes, dict) or set(hashes) != set(_RELEASE_PARITY_KEYS):
        raise VtolMissionAcceptanceError("release parity document has an incomplete hash set")
    normalized: dict[str, str] = {}
    for key in _RELEASE_PARITY_KEYS:
        value = hashes.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise VtolMissionAcceptanceError(f"release parity {key} must be a SHA-256 string")
        try:
            int(value, 16)
        except ValueError as exc:
            raise VtolMissionAcceptanceError(
                f"release parity {key} must be a SHA-256 string"
            ) from exc
        normalized[key] = value.lower()
    release_label = document.get("release_label")
    if not isinstance(release_label, str) or not release_label:
        raise VtolMissionAcceptanceError("release parity release_label is missing")
    return {
        "path": str(resolved),
        "bytes": len(raw),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "release_label": release_label,
        "hashes": normalized,
    }

from multidetect.autonomy import (
    OnboardAIExecutor,
    OnboardResponse,
    PromptEnvelope,
    PromptTaskResolver,
    PromptTaskTemplate,
    ResolvedTaskSpec,
)
from multidetect.autonomy.mission_campaign import select_and_compile_task
from multidetect.autonomy.planner import normalize_vehicle_class
from multidetect.autonomy.policy_artifact import load_policy_artifact
from multidetect.autonomy.policy_contract import (
    PolicyAction,
    PolicyDecision,
    SharedTaskPolicy,
    policy_decision_matches_selected_task,
)
from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads
from multidetect.autonomy.task_ranker import TaskRanker
from multidetect.autonomy.task_selection import TaskCandidate, select_next_task
from multidetect.mission_planning import parse_mission_compile_runtime
from multidetect.sensor_transport_binding import (
    SensorTransportManifest,
    load_sensor_transport_manifest,
)
from multidetect.simulation.index import load_scenario_index
from multidetect.simulator.flight_model import SimulationEnergyProfile
from scripts.vtol_sitl_mission_acceptance import (
    MissionTelemetrySession,
    SelectedMissionInputs,
    TaskSelectionRejected,
    VtolMissionAcceptanceError,
    _atomic_write_json,
    _cli_host_clock,
    _mission_cli_options,
    _parser,
    run_mission_acceptance,
)


def load_campaign(
    path: Path, *, vehicle_class: str = "vtol"
) -> tuple[list[TaskCandidate], dict[str, object]]:
    candidates, runtimes, _ = load_campaign_definition(path, vehicle_class=vehicle_class)
    return candidates, runtimes


def load_campaign_definition(path: Path, *, vehicle_class: str = "vtol"):
    """Load explicit task/runtime inputs for one vehicle class.

    The historical VTOL caller keeps the default.  Other adapters can use the
    same strict task/runtime document without silently parsing geometry through
    the VTOL compiler.
    """
    normalized_vehicle = normalize_vehicle_class(vehicle_class)
    raw = strict_json_loads(path.read_text(encoding="utf-8"))
    if (not isinstance(raw, dict) or not {"schema_version", "tasks"} <= set(raw)
            or set(raw) - {"schema_version", "tasks", "ranker", "prompt"}):
        raise ValueError("campaign requires schema_version and tasks")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported campaign schema")
    if not isinstance(raw["tasks"], list) or not 1 <= len(raw["tasks"]) <= 32:
        raise ValueError("campaign requires 1..32 tasks")
    candidates, runtimes = [], {}
    for item in raw["tasks"]:
        if not isinstance(item, dict) or set(item) != {"task", "priority", "runtime"}:
            raise ValueError("each campaign entry requires task, priority and runtime")
        task = ResolvedTaskSpec.from_mapping(item["task"])
        candidate = TaskCandidate(task, item["priority"])
        if task.task_id in runtimes:
            raise ValueError("duplicate campaign task ID")
        parse_mission_compile_runtime(item["runtime"], normalized_vehicle)
        candidates.append(candidate)
        runtimes[task.task_id] = item["runtime"]
    ranker = TaskRanker.from_mapping(raw["ranker"]) if "ranker" in raw else None
    return candidates, runtimes, ranker


def load_campaign_prompt(path: Path) -> PromptEnvelope | None:
    """Load an optional operator Prompt while keeping task loading compatible."""

    raw = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "prompt" not in raw:
        return None
    return PromptEnvelope.from_mapping(raw["prompt"])


def _build_campaign_prompt_resolver(
    prompt: PromptEnvelope,
    candidates: Sequence[TaskCandidate],
    runtimes: Mapping[str, object],
    *,
    require_registered_prompt: bool = False,
) -> PromptTaskResolver:
    """Bind a synthetic campaign Prompt to its explicit task manifest.

    This is a fixture catalog, not natural-language interpretation or an
    independent authorization service. Flight authority remains in the
    existing owned-SITL transaction/permit path.
    """

    if not isinstance(prompt, PromptEnvelope):
        raise TypeError("prompt must be a PromptEnvelope")
    if require_registered_prompt and prompt.prompt_id != "prompt-vtol-shared-policy-east-west-v1":
        raise VtolMissionAcceptanceError(
            "Prompt is not registered in the VTOL campaign catalog"
        )
    if require_registered_prompt and prompt.aircraft_id != "px4_vtol_gazebo_v1":
        raise VtolMissionAcceptanceError(
            "Prompt aircraft is not registered in the VTOL campaign catalog"
        )
    return PromptTaskResolver(
        resolver_id="vtol-campaign-prompt-catalog-v1",
        templates={
            prompt.prompt_id: PromptTaskTemplate(
                tuple(candidates),
                {item.task.task_id: runtimes[item.task.task_id] for item in candidates},
                aircraft_ids=(prompt.aircraft_id,),
            )
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    parser.add_argument("--campaign-file", type=Path, required=True)
    parser.add_argument("--campaign-output-directory", type=Path, required=True)
    parser.add_argument("--policy-artifact", type=Path)
    parser.add_argument("--policy-artifact-file-sha256")
    parser.add_argument("--release-parity", type=Path)
    parser.add_argument("--sensor-transport-manifest", type=Path)
    parser.add_argument("--sensor-frame-map", type=Path)
    args = parser.parse_args(argv)
    if (args.policy_artifact is None) != (args.policy_artifact_file_sha256 is None):
        parser.error("--policy-artifact and --policy-artifact-file-sha256 must be paired")
    if (args.sensor_transport_manifest is None) != (args.sensor_frame_map is None):
        parser.error("--sensor-transport-manifest and --sensor-frame-map must be paired")
    clock = _cli_host_clock(args, parser)
    if not args.acknowledge_owned_disposable_sitl:
        parser.error("owned disposable SITL acknowledgement required")
    if args.mission_journal is not None or args.mission_active_ready_file is not None:
        parser.error("campaign owns per-sortie journals and active-ready paths")
    if (args.flight_capture_ready_file is None) != (
        args.flight_capture_complete_file is None
    ):
        parser.error("flight capture ready and complete files must be paired")
    if args.flight_capture_binding_file is not None and args.flight_capture_ready_file is None:
        parser.error("flight capture binding file requires paired ready and complete files")
    values = [
        args.energy_capacity_wh,
        args.energy_avionics_power_w,
        args.energy_propulsion_max_power_w,
        args.energy_initial_remaining_wh,
        args.energy_provenance,
    ]
    if any(value is None for value in values):
        parser.error("campaign CLI requires all five explicit energy options")
    candidates, runtimes, ranker = load_campaign_definition(args.campaign_file)
    prompt = None
    if args.campaign_file.exists():
        try:
            prompt = load_campaign_prompt(args.campaign_file)
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
    sensor_transport = None
    sensor_frame_ids = None
    if args.sensor_transport_manifest is not None:
        try:
            sensor_transport = load_sensor_transport_manifest(args.sensor_transport_manifest)
            sensor_frame_ids = _load_sensor_frame_map(args.sensor_frame_map)
            _validate_sensor_frame_map(sensor_transport, candidates, sensor_frame_ids)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
    shared_policy = None
    if args.policy_artifact is not None:
        if ranker is None:
            parser.error("policy artifact requires an explicit campaign ranker")
        try:
            artifact = load_policy_artifact(
                args.policy_artifact,
                expected_sha256=args.policy_artifact_file_sha256,
                ranker=ranker,
            )
            if artifact.airframe_profile_hash != args.airframe_profile_sha256:
                raise ValueError("shared policy airframe profile mismatch")
            shared_policy = SharedTaskPolicy(ranker, artifact)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
    profile = SimulationEnergyProfile(values[0], values[1], values[2], values[4])
    if args.evidence.exists() or args.campaign_output_directory.exists():
        parser.error("campaign evidence and output directory must be new")
    options = _mission_cli_options(args)
    flight_capture_deadline_s = options.get("flight_capture_deadline_s")
    for name in (
        "evidence_path",
        "mission_journal_path",
        "mission_active_ready_file",
        "flight_capture_ready_file",
        "flight_capture_complete_file",
        "flight_capture_binding_file",
        "flight_capture_deadline_s",
    ):
        options.pop(name)
    with MissionTelemetrySession(
        container_id=args.expected_container_id,
        evaluator_port=args.evaluator_port,
        energy_profile=profile,
        initial_remaining_wh=args.energy_initial_remaining_wh,
        **({"clock": clock} if clock is not None else {}),
    ) as session:
        result = run_selected_task_campaign(
            session=session,
            candidates=candidates,
            runtimes=runtimes,
            output_directory=args.campaign_output_directory,
            mission_options=options,
            ranker=ranker if shared_policy is None else None,
            shared_policy=shared_policy,
            sensor_transport_manifest=sensor_transport,
            sensor_frame_ids=sensor_frame_ids,
            release_parity_path=args.release_parity,
            flight_capture_ready_file=args.flight_capture_ready_file,
            flight_capture_complete_file=args.flight_capture_complete_file,
            flight_capture_binding_file=args.flight_capture_binding_file,
            flight_capture_deadline_s=flight_capture_deadline_s,
            prompt=prompt,
            require_registered_prompt=prompt is not None,
        )
        if args.policy_artifact is not None:
            result["policy_artifact_input"] = {
                "path": str(args.policy_artifact.resolve()),
                "file_sha256": args.policy_artifact_file_sha256.lower(),
                "canonical_sha256": shared_policy.artifact_sha256,
            }
        _atomic_write_json(args.evidence.resolve(), result, overwrite=False)
    return 0


def run_selected_task_campaign(
    *,
    session: MissionTelemetrySession,
    candidates: Sequence[TaskCandidate],
    runtimes: Mapping[str, object],
    output_directory: Path,
    mission_options: Mapping[str, Any],
    ranker: TaskRanker | None = None,
    shared_policy: SharedTaskPolicy | None = None,
    prompt: PromptEnvelope | None = None,
    require_registered_prompt: bool = False,
    sensor_transport_manifest: SensorTransportManifest | None = None,
    sensor_frame_ids: Mapping[str, str] | None = None,
    release_parity_path: str | Path | None = None,
    flight_capture_ready_file: Path | None = None,
    flight_capture_complete_file: Path | None = None,
    flight_capture_binding_file: Path | None = None,
    flight_capture_deadline_s: float | None = None,
) -> dict[str, Any]:
    """Select from fresh projected context inside each existing mission run.

    Every iteration re-enters the original ownership, freshness, upload,
    readback, commit and permit path. Failure propagates and no later candidate
    is dispatched. Per-sortie reports/journals stay separate and are retained.
    """
    if ranker is not None and not isinstance(ranker, TaskRanker):
        raise VtolMissionAcceptanceError("TaskRanker required")
    if shared_policy is not None and not isinstance(shared_policy, SharedTaskPolicy):
        raise VtolMissionAcceptanceError("SharedTaskPolicy required")
    if shared_policy is not None and ranker is not None:
        raise VtolMissionAcceptanceError("shared_policy and ranker are mutually exclusive")
    if prompt is not None and not isinstance(prompt, PromptEnvelope):
        raise VtolMissionAcceptanceError("prompt must be a PromptEnvelope")
    if (sensor_transport_manifest is None) != (sensor_frame_ids is None):
        raise VtolMissionAcceptanceError(
            "sensor transport manifest and frame map must be provided together"
        )
    if (flight_capture_ready_file is None) != (flight_capture_complete_file is None):
        raise VtolMissionAcceptanceError(
            "flight capture ready and complete files must be provided together"
        )
    if flight_capture_binding_file is not None and flight_capture_ready_file is None:
        raise VtolMissionAcceptanceError(
            "flight capture binding file requires paired ready and complete files"
        )
    parity_start = (
        _capture_release_parity(release_parity_path)
        if release_parity_path is not None
        else None
    )
    if session.closed or session.failed or session.active:
        raise VtolMissionAcceptanceError("campaign requires idle healthy telemetry session")
    options = dict(mission_options)
    reserved = {
        "evidence_path",
        "mission_journal_path",
        "telemetry_session",
        "selection_provider",
        "mission_active_ready_file",
        "flight_capture_ready_file",
        "flight_capture_complete_file",
        "flight_capture_binding_file",
        "flight_capture_deadline_s",
    }
    if reserved.intersection(options):
        raise VtolMissionAcceptanceError("campaign owns per-sortie paths and selection callback")
    if options.get("expected_container_id") != session.container_id:
        raise VtolMissionAcceptanceError("campaign container identity mismatch")
    if options.get("acknowledge_owned_disposable_sitl") is not True:
        raise VtolMissionAcceptanceError("owned disposable SITL acknowledgement required")
    if shared_policy is not None:
        if options.get("airframe_profile_sha256") != (
            shared_policy.artifact.airframe_profile_hash
        ):
            raise VtolMissionAcceptanceError("shared policy airframe profile mismatch")
    effective_ranker = shared_policy.ranker if shared_policy is not None else ranker
    ranker_hash = effective_ranker.canonical_hash if effective_ranker is not None else None
    pending = list(candidates)
    if not pending or any(not isinstance(candidate, TaskCandidate) for candidate in pending):
        raise VtolMissionAcceptanceError("nonempty TaskCandidate sequence required")
    ids = [entry.task.task_id for entry in pending]
    if len(set(ids)) != len(ids) or any(task_id not in runtimes for task_id in ids):
        raise VtolMissionAcceptanceError("unique tasks and a runtime for every task required")
    runtime_inputs = deepcopy(dict(runtimes))
    prompt_onboard_enabled = prompt is not None and shared_policy is not None
    # Prompt provenance can still be recorded for legacy ranker callers, but
    # only the shared policy path claims the production OnboardAI handoff.
    if sensor_transport_manifest is not None and sensor_frame_ids is not None:
        try:
            # Reopen the sealed manifest at the campaign boundary.  A caller
            # cannot manufacture a dataclass with plausible hashes and bypass
            # raw-byte/path verification.
            sensor_transport_manifest = load_sensor_transport_manifest(
                sensor_transport_manifest.path
            )
            _validate_sensor_manifest_scenario_binding(
                sensor_transport_manifest,
                options.get("manifest_path"),
            )
            _validate_sensor_frame_map(sensor_transport_manifest, pending, sensor_frame_ids)
        except (OSError, ValueError, TypeError) as exc:
            raise VtolMissionAcceptanceError(str(exc)) from exc
    directory = output_directory.resolve()
    if directory.exists():
        raise VtolMissionAcceptanceError("campaign output directory must be new")
    directory.mkdir(parents=True)
    binding_path = (
        flight_capture_binding_file.resolve()
        if flight_capture_binding_file is not None
        else None
    )
    if binding_path is not None:
        if binding_path.exists():
            raise VtolMissionAcceptanceError(
                f"flight capture binding file must be new: {binding_path}"
            )
        if directory not in binding_path.parents:
            raise VtolMissionAcceptanceError(
                "flight capture binding file must be inside campaign output directory"
            )
    completed = []
    while pending:
        number = len(completed) + 1
        chosen: list[SelectedMissionInputs] = []
        chosen_policy: list[PolicyDecision | None] = []
        chosen_runtime_binding: list[dict[str, object]] = []
        chosen_onboard_decision: list[dict[str, object] | None] = []
        sensor_provenance: dict[str, object] = {}

        def select(
            context, *, chosen=chosen, chosen_policy=chosen_policy,
            pending=tuple(pending), number=number, sensor_provenance=sensor_provenance,
            chosen_runtime_binding=chosen_runtime_binding,
            chosen_onboard_decision=chosen_onboard_decision,
        ):
            if chosen:
                raise VtolMissionAcceptanceError("selection callback invoked more than once")
            onboard_decision = None
            if prompt_onboard_enabled:
                if context.airframe_profile_id != prompt.aircraft_id:
                    raise VtolMissionAcceptanceError(
                        "Prompt aircraft does not match live campaign airframe"
                    )
                # Resolve against the still-pending catalog for this sortie.
                # The resolver snapshot is immutable; the policy/planner path
                # below must produce the exact same selected task and hashes.
                sortie_prompt_resolver = _build_campaign_prompt_resolver(
                    prompt,
                    pending,
                    runtime_inputs,
                    require_registered_prompt=require_registered_prompt,
                )
                onboard = OnboardAIExecutor(
                    prompt,
                    shared_policy,
                    "vtol",
                    options["airframe_profile_sha256"],
                )
                onboard_decision = onboard.prepare_from_prompt(
                    context,
                    sortie_prompt_resolver,
                    mission_id=f"campaign-{session.container_id[:12]}-{number}",
                )
                if onboard_decision.response is not OnboardResponse.EXECUTE:
                    raise VtolMissionAcceptanceError(
                        "Prompt OnboardAI did not produce an executable decision"
                    )
            binding = select_and_compile_task(
                pending,
                context,
                "vtol",
                runtimes=runtime_inputs,
                airframe_profile_hash=options["airframe_profile_sha256"],
                mission_id=f"campaign-{session.container_id[:12]}-{number}",
                shared_policy=shared_policy,
                ranker=ranker if shared_policy is None else None,
            )
            policy_decision = binding.decision
            if (
                shared_policy is not None
                and policy_decision is not None
                and policy_decision.action is not PolicyAction.ABSTAIN
                and not policy_decision_matches_selected_task(
                    policy_decision,
                    binding.selected_task_id,
                    shared_policy.artifact_sha256,
                )
            ):
                raise VtolMissionAcceptanceError("shared policy decision identity mismatch")
            if binding.selected is None or binding.compilation is None:
                if policy_decision is not None:
                    reasons = [
                        {"task_id": item.task.task_id, "reason": policy_decision.reason}
                        for item in pending
                    ]
                else:
                    decision = select_next_task(pending, context, "vtol")
                    reasons = [
                        {"task_id": item.task_id, "reason": item.reason}
                        for item in decision.rejected
                    ]
                raise TaskSelectionRejected(context, reasons)
            if prompt_onboard_enabled:
                if onboard_decision is None:
                    raise VtolMissionAcceptanceError("Prompt OnboardAI decision is missing")
                if (
                    onboard_decision.selected_task_id != binding.selected_task_id
                    or onboard_decision.binding.executable_mission_sha256
                    != binding.compilation.executable_mission_hash
                    or onboard_decision.binding.plan_sha256
                    != binding.compilation.plan.canonical_hash
                    or onboard_decision.binding.wire_hash != binding.compilation.wire_hash
                    or not onboard_decision.planner_revalidated
                ):
                    raise VtolMissionAcceptanceError(
                        "Prompt OnboardAI decision diverges from compiled Mission"
                    )
            task = binding.selected.candidate.task
            runtime = runtime_inputs[task.task_id]
            geometry, policy = parse_mission_compile_runtime(runtime, "vtol")
            compilation = binding.compilation
            selected = SelectedMissionInputs(task, compilation, geometry, policy, prompt)
            chosen.append(selected)
            chosen_policy.append(policy_decision)
            chosen_runtime_binding.append(
                binding.to_runtime_binding(
                    airframe_profile_hash=options["airframe_profile_sha256"],
                    ranker_sha256=ranker_hash,
                    reason=(policy_decision.reason if policy_decision is not None else "selected"),
                ).to_mapping()
            )
            chosen_onboard_decision.append(
                onboard_decision.to_mapping() if onboard_decision is not None else None
            )
            if number == 1 and binding_path is not None:
                _atomic_write_json(
                    binding_path,
                    {
                        "schema": "multidetect.vtol-campaign-flight-capture-binding",
                        "schema_version": 1,
                        "task_id": task.task_id,
                        "context_sha256": binding.context_sha256,
                        "plan_sha256": compilation.plan.canonical_hash,
                        "executable_mission_sha256": (
                            compilation.executable_mission_hash
                        ),
                        "wire_hash": compilation.wire_hash,
                        "airframe_profile_sha256": options["airframe_profile_sha256"],
                        "ranker_sha256": ranker_hash,
                        "policy_artifact_sha256": (
                            shared_policy.artifact_sha256
                            if shared_policy is not None
                            else None
                        ),
                        "prompt_sha256": prompt.canonical_hash if prompt is not None else None,
                        "transaction_id": None,
                        "transaction_binding": "ready_marker_and_mission_evidence",
                        "policy_applied_to_actuator": False,
                        "truth_free": True,
                    },
                    overwrite=False,
                )
            if sensor_transport_manifest is not None and sensor_frame_ids is not None:
                sensor_binding = _sensor_transport_for_task(
                    sensor_transport_manifest,
                    sensor_frame_ids,
                    task.task_id,
                )
                if sensor_binding is None:
                    raise VtolMissionAcceptanceError(
                        "sensor transport binding unexpectedly missing"
                    )
                sensor_provenance.update(sensor_binding)
            return selected

        report = run_mission_acceptance(
            **options,
            evidence_path=directory / f"mission-{number:03d}.json",
            mission_journal_path=directory / f"mission-{number:03d}.transaction.jsonl",
            mission_active_ready_file=None,
            telemetry_session=session,
            selection_provider=select,
            flight_capture_ready_file=(
                flight_capture_ready_file if number == 1 else None
            ),
            flight_capture_complete_file=(
                flight_capture_complete_file if number == 1 else None
            ),
            flight_capture_binding_file=(
                binding_path if number == 1 else None
            ),
            flight_capture_deadline_s=(
                flight_capture_deadline_s if number == 1 else None
            ),
            original_prompt=prompt,
            require_original_prompt=prompt is not None,
            sensor_transport_binding=(
                sensor_provenance
                if sensor_transport_manifest is not None and sensor_frame_ids is not None
                else None
            ),
        )
        if len(chosen) != 1:
            session.failed = True
            raise VtolMissionAcceptanceError("mission did not consume exactly one selection")
        selected = chosen[0]
        selected_policy = chosen_policy[0]
        selected_runtime_binding = chosen_runtime_binding[0]
        selected_onboard_decision = chosen_onboard_decision[0]
        outcome = report.get("shared_outcome_shadow", {}).get("outcome", {})
        expected_hash = selected.compilation.executable_mission_hash
        documents = report.get("documents", {})
        if prompt is not None:
            prompt_document = documents.get("prompt", {})
            if (
                not isinstance(prompt_document, Mapping)
                or prompt_document.get("document") != prompt.to_mapping()
                or prompt_document.get("sha256") != prompt.canonical_hash
            ):
                session.failed = True
                raise VtolMissionAcceptanceError(
                    "original PromptEnvelope is not bound to Mission evidence"
                )
            grant_evidence = documents.get("operator_grant")
            transaction_evidence = report.get("transaction")
            if not isinstance(grant_evidence, Mapping) or not isinstance(
                transaction_evidence, Mapping
            ):
                session.failed = True
                raise VtolMissionAcceptanceError(
                    "transaction and operator grant Mission evidence are required"
                )
            grant_document = grant_evidence.get("document")
            grant_hash = grant_evidence.get("sha256")
            if not isinstance(grant_document, Mapping) or not isinstance(grant_hash, str):
                session.failed = True
                raise VtolMissionAcceptanceError("operator grant Mission evidence is invalid")
            if canonical_sha256(grant_document) != grant_hash:
                session.failed = True
                raise VtolMissionAcceptanceError("operator grant hash mismatch")
            if grant_document.get("prompt_hash") != prompt.canonical_hash:
                session.failed = True
                raise VtolMissionAcceptanceError("operator grant prompt hash mismatch")
            if transaction_evidence.get("id") != grant_document.get("transaction_id"):
                session.failed = True
                raise VtolMissionAcceptanceError("transaction/operator grant identity mismatch")
            transaction_grant_hash = transaction_evidence.get("operator_grant_sha256")
            if transaction_grant_hash is not None and transaction_grant_hash != grant_hash:
                session.failed = True
                raise VtolMissionAcceptanceError("transaction/operator grant hash mismatch")
        if (
            report.get("gate_passed") is not True
            or outcome.get("status") != "completed"
            or outcome.get("mission_hash") != expected_hash
            or documents.get("executable_mission", {}).get("sha256") != expected_hash
            or session.active
            or session.failed
            or session.closed
        ):
            session.failed = True
            raise VtolMissionAcceptanceError("selected mission completion not proven")
        sensor_binding = (
            dict(sensor_provenance)
            if sensor_transport_manifest is not None and sensor_frame_ids is not None
            else None
        )
        if sensor_binding is not None:
            report_sensor_binding = report.get("sensor_transport")
            if not isinstance(report_sensor_binding, Mapping):
                session.failed = True
                raise VtolMissionAcceptanceError(
                    "sensor transport Mission evidence binding is missing"
                )
            if any(
                report_sensor_binding.get(key) != sensor_binding.get(key)
                for key in sensor_binding
            ):
                session.failed = True
                raise VtolMissionAcceptanceError(
                    "sensor transport Mission evidence binding mismatch"
                )
            sensor_binding = dict(report_sensor_binding)
        completed.append(
            {
                "task_id": selected.task.task_id,
                "ranker_sha256": ranker_hash,
                "policy_artifact_sha256": (
                    shared_policy.artifact_sha256 if shared_policy is not None else None
                ),
                "shared_policy_used": shared_policy is not None,
                "planner_revalidated": (
                    selected_policy.planner_revalidated
                    if selected_policy is not None
                    else True
                ),
                "policy_runtime_binding": selected_runtime_binding,
                "onboard_decision": selected_onboard_decision,
                "mission_hash": expected_hash,
                "evidence_path": str(directory / f"mission-{number:03d}.json"),
                "sensor_transport": sensor_binding,
                "prompt_sha256": prompt.canonical_hash if prompt is not None else None,
            }
        )
        if selected_policy is not None:
            policy_path = directory / f"mission-{number:03d}.policy.json"
            _atomic_write_json(
                policy_path,
                {
                    "decision": selected_policy.to_mapping(),
                    "policy_artifact_sha256": (
                        shared_policy.artifact_sha256
                        if shared_policy is not None
                        else selected_policy.policy_artifact_sha256
                    ),
                    "ranker_sha256": ranker_hash,
                    "planner_revalidated": selected_policy.planner_revalidated,
                    "shared_policy_used": shared_policy is not None,
                    "mission_hash": expected_hash,
                    "policy_applied_to_actuator": False,
                },
                overwrite=False,
            )
            completed[-1]["policy_evidence_path"] = str(policy_path)
        pending = [entry for entry in pending if entry.task.task_id != selected.task.task_id]
    parity_end = (
        _capture_release_parity(release_parity_path)
        if release_parity_path is not None
        else None
    )
    parity_stable = parity_start == parity_end if release_parity_path is not None else None
    if release_parity_path is not None and not parity_stable:
        raise VtolMissionAcceptanceError("release parity drifted during shared-policy campaign")
    return {
        "status": "all_tasks_completed",
        "completed": completed,
        "session_id": session.container_id,
        "ranker_sha256": ranker_hash,
        "policy_artifact_sha256": (
            shared_policy.artifact_sha256 if shared_policy is not None else None
        ),
        "prompt": prompt.to_mapping() if prompt is not None else None,
        "shared_policy_used": shared_policy is not None,
        "prompt_sha256": prompt.canonical_hash if prompt is not None else None,
        "prompt_provenance_bound": prompt is not None,
        "onboard_ai_used": prompt_onboard_enabled,
        "onboard_ai_prompt_resolution_bound": (
            prompt_onboard_enabled
            and all(item.get("onboard_decision") is not None for item in completed)
        ),
        "planner_revalidated": all(
            item["planner_revalidated"] for item in completed
        ),
        "sensor_transport_used": sensor_transport_manifest is not None,
        "sensor_transport": (
            {
                "manifest_path": str(sensor_transport_manifest.path),
                "manifest_file_sha256": sensor_transport_manifest.file_sha256,
                "manifest_sha256": sensor_transport_manifest.manifest_sha256,
                "frame_count": len(sensor_transport_manifest.frames),
                "truth_free": True,
            }
            if sensor_transport_manifest is not None
            else None
        ),
        "release_parity_snapshot": {
            "requested": release_parity_path is not None,
            "start": parity_start,
            "end": parity_end,
            "stable": parity_stable,
            "error": None,
        },
        "release_parity_snapshot_gate_passed": (
            True if release_parity_path is None else parity_stable
        ),
    }


def _load_sensor_frame_map(path: Path | None) -> dict[str, str]:
    if path is None:
        raise ValueError("sensor frame map is required")
    raw = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("sensor frame map must be a non-empty JSON object")
    result: dict[str, str] = {}
    for task_id, frame_id in raw.items():
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("sensor frame map task IDs must be non-empty strings")
        if not isinstance(frame_id, str) or not frame_id.strip():
            raise ValueError("sensor frame map frame IDs must be non-empty strings")
        if task_id in result:
            raise ValueError(f"duplicate sensor frame map task ID: {task_id}")
        result[task_id] = frame_id
    return result


def _validate_sensor_frame_map(
    manifest: SensorTransportManifest,
    candidates: Sequence[TaskCandidate],
    frame_ids: Mapping[str, str],
) -> None:
    expected = {candidate.task.task_id for candidate in candidates}
    actual = set(frame_ids)
    if actual != expected:
        raise ValueError(
            "sensor frame map task IDs must exactly match campaign tasks; "
            f"missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}"
        )
    resolved: list[str] = []
    for task_id in sorted(expected):
        frame_id = frame_ids[task_id]
        frame = manifest.frame_for_ids(frame_id)
        if frame is None:
            raise ValueError(f"sensor frame map references unknown frame: {frame_id}")
        resolved.append(frame.frame_id)
    if len(set(resolved)) != len(resolved):
        raise ValueError("sensor frame map must assign a unique frame to each task")


def _validate_sensor_manifest_scenario_binding(
    manifest: SensorTransportManifest,
    scenario_manifest_path: Path | str | None,
) -> None:
    """Bind persisted sensor pixels to the exact scenario used by Mission.

    Older unit fixtures intentionally omit ``scenario_manifest_hash`` and do
    not pass a scenario path.  The live campaign path supplies both, so a
    missing or different hash fails before any Mission transaction starts.
    """

    if scenario_manifest_path is None:
        return
    _, scenario_index = load_scenario_index(Path(scenario_manifest_path).resolve(strict=True))
    expected = scenario_index.canonical_hash
    actual = manifest.scenario_manifest_hash
    if actual != expected:
        raise ValueError(
            "sensor transport scenario manifest identity mismatch: "
            f"expected={expected}, actual={actual}"
        )


def _sensor_transport_for_task(
    manifest: SensorTransportManifest | None,
    frame_ids: Mapping[str, str] | None,
    task_id: str,
) -> dict[str, object] | None:
    if manifest is None or frame_ids is None:
        return None
    frame_id = frame_ids[task_id]
    frame = manifest.frame_for_ids(frame_id)
    if frame is None:
        raise VtolMissionAcceptanceError(f"sensor frame is unavailable for task: {task_id}")
    binding = manifest.selection_mapping([frame.frame_id])
    binding.update(
        {
            "task_id": task_id,
            "frame_id": frame.frame_id,
            "frame_sequence": frame.sequence,
            "frame_sha256": frame.sha256,
            "frame_bytes": frame.bytes,
            "frame_captured_at_s": frame.captured_at_s,
            "frame_captured_at_clock": frame.captured_at_clock,
        }
    )
    return binding


if __name__ == "__main__":
    raise SystemExit(main())
