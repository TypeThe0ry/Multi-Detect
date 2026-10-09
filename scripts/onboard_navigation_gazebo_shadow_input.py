"""Actual Gazebo wire RGB to the existing navigation Actor, never flight control.

Only raw prompt and original RGB reach Actor messages. Wire telemetry and source
clock evidence stay in the outer receipt. A valid wire hash does not authenticate
the collecting host, prove clearance or grant permission to act on a prediction.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from uuid import uuid4

from multidetect.autonomy.contracts import PromptEnvelope
from multidetect.autonomy.onboard_live_vlm_contract import (
    _frame_facts,
    gazebo_sensor_fact_json,
)
from multidetect.autonomy.onboard_multimodal import OnboardImageFrame, OnboardMultimodalScope
from multidetect.autonomy.onboard_navigation_actor import NavigationActorFrame, NavigationActorInput
from multidetect.autonomy.onboard_navigation_inference import NavigationInferenceRequest
from multidetect.autonomy.onboard_navigation_supervision import NavigationSupervisionBudget
from multidetect.autonomy.serialization import (
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
)
from multidetect.backends.gazebo_sim import GazeboSimFrameProvenance
from multidetect.clock import validate_host_identity
from scripts.onboard_navigation_inference_backend import NavigationEngineIdentity

CONTRACT = "multidetect.onboard-navigation-gazebo-shadow-input-v1"


@dataclass(frozen=True, slots=True)
class PreparedGazeboNavigationRequest:
    request: NavigationInferenceRequest
    source_receipt_json: str

    def __post_init__(self):
        if type(self.request) is not NavigationInferenceRequest:
            raise TypeError("Shared typed navigation request required")
        self.request.validate_binding()
        if hashlib.sha256(self.source_receipt_json.encode("utf-8")).hexdigest() != (
            self.request.source_binding_sha256
        ):
            raise ValueError("Navigation source receipt changed")


@dataclass(frozen=True, slots=True)
class _Observation:
    actor: NavigationActorFrame
    source: OnboardImageFrame
    facts_json: str
    sensor_stamp_ns: int


class GazeboNavigationInputBuffer:
    """Single-owner bounded input history; inference and queue ownership are external.

    A capture callback can call ``offer`` under its own queue/thread discipline.
    This object is deliberately not thread-safe: one runtime owner must serialize
    both offer and build_request. It neither starts a model nor invokes a backend.
    """

    def __init__(
        self,
        prompt: PromptEnvelope,
        *,
        prompt_text_original,
        host_clock_identity,
        clock=time.perf_counter,
        session_id=None,
        budget=None,
        max_frame_age_s=5.0,
        deadline_s=5.0,
    ):
        if type(prompt) is not PromptEnvelope or not callable(clock):
            raise TypeError("Typed prompt and perf-counter clock required")
        if type(prompt_text_original) is not str or prompt_text_original.strip() != prompt.text:
            raise ValueError("Verbatim original prompt must match its normalized envelope")
        self._prompt_text = prompt_text_original
        self._prompt_envelope_sha = prompt.canonical_hash
        self._clock_identity = canonical_json(validate_host_identity(dict(host_clock_identity)))
        self._clock = clock
        self.budget = NavigationSupervisionBudget() if budget is None else budget
        if type(self.budget) is not NavigationSupervisionBudget:
            raise TypeError("Shared navigation budget required")
        self.max_frame_age_s = require_real(
            max_frame_age_s, "max_frame_age_s", minimum=0, maximum=30
        )
        self.deadline_s = require_real(deadline_s, "deadline_s", minimum=0, maximum=30)
        if self.max_frame_age_s == 0 or self.deadline_s == 0:
            raise ValueError("Strictly positive freshness and deadline required")
        self.scope = OnboardMultimodalScope(
            session_id=str(uuid4()) if session_id is None else session_id,
            prompt_sha256=hashlib.sha256(self._prompt_text.encode("utf-8")).hexdigest(),
            aircraft_id=prompt.aircraft_id,
            selected_task_id="navigation-shadow-unresolved-prompt",
            clock_epoch="gazebo-host-perf-counter:" + canonical_sha256(dict(host_clock_identity)),
        )
        self._history = []
        self._last_host_time = -1.0
        self._last_sequence = -1
        self._last_source_time = -1.0
        self._last_sensor_stamp = -1
        self._last_generation = 0
        self._last_requested_sequence = -1

    def _now(self):
        now = require_real(self._clock(), "host perf-counter time", minimum=0)
        if now < self._last_host_time:
            raise ValueError("Navigation host clock regressed")
        self._last_host_time = now
        return now

    def offer(self, frame, provenance):
        """Validate and copy an actual wire BGR callback; no telemetry in Actor."""
        if type(provenance) is not GazeboSimFrameProvenance:
            raise TypeError("Actual typed Gazebo frame provenance required")
        p = provenance
        now = self._now()
        if p.captured_at_clock != "host_perf_counter" or p.host_clock_identity is None:
            raise ValueError("Explicit source perf-counter identity required")
        identity = canonical_json(validate_host_identity(dict(p.host_clock_identity)))
        if identity != self._clock_identity:
            raise ValueError("Source camera is in a different host clock epoch")
        if not 0 <= now - p.captured_at_s <= self.max_frame_age_s:
            raise ValueError("Camera input is future or stale")
        if (
            p.sequence <= self._last_sequence
            or p.captured_at_s <= self._last_source_time
            or p.sensor_stamp_ns <= self._last_sensor_stamp
        ):
            raise ValueError("Camera sequence, source time or sensor stamp regressed")
        import numpy as np

        image = getattr(frame, "image_bgr", None)
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.shape != (p.height, p.width, 3)
            or p.pixel_format != "BGR24"
            or p.stride != p.width * 3
        ):
            raise ValueError("Camera image layout differs from source provenance")
        bgr = image.tobytes(order="C")
        if len(bgr) != p.payload_bytes or hashlib.sha256(bgr).hexdigest() != p.payload_sha256:
            raise ValueError("Camera payload bytes differ from source wire")
        rgb = bytearray(bgr)
        rgb[0::3], rgb[2::3] = bgr[2::3], bgr[0::3]
        rgb = bytes(rgb)
        rgb_sha = hashlib.sha256(rgb).hexdigest()
        observation = OnboardImageFrame(
            self.scope, p.frame_id, p.captured_at_s, rgb, rgb_sha, p.width, p.height, "rgb8"
        )
        facts_json = gazebo_sensor_fact_json(observation, p.to_mapping())
        # Reuse production wire reconstruction and quaternion/telemetry/skew
        # checks. The resulting facts remain OUTSIDE the navigation messages.
        _frame_facts(observation, facts_json, self._last_sensor_stamp if self._history else None)
        actor = NavigationActorFrame(p.sequence, rgb, len(rgb), rgb_sha, p.width, p.height, "rgb8")
        NavigationActorInput(self._prompt_text, p.sequence, (actor,), self.budget)
        # Source checking/decoding are part of the same freshness budget.
        after = self._now()
        if not 0 <= after - p.captured_at_s <= self.max_frame_age_s:
            raise ValueError("Camera input became stale during source validation")
        self._history.append(_Observation(actor, observation, facts_json, p.sensor_stamp_ns))
        self._history = self._history[-2:]
        self._last_sequence, self._last_source_time, self._last_sensor_stamp = (
            p.sequence,
            p.captured_at_s,
            p.sensor_stamp_ns,
        )

    def build_request(self, identity: NavigationEngineIdentity, *, generation):
        if type(identity) is not NavigationEngineIdentity:
            raise TypeError("Declared base/adapter/config file identity required")
        generation = require_int(generation, "generation", minimum=1)
        if generation <= self._last_generation:
            raise ValueError("Navigation generation must advance")
        if not self._history:
            raise ValueError("A camera observation is required")
        now = self._now()
        latest = self._history[-1]
        if not 0 <= now - latest.source.captured_at_s <= self.max_frame_age_s:
            raise ValueError("Latest camera observation is stale")
        if self._last_sequence <= self._last_requested_sequence:
            raise ValueError("A fresh, not previously submitted camera observation is required")
        rows = [
            row
            for row in self._history
            if 0 <= now - row.source.captured_at_s <= self.max_frame_age_s
        ]
        # Never enlarge the Actor's pixel budget to fit a large live camera.
        if len(rows) == 2 and (
            sum(row.actor.byte_count for row in rows) > self.budget.max_total_image_bytes
            or sum(row.actor.width * row.actor.height for row in rows)
            > self.budget.max_total_pixels
        ):
            rows = [latest]
        actor = NavigationActorInput(
            self._prompt_text, latest.actor.sequence, tuple(row.actor for row in rows), self.budget
        )
        receipt_json = canonical_json(
            {
                "contract": CONTRACT,
                "prompt_envelope_sha256": self._prompt_envelope_sha,
                "actor_identity_sha256": actor.identity_sha256,
                "scope_sha256": self.scope.canonical_hash,
                "source_facts": [row.facts_json for row in rows],
                "wire_bytes_verified": True,
                "sensor_authenticity_verified": False,
                "source_metadata_in_actor": False,
                "depth_clearance_or_navigation_safety_context_admitted": False,
                "execution_authority": False,
            }
        )
        request = NavigationInferenceRequest(
            actor_input=actor,
            scope=self.scope,
            source_frames=tuple(row.source for row in rows),
            source_binding_sha256=hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
            model_sha256=identity.model_sha256,
            adapter_sha256=identity.adapter_sha256,
            adapter_config_sha256=identity.adapter_config_sha256,
            generation=generation,
            submitted_at_s=now,
            deadline_s=now + self.deadline_s,
            max_frame_age_s=self.max_frame_age_s,
        )
        request.validate_freshness(self._now())
        prepared = PreparedGazeboNavigationRequest(request, receipt_json)
        # The prepared value revalidates/decodes the immutable binding. Sample
        # time AFTER that work, without starting another decoding check whose
        # elapsed time could escape this gate. Do not consume rejected inputs.
        checked = self._now()
        if not request.submitted_at_s <= checked < request.deadline_s or any(
            not 0 <= checked - row.captured_at_s <= request.max_frame_age_s
            for row in request.source_frames
        ):
            raise ValueError("Navigation input expired during prepared binding validation")
        self._last_generation, self._last_requested_sequence = generation, latest.actor.sequence
        return prepared
