from __future__ import annotations

import time

import numpy as np

from multidetect.autonomy.policy_v2.contracts import TaskSpecV1
from multidetect.autonomy.policy_v2.vlm_grounding import (
    AttributeGroundingResult,
    AttributeGrounder,
    TaskSpecResolver,
)


class FakeTaskVlm:
    def __init__(self, value, *, delay=0.0):
        self.value = value
        self.delay = delay
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.value


class FakeAttributeVlm:
    def __init__(self, value, *, delay=0.0):
        self.value = value
        self.delay = delay
        self.calls = 0

    def ground_attributes(self, crop, prompt):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.value


def test_task_vlm_strict_json_and_cache() -> None:
    fake = FakeTaskVlm('{"target_class":"vehicle","target_color":"red","attributes":[],"relative":"nearest","confidence":0.9,"unknown_fields":[]}')
    resolver = TaskSpecResolver(fake, dev_fallback=False)
    first = resolver.resolve("find the nearest red car")
    second = resolver.resolve("find the nearest red car")
    assert isinstance(first, TaskSpecV1)
    assert first == second
    assert fake.calls == 1


def test_malformed_and_timeout_use_unknown_or_dev_parser() -> None:
    malformed = TaskSpecResolver(FakeTaskVlm("not-json"), dev_fallback=False).resolve("find a red car")
    assert malformed.confidence == 0.0
    assert "resolver" in malformed.unknown_fields
    timed_out = TaskSpecResolver(FakeTaskVlm("{}", delay=0.05), timeout_s=0.001, dev_fallback=False).resolve("x")
    assert timed_out.confidence == 0.0
    assert "resolver" in timed_out.unknown_fields


def test_registered_prompt_id_wins_without_calling_vlm() -> None:
    fake = FakeTaskVlm("bad")
    registered = TaskSpecV1(prompt_sha256="a" * 64, target_class="vehicle", target_color="blue")
    resolver = TaskSpecResolver(fake, registered_specs={"prompt-1": registered})
    result = resolver.resolve("operator text", prompt_id="prompt-1")
    assert result is registered
    assert fake.calls == 0


def test_hsv_baseline_and_attribute_cache() -> None:
    rgb = np.zeros((32, 32, 3), dtype=np.uint8)
    rgb[:] = [255, 0, 0]
    grounder = AttributeGrounder()
    first = grounder.ground(rgb, [[0, 0, 32, 32]], target_color="red")[0]
    second = grounder.ground(rgb, [[0, 0, 32, 32]], target_color="red")[0]
    assert first.color_match == second.color_match == "true"
    assert first.source == "hsv"


def test_bad_vlm_output_fails_closed_to_unknown() -> None:
    rgb = np.zeros((20, 20, 3), dtype=np.uint8)
    rgb[:] = [255, 0, 0]
    result = AttributeGrounder(FakeAttributeVlm("{bad-json}"), timeout_s=0.2).ground(
        rgb, [[0, 0, 20, 20]], target_color="red"
    )[0]
    assert result.source == "unknown"
    assert result.class_match == result.color_match == result.attribute_match == "unknown"
    assert set(result.state_fields()) == {
        "class_match", "color_match", "attribute_match", "grounding_score"
    }


def test_state_fields_never_expose_privileged_data() -> None:
    result = AttributeGroundingResult(
        class_match="true",
        color_match="unknown",
        attribute_match="false",
        score=0.5,
        observed_class="vehicle",
        observed_color="red",
    )
    fields = result.state_fields()
    assert all("gps" not in key and "truth" not in key for key in fields)
    assert "observed_class" not in fields and "observed_color" not in fields


def test_ambiguous_prompt_abstains() -> None:
    spec = TaskSpecResolver(vlm=None).resolve("find a red or blue car")
    assert spec.target_color is None
    assert spec.target_class is None
    assert spec.confidence == 0.0
