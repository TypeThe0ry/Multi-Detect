from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

from .compat import StrEnum
from .domain import BoundingBox, TrackSnapshot
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


class PayloadTargetEligibility(StrEnum):
    ELIGIBLE_FIRE = "eligible_fire"
    ELIGIBLE_BURNING_CONTEXT = "eligible_burning_context"
    TARGET_NOT_PAYLOAD_ELIGIBLE = "target_not_payload_eligible"
    TARGET_NOT_STABLY_TRACKED = "target_not_stably_tracked"
    FIRE_EVIDENCE_UNAVAILABLE = "fire_evidence_unavailable"
    FIRE_ASSOCIATION_AMBIGUOUS = "fire_association_ambiguous"


@dataclass(frozen=True, slots=True)
class PayloadTargetGateConfig:
    maximum_evidence_age_s: float = 0.35
    direct_fire_minimum_iou: float = 0.25
    context_fire_minimum_iou: float = 0.01
    context_fire_maximum_center_distance: float = 0.22
    ambiguity_iou_margin: float = 0.06
    ambiguity_center_distance_margin: float = 0.04
    slide_confirmation_ttl_s: float = 5.0
    minimum_slide_duration_s: float = 0.6
    maximum_slide_duration_s: float = 4.0

    def __post_init__(self) -> None:
        for name, value in (
            ("maximum_evidence_age_s", self.maximum_evidence_age_s),
            ("direct_fire_minimum_iou", self.direct_fire_minimum_iou),
            ("context_fire_minimum_iou", self.context_fire_minimum_iou),
            ("context_fire_maximum_center_distance", self.context_fire_maximum_center_distance),
            ("ambiguity_iou_margin", self.ambiguity_iou_margin),
            ("ambiguity_center_distance_margin", self.ambiguity_center_distance_margin),
            ("slide_confirmation_ttl_s", self.slide_confirmation_ttl_s),
            ("minimum_slide_duration_s", self.minimum_slide_duration_s),
            ("maximum_slide_duration_s", self.maximum_slide_duration_s),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.minimum_slide_duration_s < self.maximum_slide_duration_s:
            raise ValueError("payload slide duration limits are invalid")
        for name, value in (
            ("direct_fire_minimum_iou", self.direct_fire_minimum_iou),
            ("context_fire_minimum_iou", self.context_fire_minimum_iou),
            ("context_fire_maximum_center_distance", self.context_fire_maximum_center_distance),
            ("ambiguity_iou_margin", self.ambiguity_iou_margin),
            ("ambiguity_center_distance_margin", self.ambiguity_center_distance_margin),
        ):
            if value > 1.0:
                raise ValueError(f"{name} must not exceed 1")


@dataclass(frozen=True, slots=True)
class PayloadTargetResolution:
    eligibility: PayloadTargetEligibility
    selection_command_id: str
    selected_target_id: str
    selected_target_revision: int
    selected_label: str
    resolved_at_s: float
    aimpoint_target_id: str | None = None
    aimpoint_target_revision: int | None = None
    aimpoint_bbox: BoundingBox | None = None
    composite_context: bool = False
    hil_only: bool = True
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.selection_command_id.strip() or not self.selected_target_id.strip():
            raise ValueError("payload target selection identifiers cannot be empty")
        if self.selected_target_revision < 0:
            raise ValueError("selected target revision cannot be negative")
        if not self.selected_label.strip():
            raise ValueError("selected target label cannot be empty")
        if not math.isfinite(self.resolved_at_s) or self.resolved_at_s < 0.0:
            raise ValueError("payload target resolution time is invalid")
        eligible = self.eligibility in {
            PayloadTargetEligibility.ELIGIBLE_FIRE,
            PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT,
        }
        aimpoint_fields = (
            self.aimpoint_target_id,
            self.aimpoint_target_revision,
            self.aimpoint_bbox,
        )
        if eligible != all(value is not None for value in aimpoint_fields):
            raise ValueError("eligible payload target resolution requires an atomic aimpoint")
        if self.aimpoint_target_revision is not None and self.aimpoint_target_revision < 0:
            raise ValueError("aimpoint target revision cannot be negative")
        if self.composite_context != (
            self.eligibility is PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT
        ):
            raise ValueError("payload composite-context flag is inconsistent")
        if not self.hil_only or self.physical_release_enabled:
            raise ValueError("payload target resolution must remain HIL-only")

    @property
    def eligible(self) -> bool:
        return self.aimpoint_target_id is not None


@dataclass(frozen=True, slots=True)
class PayloadSlideChallenge:
    token: str
    selection_command_id: str
    selected_target_id: str
    selected_target_revision: int
    aimpoint_target_id: str
    aimpoint_target_revision: int
    issued_at_s: float
    expires_at_s: float
    mode: str = "payload_hil"
    hil_only: bool = True
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        identifiers = (
            self.token,
            self.selection_command_id,
            self.selected_target_id,
            self.aimpoint_target_id,
        )
        if any(not value.strip() for value in identifiers):
            raise ValueError("payload slide challenge identifiers cannot be empty")
        if self.selected_target_revision < 0 or self.aimpoint_target_revision < 0:
            raise ValueError("payload slide challenge revisions cannot be negative")
        if not all(math.isfinite(value) for value in (self.issued_at_s, self.expires_at_s)):
            raise ValueError("payload slide challenge timestamps must be finite")
        if self.issued_at_s < 0.0 or self.expires_at_s <= self.issued_at_s:
            raise ValueError("payload slide challenge lifetime is invalid")
        if self.mode != "payload_hil" or not self.hil_only or self.physical_release_enabled:
            raise ValueError("payload slide challenge must remain payload-HIL-only")


@dataclass(frozen=True, slots=True)
class PayloadSlideGrant:
    token: str
    selection_command_id: str
    selected_target_id: str
    selected_target_revision: int
    aimpoint_target_id: str
    aimpoint_target_revision: int
    accepted_at_s: float
    expires_at_s: float
    hil_only: bool = True
    physical_release_enabled: bool = False


@dataclass(frozen=True, slots=True)
class PayloadTargetIntent:
    """Short-lived Mode-2 operator intent bound to the resolved fire aimpoint.

    This is an authorization prerequisite only. It cannot arm or actuate a payload,
    and the mission safety/authorization layers must still independently approve the
    current fire track and scene.
    """

    selection_command_id: str
    selected_target_id: str
    selected_target_revision: int
    aimpoint_target_id: str
    aimpoint_target_revision: int
    accepted_at_s: float
    expires_at_s: float
    hil_only: bool = True
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.selection_command_id,
                self.selected_target_id,
                self.aimpoint_target_id,
            )
        ):
            raise ValueError("payload target intent identifiers cannot be empty")
        if self.selected_target_revision < 0 or self.aimpoint_target_revision < 0:
            raise ValueError("payload target intent revisions cannot be negative")
        if not all(math.isfinite(value) for value in (self.accepted_at_s, self.expires_at_s)):
            raise ValueError("payload target intent timestamps must be finite")
        if self.accepted_at_s < 0.0 or self.expires_at_s <= self.accepted_at_s:
            raise ValueError("payload target intent lifetime is invalid")
        if not self.hil_only or self.physical_release_enabled:
            raise ValueError("payload target intent must remain HIL-only")

    def valid_at(self, now_s: float) -> bool:
        PayloadTargetResolver._require_time(now_s)
        return self.accepted_at_s <= now_s < self.expires_at_s


