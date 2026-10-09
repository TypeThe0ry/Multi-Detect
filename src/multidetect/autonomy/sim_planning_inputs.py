"""Explicit source-bound SIM estimates for an observed v2 multirotor context.

This bridge does not repair missing sensor values or grant flight permission.
It supplies a declared full-power energy assumption and sustainable cruise
capability for one pinned PX4/Gazebo scope.  The original source-qualified
snapshot and its resource flags remain unchanged.  Model and receipt hashes
bind declarations; neither hashes nor this module authenticate a sender or
prove that a container is still running.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import ConservativeFlightCapability, ContextSnapshot, ContextValueSource
from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_array,
    require_bool,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

if TYPE_CHECKING:
    from ..px4_external_mission import ExternalRuntimeAttestation

MODEL_CONTRACT = "multidetect.explicit-SIM-planning-model-v1"
QUALIFICATION_CONTRACT = "multidetect.SIM-planning-qualification-v1"
OBSERVATION_CONTRACT = "multidetect.onboard-teacher-observed-context-v2"
MAXIMUM_AGE_S = 2.0
MAXIMUM_MODEL_BYTES = 16 * 1024
MAXIMUM_OBSERVATION_BYTES = 2 * 1024 * 1024
MAXIMUM_RAW_SOURCE_BYTES = 64 * 1024
_REQUIRED_MESSAGES = frozenset(
    {
        "HEARTBEAT",
        "EXTENDED_SYS_STATE",
        "HOME_POSITION",
        "GPS_RAW_INT",
        "GLOBAL_POSITION_INT",
        "LOCAL_POSITION_NED",
        "BATTERY_STATUS",
        "SYS_STATUS",
        "VFR_HUD",
    }
)


@dataclass(frozen=True, slots=True)
class ExplicitSimPlanningModel:
    """Uncalibrated SIM-only assumptions, with units and supported domain."""

    image: str
    scenario_id: str
    scenario_manifest_sha256: str
    airframe_profile_id: str
    airframe_profile_hash: str
    capacity_wh: float
    avionics_power_w: float
    propulsion_max_power_w: float
    minimum_battery_pct: float
    maximum_battery_pct: float
    cruise_airspeed_mps: float
    maximum_wind_speed_mps: float
    energy_provenance: str
    capability_provenance: str

    def __post_init__(self) -> None:
        for name in (
            "scenario_id",
            "airframe_profile_id",
            "energy_provenance",
            "capability_provenance",
        ):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in ("scenario_manifest_sha256", "airframe_profile_hash"):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        image = require_text(self.image, "image")
        if "@sha256:" not in image or not image.split("@sha256:", 1)[0]:
            raise AutonomyContractError("SIM planning image must be digest-pinned")
        require_sha256(image.split("@sha256:", 1)[1], "image digest")
        object.__setattr__(self, "image", image)
        for name in (
            "capacity_wh",
            "avionics_power_w",
            "propulsion_max_power_w",
            "cruise_airspeed_mps",
            "maximum_wind_speed_mps",
        ):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        for name in ("minimum_battery_pct", "maximum_battery_pct"):
            object.__setattr__(
                self, name, require_real(getattr(self, name), name, minimum=0, maximum=100)
            )
        if self.capacity_wh <= 0 or self.cruise_airspeed_mps <= 0:
            raise AutonomyContractError("SIM capacity and conservative cruise must be positive")
        if self.avionics_power_w + self.propulsion_max_power_w <= 0:
            raise AutonomyContractError("SIM full-power load must be positive")
        if self.minimum_battery_pct >= self.maximum_battery_pct:
            raise AutonomyContractError("SIM supported battery domain must have positive width")

    @classmethod
    def from_mapping(cls, raw: object) -> ExplicitSimPlanningModel:
        value = strict_object(
            raw,
            name="explicit SIM planning model",
            required=frozenset(
                {
                    "contract",
                    "schema_version",
                    "runtime_kind",
                    "vehicle_class",
                    "image",
                    "scenario_id",
                    "scenario_manifest_sha256",
                    "airframe_profile_id",
                    "airframe_profile_hash",
                    "energy",
                    "capability",
                    "physical_calibration_claimed",
                }
            ),
        )
        require_schema_version(value["schema_version"])
        if (
            value["contract"] != MODEL_CONTRACT
            or value["runtime_kind"] != "SIM"
            or value["vehicle_class"] != "multirotor"
        ):
            raise AutonomyContractError("unsupported SIM planning model contract or scope")
        if require_bool(value["physical_calibration_claimed"], "physical_calibration_claimed"):
            raise AutonomyContractError(
                "an explicit SIM assumption is not a calibrated physical model"
            )
        energy = strict_object(
            value["energy"],
            name="energy model",
            required=frozenset(
                {
                    "kind",
                    "capacity_wh",
                    "avionics_power_w",
                    "propulsion_max_power_w",
                    "minimum_battery_pct",
                    "maximum_battery_pct",
                    "provenance",
                    "reserve_deducted",
                }
            ),
        )
        if energy["kind"] != "linear_remaining_energy_full_power_v1":
            raise AutonomyContractError("unsupported SIM energy model")
        if require_bool(energy["reserve_deducted"], "reserve_deducted"):
            raise AutonomyContractError("planner alone deducts the return reserve")
        capability = strict_object(
            value["capability"],
            name="capability model",
            required=frozenset(
                {"kind", "cruise_airspeed_mps", "maximum_wind_speed_mps", "provenance"}
            ),
        )
        if capability["kind"] != "conservative_sustainable_air_relative_cruise_v1":
            raise AutonomyContractError("unsupported SIM capability model")
        return cls(
            image=value["image"],
            scenario_id=value["scenario_id"],
            scenario_manifest_sha256=value["scenario_manifest_sha256"],
            airframe_profile_id=value["airframe_profile_id"],
            airframe_profile_hash=value["airframe_profile_hash"],
            capacity_wh=energy["capacity_wh"],
            avionics_power_w=energy["avionics_power_w"],
            propulsion_max_power_w=energy["propulsion_max_power_w"],
            minimum_battery_pct=energy["minimum_battery_pct"],
            maximum_battery_pct=energy["maximum_battery_pct"],
            cruise_airspeed_mps=capability["cruise_airspeed_mps"],
            maximum_wind_speed_mps=capability["maximum_wind_speed_mps"],
            energy_provenance=energy["provenance"],
            capability_provenance=capability["provenance"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": MODEL_CONTRACT,
            "schema_version": 1,
            "runtime_kind": "SIM",
            "vehicle_class": "multirotor",
            "image": self.image,
            "scenario_id": self.scenario_id,
            "scenario_manifest_sha256": self.scenario_manifest_sha256,
            "airframe_profile_id": self.airframe_profile_id,
            "airframe_profile_hash": self.airframe_profile_hash,
            "energy": {
                "kind": "linear_remaining_energy_full_power_v1",
                "capacity_wh": self.capacity_wh,
                "avionics_power_w": self.avionics_power_w,
                "propulsion_max_power_w": self.propulsion_max_power_w,
                "minimum_battery_pct": self.minimum_battery_pct,
                "maximum_battery_pct": self.maximum_battery_pct,
                "provenance": self.energy_provenance,
                "reserve_deducted": False,
            },
            "capability": {
                "kind": "conservative_sustainable_air_relative_cruise_v1",
                "cruise_airspeed_mps": self.cruise_airspeed_mps,
                "maximum_wind_speed_mps": self.maximum_wind_speed_mps,
                "provenance": self.capability_provenance,
            },
            "physical_calibration_claimed": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class SimPlanningQualification:
    """Recomputable source-binding receipt, not a Mission or runtime permit."""

    observed_context_sha256: str
    qualified_context_sha256: str
    observation_source: EvidenceBinding
    model_source: EvidenceBinding
    model_canonical_sha256: str
    runtime_attestation_sha256: str
    container_id: str
    image: str
    scenario_id: str
    scenario_manifest_sha256: str
    airframe_profile_id: str
    airframe_profile_hash: str
    clock_id: str
    captured_at_s: float
    attested_at_s: float
    qualified_at_s: float
    maximum_age_s: float
    modeled_remaining_wh: float
    modeled_endurance_before_age_s: float
    conservative_receive_age_deduction_s: float
    estimated_flight_time_s: float

    @property
    def observation_receipt_sha256(self) -> str:
        return self.observation_source.sha256

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": QUALIFICATION_CONTRACT,
            "schema_version": 1,
            "observed_context_sha256": self.observed_context_sha256,
            "qualified_context_sha256": self.qualified_context_sha256,
            "observation_source": self.observation_source.to_mapping(),
            "observation_receipt_sha256": self.observation_receipt_sha256,
            "model_source": self.model_source.to_mapping(),
            "model_canonical_sha256": self.model_canonical_sha256,
            "runtime_attestation_sha256": self.runtime_attestation_sha256,
            "container_id": self.container_id,
            "image": self.image,
            "scenario_id": self.scenario_id,
            "scenario_manifest_sha256": self.scenario_manifest_sha256,
            "airframe_profile_id": self.airframe_profile_id,
            "airframe_profile_hash": self.airframe_profile_hash,
            "runtime_kind": "SIM",
            "vehicle_class": "multirotor",
            "clock_id": self.clock_id,
            "captured_at_s": self.captured_at_s,
            "attested_at_s": self.attested_at_s,
            "qualified_at_s": self.qualified_at_s,
            "maximum_age_s": self.maximum_age_s,
            "modeled_remaining_wh": self.modeled_remaining_wh,
            "modeled_endurance_before_age_s": self.modeled_endurance_before_age_s,
            "conservative_receive_age_deduction_s": self.conservative_receive_age_deduction_s,
            "estimated_flight_time_s": self.estimated_flight_time_s,
            "reserve_deducted": False,
            "claims": {
                "physical_calibration_claimed": False,
                "sensor_values_fabricated": False,
                "resource_clearance_qualified": False,
                "source_authenticated": False,
                "live_runtime_rechecked_by_this_module": False,
                "execution_authority": False,
                "mission_approved": False,
                "hardware_control_enabled": False,
                "final_release_allowed": False,
            },
        }


def _identity(stat: object) -> tuple[int, int, int, int]:
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def _read_source(root: Path, source: EvidenceBinding, cap: int, identities: dict) -> bytes:
    if type(source) is not EvidenceBinding:
        raise TypeError("source must be an EvidenceBinding")
    if source.bytes > cap:
        raise AutonomyContractError("SIM source exceeds bounded read cap")
    path = root / source.path
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise AutonomyContractError("SIM source must be a contained regular file")
    before = path.stat()
    if before.st_size != source.bytes:
        raise AutonomyContractError("SIM source byte size differs")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    after = path.stat()
    if (
        _identity(before) != _identity(after)
        or len(raw) != source.bytes
        or hashlib.sha256(raw).hexdigest() != source.sha256
    ):
        raise AutonomyContractError("SIM source identity/bytes/SHA differ")
    prior = identities.setdefault(source.path, _identity(after))
    if prior != _identity(after):
        raise AutonomyContractError("SIM source changed during qualification")
    return raw


def _read_document(root: Path, source: EvidenceBinding, cap: int, identities: dict) -> object:
    if source.json_pointer is None:
        raise AutonomyContractError("JSON SIM source requires an explicit JSON pointer")
    value = strict_json_loads(_read_source(root, source, cap, identities))
    for token in source.json_pointer.split("/")[1:] if source.json_pointer else ():
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and key in value:
            value = value[key]
        elif (
            type(value) is list
            and key.isdecimal()
            and str(int(key)) == key
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            raise AutonomyContractError("SIM source JSON pointer cannot be resolved")
    return value


def _fresh(at_s: object, now_s: float, age_s: float, name: str) -> float:
    at = require_real(at_s, name, minimum=0)
    if at > now_s or now_s - at > age_s:
        raise AutonomyContractError(f"{name} is future or stale")
    return at


def _qualified_observation(root, source, context, now, age, clock_id, identities):
    raw = strict_object(
        _read_document(root, source, MAXIMUM_OBSERVATION_BYTES, identities),
        name="observed context receipt",
        required=frozenset(
            {
                "contract",
                "schema_version",
                "context",
                "context_sha256",
                "source_references",
                "qualification_details",
                "claims",
            }
        ),
    )
    if raw["contract"] != OBSERVATION_CONTRACT:
        raise AutonomyContractError("wrong observed context contract")
    require_schema_version(raw["schema_version"], expected=2)
    if (
        ContextSnapshot.from_mapping(raw["context"]) != context
        or raw["context_sha256"] != context.canonical_hash
    ):
        raise AutonomyContractError("observed context receipt does not bind exact context")
    details = raw["qualification_details"]
    if not isinstance(details, Mapping):
        raise AutonomyContractError("observed context qualification details must be an object")
    for flag in (
        "source_file_bytes_SHA_verified",
        "source_identity_before_after_equal",
        "qualified_plain_MC_disarmed_on_ground",
    ):
        if details.get(flag) is not True:
            raise AutonomyContractError(f"observed context lacks {flag}")
    if details.get("clock_id") != clock_id:
        raise AutonomyContractError("observed context clock differs")
    if details.get("endurance_reserve_already_deducted") is not False:
        raise AutonomyContractError(
            "observed context reserve status is missing or already deducted"
        )
    if details.get("accepted_system_component") != [1, 1] or any(
        type(v) is not int for v in details.get("accepted_system_component", ())
    ):
        raise AutonomyContractError("observed context is not the admitted PX4 source")
    times = details.get("message_received_at_s")
    if not isinstance(times, Mapping) or set(times) != _REQUIRED_MESSAGES:
        raise AutonomyContractError("observed context message timing set differs")
    received = [_fresh(v, now, age, "message receive time") for v in times.values()]
    received.append(_fresh(details.get("resource_observed_at_s"), now, age, "resource observation"))
    if (
        min(received) != context.captured_at_s
        or details.get("oldest_observation_at_s") != context.captured_at_s
    ):
        raise AutonomyContractError("observed context did not preserve oldest receive time")
    claims = raw["claims"]
    if not isinstance(claims, Mapping) or any(
        claims.get(flag) is not False
        for flag in ("execution_authority", "hardware_control_enabled", "mission_approved")
    ):
        raise AutonomyContractError("observed context must not claim flight authority")
    sources = require_array(raw["source_references"], "observed source_references")
    if not 1 <= len(sources) <= 128:
        raise AutonomyContractError("observed source reference count exceeds bounds")
    bindings = tuple(EvidenceBinding.from_mapping(v) for v in sources)
    for binding in bindings:
        _read_source(root, binding, MAXIMUM_RAW_SOURCE_BYTES, identities)
    return bindings


def _checked_attestation(attestation: ExternalRuntimeAttestation) -> ExternalRuntimeAttestation:
    from ..px4_external_mission import ExternalPx4MissionError, ExternalRuntimeAttestation

    if type(attestation) is not ExternalRuntimeAttestation:
        raise TypeError("runtime_attestation must be an ExternalRuntimeAttestation")
    value = attestation.to_mapping()
    value.pop("attestation_sha256")
    try:
        checked = ExternalRuntimeAttestation.from_mapping(value)
    except ExternalPx4MissionError as exc:
        raise AutonomyContractError(f"runtime attestation is invalid: {exc}") from exc
    if checked != attestation:
        raise AutonomyContractError("runtime attestation is not in normalized canonical form")
    return checked


def qualify_sim_planning_context(
    observed_context: ContextSnapshot,
    *,
    runtime_attestation: ExternalRuntimeAttestation,
    observation_source: EvidenceBinding,
    model_source: EvidenceBinding,
    artifact_root: str | Path,
    now_s: float,
    attested_at_s: float,
    clock_id: str,
    maximum_age_s: float = MAXIMUM_AGE_S,
) -> tuple[ContextSnapshot, SimPlanningQualification]:
    """Fill only absent model-based planning inputs; retain all observed data.

    ``attested_at_s`` and all observations must use the same named observer
    monotonic clock.  The caller owns actual session/connection reinspection;
    this function revalidates the supplied attestation structure and age only.
    Return reserve is not deducted here.  The existing planner does that once.
    """
    if type(observed_context) is not ContextSnapshot or observed_context.schema_version != 2:
        raise AutonomyContractError("an observed ContextSnapshot v2 is required")
    context = observed_context
    if context.provenance.runtime_kind != "SIM":
        raise AutonomyContractError("explicit SIM planning inputs cannot qualify HIL/REAL/REPLAY")
    if context.estimated_flight_time_s is not None or context.flight_capability is not None:
        raise AutonomyContractError("SIM model cannot overwrite existing endurance or capability")
    if context.airframe_profile_hash is None:
        raise AutonomyContractError("observed profile hash is required")
    now = require_real(now_s, "now_s", minimum=0)
    age = require_real(maximum_age_s, "maximum_age_s", minimum=0.01, maximum=MAXIMUM_AGE_S)
    clock = require_text(clock_id, "clock_id")
    _fresh(context.captured_at_s, now, age, "observed context")
    attested = _fresh(attested_at_s, now, age, "runtime attestation")
    attestation = _checked_attestation(runtime_attestation)
    if (
        attestation.vehicle_class != "multirotor"
        or attestation.airframe_profile_id != context.airframe_profile_id
    ):
        raise AutonomyContractError("observed context and runtime profile/class differ")
    root = Path(artifact_root).resolve()
    if not root.is_dir():
        raise AutonomyContractError("existing artifact_root is required")
    identities: dict = {}
    source_bindings = _qualified_observation(
        root, observation_source, context, now, age, clock, identities
    )
    model = ExplicitSimPlanningModel.from_mapping(
        _read_document(root, model_source, MAXIMUM_MODEL_BYTES, identities)
    )
    for name in ("image", "scenario_id", "scenario_manifest_sha256", "airframe_profile_id"):
        if getattr(model, name) != getattr(attestation, name):
            raise AutonomyContractError(f"SIM model runtime {name} differs")
    if model.airframe_profile_hash != context.airframe_profile_hash:
        raise AutonomyContractError("SIM model airframe profile hash differs")
    if not model.minimum_battery_pct <= context.battery_remaining_pct <= model.maximum_battery_pct:
        raise AutonomyContractError("observed battery is outside SIM model supported domain")
    if context.wind_north_mps is None or context.wind_east_mps is None:
        raise AutonomyContractError("qualified wind source is required; no calm-wind fabrication")
    if math.hypot(context.wind_north_mps, context.wind_east_mps) > model.maximum_wind_speed_mps:
        raise AutonomyContractError("wind exceeds SIM capability model supported domain")
    remaining_wh = model.capacity_wh * context.battery_remaining_pct / 100.0
    endurance = remaining_wh * 3600.0 / (model.avionics_power_w + model.propulsion_max_power_w)
    elapsed = now - context.captured_at_s
    remaining_s = endurance - elapsed
    if not math.isfinite(remaining_s) or remaining_s <= 0:
        raise AutonomyContractError("modeled remaining endurance is invalid or exhausted")
    qualified = replace(
        context,
        estimated_flight_time_s=remaining_s,
        provenance=replace(
            context.provenance,
            endurance=ContextValueSource("explicit_SIM_energy_model", model_source.sha256),
        ),
        flight_capability=ConservativeFlightCapability(
            context.airframe_profile_id,
            context.airframe_profile_hash,
            "multirotor",
            "SIM",
            model.cruise_airspeed_mps,
            model_source.sha256,
        ),
    )
    # Repeat after derivation; a mutable source cannot silently qualify twice.
    _read_source(root, observation_source, MAXIMUM_OBSERVATION_BYTES, identities)
    _read_source(root, model_source, MAXIMUM_MODEL_BYTES, identities)
    for source in source_bindings:
        _read_source(root, source, MAXIMUM_RAW_SOURCE_BYTES, identities)
    receipt = SimPlanningQualification(
        context.canonical_hash,
        qualified.canonical_hash,
        observation_source,
        model_source,
        model.canonical_hash,
        attestation.canonical_hash,
        attestation.container_id,
        attestation.image,
        attestation.scenario_id,
        attestation.scenario_manifest_sha256,
        context.airframe_profile_id,
        context.airframe_profile_hash,
        clock,
        context.captured_at_s,
        attested,
        now,
        age,
        remaining_wh,
        endurance,
        elapsed,
        remaining_s,
    )
    return qualified, receipt


def validate_sim_planning_qualification(
    receipt: SimPlanningQualification,
    *,
    observed_context: ContextSnapshot,
    qualified_context: ContextSnapshot,
    runtime_attestation: ExternalRuntimeAttestation,
    artifact_root: str | Path,
    now_s: float,
    clock_id: str,
) -> None:
    """Re-read/recompute the exact qualification and enforce current freshness.

    This does not renew the original receive time or supplied runtime check.
    Any changed raw source, model, context, scope, formula or receipt is denied.
    Actual reinspection of the owned runtime remains the caller's responsibility.
    """
    if type(receipt) is not SimPlanningQualification:
        raise TypeError("receipt must be SimPlanningQualification")
    now = require_real(now_s, "now_s", minimum=0)
    if require_text(clock_id, "clock_id") != receipt.clock_id:
        raise AutonomyContractError("qualification clock differs")
    if now < receipt.qualified_at_s:
        raise AutonomyContractError("qualification lies in the future")
    _fresh(receipt.captured_at_s, now, receipt.maximum_age_s, "observed context")
    _fresh(receipt.attested_at_s, now, receipt.maximum_age_s, "runtime attestation")
    rebuilt_context, rebuilt_receipt = qualify_sim_planning_context(
        observed_context,
        runtime_attestation=runtime_attestation,
        observation_source=receipt.observation_source,
        model_source=receipt.model_source,
        artifact_root=artifact_root,
        now_s=receipt.qualified_at_s,
        attested_at_s=receipt.attested_at_s,
        clock_id=receipt.clock_id,
        maximum_age_s=receipt.maximum_age_s,
    )
    if rebuilt_context != qualified_context or rebuilt_receipt.to_mapping() != receipt.to_mapping():
        raise AutonomyContractError("SIM planning qualification recomputation differs")


__all__ = [
    "ExplicitSimPlanningModel",
    "SimPlanningQualification",
    "qualify_sim_planning_context",
    "validate_sim_planning_qualification",
]
