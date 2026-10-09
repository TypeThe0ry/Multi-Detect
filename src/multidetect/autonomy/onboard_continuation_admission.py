"""Source-anchored VLM intent to an airborne segment, without transport authority.

This additive development path deliberately does not use the sealed whole-sortie
admission or adapter. Actual host replacement, a watchdog and receiver joining
remain separate requirements; a receipt here is not a PX4 permit or flight proof.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, replace

from ..geolocation_accuracy import wgs84_distance_m
from ..mavlink_gps import GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M, MAVLINK_QUALIFIED_FIX_TYPE_MIN
from ..mission_planning import (
    compile_planned_task_mission_from_runtime,
    parse_mission_compile_runtime,
)
from .contracts import GeoPoint
from .onboard_agent import OnboardEventKind
from .onboard_continuation import (
    ContinuationCompilation,
    ContinuationCompilePolicy,
    ContinuationParentBinding,
    compile_multirotor_continuation,
)
from .onboard_continuation_binding import ContinuationParentEvidence
from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardMultimodalScope
from .onboard_vlm_admission import (
    ExecutionAdmissionPolicy,
    ExecutionSnapshot,
    SourceAnchoredIntent,
)
from .serialization import (
    canonical_sha256,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)


class OnboardContinuationAdmissionError(ValueError):
    """The original intent cannot be admitted at this actual airborne state."""


_DEFAULT_POLICY = ExecutionAdmissionPolicy()
AUTHORING_CONTRACT = "multidetect.onboard-continuation-original-authoring-v1"


@dataclass(frozen=True, slots=True)
class ContinuationAuthoringEvidence:
    """Original host declaration, not a model's re-labelled runtime or a grant.

    Full Mission-compile runtime identity is distinct from the older shared
    policy artifact's algorithm/contract hash and environment descriptor hash.
    A real authenticated producer is still pending. Default source trust is empty.
    """

    source_receipt_bytes: bytes
    source_receipt_sha256: str
    trusted_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.to_mapping()

    def to_mapping(self) -> dict:
        payload = self.source_receipt_bytes
        if type(payload) is not bytes or not 0 < len(payload) <= 64 * 1024:
            raise OnboardContinuationAdmissionError("authoring declaration bytes must be bounded")
        require_sha256(self.source_receipt_sha256, "authoring original bytes SHA")
        if hashlib.sha256(payload).hexdigest() != self.source_receipt_sha256:
            raise OnboardContinuationAdmissionError("authoring original bytes/SHA mismatch")
        raw = strict_object(
            strict_json_loads(payload, name="original authoring declaration"),
            name="original authoring declaration",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "source_id",
                    "observed_at_s",
                    "scope_sha256",
                    "parent_mission_sha256",
                    "parent_wire_sha256",
                    "journal_context_sha256",
                    "parent_journal_sha256",
                    "parent_transaction_id",
                    "task_sha256",
                    "runtime_sha256",
                    "policy_artifact_sha256",
                }
            ),
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or (raw["contract"] != AUTHORING_CONTRACT)
        ):
            raise OnboardContinuationAdmissionError("unsupported authoring declaration")
        require_text(raw["source_id"], "original authoring source")
        require_text(raw["parent_transaction_id"], "original parent transaction ID")
        require_real(raw["observed_at_s"], "original authoring time", minimum=0)
        for name in (
            "scope_sha256",
            "parent_mission_sha256",
            "parent_wire_sha256",
            "journal_context_sha256",
            "parent_journal_sha256",
            "task_sha256",
            "runtime_sha256",
            "policy_artifact_sha256",
        ):
            require_sha256(raw[name], name)
        if not isinstance(self.trusted_source_ids, tuple) or len(
            set(self.trusted_source_ids)
        ) != len(self.trusted_source_ids):
            raise OnboardContinuationAdmissionError(
                "authoring trust must be an immutable unique tuple"
            )
        for source in self.trusted_source_ids:
            require_text(source, "trusted original authoring source")
        return raw

    def validate_binding(self, anchor, current, parent_evidence, parent) -> None:
        raw = self.to_mapping()
        if raw["source_id"] not in self.trusted_source_ids:
            raise OnboardContinuationAdmissionError(
                "original authoring source is unknown/untrusted"
            )
        expected = {
            "scope_sha256": _scope(current).canonical_hash,
            "parent_mission_sha256": parent.parent_mission_sha256,
            "parent_wire_sha256": parent.parent_wire_sha256,
            "journal_context_sha256": parent_evidence.journal_context.canonical_hash,
            "parent_journal_sha256": parent.parent_journal_sha256,
            "parent_transaction_id": parent_evidence.to_mapping()["journal_identity"][
                "transaction_id"
            ],
            "task_sha256": anchor.task.canonical_hash,
            "runtime_sha256": canonical_sha256(anchor.runtime),
            "policy_artifact_sha256": current.policy_artifact_sha256,
        }
        if any(raw[name] != value for name, value in expected.items()) or not (
            parent_evidence.journal_context.captured_at_s
            <= raw["observed_at_s"]
            <= parent_evidence.mission_binding.bound_at_s
            <= anchor.source.context.captured_at_s
        ):
            raise OnboardContinuationAdmissionError("original authoring lineage/runtime differs")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            {
                "original": self.to_mapping(),
                "source_receipt_sha256": self.source_receipt_sha256,
                "trusted_source_ids": list(self.trusted_source_ids),
            }
        )


def _scope(snapshot: ExecutionSnapshot) -> OnboardMultimodalScope:
    scope = snapshot.scope
    if isinstance(scope, OnboardInferenceScope):
        return OnboardMultimodalScope.from_inference_scope(scope)
    if isinstance(scope, OnboardMultimodalScope):
        return scope
    raise TypeError("snapshot scope must be typed")


def _distance(origin: GeoPoint, target: GeoPoint) -> float:
    return wgs84_distance_m(
        latitude1_deg=origin.latitude_deg,
        longitude1_deg=origin.longitude_deg,
        latitude2_deg=target.latitude_deg,
        longitude2_deg=target.longitude_deg,
    )


def _validate_snapshots(anchor, current, *, now_s, policy) -> None:
    if not isinstance(anchor, SourceAnchoredIntent) or not isinstance(current, ExecutionSnapshot):
        raise TypeError("continuation requires typed original anchor and latest snapshot")
    if not isinstance(policy, ExecutionAdmissionPolicy):
        raise TypeError("policy must be ExecutionAdmissionPolicy")
    require_real(now_s, "now_s", minimum=0)
    replace(policy)
    if replace(anchor).canonical_hash != anchor.canonical_hash:
        raise OnboardContinuationAdmissionError("original source anchor mutated")
    replace(current)
    anchor.request.model_input.validate_freshness(now_s)
    source = anchor.source
    if _scope(source) != _scope(current) or (
        source.mission_sha256 != current.mission_sha256
        or source.mission_revision != current.mission_revision
        or source.receipt_revision != current.receipt_revision
        or source.policy_artifact_sha256 != current.policy_artifact_sha256
    ):
        raise OnboardContinuationAdmissionError("scope/Mission/receipt/policy changed")
    if not (
        source.context.captured_at_s <= current.context.captured_at_s <= now_s
        and source.context.captured_at_s <= anchor.result.completed_at_s <= now_s
        and now_s - source.context.captured_at_s <= policy.max_source_age_s
        and now_s - current.context.captured_at_s <= policy.max_current_age_s
    ):
        raise OnboardContinuationAdmissionError("source/current/host time is stale or regressed")
    context = current.context
    geometry, runtime_policy = parse_mission_compile_runtime(anchor.runtime, "multirotor")
    original = anchor.result.response.output.flight_plan
    requested_altitude = (
        geometry.takeoff_altitude_m if original.altitude_m is None else original.altitude_m
    )
    if not (
        runtime_policy.minimum_relative_altitude_m
        <= requested_altitude
        <= runtime_policy.maximum_relative_altitude_m
        and anchor.target.altitude_m == requested_altitude
    ):
        raise OnboardContinuationAdmissionError("original requested altitude cannot be clamped")
    if (
        context.aircraft_position.altitude_reference != "relative_home"
        or context.aircraft_position.altitude_m is None
        or not runtime_policy.minimum_relative_altitude_m
        <= context.aircraft_position.altitude_m
        <= runtime_policy.maximum_relative_altitude_m
    ):
        raise OnboardContinuationAdmissionError("latest height is unknown/outside flight envelope")
    if not (
        context.geofence_valid
        and context.gps_fix_type >= MAVLINK_QUALIFIED_FIX_TYPE_MIN
        and context.gps_horizontal_accuracy_m <= GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
        and context.camera_available
        and context.perception_available
        and context.map_cache_available
        and context.dem_available
    ):
        raise OnboardContinuationAdmissionError("latest navigation/resources are unqualified")
    if (
        _distance(source.context.aircraft_position, context.aircraft_position)
        > policy.max_origin_displacement_m
    ):
        raise OnboardContinuationAdmissionError("latest origin exceeds source displacement budget")
    event = current.event
    if (
        event.kind
        in {
            OnboardEventKind.GPS_DEGRADED,
            OnboardEventKind.BATTERY_LOW,
            OnboardEventKind.OBSTACLE_RISK,
            OnboardEventKind.INFERENCE_FAULT,
            OnboardEventKind.MISSION_COMPLETE,
        }
        or event.obstacle_distance_m is None
        or event.obstacle_distance_m == 5000
        or event.obstacle_distance_m < policy.minimum_clearance_m
        or (event.target_distance_m is not None and event.target_geometry_frame != "ned")
    ):
        raise OnboardContinuationAdmissionError("latest event cannot admit continuation movement")


def prepare_anchored_continuation(
    anchor: SourceAnchoredIntent,
    current: ExecutionSnapshot,
    *,
    parent: ContinuationParentBinding,
    mission_id: str,
    now_s: float,
    policy: ExecutionAdmissionPolicy = _DEFAULT_POLICY,
    compile_policy: ContinuationCompilePolicy | None = None,
) -> ContinuationCompilation:
    """Produce canonical geometry for independent route review, never permission."""
    _validate_snapshots(anchor, current, now_s=now_s, policy=policy)
    if not isinstance(parent, ContinuationParentBinding):
        raise TypeError("parent must be ContinuationParentBinding")
    if parent.parent_mission_sha256 != current.mission_sha256:
        raise OnboardContinuationAdmissionError("compiler parent differs from actual snapshot")
    return compile_multirotor_continuation(
        anchor.task,
        current.context,
        anchor.target,
        runtime=anchor.runtime,
        mission_id=require_text(mission_id, "mission_id"),
        parent=parent,
        now_s=now_s,
        source_intent_sha256=anchor.canonical_hash,
        policy=compile_policy,
    )


@dataclass(frozen=True, slots=True)
class OnboardContinuationAdmission:
    anchor: SourceAnchoredIntent
    current: ExecutionSnapshot
    parent_evidence: ContinuationParentEvidence
    authoring_evidence: ContinuationAuthoringEvidence
    compilation: ContinuationCompilation
    admitted_at_s: float
    policy: ExecutionAdmissionPolicy
    compile_policy: ContinuationCompilePolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, SourceAnchoredIntent) or not isinstance(
            self.current, ExecutionSnapshot
        ):
            raise TypeError("admission requires typed snapshots")
        if not isinstance(self.parent_evidence, ContinuationParentEvidence) or not isinstance(
            self.compilation, ContinuationCompilation
        ):
            raise TypeError("admission requires typed parent proof and compilation")
        if not isinstance(self.authoring_evidence, ContinuationAuthoringEvidence):
            raise TypeError("admission requires original host authoring evidence")
        require_real(self.admitted_at_s, "admitted_at_s", minimum=0)
        if not isinstance(self.policy, ExecutionAdmissionPolicy):
            raise TypeError("admission requires policy")

    def validate_binding(self, *, now_s: float) -> None:
        """Reopen source, parent journal/telemetry, geometry and clearance at use."""
        if now_s < self.admitted_at_s:
            raise OnboardContinuationAdmissionError("use time precedes original admission")
        ContinuationCompilation.from_mapping(self.compilation.to_mapping())
        rebuilt = admit_anchored_continuation(
            self.anchor,
            self.current,
            parent_evidence=self.parent_evidence,
            authoring_evidence=self.authoring_evidence,
            mission_id=self.compilation.compiled_mission.executable_mission.mission_id,
            now_s=now_s,
            policy=self.policy,
            compile_policy=self.compile_policy,
        )
        rebuilt_mapping = rebuilt.compilation.to_mapping()
        # Revalidation time is a new host fact, not a mutation of the compiled
        # route's original timestamp. All other artifacts must remain exact.
        rebuilt_mapping["compiled_at_s"] = self.compilation.compiled_at_s
        if canonical_sha256(rebuilt_mapping) != self.compilation.canonical_hash:
            raise OnboardContinuationAdmissionError("continuation compilation changed")

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.onboard-vlm-continuation-admission-development-v1",
            "source_anchor": self.anchor.to_mapping(),
            "execution_snapshot": self.current.to_mapping(),
            "parent_evidence": self.parent_evidence.to_mapping(),
            "parent_evidence_sha256": self.parent_evidence.canonical_hash,
            "original_authoring": self.authoring_evidence.to_mapping(),
            "original_authoring_sha256": self.authoring_evidence.canonical_hash,
            "original_authoring_bytes_sha256": self.authoring_evidence.source_receipt_sha256,
            "original_authoring_bytes_base64": base64.b64encode(
                self.authoring_evidence.source_receipt_bytes
            ).decode("ascii"),
            "original_authoring_trusted_sources": list(self.authoring_evidence.trusted_source_ids),
            "compilation": self.compilation.to_mapping(),
            "admitted_at_s": self.admitted_at_s,
            "policy": self.policy.to_mapping(),
            "source_timestamps_rewritten": False,
            "model_saw_execution_context": False,
            "target_origin": "original_model_intent_at_source_position",
            "execution_authority": False,
            "host_replacement_transport_implemented": False,
            "hold_watchdog_implemented": False,
            "original_runtime_declaration_bound": True,
            "full_runtime_body_identity_bound": True,
            "authenticated_original_authoring_source_verified": False,
            "live_original_authoring_producer_implemented": False,
            "compile_dispatch_causal_lineage_verified": False,
            "actual_flight_verified": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def admit_anchored_continuation(
    anchor: SourceAnchoredIntent,
    current: ExecutionSnapshot,
    *,
    parent_evidence: ContinuationParentEvidence,
    authoring_evidence: ContinuationAuthoringEvidence,
    mission_id: str,
    now_s: float,
    policy: ExecutionAdmissionPolicy = _DEFAULT_POLICY,
    compile_policy: ContinuationCompilePolicy | None = None,
) -> OnboardContinuationAdmission:
    """Check structural eligibility only; this does not issue a vehicle permit."""
    _validate_snapshots(anchor, current, now_s=now_s, policy=policy)
    if not isinstance(parent_evidence, ContinuationParentEvidence):
        raise TypeError("parent evidence must be typed")
    if parent_evidence.mission_binding.task.canonical_hash != anchor.task.canonical_hash:
        raise OnboardContinuationAdmissionError("parent original task differs from source task")
    parent = parent_evidence.validate_binding(
        current_scope=_scope(current), current_context=current.context, now_s=now_s
    )
    if not isinstance(authoring_evidence, ContinuationAuthoringEvidence):
        raise TypeError("original host authoring evidence must be typed")
    authoring_evidence.validate_binding(anchor, current, parent_evidence, parent)
    # This development version accepts only a canonically authored initial
    # ordinary parent. Previous model-authored parents need their own original
    # authoring trace; do not invent that trace from a matching task ID or wire.
    original_parent = compile_planned_task_mission_from_runtime(
        anchor.task,
        parent_evidence.journal_context,
        "multirotor",
        runtime=anchor.runtime,
        airframe_profile_hash=current.context.airframe_profile_hash,
        mission_id=parent.parent_mission_id,
    )
    if (
        original_parent.executable_mission_hash != parent.parent_mission_sha256
        or original_parent.wire_hash != parent.parent_wire_sha256
    ):
        raise OnboardContinuationAdmissionError("parent original authoring lineage does not match")
    _, safety = parse_mission_compile_runtime(anchor.runtime, "multirotor")
    journal_identity = parent_evidence.to_mapping()["journal_identity"]
    if journal_identity["bindings"]["geofence_hash"] != safety.geofence_hash:
        raise OnboardContinuationAdmissionError("parent original geofence differs from runtime")
    compilation = prepare_anchored_continuation(
        anchor,
        current,
        parent=parent,
        mission_id=mission_id,
        now_s=now_s,
        policy=policy,
        compile_policy=compile_policy,
    )
    clearance = current.clearance
    if clearance is None or clearance.source_id not in policy.trusted_clearance_source_ids:
        raise OnboardContinuationAdmissionError("current route clearance is unknown/untrusted")
    replace(clearance)
    if (
        clearance.context_sha256 != current.context.canonical_hash
        or clearance.event_sha256 != current.event.canonical_hash
        or clearance.target != anchor.target
        or clearance.wire_hash != compilation.wire_hash
        or not 0 <= now_s - clearance.observed_at_s <= policy.max_current_age_s
        or clearance.observed_at_s > current.context.captured_at_s
        or clearance.minimum_clearance_m < policy.minimum_clearance_m
    ):
        raise OnboardContinuationAdmissionError("clearance does not bind this current segment")
    # Coverage still has to be independently reviewed in 3-D. The path lower
    # bound only rejects obviously incomplete evidence; it cannot prove safety.
    minimum_length = _minimum_segment_path_length_m(compilation)
    if clearance.verified_path_length_m < (
        minimum_length + 2 * current.context.gps_horizontal_accuracy_m + policy.minimum_clearance_m
    ):
        raise OnboardContinuationAdmissionError("clearance does not cover the full 3-D segment")
    return OnboardContinuationAdmission(
        anchor,
        current,
        parent_evidence,
        authoring_evidence,
        compilation,
        now_s,
        policy,
        compile_policy,
    )


def _minimum_segment_path_length_m(compilation: ContinuationCompilation) -> float:
    """3-D wire geometry only, not physical route/clearance proof."""
    import math

    previous = compilation.current_context.aircraft_position
    length = 0.0
    for item in compilation.compiled_mission.wire_items:
        if item.frame != 6 or item.command not in {16, 19}:
            raise OnboardContinuationAdmissionError("unsupported segment coverage command")
        point = GeoPoint(item.x / 1e7, item.y / 1e7, item.z, "relative_home")
        length += math.hypot(_distance(previous, point), point.altitude_m - previous.altitude_m)
        if item.command == 19:
            length += 2 * abs(item.param3)
        previous = point
    return length