class PayloadTargetResolver:
    """Resolve an operator-selected object to a qualified fire aimpoint.

    People, smoke and ordinary vehicles remain selectable for tracking and display,
    but this resolver never turns them into a payload aimpoint. A vehicle/building
    selection can only resolve through one unambiguous, independently corroborated
    fire track; the fire track, not the context object's box, becomes the aimpoint.
    """

    _FIRE_LABELS = frozenset({"fire", "flame"})
    _CONTEXT_LABELS = frozenset({"vehicle", "car", "truck", "bus", "building"})

    def __init__(self, config: PayloadTargetGateConfig | None = None) -> None:
        self.config = config or PayloadTargetGateConfig()

    def resolve(
        self,
        *,
        selection_command_id: str,
        selected_target_revision: int,
        selected: UnifiedTrackSnapshot,
        fire_tracks: Sequence[TrackSnapshot],
        now_s: float,
    ) -> PayloadTargetResolution:
        self._require_time(now_s)
        label = selected.label.strip().lower()
        common = {
            "selection_command_id": selection_command_id,
            "selected_target_id": selected.track_id,
            "selected_target_revision": selected_target_revision,
            "selected_label": label,
            "resolved_at_s": now_s,
        }
        if not self._selection_stable(selected, now_s):
            return PayloadTargetResolution(
                PayloadTargetEligibility.TARGET_NOT_STABLY_TRACKED,
                **common,
            )
        if label not in self._FIRE_LABELS | self._CONTEXT_LABELS:
            return PayloadTargetResolution(
                PayloadTargetEligibility.TARGET_NOT_PAYLOAD_ELIGIBLE,
                **common,
            )

        candidates = self._qualified_fire_candidates(selected, fire_tracks, now_s)
        if not candidates:
            return PayloadTargetResolution(
                PayloadTargetEligibility.FIRE_EVIDENCE_UNAVAILABLE,
                **common,
            )
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2], item[3].track_id))
        if len(candidates) > 1:
            first, second = candidates[:2]
            if (
                first[0] == second[0]
                and abs(first[1] - second[1]) <= self.config.ambiguity_iou_margin
                and abs(first[2] - second[2]) <= self.config.ambiguity_center_distance_margin
            ):
                return PayloadTargetResolution(
                    PayloadTargetEligibility.FIRE_ASSOCIATION_AMBIGUOUS,
                    **common,
                )
        fire_track = candidates[0][3]
        composite = label in self._CONTEXT_LABELS
        return PayloadTargetResolution(
            (
                PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT
                if composite
                else PayloadTargetEligibility.ELIGIBLE_FIRE
            ),
            aimpoint_target_id=fire_track.track_id,
            aimpoint_target_revision=fire_track.revision,
            aimpoint_bbox=fire_track.bbox,
            composite_context=composite,
            **common,
        )

    def _qualified_fire_candidates(
        self,
        selected: UnifiedTrackSnapshot,
        fire_tracks: Sequence[TrackSnapshot],
        now_s: float,
    ) -> list[tuple[int, float, float, TrackSnapshot]]:
        selected_label = selected.label.strip().lower()
        candidates: list[tuple[int, float, float, TrackSnapshot]] = []
        for track in fire_tracks:
            if track.label.strip().lower() not in self._FIRE_LABELS:
                continue
            if not track.confirmed or not track.independent_rgb_corroborated:
                continue
            if now_s - track.last_seen_at_s > self.config.maximum_evidence_age_s:
                continue
            overlap = selected.bbox.iou(track.bbox)
            distance = selected.bbox.center_distance(track.bbox)
            fire_center_x, fire_center_y = track.bbox.center
            fire_center_inside = (
                selected.bbox.x1 <= fire_center_x <= selected.bbox.x2
                and selected.bbox.y1 <= fire_center_y <= selected.bbox.y2
            )
            if selected_label in self._FIRE_LABELS:
                spatially_valid = overlap >= self.config.direct_fire_minimum_iou
            else:
                spatially_valid = (
                    fire_center_inside
                    or overlap >= self.config.context_fire_minimum_iou
                    or distance <= self.config.context_fire_maximum_center_distance
                )
            if spatially_valid:
                candidates.append((int(fire_center_inside), overlap, distance, track))
        return candidates

    def _selection_stable(self, selected: UnifiedTrackSnapshot, now_s: float) -> bool:
        return bool(
            selected.state
            in {
                UnifiedTrackState.LOCKED,
                UnifiedTrackState.TRACKING,
                UnifiedTrackState.RECOVERED,
            }
            and selected.locked
            and selected.primary
            and selected.actionable
            and now_s - selected.last_seen_at_s <= self.config.maximum_evidence_age_s
        )

    @staticmethod
    def _require_time(now_s: float) -> None:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("payload target gate time must be finite and non-negative")


