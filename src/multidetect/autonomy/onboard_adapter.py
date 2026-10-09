"""Adapter-owned Mission boundary for realtime :class:`OnboardAIExecutor` events.

``OnboardAIExecutor`` deliberately returns a proposal and has no transport
authority.  This module is the small seam that a SIM, HIL or REAL adapter can
use to turn that proposal into a new hash-bound Mission revision or a safety
request.  The adapter callback owns upload/readback/commit/permit; this class
only verifies that the Mission it is asked to install is exactly the one the
shared policy and planner produced.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import RLock

from .contracts import ContextSnapshot, ExecutableMission
from .mission_campaign import CampaignTaskBinding, select_and_compile_task
from .onboard_agent import (
    OnboardAIExecutor,
    OnboardDecision,
    OnboardFlightAction,
    OnboardFlightPlan,
    OnboardResponse,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from .onboard_task_state import (
    OnboardTaskReceipt,
    OnboardTaskStateError,
    OnboardTaskStateMachine,
)
from .planner import normalize_vehicle_class
from .prompt_resolution import PromptTaskResolver
from .serialization import require_sha256
from .task_selection import TaskCandidate


class OnboardMissionAdapterError(RuntimeError):
    """Raised when an event cannot be bound to the shared Mission contract."""


MissionInstaller = Callable[[ExecutableMission | None], None]
SafetyDispatcher = Callable[[OnboardDecision], None]
MissionTransactionDispatcher = Callable[
    [ContextSnapshot, ExecutableMission | None, object | None], None
]


@dataclass(frozen=True, slots=True)
class OnboardFlightPlanDispatch:
    """Provenance returned after converting a model plan into a Mission."""

    mission: ExecutableMission
    flight_plan_sha256: str
    mission_sha256: str
    planner_revalidated: bool
    plan_sha256: str | None = None
    wire_hash: str | None = None
    compilation: object | None = None
    selected_task_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mission, ExecutableMission):
            raise TypeError("mission must be an ExecutableMission")
        object.__setattr__(
            self,
            "flight_plan_sha256",
            require_sha256(self.flight_plan_sha256, "flight_plan_sha256"),
        )
        object.__setattr__(
            self,
            "mission_sha256",
            require_sha256(self.mission_sha256, "mission_sha256"),
        )
        if self.mission_sha256 != self.mission.canonical_hash:
            raise ValueError("mission_sha256 does not match mission")
        if type(self.planner_revalidated) is not bool or not self.planner_revalidated:
            raise ValueError("planner_revalidated must be true")
        if self.plan_sha256 is not None:
            object.__setattr__(self, "plan_sha256", require_sha256(self.plan_sha256, "plan_sha256"))
        if self.wire_hash is not None:
            object.__setattr__(self, "wire_hash", require_sha256(self.wire_hash, "wire_hash"))
        if self.compilation is not None:
            # Keep the import local: mission_planning imports the public
            # autonomy package while that package is still being initialised.
            from ..mission_planning import PlannedMissionCompilation

            if not isinstance(self.compilation, PlannedMissionCompilation):
                raise TypeError("compilation must be a PlannedMissionCompilation")
            if self.plan_sha256 != self.compilation.plan.canonical_hash:
                raise ValueError("plan_sha256 does not match compilation")
            if self.wire_hash != self.compilation.wire_hash:
                raise ValueError("wire_hash does not match compilation")
            if self.mission is not self.compilation.compiled_mission.executable_mission:
                raise ValueError("mission must be the compilation executable mission")
        if self.selected_task_id is not None:
            if not isinstance(self.selected_task_id, str) or not self.selected_task_id.strip():
                raise ValueError("selected_task_id must be a non-empty string when provided")


FlightPlanDispatcher = Callable[
    [ContextSnapshot, OnboardFlightPlan, OnboardDecision, str], OnboardFlightPlanDispatch
]


@dataclass(frozen=True, slots=True)
class OnboardMissionRevision:
    """One observable event-to-Mission adapter result."""

    sequence: int
    trigger: str
    event: OnboardRuntimeEvent | None
    decision: OnboardDecision
    mission: ExecutableMission | None
    mission_replaced: bool
    mission_cleared: bool
    safety_dispatched: bool
    flight_plan_dispatched: bool = False
    task_receipt: OnboardTaskReceipt | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-mission-revision",
            "sequence": self.sequence,
            "trigger": self.trigger,
            "event": None if self.event is None else self.event.to_mapping(),
            "event_sha256": None if self.event is None else self.event.canonical_hash,
            "decision": self.decision.to_mapping(),
            "mission_id": None if self.mission is None else self.mission.mission_id,
            "mission_sha256": None if self.mission is None else self.mission.canonical_hash,
            "mission_replaced": self.mission_replaced,
            "mission_cleared": self.mission_cleared,
            "safety_dispatched": self.safety_dispatched,
            "flight_plan_dispatched": self.flight_plan_dispatched,
            "task_receipt": None if self.task_receipt is None else self.task_receipt.to_mapping(),
            "policy_applied_to_actuator": False,
            "truth_free": True,
        }


class OnboardMissionAdapter:
    """Persist an OnboardAI session and expose an adapter-owned Mission seam.

    The installer is intentionally injected.  A deterministic SIM can bind a
    local Mission observer, while a PX4 adapter can perform its own safe
    pause/upload/readback/commit/permit transaction.  The policy never gets
    this callback and therefore cannot send MAVLink, arm, disarm or PWM.
    """

    __slots__ = (
        "executor",
        "vehicle_class",
        "airframe_profile_hash",
        "mission_id_prefix",
        "mission_installer",
        "mission_transaction_dispatcher",
        "safety_dispatcher",
        "flight_plan_dispatcher",
        "_candidates",
        "_runtimes",
        "_current_mission",
        "_prepared",
        "_sequence",
        "_history",
        "_event_revisions",
        "_mission_ids",
        "_faulted",
        "_safety_engaged",
        "_task_state",
    )

    def __init__(
        self,
        executor: OnboardAIExecutor,
        *,
        mission_id_prefix: str = "onboard",
        mission_installer: MissionInstaller | None = None,
        mission_transaction_dispatcher: MissionTransactionDispatcher | None = None,
        safety_dispatcher: SafetyDispatcher | None = None,
        flight_plan_dispatcher: FlightPlanDispatcher | None = None,
    ) -> None:
        if not isinstance(executor, OnboardAIExecutor):
            raise TypeError("executor must be an OnboardAIExecutor")
        prefix = mission_id_prefix.strip() if isinstance(mission_id_prefix, str) else ""
        if not prefix:
            raise ValueError("mission_id_prefix must be non-empty")
        if mission_installer is not None and not callable(mission_installer):
            raise TypeError("mission_installer must be callable")
        if mission_transaction_dispatcher is not None and not callable(
            mission_transaction_dispatcher
        ):
            raise TypeError("mission_transaction_dispatcher must be callable")
        if safety_dispatcher is not None and not callable(safety_dispatcher):
            raise TypeError("safety_dispatcher must be callable")
        if flight_plan_dispatcher is not None and not callable(flight_plan_dispatcher):
            raise TypeError("flight_plan_dispatcher must be callable")
        self.executor = executor
        self.vehicle_class = normalize_vehicle_class(executor.vehicle_class)
        self.airframe_profile_hash = executor.airframe_profile_hash
        self.mission_id_prefix = prefix
        self.mission_installer = mission_installer
        self.mission_transaction_dispatcher = mission_transaction_dispatcher
        self.safety_dispatcher = safety_dispatcher
        self.flight_plan_dispatcher = flight_plan_dispatcher
        self._candidates: tuple[TaskCandidate, ...] | None = None
        self._runtimes: dict[str, object] | None = None
        self._current_mission: ExecutableMission | None = None
        self._prepared = False
        self._sequence = 0
        self._history: list[OnboardMissionRevision] = []
        self._event_revisions: dict[str, tuple[str, OnboardMissionRevision]] = {}
        self._mission_ids: set[str] = set()
        self._faulted = False
        self._safety_engaged = False
        self._task_state = OnboardTaskStateMachine()

    @property
    def current_mission(self) -> ExecutableMission | None:
        return self._current_mission

    @property
    def history(self) -> tuple[OnboardMissionRevision, ...]:
        return tuple(self._history)

    @property
    def prepared(self) -> bool:
        return self._prepared

    @property
    def faulted(self) -> bool:
        """Whether an injected lifecycle callback left Mission state unknown."""

        return self._faulted

    @property
    def safety_engaged(self) -> bool:
        """Whether a safety response terminated this adapter session."""

        return self._safety_engaged

    @property
    def task_state(self) -> OnboardTaskStateMachine:
        """Read-only access to adapter-owned phase/receipt state."""

        return self._task_state

    def mark_takeoff_complete(
        self,
        context: ContextSnapshot,
        *,
        minimum_relative_altitude_m: float = 10.0,
    ) -> bool:
        """Advance the task phase only from qualified vehicle telemetry."""

        if self._faulted:
            raise OnboardMissionAdapterError("adapter is faulted and requires recovery")
        if self._safety_engaged:
            raise OnboardMissionAdapterError(
                "safety response engaged; create a new adapter session before resuming"
            )
        if not self._prepared:
            raise OnboardMissionAdapterError(
                "prepare must be called before marking takeoff complete"
            )
        return self._task_state.mark_takeoff_complete(
            context,
            minimum_relative_altitude_m=minimum_relative_altitude_m,
        )

    def prepare(
        self,
        context: ContextSnapshot,
        *,
        candidates: Sequence[TaskCandidate] | None = None,
        runtimes: Mapping[str, object] | None = None,
        resolver: PromptTaskResolver | None = None,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Resolve the initial Prompt and install its first Mission proposal."""

        if self._faulted:
            raise OnboardMissionAdapterError("adapter is faulted and requires recovery")
        if self._prepared:
            raise OnboardMissionAdapterError("Onboard session is already prepared")
        if resolver is not None:
            if candidates is not None or runtimes is not None:
                raise OnboardMissionAdapterError(
                    "resolver cannot be combined with explicit candidates/runtimes"
                )
            resolution = resolver.resolve(self.executor.prompt)
            entries = resolution.candidates
            runtime_map = dict(resolution.runtimes)
            decision = self.executor.prepare_from_prompt(
                context,
                resolver,
                mission_id=mission_id or f"{self.mission_id_prefix}-initial",
            )
        else:
            if candidates is None or runtimes is None:
                raise OnboardMissionAdapterError(
                    "prepare requires candidates/runtimes or a PromptTaskResolver"
                )
            entries, runtime_map = self._normalize_inputs(candidates, runtimes)
            decision = self.executor.prepare(
                context,
                entries,
                runtime_map,
                mission_id=mission_id or f"{self.mission_id_prefix}-initial",
            )
        initial_mission_id = mission_id or f"{self.mission_id_prefix}-initial"
        if decision.response is OnboardResponse.EXECUTE:
            try:
                revision = self._bind_decision(
                    decision,
                    context,
                    entries,
                    runtime_map,
                    trigger="prepare",
                    sequence=0,
                    event=None,
                    mission_id=initial_mission_id,
                )
            except Exception as exc:
                self._faulted = True
                self._current_mission = None
                raise OnboardMissionAdapterError(
                    "initial Mission installation failed; adapter requires recovery"
                ) from exc
        else:
            if decision.binding.mission_bound:
                raise OnboardMissionAdapterError(
                    "initial non-execute decision unexpectedly contains a bound Mission"
                )
            revision = OnboardMissionRevision(
                sequence=0,
                trigger="prepare",
                event=None,
                decision=decision,
                mission=None,
                mission_replaced=False,
                mission_cleared=False,
                safety_dispatched=False,
            )
        self._candidates = entries
        self._runtimes = runtime_map
        self._prepared = True
        self._mission_ids.add(initial_mission_id)
        self._history.append(revision)
        return revision

    def handle_event(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        *,
        candidates: Sequence[TaskCandidate] | None = None,
        runtimes: Mapping[str, object] | None = None,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Consume one event and either replace or clear the active Mission."""

        if self._faulted:
            raise OnboardMissionAdapterError("adapter is faulted and requires recovery")
        if not self._prepared:
            raise OnboardMissionAdapterError("prepare must be called before handle_event")
        if not isinstance(event, OnboardRuntimeEvent):
            raise TypeError("event must be an OnboardRuntimeEvent")
        existing = self._event_revisions.get(event.event_id)
        if existing is not None:
            if existing[0] == event.canonical_hash:
                return existing[1]
            self._faulted = True
            raise OnboardMissionAdapterError(
                f"event ID collision with a different payload: {event.event_id}"
            )
        if self._safety_engaged:
            raise OnboardMissionAdapterError(
                "safety response engaged; create a new adapter session before resuming"
            )
        entries, runtime_map = self._event_inputs(candidates, runtimes)
        sequence = self._sequence + 1
        revision_id = mission_id or f"{self.mission_id_prefix}-event-{sequence}"
        if revision_id in self._mission_ids:
            raise OnboardMissionAdapterError(f"Mission ID already used: {revision_id}")
        previous_task_id = self.executor.active_task_id
        decision = self.executor.step(
            context,
            event,
            entries,
            runtime_map,
            mission_id=revision_id,
        )
        # A safety response always has priority over the semantic task phase.
        # A model can request SEARCH while the fixed envelope vetoes movement
        # because of GPS, battery, obstacle or confidence; that veto must
        # still reach the safety dispatcher and clear the old Mission.  Only
        # an operationally compatible model proposal is allowed to create a
        # task receipt on a safety branch.
        task_action: OnboardTaskAction | None = None
        if decision.response in {OnboardResponse.EXECUTE, OnboardResponse.REPLAN}:
            try:
                task_action = self._task_state.validate(context, event, decision)
            except OnboardTaskStateError as exc:
                self._faulted = True
                self._current_mission = None
                raise OnboardMissionAdapterError(
                    f"task phase transition rejected: {type(exc).__name__}: {exc}"
                ) from exc
        elif decision.model_proposal is not None:
            proposal_action = decision.model_proposal.task_action
            safety_compatible = (
                (
                    decision.response is OnboardResponse.RETURN
                    and proposal_action
                    in {OnboardTaskAction.RECORD_LOCATION, OnboardTaskAction.RETURN_HOME}
                )
                or (
                    decision.response is OnboardResponse.HOLD
                    and proposal_action in {OnboardTaskAction.REPORT, OnboardTaskAction.HOLD}
                )
                or (
                    decision.response is OnboardResponse.LAND
                    and proposal_action is OnboardTaskAction.LAND
                )
            )
            if safety_compatible:
                try:
                    task_action = self._task_state.validate(context, event, decision)
                except OnboardTaskStateError:
                    # Keep the safety response; reject only the semantic
                    # receipt because phase/telemetry evidence is incomplete.
                    task_action = None
        if decision.response is OnboardResponse.REPLAN:
            try:
                revision = self._bind_decision(
                    decision,
                    context,
                    entries,
                    runtime_map,
                    trigger=event.kind.value,
                    sequence=sequence,
                    event=event,
                    mission_id=revision_id,
                )
            except Exception as exc:
                self._faulted = True
                self._current_mission = None
                raise OnboardMissionAdapterError(
                    "Mission replacement failed; adapter requires recovery: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        else:
            if decision.binding.mission_bound:
                raise OnboardMissionAdapterError(
                    "safety decision unexpectedly contains a bound Mission"
                )
            cleared = False
            dispatched = False
            try:
                if self.safety_dispatcher is not None:
                    # The dispatcher owns the safe-mode request/ack.  Do not
                    # clear the old Mission until that callback has succeeded.
                    self.safety_dispatcher(decision)
                    dispatched = True
                if self.mission_installer is not None and self._current_mission is not None:
                    self.mission_installer(None)
                    cleared = True
                if self.mission_transaction_dispatcher is not None:
                    self.mission_transaction_dispatcher(context, None, None)
            except Exception as exc:
                self._faulted = True
                self._current_mission = None
                raise OnboardMissionAdapterError(
                    "safety Mission transaction failed; adapter requires recovery: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            # A safety response ends the logical Mission even when this
            # adapter is observation-only and has no transport installer.
            self._current_mission = None
            model_requested_return = (
                decision.response is OnboardResponse.RETURN
                and decision.model_proposal is not None
                and decision.model_proposal.task_action
                in {OnboardTaskAction.RECORD_LOCATION, OnboardTaskAction.RETURN_HOME}
                and task_action is not None
                and decision.model_proposal.flight_plan is not None
                and decision.model_proposal.flight_plan.action.value == "return_home"
                and self.safety_dispatcher is not None
                and dispatched
            )
            # A model-requested return is a normal task leg.  Keep the
            # adapter open for return-arrival/report telemetry.  A GPS,
            # battery, obstacle or inference safety response remains terminal.
            self._safety_engaged = not model_requested_return
            revision = OnboardMissionRevision(
                sequence=sequence,
                trigger=event.kind.value,
                event=event,
                decision=decision,
                mission=None,
                mission_replaced=False,
                mission_cleared=cleared,
                safety_dispatched=dispatched,
            )
        try:
            task_receipt = self._task_state.commit(context, event, decision, task_action)
        except OnboardTaskStateError as exc:  # pragma: no cover - validate guards this path
            self._faulted = True
            self._current_mission = None
            raise OnboardMissionAdapterError("task phase receipt failed") from exc
        if task_receipt is not None:
            revision = replace(revision, task_receipt=task_receipt)
        try:
            # Selection updates the executor before compilation/installation.
            # Rebind temporal history only after both transport and task receipt
            # have committed successfully; a failed proposal must not erase it.
            self.executor.reset_model_history_after_task_switch(context, previous_task_id)
        except Exception as exc:
            # The transport may already have committed. Do not retry or publish
            # a successful revision: recovery must reconcile that actual state.
            self._faulted = True
            self._current_mission = None
            raise OnboardMissionAdapterError(
                "Mission transaction committed but model history reset failed; "
                "adapter requires recovery"
            ) from exc
        self._sequence = sequence
        self._mission_ids.add(revision_id)
        self._event_revisions[event.event_id] = (event.canonical_hash, revision)
        self._history.append(revision)
        return revision

    def _event_inputs(
        self,
        candidates: Sequence[TaskCandidate] | None,
        runtimes: Mapping[str, object] | None,
    ) -> tuple[tuple[TaskCandidate, ...], dict[str, object]]:
        if candidates is None and runtimes is None:
            if self._candidates is None or self._runtimes is None:
                raise OnboardMissionAdapterError("resolved task inputs are unavailable")
            return self._candidates, deepcopy(self._runtimes)
        if candidates is None or runtimes is None:
            raise OnboardMissionAdapterError("event candidates and runtimes must be paired")
        return self._normalize_inputs(candidates, runtimes)

    @staticmethod
    def _normalize_inputs(
        candidates: Sequence[TaskCandidate], runtimes: Mapping[str, object]
    ) -> tuple[tuple[TaskCandidate, ...], dict[str, object]]:
        if isinstance(candidates, (str, bytes, bytearray)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a sequence")
        entries = tuple(candidates)
        if not entries or any(not isinstance(item, TaskCandidate) for item in entries):
            raise ValueError("candidates must be a non-empty TaskCandidate sequence")
        if not isinstance(runtimes, Mapping):
            raise TypeError("runtimes must be a mapping")
        ids = [item.task.task_id for item in entries]
        if len(ids) != len(set(ids)) or any(task_id not in runtimes for task_id in ids):
            raise ValueError("candidate task IDs and runtimes must match")
        return entries, deepcopy(dict(runtimes))

    def _bind_decision(
        self,
        decision: OnboardDecision,
        context: ContextSnapshot,
        candidates: Sequence[TaskCandidate],
        runtimes: Mapping[str, object],
        *,
        trigger: str,
        sequence: int,
        event: OnboardRuntimeEvent | None,
        mission_id: str,
    ) -> OnboardMissionRevision:
        if decision.response not in {OnboardResponse.EXECUTE, OnboardResponse.REPLAN}:
            raise OnboardMissionAdapterError("only execute/replan decisions can bind a Mission")
        proposal = decision.model_proposal
        if proposal is not None and proposal.flight_plan is not None:
            return self._bind_model_flight_plan(
                decision,
                context,
                proposal.flight_plan,
                candidates=candidates,
                runtimes=runtimes,
                trigger=trigger,
                sequence=sequence,
                event=event,
                mission_id=mission_id,
            )
        binding: CampaignTaskBinding = select_and_compile_task(
            candidates,
            context,
            self.vehicle_class,
            runtimes=runtimes,
            airframe_profile_hash=self.airframe_profile_hash,
            shared_policy=self.executor.shared_policy,
            mission_id=mission_id,
        )
        if binding.selected is None or binding.compilation is None:
            raise OnboardMissionAdapterError("Onboard decision selected no compilable task")
        compilation = binding.compilation
        runtime_binding = decision.binding
        if (
            decision.selected_task_id != binding.selected_task_id
            or runtime_binding.plan_sha256 != compilation.plan.canonical_hash
            or runtime_binding.executable_mission_sha256 != compilation.executable_mission_hash
            or runtime_binding.wire_hash != compilation.wire_hash
            or not runtime_binding.mission_bound
            or not runtime_binding.planner_revalidated
        ):
            raise OnboardMissionAdapterError("Onboard decision diverges from compiled Mission")
        mission = compilation.compiled_mission.executable_mission
        if self.mission_installer is not None:
            self.mission_installer(mission)
        if self.mission_transaction_dispatcher is not None:
            self.mission_transaction_dispatcher(context, mission, compilation)
        replaced = self._current_mission is not None
        self._current_mission = mission
        return OnboardMissionRevision(
            sequence=sequence,
            trigger=trigger,
            event=event,
            decision=decision,
            mission=mission,
            mission_replaced=replaced,
            mission_cleared=False,
            safety_dispatched=False,
        )

    def _bind_model_flight_plan(
        self,
        decision: OnboardDecision,
        context: ContextSnapshot,
        flight_plan: OnboardFlightPlan,
        *,
        candidates: Sequence[TaskCandidate],
        runtimes: Mapping[str, object],
        trigger: str,
        sequence: int,
        event: OnboardRuntimeEvent | None,
        mission_id: str,
    ) -> OnboardMissionRevision:
        """Compile/install model navigation intent through an adapter callback.

        The callback is the platform-specific seam.  It must convert the
        bounded relative plan into a validated platform-neutral Mission and
        perform any PX4 upload/readback/commit/permit transaction.  The model
        itself never receives this callback and cannot emit transport data.
        """

        if self.flight_plan_dispatcher is None:
            raise OnboardMissionAdapterError("model flight plan has no dispatcher")
        # A calibrated perception frame already contains the measured target
        # ray in NED. For target-follow actions, bind that observation instead
        # of asking a bounded regression head to approximate a quantity that
        # camera calibration and metric depth measured directly. The model
        # still owns the semantic action; the planner and safety gates still
        # own the Mission.
        if (
            event is not None
            and event.target_geometry_frame == "ned"
            and event.target_north_offset_m is not None
            and event.target_east_offset_m is not None
            and flight_plan.action
            in {OnboardFlightAction.GOTO_RELATIVE, OnboardFlightAction.FOLLOW_TARGET}
        ):
            flight_plan = replace(
                flight_plan,
                north_offset_m=event.target_north_offset_m,
                east_offset_m=event.target_east_offset_m,
                reference_id=f"perception:{event.event_id}",
            )
            if decision.model_proposal is not None:
                decision = replace(
                    decision,
                    model_proposal=replace(
                        decision.model_proposal,
                        flight_plan=flight_plan,
                    ),
                )
        if flight_plan.action.value in {"hold_position", "return_home", "land"}:
            raise OnboardMissionAdapterError("safety flight plan must use the safety response path")
        try:
            dispatch = self.flight_plan_dispatcher(context, flight_plan, decision, mission_id)
        except Exception as exc:
            raise OnboardMissionAdapterError("model flight plan compilation failed") from exc
        if not isinstance(dispatch, OnboardFlightPlanDispatch):
            raise OnboardMissionAdapterError("flight plan dispatcher returned invalid provenance")
        if dispatch.flight_plan_sha256 != flight_plan.canonical_hash:
            raise OnboardMissionAdapterError("flight plan dispatcher hash mismatch")
        if (
            dispatch.plan_sha256 is None
            or dispatch.wire_hash is None
            or dispatch.compilation is None
        ):
            raise OnboardMissionAdapterError(
                "flight plan dispatcher must return compiled planner and wire provenance"
            )
        if not dispatch.planner_revalidated:
            raise OnboardMissionAdapterError("flight plan planner was not revalidated")
        if dispatch.selected_task_id is None:
            raise OnboardMissionAdapterError(
                "flight plan dispatcher must identify the selected task"
            )
        if dispatch.selected_task_id != decision.selected_task_id:
            raise OnboardMissionAdapterError(
                "flight plan dispatcher selected task does not match policy decision"
            )
        selected_task = next(
            (
                candidate.task
                for candidate in candidates
                if candidate.task.task_id == decision.selected_task_id
            ),
            None,
        )
        if selected_task is None or selected_task.task_id not in runtimes:
            raise OnboardMissionAdapterError("selected model task inputs are unavailable")
        # A callback's claimed flight-plan hash is not proof that it used
        # those offsets.  Independently compile the selected task and intent
        # through the canonical authoring seam before installing its result.
        from .onboard_navigation import compile_model_flight_plan

        expected = compile_model_flight_plan(
            selected_task,
            context,
            self.vehicle_class,
            runtime=runtimes[selected_task.task_id],
            flight_plan=flight_plan,
            airframe_profile_hash=self.airframe_profile_hash,
            mission_id=mission_id,
        )
        if (
            dispatch.plan_sha256 != expected.plan.canonical_hash
            or dispatch.wire_hash != expected.wire_hash
            or dispatch.mission_sha256 != expected.executable_mission_hash
        ):
            raise OnboardMissionAdapterError(
                "flight plan dispatcher diverges from canonical model compilation"
            )
        mission = dispatch.mission
        if mission.mission_id != mission_id:
            raise OnboardMissionAdapterError("flight plan Mission ID does not match request")
        if mission.airframe_profile_hash != self.airframe_profile_hash:
            raise OnboardMissionAdapterError("flight plan Mission airframe hash mismatch")
        # The policy's first compilation is used to choose and validate the
        # task.  Model navigation then compiles a replacement geometry for
        # that same task.  Rebind the decision's hashes to the replacement so
        # the receipt, UI and adapter all describe the Mission that is
        # actually installed, rather than the stale prompt route.
        bound_binding = replace(
            decision.binding,
            plan_sha256=dispatch.plan_sha256,
            executable_mission_sha256=dispatch.mission_sha256,
            wire_hash=dispatch.wire_hash,
            mission_bound=True,
            planner_revalidated=True,
        )
        bound_decision = replace(decision, binding=bound_binding)
        if self.mission_installer is not None:
            self.mission_installer(mission)
        if self.mission_transaction_dispatcher is not None:
            self.mission_transaction_dispatcher(context, mission, dispatch.compilation)
        replaced = self._current_mission is not None
        self._current_mission = mission
        return OnboardMissionRevision(
            sequence=sequence,
            trigger=trigger,
            event=event,
            decision=bound_decision,
            mission=mission,
            mission_replaced=replaced,
            mission_cleared=False,
            safety_dispatched=False,
            flight_plan_dispatched=True,
        )


class OnboardMissionEventLoop:
    """Serialize realtime runtime events through one shared Mission adapter.

    The loop is intentionally a synchronous ingress, rather than a hidden
    worker thread.  A SIM sensor tick, a HIL telemetry callback, and a Jetson
    perception callback can all call :meth:`submit` and receive the same
    hash-bound :class:`OnboardMissionRevision`.  The lock prevents two sensor
    callbacks from racing the executor's timestamp and Mission-ID guards;
    the adapter remains the owner of policy evaluation, planner revalidation,
    Mission replacement and safety dispatch.

    This class does not own a MAVLink/PX4 transport and does not create an
    actuator command.  A transport-specific installer is still injected into
    ``OnboardMissionAdapter`` and must perform its own pause/upload/readback/
    commit/permit transaction before acknowledging a replacement.
    """

    __slots__ = (
        "adapter",
        "_lock",
        "_closed",
        "_submitted_event_ids",
        "_last_revision",
    )

    def __init__(self, adapter: OnboardMissionAdapter) -> None:
        if not isinstance(adapter, OnboardMissionAdapter):
            raise TypeError("adapter must be an OnboardMissionAdapter")
        self.adapter = adapter
        self._lock = RLock()
        self._closed = False
        self._submitted_event_ids: set[str] = set()
        self._last_revision: OnboardMissionRevision | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def last_revision(self) -> OnboardMissionRevision | None:
        with self._lock:
            return self._last_revision

    @property
    def processed_event_count(self) -> int:
        """Number of distinct accepted event IDs, excluding idempotent retries."""

        with self._lock:
            return len(self._submitted_event_ids)

    def prepare(
        self,
        context: ContextSnapshot,
        *,
        candidates: Sequence[TaskCandidate] | None = None,
        runtimes: Mapping[str, object] | None = None,
        resolver: PromptTaskResolver | None = None,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Prepare the initial Mission while holding the serialized ingress lock."""

        with self._lock:
            self._require_open()
            revision = self.adapter.prepare(
                context,
                candidates=candidates,
                runtimes=runtimes,
                resolver=resolver,
                mission_id=mission_id,
            )
            self._last_revision = revision
            return revision

    def submit(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        *,
        candidates: Sequence[TaskCandidate] | None = None,
        runtimes: Mapping[str, object] | None = None,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Process one fresh event and return its replacement/safety revision.

        Re-submitting the same event ID/payload is safe and returns the
        adapter's original revision.  A changed payload for an existing ID or
        a stale/out-of-order timestamp remains fail-closed in the adapter.
        """

        with self._lock:
            self._require_open()
            revision = self.adapter.handle_event(
                context,
                event,
                candidates=candidates,
                runtimes=runtimes,
                mission_id=mission_id,
            )
            self._submitted_event_ids.add(event.event_id)
            # A historical retry is an acknowledgement, not a new state
            # transition.  Never let it roll back the latest safety/replan.
            if self._last_revision is None or revision.sequence > self._last_revision.sequence:
                self._last_revision = revision
            return revision

    # ``dispatch`` reads naturally at a sensor/telemetry boundary while
    # retaining ``submit`` as the canonical API for tests and integrations.
    dispatch = submit

    def observe_model_input(self, context: ContextSnapshot, event: OnboardRuntimeEvent) -> bool:
        """Collect a history sample without replacing or starting a Mission.

        A repeated SEARCH view may be suppressed by the sortie's Mission
        deduplication, but it remains a real observation for a temporal model.
        Sampling uses the same lock and session gates as event submission.
        """

        with self._lock:
            self._require_open()
            if self.adapter.faulted:
                raise OnboardMissionAdapterError("adapter is faulted and requires recovery")
            if self.adapter.safety_engaged:
                raise OnboardMissionAdapterError(
                    "safety response engaged; create a new adapter session before resuming"
                )
            if not self.adapter.prepared:
                raise OnboardMissionAdapterError("prepare must be called before history sampling")
            return self.adapter.executor.observe_model_input(context, event)

    def mark_takeoff_complete(
        self,
        context: ContextSnapshot,
        *,
        minimum_relative_altitude_m: float = 10.0,
    ) -> bool:
        """Serialize the telemetry-gated TAKEOFF → SEARCH transition."""

        with self._lock:
            self._require_open()
            return self.adapter.mark_takeoff_complete(
                context,
                minimum_relative_altitude_m=minimum_relative_altitude_m,
            )

    def evidence(self) -> dict[str, object]:
        """Return truth-free loop/adapter state for SIM/HIL/REAL diagnostics."""

        with self._lock:
            revision = self._last_revision
            mission = self.adapter.current_mission
            return {
                "schema_version": 1,
                "contract": "multidetect.onboard-mission-event-loop",
                "closed": self._closed,
                "prepared": self.adapter.prepared,
                "faulted": self.adapter.faulted,
                "safety_engaged": self.adapter.safety_engaged,
                "task_phase": self.adapter.task_state.phase.value,
                "task_receipt_count": len(self.adapter.task_state.receipts),
                "processed_event_count": len(self._submitted_event_ids),
                "last_revision": None if revision is None else revision.to_mapping(),
                "current_mission_id": None if mission is None else mission.mission_id,
                "current_mission_sha256": None if mission is None else mission.canonical_hash,
                "policy_applied_to_actuator": False,
                "truth_free": True,
            }

    def close(self) -> None:
        """Stop accepting new events; the underlying adapter remains inspectable."""

        with self._lock:
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise OnboardMissionAdapterError("Onboard Mission event loop is closed")


__all__ = [
    "MissionInstaller",
    "MissionTransactionDispatcher",
    "OnboardMissionAdapter",
    "OnboardMissionAdapterError",
    "OnboardMissionEventLoop",
    "OnboardMissionRevision",
    "SafetyDispatcher",
]