class PayloadSlideConfirmationController:
    """One-time slide confirmation bound to selection and resolved fire aimpoint."""

    def __init__(self, config: PayloadTargetGateConfig | None = None) -> None:
        self.config = config or PayloadTargetGateConfig()
        self._active: PayloadSlideChallenge | None = None
        self._used_tokens: set[str] = set()

    @property
    def active_challenge(self) -> PayloadSlideChallenge | None:
        return self._active

    def clear(self) -> None:
        self._active = None

    def issue(
        self,
        resolution: PayloadTargetResolution,
        *,
        now_s: float,
    ) -> PayloadSlideChallenge:
        PayloadTargetResolver._require_time(now_s)
        if not resolution.eligible:
            raise ValueError("cannot issue payload slide confirmation for an ineligible target")
        challenge = PayloadSlideChallenge(
            token=str(uuid4()),
            selection_command_id=resolution.selection_command_id,
            selected_target_id=resolution.selected_target_id,
            selected_target_revision=resolution.selected_target_revision,
            aimpoint_target_id=str(resolution.aimpoint_target_id),
            aimpoint_target_revision=int(resolution.aimpoint_target_revision),
            issued_at_s=now_s,
            expires_at_s=now_s + self.config.slide_confirmation_ttl_s,
        )
        self._active = challenge
        return challenge

    def accept(
        self,
        *,
        token: str,
        resolution: PayloadTargetResolution,
        slide_started_at_s: float,
        slide_completed_at_s: float,
        completion_fraction: float,
        continuous: bool,
    ) -> PayloadSlideGrant | None:
        challenge = self._active
        if challenge is None or token in self._used_tokens or not resolution.eligible:
            return None
        duration_s = slide_completed_at_s - slide_started_at_s
        valid = bool(
            token == challenge.token
            and self._matches(challenge, resolution)
            and challenge.issued_at_s <= slide_started_at_s <= slide_completed_at_s
            and slide_completed_at_s <= challenge.expires_at_s
            and self.config.minimum_slide_duration_s
            <= duration_s
            <= self.config.maximum_slide_duration_s
            and math.isfinite(completion_fraction)
            and completion_fraction >= 0.98
            and continuous
        )
        self._used_tokens.add(token)
        self._active = None
        if not valid:
            return None
        return PayloadSlideGrant(
            token=token,
            selection_command_id=challenge.selection_command_id,
            selected_target_id=challenge.selected_target_id,
            selected_target_revision=challenge.selected_target_revision,
            aimpoint_target_id=challenge.aimpoint_target_id,
            aimpoint_target_revision=challenge.aimpoint_target_revision,
            accepted_at_s=slide_completed_at_s,
            expires_at_s=challenge.expires_at_s,
        )

    def grant_valid(
        self,
        grant: PayloadSlideGrant,
        resolution: PayloadTargetResolution,
        *,
        now_s: float,
    ) -> bool:
        PayloadTargetResolver._require_time(now_s)
        return bool(
            now_s < grant.expires_at_s
            and resolution.eligible
            and grant.selection_command_id == resolution.selection_command_id
            and grant.selected_target_id == resolution.selected_target_id
            and grant.selected_target_revision == resolution.selected_target_revision
            and grant.aimpoint_target_id == resolution.aimpoint_target_id
            and grant.aimpoint_target_revision == resolution.aimpoint_target_revision
            and grant.hil_only
            and not grant.physical_release_enabled
        )

    @staticmethod
    def _matches(
        challenge: PayloadSlideChallenge,
        resolution: PayloadTargetResolution,
    ) -> bool:
        return bool(
            challenge.selection_command_id == resolution.selection_command_id
            and challenge.selected_target_id == resolution.selected_target_id
            and challenge.selected_target_revision == resolution.selected_target_revision
            and challenge.aimpoint_target_id == resolution.aimpoint_target_id
            and challenge.aimpoint_target_revision == resolution.aimpoint_target_revision
        )


__all__ = [
    "PayloadSlideChallenge",
    "PayloadSlideConfirmationController",
    "PayloadSlideGrant",
    "PayloadTargetEligibility",
    "PayloadTargetGateConfig",
    "PayloadTargetIntent",
    "PayloadTargetResolution",
    "PayloadTargetResolver",
]
